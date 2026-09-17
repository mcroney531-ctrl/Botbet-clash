"""Explicit, source-controlled provider name aliases.

ONE mechanism, deliberately narrow. This is NOT a nickname dictionary, a
fuzzy matcher, or a suffix rule. Each entry is a reviewed statement that
one specific provider's spelling of one specific name refers to one
specific GSIS identity.

Why not a general Josh <-> Joshua rule
--------------------------------------
The first real ingestion run measured exactly one miss: The Odds API said
"Joshua Palmer" where nflverse says "Josh Palmer". A nickname-expansion
rule would have fixed it -- and would also be the kind of rule that
eventually fuses two real people. The very same game contained a "Josh
Allen". Generalising from one observation is how a resolver stops being
trustworthy, so the alias records the observation and nothing more.

Safety properties preserved
---------------------------
An alias names a STABLE IDENTITY (GSIS), never another display name, so
it cannot chain or drift. And the alias target must still be found in the
event's own two-team roster pool at resolution time -- an alias can never
pull a player into a game he is not in, nor survive his moving teams.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from app.marketdata.dto import normalize_player_name

# (provider, normalized provider spelling) -> GSIS id
#
# Every entry must cite the run that measured it. An alias added without
# an observation behind it is a guess wearing a table's clothes.
PROVIDER_PLAYER_ALIASES: Mapping[tuple[str, str], str] = MappingProxyType(
    {
        # Measured 2026-09-17, first real ingestion run (DET @ BUF, week 3):
        # The Odds API returned "Joshua Palmer"; nflverse lists "Josh Palmer",
        # BUF WR. One miss out of fifteen players; cost 11 otherwise valid
        # quotes.
        ("THE_ODDS_API", "joshua palmer"): "00-0036988",
    }
)


def alias_target(*, provider: str, display_name: str) -> str | None:
    """The GSIS id this provider spelling refers to, or None."""

    return PROVIDER_PLAYER_ALIASES.get((provider, normalize_player_name(display_name)))
