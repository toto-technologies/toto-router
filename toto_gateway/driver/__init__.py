"""Driver layer — the Sonnet-class agent that decomposes a request into Toto tasks and
routes each task by its metadata.

This sits ABOVE the Phase-0/1 gateway: the gateway is the per-task executor (one
`complete()` per routed task); the driver orchestrates decompose -> classify -> dispatch ->
synthesize as a LangGraph graph. The routing intelligence lives here (the metadata
classifier), which is why the request-level exemplar router is retired.

Design notes:
  - Nodes are plain async methods on `Driver` (testable without LangGraph).
  - LangGraph only *wires* them (graph.py) — framework at the edge.
  - Toto is the metadata plane: only task metadata + execution provenance cross the API,
    never prompts/answers/content (feedback_toto_doesnt_want_your_data).
"""

from __future__ import annotations

from .adapters import (
    AdapterRegistry,
    ClaudeCodeAdapter,
    GatewayAdapter,
    HarnessAdapter,
    PiAdapter,
    SubagentError,
)
from .classify import TaskDecision, classify
from .core import Driver, DriverResult, Exec

__all__ = [
    "Driver", "DriverResult", "Exec", "classify", "TaskDecision",
    "HarnessAdapter", "AdapterRegistry", "GatewayAdapter", "ClaudeCodeAdapter", "PiAdapter",
    "SubagentError",
]
