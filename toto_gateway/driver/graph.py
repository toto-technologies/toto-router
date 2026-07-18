"""LangGraph wiring for the driver — framework at the edge.

The nodes live in core.py as plain async methods; this module only assembles them into a
StateGraph (verified against langgraph 1.2.7):

    START → triage ─┬─ trivial   → answer_trivial → END
                    └─ multistep → decompose → dispatch → synthesize → END

Compiled with an InMemorySaver so every run is checkpointed — `get_state` / `get_state_history`
and time-travel replay are available for testing + debugging (the "state monitoring + testing"
requirement). Swap in a durable saver (the separate `langgraph-checkpoint-sqlite` package) only
when checkpoints must survive a process restart.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from .core import RouteState

if TYPE_CHECKING:
    from .core import Driver


def _route_after_triage(state: RouteState) -> str:
    # path fn returns a key; path_map (below) maps it to a node name.
    return "answer_trivial" if state.get("kind") == "trivial" else "decompose"


def build_state_graph(driver: "Driver") -> StateGraph:
    """The uncompiled StateGraph (nodes + edges). LangGraph Studio / the platform want this
    form so they can attach their own persistence — see studio.py."""
    g = StateGraph(RouteState)
    g.add_node("triage", driver.triage)
    g.add_node("answer_trivial", driver.answer_trivial)
    g.add_node("decompose", driver.decompose)
    g.add_node("dispatch", driver.dispatch)
    g.add_node("synthesize", driver.synthesize)

    g.add_edge(START, "triage")
    g.add_conditional_edges(
        "triage",
        _route_after_triage,
        {"answer_trivial": "answer_trivial", "decompose": "decompose"},
    )
    g.add_edge("answer_trivial", END)
    g.add_edge("decompose", "dispatch")
    g.add_edge("dispatch", "synthesize")
    g.add_edge("synthesize", END)
    return g


def build_graph(driver: "Driver"):
    """Compiled with our own InMemorySaver so `driver.run()` + tests get replayable checkpoints.
    (Studio/the platform use build_state_graph and supply their own persistence instead.)"""
    # name shows as the run/graph title in LangSmith (BYO tracing, env-gated).
    return build_state_graph(driver).compile(checkpointer=InMemorySaver(), name="toto-driver")
