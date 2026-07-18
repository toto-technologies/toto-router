"""Runner adapters: one role, many box classes (context doc §7.3)."""

from .base import CartridgeManifest, NotImplementedInPhase0, Runner, Telemetry
from .fake import FakeRunner
from .registry import RunnerRegistry, default_factory

__all__ = [
    "Runner",
    "CartridgeManifest",
    "Telemetry",
    "NotImplementedInPhase0",
    "FakeRunner",
    "RunnerRegistry",
    "default_factory",
]
