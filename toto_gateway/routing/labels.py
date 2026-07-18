"""Label -> model bindings: the NVIDIA-style routing table.

A plain YAML load + a dict + a loop (policy.py house style — no rules engine). The YAML's
label keys ARE the classifier vocabulary: the prompt enumerates them and the parser accepts
only them, so vocabulary changes are data edits, never code.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ..catalog import Catalog

_DEFAULT_PATH = Path(__file__).parent / "labels.yaml"


class LabelBindings:
    """Load and query the label -> catalog-entry bindings.

    Shape (from YAML): labels: {<label>: {model: <catalog id> | null, desc: <prompt line>}}.
    A null model means the label exists (the classifier may emit it) but routing falls back
    to classify() — the ladder NVIDIA's blueprint doesn't have.
    """

    def __init__(self, path: str | Path | None = None, *, _raw: dict[str, Any] | None = None) -> None:
        if _raw is not None:
            data = _raw
        else:
            p = Path(path) if path else _DEFAULT_PATH
            data = yaml.safe_load(p.read_text()) or {}
        self.labels: dict[str, dict] = data.get("labels", {}) or {}

    def vocab(self) -> list[str]:
        """The closed label set, sorted — the prompt's and parser's single source."""
        return sorted(self.labels)

    def model_for(self, label: str) -> str | None:
        """Bound catalog id for a label; None for unbound or unknown labels (-> fallback)."""
        row = self.labels.get(label) or {}
        return row.get("model") or None

    def category_for(self, label: str) -> str | None:
        """Benchmark category an UNBOUND label routes best-in on (B3); None if none declared
        (other/redact) -> the generic fallback."""
        return (self.labels.get(label) or {}).get("category") or None

    def validate(self, catalog: Catalog) -> list[str]:
        """Misconfiguration descriptions (empty = clean). Bad bindings must refuse to boot:
        an unknown id or a fake-lane binding is a wrong file, not a runtime condition."""
        found: list[str] = []
        for label, row in self.labels.items():
            model = (row or {}).get("model")
            if model is None:
                continue
            entry = catalog.get(model)
            if entry is None:
                found.append(f"label {label!r} binds unknown catalog id {model!r}")
            elif entry.endpoint == "fake":
                found.append(f"label {label!r} binds fake-lane entry {model!r}")
        return found
