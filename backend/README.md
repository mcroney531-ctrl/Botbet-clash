# Backend — Phase 1 domain skeleton

Implements constitution §117 **Phase 1 (Domain skeleton)**: `Season`,
`SeasonRules`, `Week`, `Competitor`, the bankroll ledger, `Ticket`,
`Wager`, `Settlement`, and `CompetitionEvent`, driven end-to-end against a
`FakePropMarket` — no real sports-data provider or AI adapter yet (those
are Phase 3/6). See `../RULES.md`, `../ARCHITECTURE.md`, and
`../DATABASE.md` for the rules and design this code implements.

This is plain Python (dataclasses + stdlib), not yet backed by
SQLAlchemy/Postgres — persistence matching `DATABASE.md` is layered on in
Phase 2 without changing these domain interfaces.

## Layout

```
app/
  core/     money (integer cents), clock, id generation
  domain/   enums, models, ledger, risk math, event bus, SeasonService
  tests/    Phase 1 acceptance test: a full mocked weekly lifecycle
```

## Running the tests

```
cd backend
pip install -e ".[dev]"
pytest -q
```

`app/tests/test_season_service.py` exercises: BET → WIN, PASS with
structured reasoning, a Pounce ticket → LOSS, the one-official-decision-
per-week rule, the Pounce-ticket limit, and bankruptcy (bankroll driven
below the minimum stake freezes the competitor and blocks further
tickets).
