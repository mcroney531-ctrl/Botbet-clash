"""nflverse roster provider.

THE ONLY module permitted to know nflverse column names or asset URLs.

Consumption is a direct release download parsed with the stdlib `csv`
module, deliberately NOT `nfl_data_py`:

  * zero new dependencies -- nfl_data_py pulls pandas + pyarrow, neither
    of which this backend has or otherwise needs;
  * we keep the EXACT raw bytes. The whole provenance model is sha256 over
    what arrived on the wire, and a DataFrame reader hands us its
    interpretation while the bytes are gone;
  * no coupling to an R package, nor to a Python wrapper's release cadence.

The tradeoff we accept in exchange is owning the column contract: if
nflverse renames a column we break. That is mitigated by asserting the
expected header on load and failing as MALFORMED_ROSTER_RESPONSE, never
by silently reading None.

No credential is involved. nflverse is public, which is why this provider
has no key handling at all -- but the sanitizer still applies to error
messages on principle.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

import httpx

from app.marketdata.base import ProviderCallMetadata
from app.marketdata.telemetry import sha256_hex
from app.rosterdata.base import (
    RosterDataError,
    RosterEntry,
    RosterFetchResult,
    RosterSnapshot,
)
from app.rosterdata.teams import TeamMappingError, canonical_from_nflverse

PROVIDER_NAME = "NFLVERSE"
RELEASE_BASE = "https://github.com/nflverse/nflverse-data/releases/download"
DEFAULT_TIMEOUT_SECONDS = 60.0

REQUIRED_COLUMNS = ("season", "team", "position", "full_name", "gsis_id")
"""Asserted on every load. A missing column is a contract break, not a
row we quietly skip."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class NflverseRosterProvider:
    provider_name = PROVIDER_NAME

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.Client | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self._client = client

    # -- transport --------------------------------------------------

    def _download(
        self, *, capability: str, path: str
    ) -> tuple[bytes | None, ProviderCallMetadata, tuple[str, str] | None]:
        requested_at = _utcnow()
        base_meta = ProviderCallMetadata(endpoint_capability=capability, requested_at=requested_at)
        client = self._client or httpx.Client(timeout=self.timeout_seconds, follow_redirects=True)
        owns = self._client is None
        try:
            response = client.get(f"{RELEASE_BASE}/{path}")
        except httpx.TimeoutException:
            return None, base_meta, ("ROSTER_TIMEOUT", "roster download timed out")
        except httpx.TransportError:
            return None, base_meta, (
                "ROSTER_SOURCE_UNAVAILABLE",
                "could not reach the roster source (transport failure)",
            )
        except Exception:
            return None, base_meta, (
                "UNKNOWN_ROSTER_ERROR",
                "roster download raised an unexpected client error",
            )
        finally:
            if owns:
                client.close()

        body = response.content
        meta = ProviderCallMetadata(
            endpoint_capability=capability,
            requested_at=requested_at,
            responded_at=_utcnow(),
            http_status=response.status_code,
            raw_response_body=body,
            raw_response_sha256=sha256_hex(body),
            raw_response_bytes=len(body),
        )
        if response.status_code == 404:
            return None, meta, (
                "UNSUPPORTED_SEASON_WEEK",
                "the roster source has no published asset for that season",
            )
        if response.status_code >= 400:
            return None, meta, (
                "ROSTER_SOURCE_UNAVAILABLE",
                f"roster source returned status {response.status_code}",
            )
        return body, meta, None

    # -- parsing ----------------------------------------------------

    def _parse(
        self, body: bytes, *, season: int, week: int | None, basis: str, retrieved_at: datetime
    ) -> tuple[RosterSnapshot | None, RosterDataError | None]:
        try:
            reader = csv.DictReader(io.StringIO(body.decode("utf-8-sig")))
            header = set(reader.fieldnames or ())
        except (UnicodeDecodeError, csv.Error):
            return None, RosterDataError(
                category="MALFORMED_ROSTER_RESPONSE", message="roster asset was not readable CSV"
            )

        missing = [c for c in REQUIRED_COLUMNS if c not in header]
        if missing:
            return None, RosterDataError(
                category="MALFORMED_ROSTER_RESPONSE",
                message=f"roster asset is missing expected column(s): {', '.join(missing)}",
            )

        entries: list[RosterEntry] = []
        for row in reader:
            if week is not None and (row.get("week") or "").strip():
                try:
                    if int(row["week"]) != week:
                        continue
                except ValueError:
                    continue
            try:
                team = canonical_from_nflverse(row["team"])
            except TeamMappingError:
                # An unknown team code is quarantined, never guessed. It
                # simply does not enter the candidate pool.
                continue
            row_week: int | None
            try:
                row_week = int(row["week"]) if (row.get("week") or "").strip() else None
            except ValueError:
                row_week = None
            entries.append(
                RosterEntry(
                    stable_id=(row.get("gsis_id") or "").strip(),
                    display_name=(row.get("full_name") or "").strip(),
                    team=team,
                    position=((row.get("position") or "").strip() or None),
                    season=season,
                    week=row_week,
                )
            )

        if not entries:
            return None, RosterDataError(
                category="MALFORMED_ROSTER_RESPONSE",
                message="roster asset parsed to zero usable entries",
            )
        return (
            RosterSnapshot(
                provider=self.provider_name,
                season=season,
                week=week,
                basis=basis,
                entries=tuple(entries),
                retrieved_at=retrieved_at,
            ),
            None,
        )

    # -- RosterDataProvider ----------------------------------------

    def fetch_current_roster(self, *, season: int) -> RosterFetchResult[RosterSnapshot]:
        """The CURRENT season roster.

        Captured by us near a checkpoint, this IS contemporaneous roster
        evidence -- which is why live resolution uses it rather than the
        newest numbered weekly file, which would be an older week's state
        wearing a current label.
        """

        body, meta, failure = self._download(
            capability="FETCH_CURRENT_ROSTER", path=f"rosters/roster_{season}.csv"
        )
        if failure is not None:
            return RosterFetchResult(
                payload=None,
                error=RosterDataError(category=failure[0], message=failure[1]),
                call_metadata=meta,
            )
        snapshot, error = self._parse(
            body,
            season=season,
            week=None,
            basis="CURRENT_CONTEMPORANEOUS",
            retrieved_at=meta.responded_at or _utcnow(),
        )
        return RosterFetchResult(payload=snapshot, error=error, call_metadata=meta)

    def fetch_weekly_roster(self, *, season: int, week: int) -> RosterFetchResult[RosterSnapshot]:
        """The week-level HISTORICAL product.

        Marked HISTORICAL_RECONSTRUCTED because today's weekly files are
        not, by themselves, proof of what was available at that old
        checkpoint. We never claim contemporaneity we did not establish.
        """

        body, meta, failure = self._download(
            capability="FETCH_WEEKLY_ROSTER", path=f"weekly_rosters/roster_weekly_{season}.csv"
        )
        if failure is not None:
            return RosterFetchResult(
                payload=None,
                error=RosterDataError(category=failure[0], message=failure[1]),
                call_metadata=meta,
            )
        snapshot, error = self._parse(
            body,
            season=season,
            week=week,
            basis="HISTORICAL_RECONSTRUCTED",
            retrieved_at=meta.responded_at or _utcnow(),
        )
        if snapshot is None and error is not None and "zero usable entries" in error.message:
            error = RosterDataError(
                category="UNSUPPORTED_SEASON_WEEK",
                message=f"no roster entries published for {season} week {week}",
            )
        return RosterFetchResult(payload=snapshot, error=error, call_metadata=meta)


# ---------------------------------------------------------------------
# Schedule (Phase 4A.6)
#
# Registration needs an AUTHORITATIVE answer to "which NFL week does this
# provider event belong to". The first version answered it with "whichever
# week the operator typed", which is not an answer: `Game.week_number` is
# part of a permanent identity scope, and the Phase 4A.2 run had already
# recorded DET @ BUF as week 3 when the schedule says week 2.
#
# Same free release base as the rosters, so this costs nothing and adds no
# new vendor relationship.
# ---------------------------------------------------------------------

SCHEDULE_COLUMNS = ("season", "game_type", "week", "gameday", "gametime", "away_team", "home_team")
"""Asserted on every load. A missing column is a contract break."""

SCHEDULE_TIMEZONE = "America/New_York"
"""nflverse publishes `gametime` as US Eastern wall-clock, not UTC. Parsing
it as UTC would shift every kickoff by four or five hours and silently
break kickoff agreement checks."""


class NflverseScheduleProvider(NflverseRosterProvider):
    """The schedule half of the same nflverse release feed.

    Subclasses the roster provider purely to reuse its download/telemetry
    transport -- the URL base, timeout handling and ProviderCallMetadata
    construction are identical, and a second copy of them would be the same
    duplication this phase keeps removing.
    """

    def fetch_schedule(self, *, season: int):
        from app.scheduledata.base import (
            ScheduleDataError,
            ScheduledGame,
            ScheduleFetchResult,
            ScheduleSnapshot,
        )

        body, meta, failure = self._download(
            capability="FETCH_SCHEDULE", path="schedules/games.csv"
        )
        if failure is not None:
            return ScheduleFetchResult(
                payload=None,
                error=ScheduleDataError(category=failure[0], message=failure[1]),
                call_metadata=meta,
            )

        try:
            reader = csv.DictReader(io.StringIO(body.decode("utf-8-sig")))
            header = set(reader.fieldnames or ())
        except (UnicodeDecodeError, csv.Error):
            return ScheduleFetchResult(
                payload=None,
                error=ScheduleDataError(
                    category="MALFORMED_ROSTER_RESPONSE",
                    message="schedule asset was not readable CSV",
                ),
                call_metadata=meta,
            )

        missing = [c for c in SCHEDULE_COLUMNS if c not in header]
        if missing:
            return ScheduleFetchResult(
                payload=None,
                error=ScheduleDataError(
                    category="MALFORMED_ROSTER_RESPONSE",
                    message=f"schedule asset is missing column(s): {', '.join(missing)}",
                ),
                call_metadata=meta,
            )

        games = []
        for row in reader:
            if row.get("season") != str(season):
                continue
            try:
                home = canonical_from_nflverse(row["home_team"])
                away = canonical_from_nflverse(row["away_team"])
            except TeamMappingError:
                # An unmappable team in the schedule is reported by its
                # ABSENCE from the snapshot, which makes the event
                # unresolvable and therefore unregisterable. Silently
                # guessing a code here would be the one thing worse.
                continue
            games.append(
                ScheduledGame(
                    season=season,
                    week=int(row["week"]),
                    game_type=row["game_type"],
                    home=home,
                    away=away,
                    kickoff_at=_schedule_kickoff(row.get("gameday"), row.get("gametime")),
                )
            )

        return ScheduleFetchResult(
            payload=ScheduleSnapshot(
                provider=PROVIDER_NAME,
                season=season,
                retrieved_at=meta.responded_at or _utcnow(),
                games=tuple(games),
            ),
            error=None,
            call_metadata=meta,
        )


def _schedule_kickoff(gameday: str | None, gametime: str | None) -> datetime | None:
    """`gameday` + `gametime` (US Eastern wall clock) -> aware UTC.

    Returns None rather than guessing when either field is blank: nflverse
    publishes future games with an empty `gametime` before the broadcast
    window is set, and inventing 00:00 would make a placeholder look like a
    real midnight kickoff.
    """

    from zoneinfo import ZoneInfo

    if not gameday or not gametime:
        return None
    try:
        naive = datetime.strptime(f"{gameday} {gametime}", "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    return naive.replace(tzinfo=ZoneInfo(SCHEDULE_TIMEZONE)).astimezone(timezone.utc)
