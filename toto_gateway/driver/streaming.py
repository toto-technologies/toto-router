"""Streamed-answer machinery — delta batching + the mid-stream tool-call guard.

`stream_run` is one streamed attempt against the driver's wired `stream_fn`. The driver's
`_answer`/`_answer_gated` call it through `_call`, so a retry or fallback restarts the
answer with a fresh buffer.
"""

from __future__ import annotations

import inspect
import re
import time
from typing import TYPE_CHECKING

from ..schemas import ChatCompletionRequest
from .contracts import Exec

if TYPE_CHECKING:
    from .core import Driver

# A JSON tool-call object opening. In a gated companion stream, a committed plain answer that
# grows a trailing `{"tool" …}` is the model narrating then appending the call it should have
# emitted alone — freeze at the brace so the raw JSON never streams (or gets spoken). Whitespace
# after `{` tolerated; a real answer never contains this literal.
_TOOL_OBJ_RE = re.compile(r'\{\s*"tool"')


async def stream_run(driver: "Driver", req: ChatCompletionRequest, node: str, gate=None) -> Exec:
    """One streamed attempt with a FRESH batch buffer, so a retry/fallback restarts cleanly.
    Stale deltas from a failed attempt stay in the event log but are superseded by the
    terminal snapshot — the client swaps to authoritative text at run_done.

    gate (companion agent only): a callable(prelude)->True/False/None that inspects the leading
    text before anything is published — True to start streaming (plain answer), False to
    suppress the whole reply (it's a tool call, parsed by the caller from the returned Exec),
    None to keep buffering while ambiguous. Absent (driver answer nodes, which already know
    the reply is an answer) → emit from the first delta as before."""
    buf: list[str] = []
    whole: list[str] = []   # full stream so far, for the mid-stream tool-call guard
    last = [time.monotonic()]
    emit = [gate is None]   # may we publish yet? (True immediately when there's no gate)
    suppress = [False]      # gate ruled it a tool call → publish nothing, ever
    frozen = [False]        # committed answer grew a trailing {"tool" object → stop emitting
    seen_brace = [False]    # cheap gate: only scan for the tool object once a '{' appears

    async def flush() -> None:
        if not emit[0] or suppress[0]:
            return
        if buf:
            r = driver._emit_delta(node, "".join(buf))  # async publish (prod) or sync (tests)
            if inspect.isawaitable(r):
                await r
            buf.clear()
            last[0] = time.monotonic()

    async def on_delta(chunk: str) -> None:
        if suppress[0] or frozen[0]:
            return
        whole.append(chunk)
        buf.append(chunk)
        if not emit[0]:
            verdict = gate("".join(buf))
            if verdict is False:
                suppress[0] = True
                buf.clear()
                return
            if verdict is not True:
                return       # still ambiguous — hold the buffer, emit nothing
            emit[0] = True   # decided: plain answer — the held buffer flushes below
        # Mid-stream tool-call guard (gated companion path only): a committed answer that
        # sprouts a trailing `{"tool" …}` object — the model narrated then appended the call.
        # Emit only the clean prose up to the brace, then freeze so the JSON never streams.
        if gate is not None and ("{" in chunk or seen_brace[0]):
            seen_brace[0] = True
            s = "".join(whole)
            m = _TOOL_OBJ_RE.search(s)
            if m is not None:
                already = len(s) - sum(len(x) for x in buf)  # chars already published
                buf[:] = [s[already:m.start()]] if already < m.start() else []
                await flush()
                frozen[0] = True
                return
        if sum(len(x) for x in buf) >= driver._delta_chars or \
                time.monotonic() - last[0] >= driver._delta_secs:
            await flush()

    ex = await driver._stream(req, on_delta)
    await flush()  # emit the tail
    return ex
