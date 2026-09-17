"""Read-only inspection of what real ingestion actually persisted.

    python -m app.marketdata.inspect_ingestion --game-id <uuid>

Makes NO provider calls and spends NO credits. It writes nothing. It
exists because the acceptance report describes what one RUN did, which is
not the same as what the TABLES now hold -- and the revalidation
invariants (relationships stable, observations appended, distinct
provider calls) are statements about the tables across runs.

Answering that by re-running the paid ingestion would be the wrong tool:
it costs credits and changes the very state being inspected.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from collections import Counter, defaultdict
from typing import Sequence

from sqlalchemy import func, select

from app.db.models.ingestion import ProviderCall
from app.db.models.markets import Game, Player, PropMarket, PropQuote
from app.db.models.roster import GamePlayer, GamePlayerObservation
from app.db.session import session_scope


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only ingestion inspection")
    parser.add_argument("--game-id", required=True, type=uuid.UUID)
    args = parser.parse_args(argv)

    out: list[str] = []
    add = out.append

    with session_scope() as session:
        game = session.get(Game, args.game_id)
        if game is None:
            print(f"no game {args.game_id}")
            return 1

        add("=" * 72)
        add("INGESTION INSPECTION (read-only, no provider calls, no credits)")
        add("=" * 72)
        add(f"game:  {game.away_team} @ {game.home_team}  "
            f"[{game.away_team_canonical} @ {game.home_team_canonical}]")
        add(f"week:  {game.week_number}   kickoff {game.kickoff_at.isoformat()}")
        add("")

        gps = session.execute(
            select(GamePlayer).where(GamePlayer.game_id == game.id)
        ).scalars().all()
        gp_ids = [gp.id for gp in gps]

        obs = session.execute(
            select(GamePlayerObservation)
            .where(GamePlayerObservation.game_player_id.in_(gp_ids))
            .order_by(GamePlayerObservation.observed_at)
        ).scalars().all() if gp_ids else []

        add("--- identity ------------------------------------------------------")
        add(f"  GamePlayer rows:            {len(gps)}")
        add(f"  GamePlayerObservation rows: {len(obs)}")
        add("")

        per_gp: dict[uuid.UUID, list] = defaultdict(list)
        for o in obs:
            per_gp[o.game_player_id].append(o)

        counts = Counter(len(v) for v in per_gp.values())
        add("  observations per relationship:")
        for n, how_many in sorted(counts.items()):
            add(f"    {how_many} player(s) with {n} observation(s)")

        distinct_calls = {o.roster_provider_call_id for o in obs}
        add(f"  distinct roster provider_calls across observations: {len(distinct_calls)}")
        versions = Counter(o.resolver_version for o in obs)
        for version, n in sorted(versions.items()):
            add(f"    resolver_version {version}: {n} observation(s)")
        add("")

        # The invariant that matters: did any relationship get REWRITTEN?
        rewritten = []
        for gp in gps:
            teams = {o.resolved_team for o in per_gp.get(gp.id, [])}
            if teams and (len(teams) > 1 or gp.team not in teams):
                rewritten.append((gp, teams))
        add(f"  relationships whose observations disagree with the accepted team: "
            f"{len(rewritten)}")
        for gp, teams in rewritten:
            player = session.get(Player, gp.player_id)
            add(f"    CONFLICT {player.name}: accepted {gp.team}, observed {sorted(teams)}")
        add("")

        add("--- market --------------------------------------------------------")
        markets = session.execute(
            select(PropMarket).where(PropMarket.game_id == game.id)
        ).scalars().all()
        market_ids = [m.id for m in markets]
        quotes = session.execute(
            select(PropQuote).where(PropQuote.market_id.in_(market_ids))
        ).scalars().all() if market_ids else []

        add(f"  PropMarket rows: {len(markets)}")
        add(f"  PropQuote rows:  {len(quotes)}")
        quote_calls = {q.provider_call_id for q in quotes}
        add(f"  distinct market provider_calls across quotes: {len(quote_calls)}")
        as_ofs = sorted({q.as_of_at for q in quotes})
        add(f"  distinct as_of_at observation times: {len(as_ofs)}")
        for t in as_ofs:
            n = sum(1 for q in quotes if q.as_of_at == t)
            add(f"    {t.isoformat()}  {n} quote(s)")
        add("")

        # Did any book's price actually MOVE between observations? This is
        # what distinguishes "append on real movement" from "append on an
        # identical re-observation" -- both are correct, but they prove
        # different things.
        if len(as_ofs) >= 2:
            first, last = as_ofs[0], as_ofs[-1]
            def state(t):
                return {
                    (q.market_id, q.sportsbook): (q.line, q.over_price, q.under_price)
                    for q in quotes if q.as_of_at == t
                }
            a, b = state(first), state(last)
            shared = set(a) & set(b)
            moved = [k for k in shared if a[k] != b[k]]
            add("--- movement between first and last observation --------------------")
            add(f"  book/market pairs present in both: {len(shared)}")
            add(f"  pairs whose line or price CHANGED: {len(moved)}")
            unchanged = [k for k in shared if a[k] == b[k]]
            add(f"  pairs that stayed IDENTICAL:       {len(unchanged)}")
            add("")

            # Sample ACROSS books, not alphabetically. Taking the first N of a
            # name-sorted list showed eight BETMGM rows and made league-wide
            # movement look like one book's activity.
            add("  sample of movement, one per book:")
            seen: set[str] = set()
            for market_id, book in sorted(moved, key=lambda k: k[1]):
                if book in seen:
                    continue
                seen.add(book)
                market = session.get(PropMarket, market_id)
                player = session.get(Player, market.player_id)
                add(f"    {book:12} {player.name} {market.stat_type}: "
                    f"{a[(market_id, book)]} -> {b[(market_id, book)]}")

            if unchanged:
                add("")
                add("  sample of IDENTICAL re-observations (the retention rule's whole")
                add("  point: these produced a second row anyway, proving the book was")
                add("  still quoting rather than having dropped out of the feed):")
                seen_same: set[str] = set()
                for market_id, book in sorted(unchanged, key=lambda k: k[1]):
                    if book in seen_same:
                        continue
                    seen_same.add(book)
                    market = session.get(PropMarket, market_id)
                    player = session.get(Player, market.player_id)
                    add(f"    {book:12} {player.name} {market.stat_type}: "
                        f"{a[(market_id, book)]} (unchanged, 2 rows)")
            else:
                add("    (no shared pair held steady; every one moved)")
            if not moved:
                add("    (none moved — every shared quote was an identical")
                add("     re-observation, which still produced a new row)")
        add("")
        add("=" * 72)

    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
