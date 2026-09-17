"""The one canonical NFL team vocabulary.

Both external providers map INTO this; neither defines it. Everything
internal -- Game, GamePlayer, opponent derivation -- speaks only
CanonicalTeam.

This exists because of a real bug. app/ai/orchestrator.py compared a
roster team to a market team with string equality:

    opponent = game.away_team if player.team == game.home_team else ...

With synthetic data both sides were "KC" and it passed. With real data it
compares "BUF" to "Buffalo Bills", never matches, and silently returns the
home team for EVERY player -- including the home team's own. Half the
slate would have been told it faces itself, with no crash and no failing
test.

NO HEURISTICS. No substring matching, no fuzzy matching, no title-casing,
no "strip the city and compare the nickname". A heuristic that maps
Washington correctly today will map something else wrongly later and never
say so. Unmapped input raises; the caller quarantines.
"""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType
from typing import Mapping


class CanonicalTeam(StrEnum):
    """32 members. The codes are nflverse's, which were OBSERVED in the
    real 2026 roster file rather than recalled -- note `LA`, not `LAR`."""

    ARI = "ARI"
    ATL = "ATL"
    BAL = "BAL"
    BUF = "BUF"
    CAR = "CAR"
    CHI = "CHI"
    CIN = "CIN"
    CLE = "CLE"
    DAL = "DAL"
    DEN = "DEN"
    DET = "DET"
    GB = "GB"
    HOU = "HOU"
    IND = "IND"
    JAX = "JAX"
    KC = "KC"
    LA = "LA"
    LAC = "LAC"
    LV = "LV"
    MIA = "MIA"
    MIN = "MIN"
    NE = "NE"
    NO = "NO"
    NYG = "NYG"
    NYJ = "NYJ"
    PHI = "PHI"
    PIT = "PIT"
    SEA = "SEA"
    SF = "SF"
    TB = "TB"
    TEN = "TEN"
    WAS = "WAS"


class TeamMappingError(ValueError):
    """An external team string is not in the explicit table."""


# nflverse -> canonical. Identity, because the canonical codes ARE the
# observed nflverse codes. Written out anyway so that a future nflverse
# code change is a mapping edit rather than a silent divergence.
NFLVERSE_TEAM_CODES: Mapping[str, CanonicalTeam] = MappingProxyType(
    {team.value: team for team in CanonicalTeam}
)

# The Odds API -> canonical. Full display names.
#
# "Detroit Lions" and "Buffalo Bills" are CONFIRMED from the 2026-09-17
# validation probe payload. The remaining 30 are the standard franchise
# names and are UNCONFIRMED against this vendor. That is deliberate and
# safe: an unconfirmed spelling raises TeamMappingError and quarantines
# the market, so a mismatch shows up as a named diagnostic on first
# contact rather than as wrong research data.
ODDS_API_TEAM_NAMES: Mapping[str, CanonicalTeam] = MappingProxyType(
    {
        "Arizona Cardinals": CanonicalTeam.ARI,
        "Atlanta Falcons": CanonicalTeam.ATL,
        "Baltimore Ravens": CanonicalTeam.BAL,
        "Buffalo Bills": CanonicalTeam.BUF,          # confirmed by probe
        "Carolina Panthers": CanonicalTeam.CAR,
        "Chicago Bears": CanonicalTeam.CHI,
        "Cincinnati Bengals": CanonicalTeam.CIN,
        "Cleveland Browns": CanonicalTeam.CLE,
        "Dallas Cowboys": CanonicalTeam.DAL,
        "Denver Broncos": CanonicalTeam.DEN,
        "Detroit Lions": CanonicalTeam.DET,          # confirmed by probe
        "Green Bay Packers": CanonicalTeam.GB,
        "Houston Texans": CanonicalTeam.HOU,
        "Indianapolis Colts": CanonicalTeam.IND,
        "Jacksonville Jaguars": CanonicalTeam.JAX,
        "Kansas City Chiefs": CanonicalTeam.KC,
        "Las Vegas Raiders": CanonicalTeam.LV,
        "Los Angeles Chargers": CanonicalTeam.LAC,
        "Los Angeles Rams": CanonicalTeam.LA,
        "Miami Dolphins": CanonicalTeam.MIA,
        "Minnesota Vikings": CanonicalTeam.MIN,
        "New England Patriots": CanonicalTeam.NE,
        "New Orleans Saints": CanonicalTeam.NO,
        "New York Giants": CanonicalTeam.NYG,
        "New York Jets": CanonicalTeam.NYJ,
        "Philadelphia Eagles": CanonicalTeam.PHI,
        "Pittsburgh Steelers": CanonicalTeam.PIT,
        "San Francisco 49ers": CanonicalTeam.SF,
        "Seattle Seahawks": CanonicalTeam.SEA,
        "Tampa Bay Buccaneers": CanonicalTeam.TB,
        "Tennessee Titans": CanonicalTeam.TEN,
        "Washington Commanders": CanonicalTeam.WAS,
    }
)

# Legacy/synthetic values already in the database. Kept separate from the
# provider tables so that test fixtures cannot quietly satisfy a real
# provider lookup.
LEGACY_TEAM_VALUES: Mapping[str, CanonicalTeam] = MappingProxyType(
    {team.value: team for team in CanonicalTeam}
)


def canonical_from_odds_api(name: str) -> CanonicalTeam:
    try:
        return ODDS_API_TEAM_NAMES[name.strip()]
    except KeyError:
        raise TeamMappingError(
            f"The Odds API team name {name!r} is not in the explicit mapping "
            "table. Add it deliberately in app/rosterdata/teams.py -- this is "
            "never resolved by guessing."
        ) from None


def canonical_from_nflverse(code: str) -> CanonicalTeam:
    try:
        return NFLVERSE_TEAM_CODES[code.strip().upper()]
    except KeyError:
        raise TeamMappingError(
            f"nflverse team code {code!r} is not in the explicit mapping table. "
            "Add it deliberately in app/rosterdata/teams.py."
        ) from None
