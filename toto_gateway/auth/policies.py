"""Per-scope policy overlays: catalog RBAC, routing overlay, budgets, catalog adoptions, and
price overrides. Absence of a row always means "no overlay" — byte-identical global behavior."""

from __future__ import annotations

import json
import time

from .vocab import CATALOG_MODES, ROUTING_OPTIMIZE


class PoliciesMixin:
    # --- catalog-scoped RBAC ---------------------------------------------------
    # Per-team allow/deny overlay over catalog ids. Keyed by team_id; org_id carried for the
    # admin API's org-isolation check. Absence of a row = permissive (effective_policy returns
    # None → the router keeps its global policy, ZERO behavior change).

    async def get_catalog_policy(self, team_id: str) -> dict | None:
        """The team's catalog policy as a dict (models/residency parsed from JSON), or None when
        the team has no policy. None = permissive."""
        if not team_id:
            return None
        row = await self._one("SELECT * FROM catalog_policies WHERE team_id = ?", (team_id,))
        if row is None:
            return None
        d = dict(row)
        d["models"] = json.loads(d.get("models") or "[]")
        d["residency"] = json.loads(d["residency"]) if d.get("residency") else None
        return d

    async def set_catalog_policy(self, team_id: str, org_id: str, *, mode: str = "allow",
                                 models: list[str] | None = None,
                                 residency: list[str] | None = None,
                                 default_model: str | None = None,
                                 updated_by: str | None = None) -> dict:
        """Upsert the team's catalog policy, bumping `version` on every write. Fail-closed on an
        unknown mode. Returns the stored policy. One dual-dialect UPSERT: `excluded.`/self-reference
        work identically on SQLite (3.24+) and Postgres."""
        if mode not in CATALOG_MODES:
            raise ValueError(f"unknown catalog-policy mode {mode!r}")
        models_json = json.dumps(list(models or []))
        residency_json = json.dumps(list(residency)) if residency is not None else None
        await self._exec(
            "INSERT INTO catalog_policies (team_id, org_id, mode, models, residency, "
            "default_model, version, updated_by, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?) "
            "ON CONFLICT (team_id) DO UPDATE SET org_id=excluded.org_id, mode=excluded.mode, "
            "models=excluded.models, residency=excluded.residency, "
            "default_model=excluded.default_model, version=catalog_policies.version+1, "
            "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
            (team_id, org_id, mode, models_json, residency_json, default_model, updated_by,
             time.time()),
        )
        return await self.get_catalog_policy(team_id)

    # --- routing overlay -------------------------------------------------------
    # Per-team tag->model overlay on top of the global routing/labels.yaml. Keyed by team_id;
    # org_id carried for the admin API's org-isolation check. Absence of a row = pure global
    # behavior (effective_policy carries no overlay -> unchanged routing, ZERO change).

    async def get_routing_policy(self, team_id: str) -> dict | None:
        """The team's routing overlay as a dict (bindings parsed from JSON), or None when the team
        has no policy. None = pure global behavior."""
        if not team_id:
            return None
        row = await self._one("SELECT * FROM routing_policies WHERE team_id = ?", (team_id,))
        if row is None:
            return None
        d = dict(row)
        d["bindings"] = json.loads(d.get("bindings") or "{}")
        d["custom_labels"] = json.loads(d.get("custom_labels") or "[]")  # team-invented task types
        d["prewarm"] = bool(d.get("prewarm"))  # 0/1 column -> bool for the API view + prewarm route read
        d["stick_ttls"] = json.loads(d.get("stick_ttls") or "{}")  # per-task-type memo holds
        d["cache"] = json.loads(d.get("cache") or "{}")  # per-org cache-behavior overrides
        # 'open' (default) | 'closed'; a JSON object is a per-reason matrix (parse it back).
        fp = d.get("fail_policy") or "open"
        if isinstance(fp, str) and fp.startswith("{"):
            try:
                fp = json.loads(fp)
            except (json.JSONDecodeError, ValueError):
                fp = "open"
        d["fail_policy"] = fp
        d["taxonomy"] = json.loads(d.get("taxonomy") or "{}")  # data-classification taxonomy
        d["classifier_model"] = d.get("classifier_model") or None  # org classifier (NULL = default)
        return d

    async def set_routing_policy(self, team_id: str, org_id: str, *,
                                 bindings: dict[str, str] | None = None,
                                 optimize: str | None = None,
                                 custom_labels: list[dict] | None = None,
                                 prewarm: bool | None = None,
                                 stick_ttls: dict | None = None,
                                 cache: dict | None = None,
                                 fail_policy: str | dict | None = None,
                                 taxonomy: dict | None = None,
                                 classifier_model: str | None = None,
                                 updated_by: str | None = None) -> dict:
        """Upsert the team's routing overlay, bumping `version` on every write. Fail-closed on an
        unknown optimize preset (catalog-existence of a bound model + custom-label slug/collision are
        validated at the API layer, which has the catalog + global vocab handles). custom_labels is
        the team's invented task types [{name, desc, model}]. Returns the stored policy. One
        dual-dialect UPSERT."""
        if optimize is not None and optimize not in ROUTING_OPTIMIZE:
            raise ValueError(f"unknown optimize preset {optimize!r}")
        bindings_json = json.dumps(dict(bindings or {}))
        custom_json = json.dumps(list(custom_labels or []))
        prewarm_int = int(bool(prewarm))  # full-replace semantics: omitted -> OFF, like bindings
        stick_json = json.dumps(dict(stick_ttls or {}))  # full-replace: omitted -> {} -> flat holds
        cache_json = json.dumps(dict(cache or {}))  # full-replace: omitted -> {} -> inherit global env
        # Scalar 'open'/'closed', or a per-reason matrix dict stored as JSON. Full-replace:
        # omitted -> 'open'.
        if isinstance(fail_policy, dict):
            fp = json.dumps(fail_policy)
        else:
            fp = "closed" if fail_policy == "closed" else "open"
        taxonomy_json = json.dumps(dict(taxonomy or {}))  # full-replace: omitted -> {} -> no taxonomy
        cm = classifier_model or None  # full-replace: omitted -> NULL -> gateway default classifier
        await self._exec(
            "INSERT INTO routing_policies (team_id, org_id, bindings, optimize, custom_labels, prewarm, "
            "stick_ttls, cache, fail_policy, taxonomy, classifier_model, version, updated_by, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?) "
            "ON CONFLICT (team_id) DO UPDATE SET org_id=excluded.org_id, bindings=excluded.bindings, "
            "optimize=excluded.optimize, custom_labels=excluded.custom_labels, "
            "prewarm=excluded.prewarm, stick_ttls=excluded.stick_ttls, cache=excluded.cache, "
            "fail_policy=excluded.fail_policy, taxonomy=excluded.taxonomy, "
            "classifier_model=excluded.classifier_model, "
            "version=routing_policies.version+1, "
            "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
            (team_id, org_id, bindings_json, optimize, custom_json, prewarm_int, stick_json,
             cache_json, fp, taxonomy_json, cm, updated_by, time.time()),
        )
        return await self.get_routing_policy(team_id)

    # --- monthly budgets -------------------------------------------------------
    # Per-team monthly USD budget; org-default = the sentinel row (team_id == org_id), SAME shape
    # as routing_policies. Absence of a row = no budget = unchanged behavior.

    async def get_budget(self, team_id: str) -> dict | None:
        """The scope's budget row (thresholds parsed from JSON), or None. None = no budget."""
        if not team_id:
            return None
        row = await self._one("SELECT * FROM budgets WHERE team_id = ?", (team_id,))
        if row is None:
            return None
        d = dict(row)
        d["thresholds"] = json.loads(d.get("thresholds") or "[]")
        return d

    async def list_budgets(self, org_id: str) -> list[dict]:
        """Every budget row in an org (org-default sentinel included), by team_id. [] = none."""
        rows = await self._all("SELECT * FROM budgets WHERE org_id = ? ORDER BY team_id", (org_id,))
        out = []
        for r in rows:
            d = dict(r)
            d["thresholds"] = json.loads(d.get("thresholds") or "[]")
            out.append(d)
        return out

    async def set_budget(self, team_id: str, org_id: str, *, monthly_usd: float,
                         action: str = "observe", thresholds: list | None = None,
                         updated_by: str | None = None) -> dict:
        """Upsert the scope's budget, bumping `version`. Fail-closed on an unknown action. Returns
        the stored budget. One dual-dialect UPSERT (same idiom as set_routing_policy)."""
        from ..budgets import BUDGET_ACTIONS

        if action not in BUDGET_ACTIONS:
            raise ValueError(f"unknown budget action {action!r}")
        thr_json = json.dumps(list(thresholds) if thresholds is not None else [0.5, 0.8, 1.0])
        await self._exec(
            "INSERT INTO budgets (team_id, org_id, monthly_usd, action, thresholds, version, "
            "updated_by, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?) "
            "ON CONFLICT (team_id) DO UPDATE SET org_id=excluded.org_id, "
            "monthly_usd=excluded.monthly_usd, action=excluded.action, "
            "thresholds=excluded.thresholds, version=budgets.version+1, "
            "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
            (team_id, org_id, float(monthly_usd), action, thr_json, updated_by, time.time()),
        )
        return await self.get_budget(team_id)

    async def delete_budget(self, key: str) -> bool:
        """Remove a budget row by its scope key (team_id / org-default sentinel / member key). True if
        a row was there, False if not — the route maps False to 404. Used to clear a per-member cap so
        the member falls back to the team/org-default again."""
        if await self.get_budget(key) is None:
            return False
        await self._exec("DELETE FROM budgets WHERE team_id = ?", (key,))
        return True

    async def budget_alert_fire_once(self, scope_key: str, period: str, threshold: float) -> bool:
        """True only the FIRST time (scope, month, threshold) is recorded — the dedupe behind the
        once-per-threshold budget alert. A PK conflict (already fired) OR any error returns False:
        fail-safe, never double-alert, never crash the budget check."""
        try:
            await self._exec(
                "INSERT INTO budget_alerts (scope_key, period, threshold, fired_at) "
                "VALUES (?, ?, ?, ?)", (scope_key, period, float(threshold), time.time()))
            return True
        except Exception:
            return False

    # --- catalog adoptions -----------------------------------------------------
    # Server-side "add this provider-library model to my catalog." Scoped by scope_key = team_id or
    # org_id (resolved in deps._resolve_adoptions, the SAME fallback as _resolve_routing_policy).
    # entry_json is the materialized CatalogEntry the API derived from the provider discovery
    # snapshot — the store just persists it; all fact-derivation + naming validation happens in the
    # admin route (which holds the base catalog + discovery handles). Absence of rows = base catalog
    # only (ZERO behavior change).

    @staticmethod
    def _adoption_row(row) -> dict:
        d = dict(row)
        d["entry"] = json.loads(d["entry_json"])  # the materialized CatalogEntry dict
        return d

    async def list_adoptions(self, scope_key: str) -> list[dict]:
        """The scope's adoptions (each with `entry` = the parsed CatalogEntry dict), by id. [] = none."""
        if not scope_key:
            return []
        rows = await self._all(
            "SELECT scope_key, id, entry_json, upstream_model, provider, created_by, updated_at "
            "FROM catalog_adoptions WHERE scope_key = ? ORDER BY id", (scope_key,))
        return [self._adoption_row(r) for r in rows]

    async def get_adoption(self, scope_key: str, id: str) -> dict | None:
        if not scope_key:
            return None
        row = await self._one(
            "SELECT scope_key, id, entry_json, upstream_model, provider, created_by, updated_at "
            "FROM catalog_adoptions WHERE scope_key = ? AND id = ?", (scope_key, id))
        return self._adoption_row(row) if row is not None else None

    async def add_adoption(self, scope_key: str, id: str, *, entry_json: str,
                           upstream_model: str, provider: str,
                           created_by: str | None = None) -> dict:
        """Upsert one adoption. One dual-dialect UPSERT (re-adopting the same id replaces the entry)."""
        await self._exec(
            "INSERT INTO catalog_adoptions (scope_key, id, entry_json, upstream_model, provider, "
            "created_by, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (scope_key, id) DO UPDATE SET entry_json=excluded.entry_json, "
            "upstream_model=excluded.upstream_model, provider=excluded.provider, "
            "created_by=excluded.created_by, updated_at=excluded.updated_at",
            (scope_key, id, entry_json, upstream_model, provider, created_by, time.time()))
        return await self.get_adoption(scope_key, id)

    async def remove_adoption(self, scope_key: str, id: str) -> bool:
        """Delete one adoption, scope-pinned. False when the row isn't in this scope (→ 404, so a
        caller can't probe another scope's ids)."""
        if not scope_key:
            return False
        row = await self._one(
            "SELECT id FROM catalog_adoptions WHERE scope_key = ? AND id = ?", (scope_key, id))
        if row is None:
            return False
        await self._exec("DELETE FROM catalog_adoptions WHERE scope_key = ? AND id = ?",
                         (scope_key, id))
        return True

    # --- price overrides — same shape/discipline as adoptions ------------------

    async def list_price_overrides(self, *scope_keys: str) -> list[dict]:
        """Override rows for the given scopes (deduped, falsy keys dropped), ordered by model_id.
        Callers merge precedence themselves — this is a plain read."""
        keys = tuple(dict.fromkeys(k for k in scope_keys if k))
        if not keys:
            return []
        marks = ",".join("?" for _ in keys)
        rows = await self._all(
            "SELECT scope_key, model_id, prompt_usd_per_1k, completion_usd_per_1k, "
            "cache_read_multiplier, updated_by, updated_at "
            f"FROM price_overrides WHERE scope_key IN ({marks}) ORDER BY model_id", keys)
        return [dict(r) for r in rows]

    async def set_price_override(self, scope_key: str, model_id: str, *,
                                 prompt_usd_per_1k: float, completion_usd_per_1k: float,
                                 cache_read_multiplier: float | None = None,
                                 updated_by: str | None = None) -> dict:
        """Upsert one override (per-1k figures — the API boundary owns the per-Mtok conversion)."""
        await self._exec(
            "INSERT INTO price_overrides (scope_key, model_id, prompt_usd_per_1k, "
            "completion_usd_per_1k, cache_read_multiplier, updated_by, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (scope_key, model_id) DO UPDATE SET "
            "prompt_usd_per_1k=excluded.prompt_usd_per_1k, "
            "completion_usd_per_1k=excluded.completion_usd_per_1k, "
            "cache_read_multiplier=excluded.cache_read_multiplier, "
            "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
            (scope_key, model_id, prompt_usd_per_1k, completion_usd_per_1k,
             cache_read_multiplier, updated_by, time.time()))
        rows = await self.list_price_overrides(scope_key)
        return next(r for r in rows if r["model_id"] == model_id)

    async def remove_price_override(self, scope_key: str, model_id: str) -> bool:
        """Delete one override, scope-pinned (mirrors remove_adoption's 404 discipline)."""
        if not scope_key:
            return False
        row = await self._one(
            "SELECT model_id FROM price_overrides WHERE scope_key = ? AND model_id = ?",
            (scope_key, model_id))
        if row is None:
            return False
        await self._exec("DELETE FROM price_overrides WHERE scope_key = ? AND model_id = ?",
                         (scope_key, model_id))
        return True
