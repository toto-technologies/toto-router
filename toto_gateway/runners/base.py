"""The Runner contract (context doc §7.3).

A box is a valid runner if it speaks this contract. Mac Studio (MLX) and a frontier API
become two *reference configurations of one role*, not two codebases. In Phase 0 two methods
are REAL (`chat`, `stream`, `models`) and the appliance-management surface (`cartridge_manifest`,
`load`, `unload`, `health`) is STUBBED with committed shapes — Phase 2/3 fills the bodies in.
This mirrors the locked decision: one runner real, the rest ship as Protocol stubs.

Streaming + usage accounting contract:
- `chat()` returns a complete ChatCompletionResponse whose `.usage` is populated when the
  upstream reports it.
- `stream()` yields ChatCompletionChunk objects. Whenever the upstream reports usage, the
  runner SHOULD ALWAYS emit a trailing usage chunk (empty `choices`, populated `usage`) —
  regardless of the client's `stream_options.include_usage`. The gateway consumes that chunk
  for exact accounting and forwards it to the client only if `include_usage` was requested.
  If the upstream reports nothing, the gateway estimates from the streamed text and flags
  `cost_estimated=True` so the cost metric never silently lies (Gary fold G2).
"""

from __future__ import annotations

from typing import AsyncIterator, Protocol, runtime_checkable

from pydantic import BaseModel

from ..catalog import CatalogEntry
from ..schemas import ChatCompletionChunk, ChatCompletionRequest, ChatCompletionResponse, Model


def auto_cache_prefs(req: ChatCompletionRequest, settings) -> tuple[bool, int]:
    """(auto_inject, min_messages) for cache auto-injection. A per-request `cache_prefs` (the
    gateway stamps it from the caller's org/team cache policy, A8) wins over the global env
    default; a direct caller (tests, no gateway) has no cache_prefs → the settings default,
    byte-identical to pre-A8. Both runner paths (frontier auto-cache, openrouter inject) share this
    so the org override reaches both."""
    prefs = getattr(req, "cache_prefs", None) or {}
    auto = prefs.get("auto_inject")
    mm = prefs.get("auto_inject_min_messages")
    return (settings.anthropic_auto_cache if auto is None else bool(auto),
            settings.anthropic_auto_cache_min_messages if mm is None else int(mm))


class CartridgeManifest(BaseModel):
    """What cartridges (LoRA adapters / KV-cache contexts) a runner has loaded. Stub in Phase 0."""

    base_model: str
    base_quant: str | None = None
    adapters: list[str] = []  # capability cartridges (Phase 2)
    contexts: list[str] = []  # context cartridges (Phase 3)


class Telemetry(BaseModel):
    """Health/utilization signal feeding contention-aware routing (Phase 3). Stub in Phase 0."""

    healthy: bool = True
    utilization: float = 0.0
    queue_depth: int = 0
    memory_headroom_mb: int | None = None
    hot_cartridges: list[str] = []


class NotImplementedInPhase0(NotImplementedError):
    """Raised by contract methods that are deliberately stubbed until a later phase."""


@runtime_checkable
class Runner(Protocol):
    """The interface every lane implements. See module docstring for the streaming contract."""

    runner_id: str

    async def chat(
        self, req: ChatCompletionRequest, entry: CatalogEntry
    ) -> ChatCompletionResponse:
        """Non-streaming completion. `.usage` populated if the upstream reports it."""
        ...

    def stream(
        self, req: ChatCompletionRequest, entry: CatalogEntry
    ) -> AsyncIterator[ChatCompletionChunk]:
        """Streaming completion. Yields chunks; SHOULD emit a trailing usage chunk if known."""

    def models(self) -> list[Model]:
        """OpenAI-compatible model cards this runner serves."""

    # --- Appliance-management surface: stubbed in Phase 0, real in Phase 2/3 -------------

    def cartridge_manifest(self) -> CartridgeManifest:
        ...

    async def load(self, cartridge_ref: str) -> None:
        ...

    async def unload(self, cartridge_ref: str) -> None:
        ...

    def health(self) -> Telemetry:
        ...
