"""In-process load microbench (Chunk H3).

~1 screen of asyncio + httpx.ASGITransport. Fires N requests at the in-process app at a bounded
concurrency, records per-request wall-clock, computes p50/p95/p99 + error rate. NOT k6, NOT
locust — no dep, no server, runs in CI in seconds. Providers are faked so we measure OUR
overhead, not the LLM. k6 stays for the staging design-point.

Run the percentile self-check standalone:  python -m tests.harness.loaddriver
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from math import ceil


@dataclass
class Stats:
    n: int
    errors: int
    p50: float
    p95: float
    p99: float
    mean: float
    max: float

    @property
    def error_rate(self) -> float:
        return self.errors / self.n if self.n else 0.0

    def table(self, label: str = "") -> str:
        return (f"{label:<20} n={self.n:<5} err={self.error_rate:6.2%}  "
                f"p50={self.p50:6.1f}ms  p95={self.p95:6.1f}ms  p99={self.p99:6.1f}ms  "
                f"max={self.max:6.1f}ms")


def _pct(sorted_ms: list[float], q: float) -> float:
    """Nearest-rank percentile (q in 0..100). Empty → 0.0."""
    if not sorted_ms:
        return 0.0
    k = ceil(q / 100.0 * len(sorted_ms)) - 1
    return sorted_ms[max(0, min(len(sorted_ms) - 1, k))]


def stats_from(latencies_ms: list[float], errors: int) -> Stats:
    s = sorted(latencies_ms)
    return Stats(n=len(s), errors=errors, p50=_pct(s, 50), p95=_pct(s, 95), p99=_pct(s, 99),
                 mean=(sum(s) / len(s)) if s else 0.0, max=(s[-1] if s else 0.0))


async def bench(client, path: str, n: int, concurrency: int, *,
                method: str = "GET", json=None, ok=(200, 202)) -> Stats:
    """Fire n `method path` requests through `client` at most `concurrency` in flight. A response
    whose status is not in `ok` (or an exception) counts as an error. Returns Stats."""
    sem = asyncio.Semaphore(concurrency)
    latencies: list[float] = []
    errors = 0

    async def one() -> None:
        nonlocal errors
        async with sem:
            t0 = time.perf_counter()
            try:
                r = await client.request(method, path, json=json)
                if r.status_code not in ok:
                    errors += 1
            except Exception:
                errors += 1
            latencies.append((time.perf_counter() - t0) * 1000.0)

    await asyncio.gather(*[one() for _ in range(n)])
    return stats_from(latencies, errors)


def demo() -> None:
    """Self-check: the percentile math must be right or the SLO gate is a lie. ponytail: one
    runnable assert set, no framework."""
    s = stats_from([float(i) for i in range(1, 101)], errors=3)
    assert s.p50 == 50.0, s.p50          # nearest-rank of 1..100
    assert s.p95 == 95.0, s.p95
    assert s.p99 == 99.0, s.p99
    assert s.max == 100.0
    assert abs(s.error_rate - 0.03) < 1e-9
    assert _pct([], 95) == 0.0           # empty is defined, not a crash
    assert _pct([42.0], 99) == 42.0      # single sample
    print("loaddriver self-check OK:", s.table("demo"))


# --- Soak / leak variant (Chunk H4) -----------------------------------------------------------
# A slow asyncio-task or connection leak surfaces as a 3am OOM, not a red test. The soak loop runs
# a step M times and samples two cheap growth signals — live asyncio Task count and RSS — after a
# warmup baseline. Healthy code plateaus (samples flat, tiny end-vs-baseline growth); a leak (a
# never-cancelled task, a client whose aclose is skipped) grows one or both monotonically → red.
# ponytail: stdlib resource.getrusage for RSS + len(asyncio.all_tasks()) — NO new dep (no psutil).


def _live_tasks() -> int:
    return len([t for t in asyncio.all_tasks() if not t.done()])


def _rss() -> int:
    """Peak RSS (getrusage ru_maxrss; macOS bytes / Linux KB). Self-compared, so units are moot.
    Peak is monotonic-non-decreasing, so we baseline it AFTER warmup and gate on bounded GROWTH."""
    import resource

    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


@dataclass
class SoakResult:
    iters: int
    base_tasks: int
    end_tasks: int
    max_tasks: int
    base_rss: int
    end_rss: int
    samples: list  # (i, tasks, rss)

    @property
    def task_growth(self) -> int:
        return self.max_tasks - self.base_tasks

    @property
    def rss_growth(self) -> float:
        return (self.end_rss - self.base_rss) / self.base_rss if self.base_rss else 0.0

    @property
    def tasks_monotonic(self) -> bool:
        """A leak signature: live-task count strictly increases at EVERY sample."""
        t = [s[1] for s in self.samples]
        return len(t) >= 2 and all(b > a for a, b in zip(t, t[1:]))

    def bounded(self, *, max_task_growth: int, max_rss_growth: float) -> bool:
        return (self.task_growth <= max_task_growth and self.rss_growth <= max_rss_growth
                and not self.tasks_monotonic)

    def table(self) -> str:
        return (f"soak iters={self.iters} tasks {self.base_tasks}→{self.end_tasks} "
                f"(max {self.max_tasks}, +{self.task_growth})  rss +{self.rss_growth:.1%}")


async def soak(step, m: int, *, warmup: int = 50, sample_every: int = 50) -> SoakResult:
    """Run `await step()` m times; sample live task count + RSS every `sample_every`. Baselines are
    captured AFTER `warmup` iters so first-touch pool/cache allocation isn't miscounted as a leak."""
    import gc

    for _ in range(warmup):
        await step()
    gc.collect()
    base_tasks, base_rss = _live_tasks(), _rss()
    max_tasks = base_tasks
    samples: list = []
    for i in range(1, m + 1):
        await step()
        if i % sample_every == 0:
            gc.collect()
            t = _live_tasks()
            max_tasks = max(max_tasks, t)
            samples.append((i, t, _rss()))
    gc.collect()
    return SoakResult(iters=m, base_tasks=base_tasks, end_tasks=_live_tasks(), max_tasks=max_tasks,
                      base_rss=base_rss, end_rss=_rss(), samples=samples)


if __name__ == "__main__":
    demo()
