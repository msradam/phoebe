"""Telemetry client abstraction the FSM query actions call.

The FSM's ``query_metrics`` / ``query_logs`` / ``query_traces`` actions
own the telemetry queries (circe-style: the operation IS the FSM action,
not a separate tool surface). They reach the backend through a
``TelemetryClient`` bound on a ContextVar, so the same FSM works in
multiple environments:

* Inside the Harbor agent runner: the bound client proxies to Grafana's
  MCP server (Prometheus / Loki / Tempo) over the live session.
* In tests / demos: a ``MockTelemetryClient`` returns canned rows.
* Standalone with no client bound: the query actions raise a clear
  error telling the operator to bind one.

This keeps the FSM backend-agnostic: it speaks ``query(backend, query)``;
the bound client maps that onto whatever concrete tool the environment
exposes.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Protocol, runtime_checkable

_CLIENT: ContextVar[TelemetryClient | None] = ContextVar("o11y_fsm_telemetry_client", default=None)


@runtime_checkable
class TelemetryClient(Protocol):
    """Minimal surface the FSM query actions need."""

    async def query(self, backend: str, query: str, **kwargs: Any) -> dict[str, Any]:
        """Run ``query`` against ``backend`` (e.g. "prometheus", "loki",
        "tempo"). Return a dict with at least ``ok: bool`` and a
        ``summary`` string; may include ``rows`` / ``raw``."""
        ...

    async def list_datasources(self) -> list[dict[str, Any]]:
        """Return the reachable datasources, each a dict with ``name`` and
        ``type`` (used by the survey step)."""
        ...


def bind_telemetry_client(client: TelemetryClient | None):
    """Bind a client for the current context. Returns the ContextVar token
    so the caller can reset it (``_CLIENT.reset(token)``)."""
    return _CLIENT.set(client)


def get_telemetry_client() -> TelemetryClient | None:
    return _CLIENT.get()


class TelemetryClientNotBound(RuntimeError):
    """Raised when a query action runs with no client bound."""


def require_telemetry_client() -> TelemetryClient:
    client = _CLIENT.get()
    if client is None:
        raise TelemetryClientNotBound(
            "No telemetry client is bound. The o11y-fsm query actions need a "
            "backend (Grafana MCP in the Harbor runner, or a MockTelemetryClient "
            "in tests/demos). Bind one with bind_telemetry_client(...)."
        )
    return client


class MockTelemetryClient:
    """Deterministic stand-in for tests and standalone demos.

    Returns canned rows keyed loosely off the backend so a walk of the FSM
    produces non-trivial evidence without any real Grafana stack.
    """

    def __init__(self, datasources: list[dict[str, Any]] | None = None):
        self._datasources = datasources or [
            {"name": "prometheus", "type": "prometheus"},
            {"name": "loki", "type": "loki"},
            {"name": "tempo", "type": "tempo"},
        ]
        self.calls: list[dict[str, Any]] = []

    async def query(self, backend: str, query: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"backend": backend, "query": query, **kwargs})
        return {
            "ok": True,
            "backend": backend,
            "summary": f"[mock {backend}] {query[:60]} -> 1 series, non-zero",
            "rows": [{"metric": "mock", "value": 1}],
        }

    async def list_datasources(self) -> list[dict[str, Any]]:
        return list(self._datasources)
