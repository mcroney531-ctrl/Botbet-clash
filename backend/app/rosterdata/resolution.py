"""Odds display name -> stable player identity.

The whole algorithm rests on one constraint: an odds prop belongs to an
EVENT, and an event has exactly two teams. Matching a name against the
whole league is unsafe, and that is measured rather than asserted --
in a single 2026 week the roster contained four duplicate normalized
names league-wide (`justin jefferson` at CLE and MIN, `devonta smith` at
CAR and PHI, plus two more), and zero duplicates within any single
game's two teams. A global match would fuse two real people into one
Player row permanently.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Mapping, Sequence

from app.marketdata.dto import normalize_player_name
from app.rosterdata.aliases import alias_target
from app.rosterdata.base import PlayerResolution, RosterEntry, RosterSnapshot
from app.rosterdata.teams import CanonicalTeam

RESOLVER_VERSION = "two-team-exact-alias-v2"
"""The roster equivalent of PropQuote.parser_version. If normalization or
matching logic changes, we must be able to tell which logic produced an
old observation. Provenance only -- nothing filters on it.

v1 -> v2: added the explicit provider alias pass (app/rosterdata/aliases.py)
after the first real run measured one genuine miss. Exact matching still
wins; the alias is only consulted when exact matching finds nothing."""


def stable_player_external_ref(stable_id: str) -> str:
    """The durable identity persisted on Player.external_ref.

    GSIS, not the resolving vendor: nflverse RESOLVES the identity, but
    GSIS IS the identity, so replacing the roster provider later must not
    orphan every Player row.
    """

    return f"GSIS:{stable_id}"


def index_by_team(
    snapshot: RosterSnapshot, teams: Sequence[CanonicalTeam]
) -> Mapping[str, list[RosterEntry]]:
    """Normalized-name index over ONLY the given teams' entries."""

    wanted = set(teams)
    index: dict[str, list[RosterEntry]] = defaultdict(list)
    for entry in snapshot.entries:
        if entry.team in wanted:
            index[normalize_player_name(entry.display_name)].append(entry)
    return index


def index_by_stable_id(
    snapshot: RosterSnapshot, teams: Sequence[CanonicalTeam]
) -> Mapping[str, list[RosterEntry]]:
    """Stable-id index over ONLY the given teams' entries."""

    wanted = set(teams)
    index: dict[str, list[RosterEntry]] = defaultdict(list)
    for entry in snapshot.entries:
        if entry.team in wanted and (entry.stable_id or "").strip():
            index[entry.stable_id].append(entry)
    return index


def resolve_player(
    *,
    odds_display_name: str,
    home_team: CanonicalTeam,
    away_team: CanonicalTeam,
    snapshot: RosterSnapshot,
    provider: str | None = None,
) -> PlayerResolution:
    """Resolve one odds name against one game's two rosters.

    Conservative by construction: exact match on a conservatively
    normalized name, restricted to the event's two teams. No fuzzy
    matching, no suffix stripping, no nickname handling, no global
    fallback. A miss is reported, never guessed at.
    """

    index = index_by_team(snapshot, (home_team, away_team))
    candidates = index.get(normalize_player_name(odds_display_name), [])
    via_alias = False

    if not candidates and provider is not None:
        # Second pass ONLY. Exact matching always wins, so an alias can never
        # override a real roster name -- it can only rescue a name that
        # matched nothing.
        target = alias_target(provider=provider, display_name=odds_display_name)
        if target is not None:
            # The alias names a stable identity, but that identity must still
            # be present on one of THIS event's two teams. An alias can never
            # pull a player into a game he is not in, and it stops working the
            # moment he changes teams -- which is the intended behaviour, not
            # a limitation.
            by_id = index_by_stable_id(snapshot, (home_team, away_team))
            candidates = by_id.get(target, [])
            via_alias = True
            if not candidates:
                return PlayerResolution(
                    outcome="UNRESOLVED_PLAYER",
                    detail=(
                        f"alias maps {odds_display_name!r} to {target}, but that "
                        f"identity is not on {home_team.value} or {away_team.value} "
                        "in this roster snapshot; refusing to force it into the game"
                    ),
                )

    if not candidates:
        return PlayerResolution(
            outcome="UNRESOLVED_PLAYER",
            detail=(
                f"{odds_display_name!r} is not on either {home_team.value} or "
                f"{away_team.value} in the {snapshot.season} week "
                f"{snapshot.week} roster snapshot"
            ),
        )
    if len(candidates) > 1:
        return PlayerResolution(
            outcome="AMBIGUOUS_PLAYER",
            detail=(
                f"{odds_display_name!r} matches {len(candidates)} roster entries "
                f"across {sorted({c.team.value for c in candidates})}; refusing to "
                "choose between real people"
            ),
        )

    entry = candidates[0]
    if not (entry.stable_id or "").strip():
        return PlayerResolution(
            outcome="MISSING_STABLE_ID",
            detail=f"roster entry for {odds_display_name!r} carries no stable id",
        )

    # The index already restricted to the two teams, so this cannot
    # normally trip. It is asserted anyway because the opposite mistake --
    # assuming "not home means away" -- is exactly the bug this seam was
    # written to kill.
    if entry.team == home_team:
        opponent = away_team
    elif entry.team == away_team:
        opponent = home_team
    else:
        return PlayerResolution(
            outcome="TEAM_GAME_MISMATCH",
            detail=(
                f"resolved team {entry.team.value} is neither {home_team.value} "
                f"nor {away_team.value}"
            ),
        )

    return PlayerResolution(
        outcome="RESOLVED",
        stable_id=entry.stable_id,
        team=entry.team,
        position=(entry.position or None),
        opponent=opponent,
        detail=(
            f"resolved via explicit alias to {entry.stable_id}" if via_alias else ""
        ),
    )


def derive_opponent(
    *, team: CanonicalTeam, home_team: CanonicalTeam, away_team: CanonicalTeam
) -> CanonicalTeam:
    """The correct replacement for orchestrator.py's broken derivation.

    Explicit three-way branch. There is NO `else means away team`
    shortcut: if a player's team is neither side of the game, that is
    corrupted context and it must stop request construction rather than
    produce a plausible-looking wrong answer.
    """

    if team == home_team:
        return away_team
    if team == away_team:
        return home_team
    raise TeamGameMismatch(
        f"player team {team.value} is neither {home_team.value} (home) nor "
        f"{away_team.value} (away); refusing to guess an opponent"
    )


class TeamGameMismatch(ValueError):
    """A resolved team is not one of the game's two teams."""
