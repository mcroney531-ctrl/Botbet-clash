"""Every place in `app/` that can create money-bearing competition state.

The Game-repair dependency census established the pattern and the reason
for it: a guard is only as complete as the set of call sites it covers, and
that set is not a fixed fact -- it grows every time someone adds a writer.
Prose in a review ("I grepped, it's fine") decays the moment the next
writer lands. So the set is enumerated mechanically from the AST and
compared against a registry that a human had to fill in.

What is enumerated:

  * construction of the four money-bearing ORM rows and their domain
    counterparts -- Ticket, Wager, Settlement, BankrollTransaction
  * every call to the repository methods that persist them --
    `add_ticket`, `add_wager`, `add_settlement`, `record`

`record` is matched by NAME, on any receiver. That is deliberately broad:
narrowing it to receivers that look like a ledger would let a future writer
escape the census by naming its variable something else, which is precisely
the failure this exists to prevent. An unrelated `.record()` shows up and
gets classified NON_PERSISTENT once; that is a cheap price.

What this census does NOT claim
-------------------------------

It does not prove the rehearsal boundary holds. A census can only show that
every writer was LOOKED AT. The closure argument is the database trigger
from migration `a3f81c6b57e9`: a bankroll-altering row scoped to a
non-real-money week is refused by Postgres regardless of which Python
object wrote it, including a raw `session.add()` that never touches a
repository. The census makes new writers visible; the trigger makes them
harmless.
"""

from __future__ import annotations

import ast
import pathlib
from dataclasses import dataclass
from enum import StrEnum

APP_ROOT = pathlib.Path(__file__).resolve().parent.parent

# The money-bearing constructors, ORM rows and their domain counterparts
# alike. Both spellings are listed because `app/db/mappers/` imports the
# ORM classes under a `...Row` alias and the domain classes unaliased.
MONEY_ROWS = frozenset({
    "Ticket", "TicketRow",
    "Wager", "WagerRow",
    "Settlement", "SettlementRow",
    "BankrollTransaction", "BankrollTransactionRow",
})

PERSISTING_METHODS = frozenset({
    "add_ticket", "add_wager", "add_settlement", "record",
})


class Classification(StrEnum):
    THE_GUARD = "THE_GUARD"
    """This call site IS the rehearsal boundary, or sits behind it in the
    same transaction. A change here changes the guard."""

    GUARDED = "GUARDED"
    """Persists a real row and passes through the boundary -- the service
    resolves the week profile first, and the database trigger stands behind
    it either way."""

    NON_PERSISTENT = "NON_PERSISTENT"
    """Constructs an object that never reaches a database session: a pure
    mapper's return value, or the Phase-1 in-memory domain layer whose
    `BankrollLedger` is a Python list (`app/domain/ledger.py`). No row, no
    money."""


@dataclass(frozen=True, slots=True, order=True)
class WriterSite:
    """One syntactic place that builds or persists money-bearing state."""

    module: str
    qualname: str
    target: str

    def describe(self) -> str:
        return f"{self.module}::{self.qualname} -> {self.target}"


@dataclass(frozen=True, slots=True)
class Entry:
    classification: Classification
    reason: str


# ---------------------------------------------------------------------
# The registry. Every discovered site must appear here, and every entry
# here must still be discoverable -- `audit_census()` enforces both
# directions, so a deleted writer cannot leave a stale approval behind.
# ---------------------------------------------------------------------

CENSUS: dict[WriterSite, Entry] = {}


def _register(module: str, qualname: str, target: str, classification: Classification, reason: str) -> None:
    CENSUS[WriterSite(module, qualname, target)] = Entry(classification, reason)


for _q, _t in (
    ("ticket_to_orm", "TicketRow"),
    ("ticket_to_domain", "Ticket"),
    ("wager_to_orm", "WagerRow"),
    ("wager_to_domain", "Wager"),
    ("settlement_to_orm", "SettlementRow"),
    ("settlement_to_domain", "Settlement"),
    ("bankroll_transaction_to_orm", "BankrollTransactionRow"),
    ("bankroll_transaction_to_domain", "BankrollTransaction"),
):
    _register(
        "app.db.mappers.competition", _q, _t, Classification.NON_PERSISTENT,
        "Pure field-by-field translation. The module holds no session and "
        "imports none; its return value is persisted, if ever, by a caller "
        "that is itself in this census.",
    )

for _q, _t in (
    ("SeasonService.register_competitor", "BankrollTransaction"),
    ("SeasonService.register_competitor", ".record()"),
    ("SeasonService.issue_ticket", "Ticket"),
    ("SeasonService.record_execution", "Wager"),
    ("SeasonService.record_execution", "BankrollTransaction"),
    ("SeasonService.record_execution", ".record()"),
    ("SeasonService.settle_wager", "Settlement"),
    ("SeasonService.settle_wager", "BankrollTransaction"),
    ("SeasonService.settle_wager", ".record()"),
):
    _register(
        "app.domain.season_service", _q, _t, Classification.NON_PERSISTENT,
        "The Phase-1 in-memory service. Its `ledger` is a `BankrollLedger` "
        "(app/domain/ledger.py), which appends to a Python list and has no "
        "database access whatsoever. It cannot write a row, so it cannot "
        "move the official bankroll. If it is ever backed by a real "
        "repository this entry must be reclassified -- which is what the "
        "self-audit is for.",
    )

_register(
    "app.services.season_commissioner", "SeasonCommissioner.register_competitor",
    "BankrollTransactionRow", Classification.GUARDED,
    "SEASON_START funding. Carries `week_id=None` by construction, so it is "
    "outside the week-scoped boundary by design -- season funding is not a "
    "week's money. Both the repository guard and the database trigger pass "
    "a NULL week_id through untouched.",
)
_register(
    "app.services.season_commissioner", "SeasonCommissioner.register_competitor",
    ".record()", Classification.GUARDED,
    "Same call: the SEASON_START row above, persisted through the guarded "
    "repository method.",
)
_register(
    "app.services.season_commissioner", "SeasonCommissioner.issue_ticket",
    "TicketRow", Classification.GUARDED,
    "A ticket carries no money. It is permitted on a rehearsal week on "
    "purpose -- candidate selection, Kelly sizing, caps and the Pounce "
    "limit are what the rehearsal exists to exercise -- and is announced "
    "under a SIMULATED_* event type so nothing downstream reads it as an "
    "instruction to place a bet.",
)
_register(
    "app.services.season_commissioner", "SeasonCommissioner.issue_ticket",
    ".add_ticket()", Classification.GUARDED,
    "Persists the ticket above, after `_require_profile` has refused any "
    "week whose flags match no reviewed profile.",
)
_register(
    "app.services.season_commissioner", "SeasonCommissioner.record_execution",
    "WagerRow", Classification.THE_GUARD,
    "The execution boundary itself. The status is checked against the week "
    "profile before the row is built, so a rehearsal cannot produce a "
    "PLACED wager and a competitive week cannot produce a SIMULATED one. "
    "The `wagers` trigger refuses a PLACED row on a non-real-money week "
    "even if this check is bypassed.",
)
_register(
    "app.services.season_commissioner", "SeasonCommissioner.record_execution",
    ".add_wager()", Classification.THE_GUARD,
    "Persists the wager above inside the same transaction as the stake "
    "debit, so the two are never observable apart.",
)
_register(
    "app.services.season_commissioner", "SeasonCommissioner.record_execution",
    "BankrollTransactionRow", Classification.THE_GUARD,
    "The STAKE debit. Reached only under `status.moves_money`, which is "
    "true for PLACED alone.",
)
_register(
    "app.services.season_commissioner", "SeasonCommissioner.record_execution",
    ".record()", Classification.THE_GUARD,
    "Persists the STAKE debit through the guarded repository method.",
)
_register(
    "app.services.season_commissioner", "SeasonCommissioner.settle_wager",
    "SettlementRow", Classification.GUARDED,
    "The settlement result is persisted for a simulated wager too -- "
    "rehearsing settlement is the point -- and carries no money itself. "
    "UNIQUE(wager_id) makes it duplicate-safe in both modes.",
)
_register(
    "app.services.season_commissioner", "SeasonCommissioner.settle_wager",
    ".add_settlement()", Classification.GUARDED,
    "Persists the settlement above.",
)
_register(
    "app.services.season_commissioner", "SeasonCommissioner.settle_wager",
    "BankrollTransactionRow", Classification.THE_GUARD,
    "The return credit. Reached only under `credits_money`, which requires "
    "BOTH a PLACED wager and a COMPETITIVE week.",
)
_register(
    "app.services.season_commissioner", "SeasonCommissioner.settle_wager",
    ".record()", Classification.THE_GUARD,
    "Persists the return credit through the guarded repository method.",
)


# ---------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------


class _Visitor(ast.NodeVisitor):
    def __init__(self, module: str) -> None:
        self.module = module
        self.stack: list[str] = []
        self.found: set[WriterSite] = set()

    def _qualname(self) -> str:
        return ".".join(self.stack) if self.stack else "<module>"

    def _scoped(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_ClassDef = _scoped
    visit_FunctionDef = _scoped
    visit_AsyncFunctionDef = _scoped

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Name) and func.id in MONEY_ROWS:
            self.found.add(WriterSite(self.module, self._qualname(), func.id))
        elif isinstance(func, ast.Attribute) and func.attr in PERSISTING_METHODS:
            self.found.add(
                WriterSite(self.module, self._qualname(), f".{func.attr}()")
            )
        self.generic_visit(node)


def discover(root: pathlib.Path | None = None) -> set[WriterSite]:
    """Every money-writing site under `app/`, excluding tests.

    Tests are excluded because a test that deliberately attempts a
    forbidden write -- and there are several, proving the guard and the
    trigger refuse it -- is not a production writer and classifying it
    would say the opposite of what it proves.
    """

    root = root or APP_ROOT
    found: set[WriterSite] = set()
    for path in sorted(root.rglob("*.py")):
        if "tests" in path.parts:
            continue
        module = "app." + ".".join(path.relative_to(root).with_suffix("").parts)
        visitor = _Visitor(module)
        visitor.visit(ast.parse(path.read_text()))
        found |= visitor.found
    return found


@dataclass(frozen=True, slots=True)
class CensusAudit:
    unclassified: tuple[WriterSite, ...]
    stale: tuple[WriterSite, ...]

    @property
    def clean(self) -> bool:
        return not self.unclassified and not self.stale

    def render(self) -> str:
        out = ["MONEY-WRITER CENSUS"]
        if self.unclassified:
            out.append("  UNCLASSIFIED — a new money writer appeared and nobody looked at it:")
            out += [f"    {s.describe()}" for s in sorted(self.unclassified)]
        if self.stale:
            out.append("  STALE — classified, but no longer present:")
            out += [f"    {s.describe()}" for s in sorted(self.stale)]
        if self.clean:
            out.append(f"  {len(CENSUS)} writers, all classified.")
        return "\n".join(out)


def audit_census(root: pathlib.Path | None = None) -> CensusAudit:
    found = discover(root)
    registered = set(CENSUS)
    return CensusAudit(
        unclassified=tuple(sorted(found - registered)),
        stale=tuple(sorted(registered - found)),
    )


# ---------------------------------------------------------------------
# The standings / awards tripwire
# ---------------------------------------------------------------------
#
# `counts_toward_standings` and `counts_toward_awards` are currently read
# by nothing that computes a result, because nothing computes a result yet
# -- the season table and the awards are a later phase. That makes them
# dead metadata, and dead metadata is how a rehearsal week ends up in the
# standings: whoever writes the leaderboard six weeks from now has no
# reason to go looking for two booleans nobody ever consulted.
#
# The invariant, stated here so it is written down somewhere executable:
#
#     Any code that accumulates a competitor's SEASON-LEVEL standing or
#     decides a SEASON AWARD must exclude weeks whose corresponding flag
#     is false. The Week row is the authority; the flag is not advisory.
#
# CONSTITUTION.md §6 and RULES.md §3 are the source: a rehearsal week
# produces "no official bankroll results / no standings / no season
# awards".
#
# The tripwire below cannot enforce the invariant -- no such code exists to
# check -- so it enforces the next best thing: the moment a module appears
# whose name or functions are about standings, awards or a leaderboard, it
# must mention at least one of the flags, or this fails and a human has to
# say why not.

STANDINGS_WORDS = ("standing", "award", "leaderboard")
FLAG_NAMES = ("counts_toward_standings", "counts_toward_awards")

# Modules that legitimately talk about standings/awards without deciding
# anything: the definitions themselves and this census.
STANDINGS_EXEMPT = frozenset({
    "app.domain.week_profile",
    "app.services.money_writer_census",
})


@dataclass(frozen=True, slots=True)
class StandingsAudit:
    unguarded: tuple[str, ...]

    @property
    def clean(self) -> bool:
        return not self.unguarded

    def render(self) -> str:
        if self.clean:
            return (
                "STANDINGS / AWARDS TRIPWIRE\n"
                "  No module decides a standing or an award yet. The invariant "
                "is recorded and this will fail the moment one appears without "
                "reading the flags."
            )
        return "\n".join(
            ["STANDINGS / AWARDS TRIPWIRE", "  These decide standings or awards and never read the week flags:"]
            + [f"    {m}" for m in self.unguarded]
        )


def audit_standings_flags(root: pathlib.Path | None = None) -> StandingsAudit:
    root = root or APP_ROOT
    unguarded: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if "tests" in path.parts:
            continue
        module = "app." + ".".join(path.relative_to(root).with_suffix("").parts)
        if module in STANDINGS_EXEMPT:
            continue
        source = path.read_text()
        tree = ast.parse(source)
        names = [module.rsplit(".", 1)[-1]] + [
            n.name for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        if not any(w in n.lower() for n in names for w in STANDINGS_WORDS):
            continue
        if not any(flag in source for flag in FLAG_NAMES):
            unguarded.append(module)
    return StandingsAudit(unguarded=tuple(unguarded))


if __name__ == "__main__":  # pragma: no cover - operator convenience
    print(audit_census().render())
    print(audit_standings_flags().render())
