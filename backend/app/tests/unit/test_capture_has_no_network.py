"""`capture_checkpoint` must be reachable only by database reads.

Stated structurally rather than by convention. A provider call inside a
capture would hold a transaction open across an unbounded network wait,
and a provider timeout would abort a capture that had already written half
a slate's snapshots — the failure would look like a database problem and
be diagnosed as one.

The guard walks the ACTUAL import graph rather than one module's import
list, because the dangerous version of this regression is transitive: a
helper three modules down growing an adapter import is exactly how a
network call gets into a transaction without anyone deciding to put it
there.
"""

from __future__ import annotations

import ast
import pathlib

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[3]

# Anything that can open a socket, plus the adapter packages that wrap them.
FORBIDDEN_PREFIXES = (
    "app.marketdata.providers",
    "app.rosterdata.providers",
    "app.ai.providers",
    "httpx",
    "requests",
    "urllib.request",
    "socket",
    "http.client",
)

ENTRY_POINTS = (
    "app.forecast_lab.checkpoint_service",
    "app.forecast_lab.market_snapshot_service",
    "app.forecast_lab.quote_selection",
    "app.forecast_lab.evidence_service",
)


def _module_path(module: str) -> pathlib.Path | None:
    base = BACKEND_ROOT / pathlib.Path(*module.split("."))
    if base.with_suffix(".py").exists():
        return base.with_suffix(".py")
    if (base / "__init__.py").exists():
        return base / "__init__.py"
    return None


def _direct_imports(path: pathlib.Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import; this codebase uses absolute
                continue
            if node.module:
                found.add(node.module)
                found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def _reachable(entry: str) -> set[str]:
    seen: set[str] = set()
    stack = [entry]
    while stack:
        module = stack.pop()
        if module in seen:
            continue
        seen.add(module)
        path = _module_path(module)
        if path is None:
            continue  # stdlib / third-party leaf, or an imported symbol
        for imported in _direct_imports(path):
            if imported not in seen:
                stack.append(imported)
    return seen


def test_the_capture_path_cannot_reach_a_network_client():
    offenders: list[str] = []
    for entry in ENTRY_POINTS:
        assert _module_path(entry) is not None, f"{entry} not found; the guard is scanning nothing"
        for module in sorted(_reachable(entry)):
            if module.startswith(FORBIDDEN_PREFIXES):
                offenders.append(f"{entry} -> {module}")
    assert offenders == [], "network client reachable from the capture path:\n" + "\n".join(offenders)


def test_the_guard_would_catch_a_transitive_import():
    """The guard is only worth having if it follows the graph. Proven by
    pointing it at a module that legitimately DOES reach a provider: the
    cycle's own refresh side."""

    reachable = _reachable("app.marketdata.live_ingest")
    assert any(m.startswith("app.marketdata.providers") for m in reachable)


def test_the_cycle_separates_refresh_from_capture():
    """The choreography module may import the capture, but the capture must
    not import the choreography — otherwise the direction of the dependency
    stops enforcing anything."""

    cycle = _reachable("app.marketdata.checkpoint_cycle")
    assert "app.forecast_lab.checkpoint_service" in cycle

    capture = _reachable("app.forecast_lab.checkpoint_service")
    assert "app.marketdata.checkpoint_cycle" not in capture
