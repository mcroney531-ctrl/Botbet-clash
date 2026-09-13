"""Injectable clock so domain services are deterministic in tests."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock:
    """Test/Week-0-harness clock: advances only when told to."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime.now(timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, **timedelta_kwargs: float) -> None:
        from datetime import timedelta

        self._now = self._now + timedelta(**timedelta_kwargs)

    def set(self, when: datetime) -> None:
        self._now = when
