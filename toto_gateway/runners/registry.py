"""Runner registry — maps a catalog entry to a live Runner, lazily and per-lane.

Lane modules are imported lazily so (a) the package imports even before every adapter exists,
and (b) requesting a lane you haven't configured fails only for that lane. The factory is
injectable so tests can swap in fakes without touching real upstreams.

Runner constructor contract: every Runner is built as `Runner(entry: CatalogEntry)` and exposes
a `runner_id: str` attribute plus the methods in `base.Runner`.
"""

from __future__ import annotations

from typing import Callable

from ..catalog import CatalogEntry
from .base import Runner

RunnerFactory = Callable[[CatalogEntry], Runner]


def default_factory(entry: CatalogEntry) -> Runner:
    if entry.lane == "fake":
        from .fake import FakeRunner

        return FakeRunner(entry)
    # OpenAI-compatible provider (OpenAI, OpenRouter, Together, …) — any lane. base_url +
    # api_key_env on the entry select the host, so a local *tier* can be served by a cloud
    # provider when there's no on-prem box.
    if entry.endpoint == "openai":
        from .openai import OpenAIRunner

        return OpenAIRunner(entry)
    if entry.lane == "economy":
        from .mlx import MLXRunner  # OpenAI-compatible upstream at a bare URL (mlx_lm.server, …)

        return MLXRunner(entry)
    if entry.lane == "frontier":
        from .frontier import FrontierRunner  # native Anthropic

        return FrontierRunner(entry)
    raise ValueError(f"no runner for lane '{entry.lane}' (model {entry.id})")


class RunnerRegistry:
    def __init__(self, factory: RunnerFactory = default_factory) -> None:
        self._factory = factory
        self._cache: dict[tuple[str, str | None, str | None, str, str], Runner] = {}

    def for_entry(self, entry: CatalogEntry) -> Runner:
        key = (
            entry.id,
            entry.provider,
            entry.base_url,
            entry.effective_upstream_model,
            entry.api_key_env,
        )
        runner = self._cache.get(key)
        if runner is None:
            runner = self._factory(entry)
            self._cache[key] = runner
        return runner

    def clear(self) -> None:
        self._cache.clear()
