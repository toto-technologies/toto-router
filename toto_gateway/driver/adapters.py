"""HarnessAdapter — the executor dispatch seam.

The classifier decides WHICH model runs a task (`driver/classify.py`); the adapter decides
HOW it runs:
  - `GatewayAdapter`  — a raw OpenAI-compatible call via the passthrough gateway (any catalog
                        model / provider). The default catch-all.
  - `ClaudeCodeAdapter` — a headless Claude Code run (`claude -p … --output-format json`) on
                        this box, on the customer's own claude auth. LIVE behind
                        TOTO_GW_SUBAGENT_RUNNERS.
  - `PiAdapter`       — a headless pi coding-agent run (`pi -p`), pointed BACK at this gateway
                        as its provider so every inner completion is routed/traced/guarded by
                        us. LIVE behind TOTO_GW_SUBAGENT_RUNNERS.

One Protocol, many backends, selected per task. A task can PIN a harness via
`metadata.requires.runner` ("pi" / "claude_code"); otherwise the GatewayAdapter runs it.
The pin only survives parsing when TOTO_GW_SUBAGENT_RUNNERS is on (prompts._clean_task
allowlist), and the subagent adapters are only registered then (with_subagents) — flag off is
byte-identical to the pre-runner gateway-only registry.

A pinned task FAILS honestly when its runner can't run (binary missing, timeout, crash) — it
never silently downgrades to a one-shot gateway completion; a pinned task wants an agent.
SubagentError is deliberately non-retryable (resilience.is_retryable → False): re-running a
dead binary or a timed-out agent multiplies the damage, and _call's model-fallback can't help
a harness failure anyway.

Selection rule: first adapter whose `handles()` returns True wins, so the catch-all
GatewayAdapter must be registered LAST.

ponytail: the subagent executes INSIDE the dispatched task — its output IS the task result,
no nested session rows. Nested sessions (live subagent progress in the UI) = S3+ if wanted.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Protocol, runtime_checkable

from ..schemas import ChatCompletionRequest

if TYPE_CHECKING:
    from .core import Exec

# Executes one request and returns its normalized result. The GatewayAdapter wraps the
# gateway's complete(); other adapters build their own.
CompleteFn = Callable[[ChatCompletionRequest], "Awaitable[Exec]"]

# Keep at most this many chars of a subagent's stdout/stderr — plenty for any real answer,
# bounded against a runaway agent flooding the pipe into driver memory.
_OUTPUT_CAP = 65536


class SubagentError(RuntimeError):
    """A subagent harness failure (missing binary, wall-clock timeout, crash, unparseable
    output). Plain RuntimeError → resilience.is_retryable says NO, so Driver._call raises
    immediately instead of retrying/falling back — the per-task failure narration takes over."""


@runtime_checkable
class HarnessAdapter(Protocol):
    name: str

    def handles(self, model_id: str, metadata: dict) -> bool:
        """True if this adapter should run the task (model + its metadata)."""
        ...

    async def run(self, req: ChatCompletionRequest) -> "Exec":
        """Execute the request and return an Exec (with `.adapter` set to this adapter's name)."""
        ...


def _pinned_runner(metadata: dict) -> str | None:
    return (metadata.get("requires") or {}).get("runner")


def _resolve_bin(bin_path: str, runner: str) -> str:
    """The pinned runner's executable, or a clear non-retryable failure — NEVER a silent
    downgrade to the gateway (a pinned task wants an agent, not a one-shot pretending)."""
    found = shutil.which(bin_path)
    if not found:
        raise SubagentError(
            f"runner {runner!r} is pinned but its binary {bin_path!r} was not found — install it "
            f"on this box or turn off TOTO_GW_SUBAGENT_RUNNERS")
    return found


def _prompt_parts(req: ChatCompletionRequest) -> tuple[str, str]:
    """(system_text, task_text) from the executor request. The task text becomes the subagent's
    prompt via STDIN; the system text (EXECUTOR_PROMPT) rides --append-system-prompt on both
    CLIs (our own trusted prompt — never model-authored task text)."""
    system = "\n\n".join(m.text() for m in req.messages if m.role == "system").strip()
    task = "\n\n".join(m.text() for m in req.messages if m.role != "system").strip()
    return system, task


def _min_env(extra: dict | None = None) -> dict:
    """Minimal subprocess env: just what a node/CLI runner needs (PATH to find node, HOME for
    its own auth/config), never the gateway's full env (provider keys stay home)."""
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "TMPDIR", "LANG", "TERM")}
    env.update(extra or {})
    return env


async def _run_subagent(argv: list[str], *, env: dict, cwd: str, timeout: float,
                        stdin_data: str | None = None) -> tuple[str, str, int]:
    """One subagent subprocess: exec (never shell), its own process group (so killing it reaps
    its children too), a hard wall-clock budget, stdout/stderr captured and truncated.

    The task text rides STDIN (`stdin_data`), never argv: a model-authored prompt that looks
    like a flag ("--dangerously-skip-permissions") must never be parsed as one, and pi's
    parser has NO "--" end-of-options terminator (a bare "--" is an unknown flag that swallows
    the NEXT token — verified against the installed dist). Both CLIs read a piped stdin as the
    prompt in print mode (verified live 2026-07-12).

    Kill + reap on EVERY exit path — timeout, cancellation (client disconnect / run cancel),
    anything: the group is detached (start_new_session), so an unkilled child would survive as
    an orphan (on the claude path: a live process egressing on the customer's own auth)."""
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        stdin=(asyncio.subprocess.PIPE if stdin_data is not None else asyncio.subprocess.DEVNULL),
        env=env, cwd=cwd,
        start_new_session=True)  # own pgid → killpg below also reaps grandchildren
    try:
        out_b, err_b = await asyncio.wait_for(
            proc.communicate(stdin_data.encode() if stdin_data is not None else None),
            timeout=timeout)
    except BaseException as exc:  # incl. CancelledError — the group dies with us, no exceptions
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass  # already gone
        await proc.wait()  # reap — no zombie
        if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
            raise SubagentError(
                f"subagent {argv[0]!r} exceeded TOTO_GW_SUBAGENT_TIMEOUT ({timeout:g}s) — "
                f"killed (process group)") from None
        raise
    return (out_b.decode("utf-8", "replace")[:_OUTPUT_CAP],
            err_b.decode("utf-8", "replace")[:_OUTPUT_CAP],
            proc.returncode)


class GatewayAdapter:
    """Default catch-all — run the task on the passthrough gateway (raw OpenAI-compatible API,
    any catalog model / OpenRouter / etc.)."""

    name = "gateway"

    def __init__(self, complete_fn: CompleteFn) -> None:
        self._complete = complete_fn

    def handles(self, model_id: str, metadata: dict) -> bool:
        return True  # catch-all — MUST be registered last

    async def run(self, req: ChatCompletionRequest) -> "Exec":
        ex = await self._complete(req)
        ex.adapter = self.name
        return ex


class ClaudeCodeAdapter:
    """Headless Claude Code: `claude -p <task> --output-format json` on this box, on the
    customer's own claude auth (the gateway exposes no Anthropic-native endpoint). The JSON
    result carries the answer + real cost/usage, mapped into the Exec so provenance stays
    honest. Selected when a task declares `metadata.requires.runner == "claude_code"`.

    RESIDENCY: this runner egresses to Anthropic directly — the driver refuses it pre-spawn
    for any in-perimeter-pinned task (core._dispatch_one), because the adapter alone can't
    know the decision."""

    name = "claude_code"

    def __init__(self, *, timeout: float = 300, claude_bin: str = "claude") -> None:
        self._timeout = timeout
        self._bin = claude_bin

    def handles(self, model_id: str, metadata: dict) -> bool:
        return _pinned_runner(metadata) == "claude_code"

    async def run(self, req: ChatCompletionRequest) -> "Exec":
        from .core import Exec  # runtime import — core imports this module

        binp = _resolve_bin(self._bin, self.name)
        system, task = _prompt_parts(req)
        # Task text via STDIN, never argv (see _run_subagent) — a task that IS a flag token
        # must reach claude as prompt bytes, not be parsed on the customer-auth CLI. The
        # system text is our own EXECUTOR_PROMPT (trusted), safe as an option value.
        argv = [binp, "-p", "--output-format", "json"]
        if system:
            argv += ["--append-system-prompt", system]
        t0 = time.monotonic()
        # cwd = a per-spawn temp dir: the agent works on a scratch floor, never inside the
        # gateway's own checkout.
        with tempfile.TemporaryDirectory(prefix="toto-claude-") as tmp:
            out, err, rc = await _run_subagent(argv, env=_min_env(), cwd=tmp,
                                               timeout=self._timeout, stdin_data=task)
        if rc != 0:
            raise SubagentError(f"claude exited {rc}: {(err or out)[-500:]}")
        try:
            data = json.loads(out)
        except ValueError as e:
            raise SubagentError(f"claude emitted unparseable JSON: {e}") from None
        # Documented headless result shape (verified live 2026-07-12): {"type":"result",
        # "subtype":"success","is_error":false,"result":"…","total_cost_usd":…,"duration_ms":…,
        # "usage":{"input_tokens":…,"output_tokens":…,"cache_read_input_tokens":…}}
        if data.get("is_error") or data.get("subtype") != "success":
            raise SubagentError(
                f"claude run failed: subtype={data.get('subtype')!r} "
                f"{str(data.get('result') or '')[:500]}")
        usage = data.get("usage") or {}
        return Exec(
            text=str(data.get("result") or ""), model=req.model, adapter=self.name,
            tokens_prompt=int(usage.get("input_tokens") or 0),
            tokens_completion=int(usage.get("output_tokens") or 0),
            tokens_cached=int(usage.get("cache_read_input_tokens") or 0),
            cost_usd=data.get("total_cost_usd"),
            latency_ms=int(data.get("duration_ms") or (time.monotonic() - t0) * 1000))


class PiAdapter:
    """Headless pi coding agent (`pi -p`), pointed BACK at this gateway as its provider: a
    per-spawn PI_CODING_AGENT_DIR carries a generated models.json whose one provider is the
    gateway's /v1 with the model the ladder picked — so every inner completion the agent makes
    is routed, traced, and guarded by us (the user's ~/.pi is never touched). Selected when a
    task declares `metadata.requires.runner == "pi"`."""

    name = "pi"

    def __init__(self, *, gateway_base_url: str = "http://127.0.0.1:8080/v1",
                 api_key: str = "", timeout: float = 300, pi_bin: str = "pi") -> None:
        self._base_url = gateway_base_url
        # What pi presents back to the gateway: the operator token when auth is on, else a
        # dummy (the open gateway accepts any string). Lives only in the per-spawn temp dir.
        self._api_key = api_key or "toto-subagent"
        self._timeout = timeout
        self._bin = pi_bin

    def handles(self, model_id: str, metadata: dict) -> bool:
        return _pinned_runner(metadata) == "pi"

    def _models_json(self, model_id: str) -> str:
        # pi's custom-provider file (docs/harness-wiring.md §Pi, verified against the installed
        # pi's model-registry schema): api "openai-completions" = any OpenAI-compatible upstream.
        return json.dumps({"providers": {"toto-gateway": {
            "baseUrl": self._base_url, "apiKey": self._api_key, "api": "openai-completions",
            "models": [{"id": model_id, "name": f"{model_id} (via toto-gateway)",
                        "reasoning": False, "input": ["text"],
                        "contextWindow": 128000, "maxTokens": 8192,
                        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}],
        }}})

    async def run(self, req: ChatCompletionRequest) -> "Exec":
        from .core import Exec  # runtime import — core imports this module

        binp = _resolve_bin(self._bin, self.name)
        system, task = _prompt_parts(req)
        t0 = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="toto-pi-") as tmp:
            agent_dir = Path(tmp, "agent")  # pi's config home (models.json, NOT the user's ~/.pi)
            work = Path(tmp, "work")        # the agent's scratch floor, never the gateway repo
            agent_dir.mkdir()
            work.mkdir()
            (agent_dir / "models.json").write_text(self._models_json(req.model))
            # -p = non-interactive print mode; --no-session/-extensions/-skills/… strip every
            # user-profile surface so the run is hermetic and reproducible. Task text via
            # STDIN, never argv (see _run_subagent): pi's parser has no "--" terminator, and
            # a task starting with "--"/"-"/"@" would otherwise be read as flag/file, not prompt.
            argv = [binp, "-p", "--no-session", "--no-extensions", "--no-skills",
                    "--no-prompt-templates", "--no-context-files",
                    "--provider", "toto-gateway", "--model", req.model]
            if system:
                argv += ["--append-system-prompt", system]
            out, err, rc = await _run_subagent(
                argv, env=_min_env({"PI_CODING_AGENT_DIR": str(agent_dir)}),
                cwd=str(work), timeout=self._timeout, stdin_data=task)
        if rc != 0:
            raise SubagentError(f"pi exited {rc}: {(err or out)[-500:]}")
        # cost/tokens deliberately unset: pi's inner completions came back THROUGH this gateway,
        # so they're already accounted on the passthrough traces — stamping them here too would
        # double-count.
        return Exec(text=out.strip(), model=req.model, adapter=self.name,
                    latency_ms=int((time.monotonic() - t0) * 1000))


class AdapterRegistry:
    """Selects the adapter for a task: first whose `handles()` returns True. The GatewayAdapter
    catch-all must be last. `default_gateway()` is the flag-off registry; `with_subagents()`
    the flag-on one."""

    def __init__(self, adapters: list[HarnessAdapter]) -> None:
        if not adapters:
            raise ValueError("AdapterRegistry needs at least one adapter")
        self._adapters = adapters

    @classmethod
    def default_gateway(cls, complete_fn: CompleteFn) -> "AdapterRegistry":
        """The flag-OFF default: gateway only. With TOTO_GW_SUBAGENT_RUNNERS off,
        `requires.runner` never survives parsing (prompts._clean_task), so the subagent
        adapters simply aren't registered — byte-identical pre-runner behavior."""
        return cls([GatewayAdapter(complete_fn)])

    @classmethod
    def with_subagents(cls, complete_fn: CompleteFn, *, gateway_base_url: str,
                       gateway_api_key: str = "", timeout: float = 300,
                       pi_bin: str = "pi", claude_bin: str = "claude") -> "AdapterRegistry":
        """The flag-ON registry (TOTO_GW_SUBAGENT_RUNNERS): live pi + claude_code subagents
        before the gateway catch-all."""
        return cls([
            PiAdapter(gateway_base_url=gateway_base_url, api_key=gateway_api_key,
                      timeout=timeout, pi_bin=pi_bin),
            ClaudeCodeAdapter(timeout=timeout, claude_bin=claude_bin),
            GatewayAdapter(complete_fn),
        ])

    def select(self, model_id: str, metadata: dict) -> HarnessAdapter:
        for adapter in self._adapters:
            if adapter.handles(model_id, metadata or {}):
                return adapter
        raise ValueError(f"no harness adapter handles model {model_id!r}")

    async def run(self, req: ChatCompletionRequest, metadata: dict) -> "Exec":
        return await self.select(req.model, metadata or {}).run(req)

    @property
    def names(self) -> list[str]:
        return [a.name for a in self._adapters]
