"""LangGraph Studio / `langgraph dev` entrypoint.

Exposes the compiled driver graph so LangGraph Studio can VISUALIZE it and run it interactively
(step through triage → decompose → dispatch → synthesize, inspect state at each node, time-travel
via the checkpointer). Reads the same env/config as the server (TOTO_GW_*): set
`TOTO_GW_CATALOG`, `TOTO_GW_DRIVER_MODEL`, `TOTO_GW_TRIAGE_MODEL`, the provider key
(e.g. `OPENROUTER_API_KEY`), and optionally `TOTO_GW_TOTO_TOKEN` — put them in `.env`.

Launch:  pip install "langgraph-cli[inmem]"  &&  langgraph dev
"""

from __future__ import annotations

# Absolute imports: langgraph dev loads this file by path (not as a package submodule), so
# relative imports (`from ..app`) fail with "no known parent package". The package is pip-
# installed, so absolute imports resolve fine.
from toto_gateway.app import build_driver, build_gateway
from toto_gateway.config import get_settings
from toto_gateway.driver.graph import build_state_graph


def make_graph():
    settings = get_settings()
    gateway = build_gateway(settings)
    driver = build_driver(settings, gateway)
    # Uncompiled StateGraph — LangGraph Studio / the API attach their own persistence, and
    # reject a graph that ships its own checkpointer.
    return build_state_graph(driver)


# Studio loads this graph object.
graph = make_graph()
