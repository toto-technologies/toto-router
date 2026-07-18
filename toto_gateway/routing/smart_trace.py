"""LangSmith trace for the smart passthrough route (BYO tracing, env-gated).

The smart route is NOT a LangGraph — `routing/smart.py` is a pure classify->route on the
passthrough — so it gets no automatic LangSmith run the way the driver's StateGraph does
(`driver/graph.py`). This emits the equivalent: one `toto/smart` chain run per smart request
with a `classify` child span and the routing decision + served model + token usage attached,
so smart routing shows up in the SAME LangSmith project as the driver.

Reconstructed post-hoc from the finished request (the gateway already writes its native trace
record at finalize the same way) so tracing never has to thread through the hot retry/fallback
loop. Gated per call on `tracing_is_enabled()` (LANGSMITH_TRACING=true + a key) — zero LangSmith
coupling unless the customer opts in. Best-effort throughout: tracing must never break a served
request, so every path swallows its own errors.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def tracing_enabled() -> bool:
    """True iff LangSmith tracing is on. Mirrors driver.core._ls_enabled — the same BYO gate."""
    try:
        from langsmith.utils import tracing_is_enabled

        return bool(tracing_is_enabled())
    except Exception:
        return False


def _provider_of(model: str | None) -> str:
    return model.split("/")[0] if "/" in (model or "") else (model or "")


def emit(*, messages, classifier_model, label, route_reason, resolved_model, served_model,
         content, tokens_prompt, tokens_completion, cost_usd, latency_ms, classify_ms,
         rating: str | None = None, request_id: str | None = None,
         conversation_key: str | None = None) -> None:
    """Post a `toto/smart` LangSmith run for one finished smart request. Never raises.

    - root (chain): the user messages in, the served response + usage_metadata out (usage in
      OUTPUTS is what LangSmith rolls up into trace-level token/cost columns), the routing
      DECISION on metadata (classified_as / route_reason / resolved vs served model).
    - classify (llm child): the Gemini-Flash tagging step, its own latency, the label out.
    """
    try:
        from langsmith import RunTree

        now = datetime.now(timezone.utc)
        start = now - timedelta(milliseconds=float(latency_ms or 0))
        root = RunTree(
            name="toto/smart",
            run_type="chain",
            start_time=start,
            inputs={"model": "smart", "messages": messages},
            tags=["toto", "smart-route"],
        )
        root.post()

        cl_end = start + timedelta(milliseconds=float(classify_ms or 0))
        child = root.create_child(
            name="classify", run_type="llm", start_time=start,
            inputs={"model": classifier_model, "messages": messages},
        )
        child.post()
        child.end(outputs={"label": label}, end_time=cl_end)
        child.patch()

        root.end(
            outputs={"content": content,
                     "usage_metadata": {
                         "input_tokens": tokens_prompt or 0,
                         "output_tokens": tokens_completion or 0,
                         "total_tokens": (tokens_prompt or 0) + (tokens_completion or 0)}},
            metadata={"classified_as": label,
                      "route_reason": route_reason,
                      "resolved_model": resolved_model,
                      "rating": rating,
                      # Join keys: request_id → the gateway's own trace record (gateway_events);
                      # conversation_key → every turn of the same multi-turn chat.
                      "request_id": request_id,
                      "conversation_key": conversation_key,
                      # ls_model_name/ls_provider are LangSmith's conventional keys — surface the
                      # SERVED model (may differ from resolved after a fallback) in the Model chip.
                      "ls_model_name": served_model,
                      "ls_provider": _provider_of(served_model),
                      "cost_usd": cost_usd,
                      "latency_ms": latency_ms},
            end_time=now,
        )
        root.patch()
    except Exception:  # tracing is best-effort — never break a served request
        pass
