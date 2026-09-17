"""Provision the durable research season.

    python -m app.services.provision_season --name "BotBet Clash 2026" --year 2026

Creates ONE season whose rules are pinned to the real providers, so that
`live_ingest` will accept it. Nothing else.

WHY THIS IS NOT A SMOKE SEASON
------------------------------
`Game.external_ref` is globally UNIQUE. The first real provider event we
persist is bound to whatever season it lands in, permanently -- so if the
acceptance run were pointed at a throwaway "Phase 4 acceptance" season, it
would recreate exactly the scoping problem the `--season-id` guard was
added to prevent. This must be the durable 2026 season we intend to keep.

It makes no provider or model calls and writes no Game, Player,
PropMarket, PropQuote or bankroll rows. It does not open a week, register
competitors, or start real-money competition. The constitution's rule
values are unchanged -- the only difference from any other season is that
the two provider pins name real sources instead of SYNTHETIC.
"""

from __future__ import annotations

import argparse
import sys
import uuid
from decimal import Decimal
from typing import Sequence

from sqlalchemy import select

from app.db.models.season import Season, SeasonRules as SeasonRulesRow
from app.db.session import session_scope
from app.domain.models import Money, SeasonRules
from app.marketdata.providers.the_odds_api import PROVIDER_NAME as ODDS_PROVIDER
from app.rosterdata.providers.nflverse import PROVIDER_NAME as ROSTER_PROVIDER
from app.services.season_commissioner import SeasonCommissioner

CANONICAL_BOOK = "DRAFTKINGS"


def _constitution_rules(*, year: int) -> SeasonRules:
    """The locked constitution values. Unchanged from every other season."""

    return SeasonRules(
        rules_version=f"{year}-research-v1",
        starting_bankroll=Money.from_dollars_str("15.00"),
        kelly_fraction=Decimal("0.20"),
        standard_max_bankroll_fraction=Decimal("0.20"),
        exceptional_max_bankroll_fraction=Decimal("0.30"),
        minimum_stake=Money(25),
        stake_increment=Money(25),
        pounce_limit=1,
    )


def existing_research_seasons(session) -> list[Season]:
    """Seasons already pinned to the real providers.

    Deliberately keyed on the PINS, not on the name: two differently-named
    seasons both claiming the real feeds is the ambiguity that matters, and
    it is what would make a later `--season-id` choice a coin flip.
    """

    rows = session.execute(
        select(Season)
        .join(SeasonRulesRow, SeasonRulesRow.season_id == Season.id)
        .where(
            SeasonRulesRow.market_data_provider == ODDS_PROVIDER,
            SeasonRulesRow.roster_data_provider == ROSTER_PROVIDER,
            SeasonRulesRow.superseded_by.is_(None),
        )
    ).scalars().all()
    return list(rows)


def provision(*, name: str, year: int) -> uuid.UUID:
    with session_scope() as session:
        existing = existing_research_seasons(session)
        if existing:
            listed = "\n".join(f"  {s.id}  {s.name} ({s.year})" for s in existing)
            raise SystemExit(
                "A research season pinned to the real providers already exists:\n"
                f"{listed}\n"
                "Refusing to create a second one. Real provider events are globally "
                "unique, so two candidate research seasons make every later "
                "--season-id choice ambiguous. Pass the existing id to live_ingest."
            )

    commissioner = SeasonCommissioner.create_season(
        name=name,
        year=year,
        rules=_constitution_rules(year=year),
        market_data_provider=ODDS_PROVIDER,
        roster_data_provider=ROSTER_PROVIDER,
    )
    return uuid.UUID(str(commissioner.season_id))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Provision the durable research season")
    parser.add_argument("--name", required=True, help='e.g. "BotBet Clash 2026"')
    parser.add_argument("--year", required=True, type=int)
    args = parser.parse_args(argv)

    season_id = provision(name=args.name, year=args.year)

    with session_scope() as session:
        rules = session.execute(
            select(SeasonRulesRow).where(SeasonRulesRow.season_id == season_id)
        ).scalars().one()
        print("=" * 66)
        print("RESEARCH SEASON PROVISIONED")
        print("=" * 66)
        print(f"  season_id              {season_id}")
        print(f"  name                   {args.name}")
        print(f"  year                   {args.year}")
        print(f"  rules_version          {rules.rules_version}")
        print(f"  market_data_provider   {rules.market_data_provider}")
        print(f"  roster_data_provider   {rules.roster_data_provider}")
        print(f"  canonical_sportsbook   {rules.canonical_sportsbook}")
        print(f"  starting_bankroll      ${rules.starting_bankroll_cents / 100:.2f}")
        print(f"  supported_prop_types   {', '.join(rules.supported_prop_types)}")
        print("")
        print("  No competitors registered, no week opened, no market data fetched.")
        print("  Pass this season_id to app.marketdata.live_ingest.")
        print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
