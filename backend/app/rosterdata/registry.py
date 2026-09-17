"""Roster provider name -> adapter."""

from __future__ import annotations

from app.rosterdata.base import RosterDataProvider
from app.rosterdata.providers.nflverse import PROVIDER_NAME as NFLVERSE_NAME
from app.rosterdata.providers.nflverse import NflverseRosterProvider


def build_roster_provider(name: str, **kwargs) -> RosterDataProvider:
    if name == NFLVERSE_NAME:
        return NflverseRosterProvider(**kwargs)
    raise KeyError(f"unknown roster provider {name!r}; expected {NFLVERSE_NAME!r}")
