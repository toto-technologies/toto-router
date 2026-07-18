"""Phase 1 decision pipeline — the seams the routing brain plugs into.

The Phase-0 gateway resolved a request straight to its catalog entry. Phase 1 inserts a
decision stage before dispatch: cache -> signals -> guard -> route. Each stage is a small
Protocol with a lean no-op default, so the gateway works unchanged until real components are
injected (every default below reproduces exact Phase-0 behaviour).

Ponytail: flat dataclasses, stdlib only, no framework. The router returns a catalog *model id*
(routing = choosing which catalog entry answers), not a bespoke object graph.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .catalog import Catalog
from .schemas import ChatCompletionRequest, ChatCompletionResponse
from .tokens import estimate_prompt_tokens

# Guard actions. downgrade_local = answer on the box instead of refusing (Gary fold G2):
# over-blocking degrades to "ran local", never "refused". block = true MNPI-would-leak.
ALLOW, DOWNGRADE_LOCAL, BLOCK = "allow", "downgrade_local", "block"


@dataclass
class Signal:
    intent: str = "unknown"          # e.g. code_edit, search, summarize, extract...
    complexity: str = "unknown"      # low | medium | high
    token_estimate: int = 0
    has_tools: bool = False
    embedding: tuple[float, ...] | None = None


@dataclass
class GuardVerdict:
    action: str = ALLOW              # allow | downgrade_local | block
    reasons: list[str] = field(default_factory=list)


@dataclass
class Decision:
    model_id: str                    # catalog id that will answer (may differ from req.model)
    reason: str = "catalog"          # human-readable route reason, logged to the trace


class SignalExtractor(Protocol):
    def extract(self, req: ChatCompletionRequest) -> Signal: ...


class Guard(Protocol):
    def check(self, req: ChatCompletionRequest, signal: Signal) -> GuardVerdict: ...


class Router(Protocol):
    def decide(
        self, req: ChatCompletionRequest, signal: Signal, verdict: GuardVerdict, catalog: Catalog,
        policy: object | None = None,
    ) -> Decision: ...


class ResponseCache(Protocol):
    def get(self, req: ChatCompletionRequest) -> ChatCompletionResponse | None: ...
    def put(self, req: ChatCompletionRequest, resp: ChatCompletionResponse) -> None: ...


class BlockedError(Exception):
    """Raised when a guard blocks a request (MNPI would leak). Mapped to HTTP 403."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("blocked: " + "; ".join(reasons))


class ModelNotPermittedError(Exception):
    """Raised when catalog policy forbids the resolved model BEFORE any upstream call. Mapped to
    HTTP 403 model_not_permitted, carrying the offending model id. `allowlist=True` marks the C3
    ORG deny-by-default gate (vs the C2 per-team deny): the route surfaces the ask-your-admin body
    and writes a denial audit row for it."""

    def __init__(self, model_id: str, *, allowlist: bool = False) -> None:
        self.model_id = model_id
        self.allowlist = allowlist
        who = "your organization's allowlist" if allowlist else "this team"
        super().__init__(f"model {model_id!r} is not permitted for {who}")


class DataPolicyDeniedError(Exception):
    """Raised when the org's data-classification taxonomy (W2-C7) binds this request's classification
    to a 'deny' constraint — the request is rejected BEFORE any upstream call. Mapped to HTTP 403
    with code data_policy_denied. `data_label` is the classification that triggered it (None when the
    taxonomy default denied an unclassifiable request)."""

    def __init__(self, data_label: str | None) -> None:
        self.data_label = data_label
        super().__init__(f"data policy denied for classification {data_label!r}")


# --- Lean defaults: each reproduces Phase-0 behaviour ------------------------


class NoExtractor:
    """Minimal signal: token estimate + tool-use flag. No embeddings."""

    def extract(self, req: ChatCompletionRequest) -> Signal:
        has_tools = bool(req.model_dump(exclude_none=True).get("tools"))
        return Signal(token_estimate=estimate_prompt_tokens(req.messages), has_tools=has_tools)


class AllowGuard:
    def check(self, req: ChatCompletionRequest, signal: Signal) -> GuardVerdict:
        return GuardVerdict()


class CatalogRouter:
    """Phase-0 behaviour: answer with exactly the requested model."""

    def decide(self, req, signal, verdict, catalog, policy=None) -> Decision:
        return Decision(model_id=req.model, reason="catalog")


class NoCache:
    def get(self, req: ChatCompletionRequest) -> ChatCompletionResponse | None:
        return None

    def put(self, req: ChatCompletionRequest, resp: ChatCompletionResponse) -> None:
        pass
