"""Experience-kNN model proposer (P1 of the embedding-routing plan) — dark by default.

Given a new task's text, find the most similar PAST dispatched tasks (cosine over their stored
embeddings), weight them by how they went (feedback verdict, outcome, cost), and propose the
model that similar tasks succeeded with. This overrides the benchmark prior for model choice
WITHIN the lane the classifier already decided — it never moves the lane, so privacy/guard stay
supreme (the driver also skips this proposer entirely for privacy-pinned tasks). Yields to user
pins. See _dispatch_one in core.py for the precedence wiring.

Corpus reality: today's task_embeddings table is tiny. This stays behind TOTO_GW_EXPERIENCE_KNN
until ~200 labeled tasks exist; below the neighbor threshold it returns None and the benchmark
prior is used. Brute-force cosine over rows held in memory (thousands max), reloaded periodically.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from ..embeddings import _cos


@dataclass
class Proposal:
    model_id: str
    reason: str   # neighbor evidence, appended to the route_reason trace


class ExperienceKNN:
    def __init__(self, store, embedder, catalog, *, k: int = 3, sim_threshold: float = 0.75,
                 refresh_seconds: int = 300, max_rows: int = 5000, cost_coeff: float = 0.0) -> None:
        self._store = store
        self._embedder = embedder         # reused for embed_one (cache-shared with skill inference)
        self._catalog = catalog
        self._k = k
        self._sim = sim_threshold
        self._refresh = refresh_seconds
        self._max_rows = max_rows
        self._cost_coeff = cost_coeff
        self._rows: list[dict] = []        # parsed corpus: {vector, model_id, lane, verdict, outcome, cost}
        self._loaded_at = 0.0

    async def _ensure_rows(self) -> None:
        """Reload + parse the corpus at most every refresh_seconds. Rows whose model left the
        catalog are dropped (can't dispatch to them); vectors parsed once here, not per query."""
        now = time.monotonic()
        if self._rows and now - self._loaded_at < self._refresh:
            return
        raw = await self._store.experience_rows(self._max_rows)
        parsed = []
        for r in raw:
            entry = self._catalog.get(r["model_id"])
            if entry is None:
                continue
            parsed.append({
                "vector": json.loads(r["vector"]),
                "model_id": r["model_id"],
                "lane": entry.lane,
                "verdict": r.get("verdict"),
                "outcome": r.get("outcome") or "",
                "cost": r.get("cost_usd") or 0.0,
            })
        self._rows = parsed
        self._loaded_at = now

    def _quality(self, r: dict) -> float:
        """How much a neighbor should count FOR its model: a thumbs-up amplifies, a thumbs-down
        or a failed outcome suppresses (can push the contribution negative), cost nudges down."""
        q = 1.0
        if r["verdict"] == "up":
            q += 1.0
        elif r["verdict"] == "down":
            q -= 1.5
        if r["outcome"] == "failed":
            q -= 1.0
        q -= self._cost_coeff * r["cost"]
        return q

    async def propose(self, text: str, lane: str) -> Proposal | None:
        """A model proposal for a task routed to `lane`, or None to defer to the benchmark prior.
        None on: no embedder/vector, empty corpus, fewer than k neighbors above threshold, no
        in-lane neighbor, or no model with net-positive weighted support."""
        if self._embedder is None:
            return None
        await self._ensure_rows()
        if not self._rows:
            return None
        vec = await self._embedder.embed_one(text)
        if vec is None:
            return None
        neighbors = [(sim, r) for r in self._rows if (sim := _cos(vec, r["vector"])) >= self._sim]
        if len(neighbors) < self._k:
            return None  # sparse → benchmark prior stays
        scores: dict[str, float] = {}
        votes: dict[str, list[int]] = {}   # model -> [up, down, n]
        for sim, r in neighbors:
            if r["lane"] != lane:          # kNN chooses WITHIN the decided lane, never across it
                continue
            m = r["model_id"]
            scores[m] = scores.get(m, 0.0) + sim * self._quality(r)
            v = votes.setdefault(m, [0, 0, 0])
            v[0] += r["verdict"] == "up"
            v[1] += r["verdict"] == "down"
            v[2] += 1
        if not scores:
            return None
        best = max(scores, key=scores.get)
        if scores[best] <= 0:              # only propose on net-positive experience
            return None
        up, down, n = votes[best]
        return Proposal(best, f"knn: {n} similar tasks favored {best} ({up} up, {down} down)")


def build_experience_knn(settings, store, embedder, catalog):
    """An ExperienceKNN when the flag is on AND an embedder exists (kNN needs embeddings), else
    None → the driver's kNN seam is skipped entirely (flag-off is byte-identical)."""
    if not settings.experience_knn or embedder is None or store is None:
        return None
    return ExperienceKNN(store, embedder, catalog, k=settings.knn_k, sim_threshold=settings.knn_sim,
                         refresh_seconds=settings.knn_refresh_seconds, max_rows=settings.knn_max_rows,
                         cost_coeff=settings.knn_cost_coeff)
