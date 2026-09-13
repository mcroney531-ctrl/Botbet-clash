"""In-process CompetitionEventBus (ARCHITECTURE.md §7).

Phase 1 keeps this in memory: `publish` appends to an ordered log and then
fans out to subscribers, matching the "persisted first, dispatched second"
rule so a subscriber exception can never make an event vanish from the
audit trail. A real deployment backs `events` with the `competition_events`
table (DATABASE.md §10) instead of a list, without changing this
interface.
"""

from __future__ import annotations

from collections.abc import Callable

from app.domain.models import CompetitionEvent

Subscriber = Callable[[CompetitionEvent], None]


class CompetitionEventBus:
    def __init__(self) -> None:
        self._events: list[CompetitionEvent] = []
        self._subscribers: list[Subscriber] = []

    def subscribe(self, subscriber: Subscriber) -> None:
        self._subscribers.append(subscriber)

    def publish(self, event: CompetitionEvent) -> None:
        self._events.append(event)
        for subscriber in self._subscribers:
            # A subscriber failing must never un-append the event above —
            # the event log is the source of truth even if a downstream
            # consumer (e.g. a notification channel) chokes on it.
            subscriber(event)

    @property
    def events(self) -> tuple[CompetitionEvent, ...]:
        return tuple(self._events)

    def events_for_week(self, week_id: str) -> tuple[CompetitionEvent, ...]:
        return tuple(e for e in self._events if e.week_id == week_id)

    def events_for_competitor(self, competitor_id: str) -> tuple[CompetitionEvent, ...]:
        return tuple(e for e in self._events if e.competitor_id == competitor_id)
