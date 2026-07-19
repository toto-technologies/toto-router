"""Canvas/board plane: Toto lists, spatial positions, desks, bindles, documents, generic objects.

Invariants carried throughout: a position may only back a real object (orphans are phantom UI);
every upsert has a fail-closed owner guard (a foreign or NULL-owner collision DO-UPDATEs
nothing); mutations fan out to the owner's live-board SSE channel via _publish_board.
"""

from __future__ import annotations

import json
import time

from .. import db as _db_mod
from .scoping import _scope

# Desk size tiers: a desk is a predictable size, chosen not emergent. World dims per tier live
# SERVER-SIDE (shared truth) so agents reason about the same surface every client draws.
# Custom carries its own w/h; named tiers derive from here.
DESK_TIERS: dict[str, tuple[float, float]] = {
    "small":  (1920.0, 1200.0),   # one task's worth of work
    "medium": (2560.0, 1600.0),   # a project's active set — the default
    "large":  (4096.0, 2560.0),   # a wall — many clusters/piles
}
DESK_TIER_NAMES = frozenset(DESK_TIERS) | {"custom"}
DEFAULT_DESK_TIER = "medium"
DEFAULT_DESK_MATERIAL = "guilloche"  # deliberately NOT MATERIALS[0] — shared identity, not device taste


def desk_dims(tier: str, w: float | None, h: float | None) -> tuple[float, float]:
    """Effective (w, h) for a desk record: custom uses its stored dims (falling back to the
    default tier if either is missing — a custom row can't render dimensionless); a named tier
    always derives from DESK_TIERS, ignoring any stale stored w/h."""
    if tier == "custom" and w and h:
        return float(w), float(h)
    return DESK_TIERS.get(tier, DESK_TIERS[DEFAULT_DESK_TIER])


# A canvas position points at an object living in ONE of four tables. Three kinds keep their own
# table (kind -> (table, id column)); every OTHER kind is a generic row in canvas_objects. Naming
# only the stable own-table kinds keeps the existence check + orphan cleanup drift-free as new
# generic kinds land (a position may only back a real object).
_OWN_TABLE_KINDS: dict[str, tuple[str, str]] = {
    "list":    ("lists", "list_id"),
    "session": ("sessions", "run_id"),
    "bindle":  ("bindles", "bindle_id"),
}


class CanvasMixin:
    """Canvas/board surface of RunStore (lists, positions, desks, bindles, documents, objects)."""

    # --- lists (canvas Toto lists) -----------------------------------------------

    async def create_list(self, list_id: str, name: str, user_id: str | None = None) -> None:
        await self._exec(
            "INSERT INTO lists (list_id, name, created_at, user_id) VALUES (?, ?, ?, ?)",
            (list_id, name, time.time(), user_id),
        )
        await self._publish_board(user_id, "list_created", {"list_id": list_id, "name": name})

    async def add_item(self, list_id: str, item_id: str, task: str,
                       user_id: str | None = None) -> None:
        # position = MAX+1 in ONE atomic statement (was a lock-guarded read-then-insert; the pool
        # has no single-conn lock, so fold it into the INSERT — correct in both dialects).
        await self._exec(
            "INSERT INTO list_items (list_id, item_id, task, position, created_at) "
            "SELECT ?, ?, ?, COALESCE(MAX(position), 0) + 1, ? FROM list_items WHERE list_id = ?",
            (list_id, item_id, task, time.time(), list_id),
        )
        await self._publish_board(user_id, "item_added",
                                  {"list_id": list_id, "item_id": item_id, "task": task})

    async def enrich_item(self, list_id: str, item_id: str, description: str, metadata: dict,
                    model: str) -> None:
        await self._exec(
            "UPDATE list_items SET description = ?, metadata = ?, enriched_model = ? "
            "WHERE list_id = ? AND item_id = ?",
            (description, json.dumps(metadata), model, list_id, item_id),
        )

    async def set_item_status(self, list_id: str, item_id: str, status: str,
                              user_id: str | None = None) -> None:
        """Set an item's done-state ('' | 'doing' | 'done'). No-op if the item doesn't exist."""
        await self._exec(
            "UPDATE list_items SET status = ? WHERE list_id = ? AND item_id = ?",
            (status, list_id, item_id),
        )
        await self._publish_board(user_id, "item_status",
                                  {"list_id": list_id, "item_id": item_id, "status": status})

    async def delete_item(self, list_id: str, item_id: str,
                          user_id: str | None = None) -> None:
        """Remove one item from a list (the prod list's X-delete). No-op if already gone."""
        await self._exec(
            "DELETE FROM list_items WHERE list_id = ? AND item_id = ?", (list_id, item_id),
        )
        await self._publish_board(user_id, "item_deleted",
                                  {"list_id": list_id, "item_id": item_id})

    async def delete_list(self, list_id: str, user_id: str | None = None) -> bool:
        """Delete a whole list — owner-scoped, with cascade. Removes the list's items and its
        canvas position row (kind='list', if placed), then the `lists` row. Returns True iff a
        list row was removed (mirrors delete_object's scoped-rowcount shape). Ownership is proven
        by the scoped DELETE on `lists` FIRST, so children are only touched once we know the
        caller owns the list (a non-owner gets changed=False and nothing is deleted)."""
        clause, params = _scope(user_id)
        sql = "DELETE FROM lists WHERE list_id = ?" + (f" AND {clause}" if clause else "")
        if self._pool is not None:
            await self._open_pool()
            async with self._pool.connection() as c:
                cur = await c.execute(_db_mod._PgConn._t(sql), (list_id, *params))
                changed = cur.rowcount > 0
        else:
            with self._lock:
                cur = self._db.execute(sql, (list_id, *params))
                self._db.commit()
                changed = cur.rowcount > 0
        if not changed:
            return False
        await self._exec("DELETE FROM list_items WHERE list_id = ?", (list_id,))
        await self._exec("DELETE FROM canvas_positions WHERE kind = 'list' AND object_id = ?",
                         (list_id,))
        await self._publish_board(user_id, "list_deleted", {"list_id": list_id})
        return True

    async def update_item(self, list_id: str, item_id: str, *, task: str | None = None,
                          description: str | None = None, metadata: dict | None = None,
                          user_id: str | None = None) -> bool:
        """Edit an item's task/description/metadata in place — only the provided fields. Owner-
        scoped through the parent list (list_items has no user_id of its own); True if a row
        changed. rowcount needs the raw cursor — same dual-branch shape as delete_object."""
        sets, vals = [], []
        if task is not None:
            sets.append("task = ?"); vals.append(task)
        if description is not None:
            sets.append("description = ?"); vals.append(description)
        if metadata is not None:
            sets.append("metadata = ?"); vals.append(json.dumps(metadata))
        if not sets:
            return False
        clause, params = _scope(user_id)
        scope = f" AND list_id IN (SELECT list_id FROM lists WHERE {clause})" if clause else ""
        sql = f"UPDATE list_items SET {', '.join(sets)} WHERE list_id = ? AND item_id = ?" + scope
        args = (*vals, list_id, item_id, *params)
        if self._pool is not None:
            await self._open_pool()
            async with self._pool.connection() as c:
                cur = await c.execute(_db_mod._PgConn._t(sql), args)
                changed = cur.rowcount > 0
        else:
            with self._lock:
                cur = self._db.execute(sql, args)
                self._db.commit()
                changed = cur.rowcount > 0
        if changed:
            await self._publish_board(user_id, "item_updated",
                                      {"list_id": list_id, "item_id": item_id})
        return changed

    async def get_list(self, list_id: str, user_id: str | None = None) -> dict | None:
        clause, params = _scope(user_id)
        row = await self._one(
            "SELECT * FROM lists WHERE list_id = ?" + (f" AND {clause}" if clause else ""),
            (list_id, *params),
        )
        if row is None:
            return None
        items = await self._all(
            "SELECT * FROM list_items WHERE list_id = ? ORDER BY position", (list_id,)
        )
        out = dict(row)
        out["items"] = [{**dict(i), "metadata": json.loads(i["metadata"])} for i in items]
        return out

    async def list_lists(self, user_id: str | None = None) -> list[dict]:
        clause, params = _scope(user_id)
        rows = await self._all(
            "SELECT l.*, COUNT(i.item_id) AS n_items, "
            "SUM(CASE WHEN i.enriched_model != '' THEN 1 ELSE 0 END) AS n_enriched "
            "FROM lists l LEFT JOIN list_items i ON i.list_id = l.list_id "
            + (f"WHERE {clause.replace('user_id', 'l.user_id')} " if clause else "")
            + "GROUP BY l.list_id ORDER BY l.created_at DESC",
            params,
        )
        return [dict(r) for r in rows]

    # --- canvas positions (Miro-style spatial canvas) --------------------------

    async def get_positions(self, user_id: str | None = None,
                      parent: str | None = None) -> list[dict]:
        """Positions, optionally filtered to one surface (parent). parent=None → all surfaces
        (backward compatible); parent='' → the default world; parent=<id> → inside a container."""
        clause, params = _scope(user_id)
        wheres = ([clause] if clause else []) + (["parent = ?"] if parent is not None else [])
        args = list(params) + ([parent] if parent is not None else [])
        sql = "SELECT kind, object_id, x, y, z, parent, w, h, actor FROM canvas_positions"
        if wheres:
            sql += " WHERE " + " AND ".join(wheres)
        return [dict(r) for r in await self._all(sql, args)]

    async def set_positions(self, rows: list[dict], user_id: str | None = None,
                            actor: str | None = None) -> None:
        """Batch upsert positions keyed on (kind, object_id); one updated_at for the batch.
        user_id stamps new rows and is preserved on update (owner never reassigned). `parent`
        (default '') is the object's surface — it IS overwritten on update, so a PUT must carry
        the row's current parent or the object jumps back to the world. `w`/`h` (card
        width/height) are the OPPOSITE: absent → PRESERVED (COALESCE keeps the stored value),
        because only list cards send a size and a generic drag PUT shouldn't blow it away.
        Fail-closed owner guard on the upsert: a (kind, object_id) collision only overwrites a
        row the caller owns — a foreign or NULL-owner collision DO-UPDATEs nothing (no
        cross-tenant move/reparent). Null-safe so the operator path (user_id NULL) still
        updates its own NULL-owner rows."""
        now = time.time()
        # SQLite: `IS` is null-safe equality; PG: `IS NOT DISTINCT FROM`. Both compare NULL=NULL true.
        guard = ("canvas_positions.user_id IS NOT DISTINCT FROM excluded.user_id" if self._pg
                 else "canvas_positions.user_id IS excluded.user_id")
        # actor: stamped on insert AND refreshed on update — the row reflects its LATEST writer,
        # which is what a provenance chip renders. ponytail: last-writer-wins on actor too, no
        # conflict resolution — the write layer keeps whoever wrote last; a future round can arbitrate.
        await self._many(
            "INSERT INTO canvas_positions (kind, object_id, x, y, z, parent, w, h, updated_at, user_id, actor) "
            "VALUES (:kind, :object_id, :x, :y, :z, :parent, :w, :h, :updated_at, :user_id, :actor) "
            "ON CONFLICT(kind, object_id) DO UPDATE SET "
            "x = excluded.x, y = excluded.y, z = excluded.z, parent = excluded.parent, "
            "w = COALESCE(excluded.w, canvas_positions.w), "
            "h = COALESCE(excluded.h, canvas_positions.h), updated_at = excluded.updated_at, "
            "actor = excluded.actor "
            "WHERE " + guard,
            [{**r, "parent": r.get("parent", ""), "w": r.get("w"), "h": r.get("h"), "updated_at": now,
              "user_id": user_id, "actor": actor} for r in rows],
        )
        await self._publish_board(user_id, "positions_set", {"rows": [
            {"kind": r["kind"], "object_id": r["object_id"], "x": r.get("x"), "y": r.get("y"),
             "z": r.get("z"), "parent": r.get("parent", ""), "w": r.get("w"), "h": r.get("h"),
             "actor": actor}
            for r in rows]})

    async def count_children(self, parent: str, user_id: str | None = None) -> int:
        """How many positions sit inside a container/space — gates its deletion (no orphans)."""
        clause, params = _scope(user_id)
        sql = "SELECT COUNT(*) AS n FROM canvas_positions WHERE parent = ?" + (
            f" AND {clause}" if clause else "")
        return (await self._one(sql, (parent, *params)))["n"]

    async def existing_keys(self, pairs, user_id: str | None = None) -> set[tuple[str, str]]:
        """Which (kind, object_id) pairs actually back a real object, owner-scoped — a position may
        only point at something that exists (no writes into the void). Each kind is checked
        against its backing table: own-table kinds (list/session/bindle) in theirs, every other kind
        in canvas_objects. Per-kind IN queries (portable, no row-value syntax); a handful of kinds,
        so a handful of queries. A pair for an unknown owner simply isn't found → the caller 422s."""
        from collections import defaultdict

        by_kind: dict[str, list[str]] = defaultdict(list)
        for kind, oid in pairs:
            by_kind[kind].append(oid)
        clause, sparams = _scope(user_id)
        found: set[tuple[str, str]] = set()
        for kind, ids in by_kind.items():
            ph = ",".join(["?"] * len(ids))
            if kind in _OWN_TABLE_KINDS:
                table, col = _OWN_TABLE_KINDS[kind]
                sql = (f"SELECT {col} AS oid FROM {table} WHERE "
                       + (f"{clause} AND " if clause else "") + f"{col} IN ({ph})")
                args = (*sparams, *ids)
            else:
                sql = ("SELECT object_id AS oid FROM canvas_objects WHERE "
                       + (f"{clause} AND " if clause else "") + f"kind = ? AND object_id IN ({ph})")
                args = (*sparams, kind, *ids)
            for r in await self._all(sql, args):
                found.add((kind, r["oid"]))
        return found

    # --- desk identity (tier + material are server truth, not device-local) --------------

    async def get_desk(self, user_id: str | None = None, parent: str = "") -> dict:
        """The EFFECTIVE desk for one surface: the stored row if present, else the medium default.
        Dims are resolved server-side (desk_dims) so a client — or an agent placing work — reads the
        same finite surface every time. Owner-scoped read (strict per-user; a real user never sees
        another's or a NULL-owner desk)."""
        clause, params = _scope(user_id)
        row = await self._one(
            "SELECT tier, w, h, material FROM canvas_desks WHERE parent = ?"
            + (f" AND {clause}" if clause else ""), (parent, *params))
        tier = row["tier"] if row else DEFAULT_DESK_TIER
        material = row["material"] if row else DEFAULT_DESK_MATERIAL
        w, h = desk_dims(tier, row["w"] if row else None, row["h"] if row else None)
        return {"parent": parent, "tier": tier, "w": w, "h": h, "material": material}

    async def set_desk(self, user_id: str | None, parent: str, tier: str,
                       w: float | None, h: float | None, material: str) -> None:
        """Upsert a surface's desk identity, then broadcast so live clients converge.
        Null-safe delete+insert rather than ON CONFLICT: the key is (user_id, parent), and SQLite
        treats a NULL user_id (the operator) as DISTINCT in a PK conflict — so ON CONFLICT would let
        an operator accumulate duplicate rows per surface. The delete matches null-safely (`IS`) and
        the insert replaces, so the operator path is as correct as a real user's. There is no
        cross-owner reach: the delete is owner-matched, so a caller only ever replaces its own row.
        ponytail: two statements, not atomic — a rare user-initiated write, not a hot path; a
        concurrent set could interleave. Add a transaction if desk-set ever contends."""
        now = time.time()
        eq = "IS NOT DISTINCT FROM" if self._pg else "IS"  # null-safe owner match on both engines
        await self._exec(
            f"DELETE FROM canvas_desks WHERE parent = ? AND user_id {eq} ?", (parent, user_id))
        await self._exec(
            "INSERT INTO canvas_desks (user_id, parent, tier, w, h, material, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, parent, tier, w, h, material, now),
        )
        eff_w, eff_h = desk_dims(tier, w, h)
        await self._publish_board(user_id, "desk_set", {
            "parent": parent, "tier": tier, "w": eff_w, "h": eff_h, "material": material})

    # --- bindles (rendered broadsheets as canvas objects) ----------------------

    async def put_bindle(self, bindle_id: str, edition: str, subtitle: str, pages: int,
                   html: str, created_at: float | None = None, user_id: str | None = None) -> None:
        """Upsert a rendered bindle. created_at is preserved on update unless passed; user_id
        stamps a new row and is preserved on update (owner never reassigned). Fail-closed owner
        guard on the upsert: a bindle_id collision only overwrites a row the caller owns — a
        foreign or NULL-owner collision DO-UPDATEs nothing (else a caller could overwrite another
        tenant's bindle HTML → stored XSS). Null-safe so the operator path (user_id NULL) still
        updates its own NULL-owner rows."""
        # SQLite: `IS` is null-safe equality; PG: `IS NOT DISTINCT FROM`. Both compare NULL=NULL true.
        guard = ("bindles.user_id IS NOT DISTINCT FROM excluded.user_id" if self._pg
                 else "bindles.user_id IS excluded.user_id")
        if created_at is None:
            # Keep the original timestamp on update; stamp now on first insert.
            await self._exec(
                "INSERT INTO bindles (bindle_id, edition, subtitle, pages, html, created_at, "
                "user_id) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(bindle_id) DO UPDATE SET "
                "edition = excluded.edition, subtitle = excluded.subtitle, "
                "pages = excluded.pages, html = excluded.html "
                "WHERE " + guard,
                (bindle_id, edition, subtitle, pages, html, time.time(), user_id),
            )
        else:
            await self._exec(
                "INSERT INTO bindles (bindle_id, edition, subtitle, pages, html, created_at, "
                "user_id) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(bindle_id) DO UPDATE SET "
                "edition = excluded.edition, subtitle = excluded.subtitle, "
                "pages = excluded.pages, html = excluded.html, created_at = excluded.created_at "
                "WHERE " + guard,
                (bindle_id, edition, subtitle, pages, html, created_at, user_id),
            )

    async def get_bindles(self, user_id: str | None = None) -> list[dict]:
        """Listing rows without the html blob — `bytes` is the html length for a size hint."""
        clause, params = _scope(user_id)
        rows = await self._all(
            "SELECT bindle_id, edition, subtitle, pages, created_at, LENGTH(html) AS bytes "
            "FROM bindles " + (f"WHERE {clause} " if clause else "") + "ORDER BY created_at DESC",
            params,
        )
        return [dict(r) for r in rows]

    async def get_bindle_html(self, bindle_id: str, user_id: str | None = None) -> str | None:
        clause, params = _scope(user_id)
        row = await self._one(
            "SELECT html FROM bindles WHERE bindle_id = ?" + (f" AND {clause}" if clause else ""),
            (bindle_id, *params),
        )
        return row["html"] if row else None

    # --- session documents (markdown results in the ObjectStore) --------------

    async def document_create(self, doc_id: str, user_id: str | None, run_id: str,
                        title: str, key: str, sha256: str, bytes: int) -> None:
        """Index one saved document. The bytes already live in the ObjectStore under `key`; this
        row is what makes them listable (the store has no list op)."""
        await self._exec(
            "INSERT INTO documents (doc_id, user_id, run_id, title, key, sha256, bytes, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (doc_id, user_id, run_id, title, key, sha256, bytes, time.time()),
        )

    async def documents_for(self, user_id: str | None, limit: int = 100,
                      before: float | None = None) -> list[dict]:
        """The caller's own documents, newest first. NULL-owner rows never returned (fail-closed
        _scope). `before` (a created_at) paginates: rows strictly older than it."""
        clause, params = _scope(user_id)
        wheres = ([clause] if clause else []) + (["created_at < ?"] if before is not None else [])
        args = list(params) + ([before] if before is not None else []) + [limit]
        sql = "SELECT doc_id, run_id, title, sha256, bytes, created_at FROM documents "
        if wheres:
            sql += "WHERE " + " AND ".join(wheres) + " "
        rows = await self._all(sql + "ORDER BY created_at DESC LIMIT ?", args)
        return [dict(r) for r in rows]

    async def document(self, user_id: str | None, doc_id: str) -> dict | None:
        """Owner-scoped lookup — None for another user's id or a NULL-owner row (existence hidden)."""
        clause, params = _scope(user_id)
        row = await self._one(
            "SELECT * FROM documents WHERE doc_id = ?" + (f" AND {clause}" if clause else ""),
            (doc_id, *params),
        )
        return dict(row) if row is not None else None

    # --- canvas objects (generic data-only kinds: note, clip, ...) -------------

    async def put_object(self, kind: str, object_id: str, payload: dict,
                   user_id: str | None = None, actor: str | None = None) -> None:
        """Upsert a data-only canvas object; created_at is preserved on update
        (omitted from the SET clause), updated_at always bumped. user_id stamps a new row and
        is preserved on update (owner never reassigned). Fail-closed owner guard on the upsert:
        a (kind, object_id) collision only overwrites a row the caller owns — a foreign or
        NULL-owner collision DO-UPDATEs nothing. Null-safe so the operator path (user_id NULL)
        still updates its own NULL-owner rows. actor records the latest writer's provenance."""
        now = time.time()
        cast = "::jsonb" if self._pg else ""  # payload column is JSONB on PG (see _pg_optimize)
        # SQLite: `IS` is null-safe equality; PG: `IS NOT DISTINCT FROM`. Both compare NULL=NULL true.
        guard = ("canvas_objects.user_id IS NOT DISTINCT FROM excluded.user_id" if self._pg
                 else "canvas_objects.user_id IS excluded.user_id")
        await self._exec(
            "INSERT INTO canvas_objects (kind, object_id, payload, created_at, updated_at, "
            f"user_id, actor) VALUES (?, ?, ?{cast}, ?, ?, ?, ?) "
            "ON CONFLICT(kind, object_id) DO UPDATE SET "
            "payload = excluded.payload, updated_at = excluded.updated_at, actor = excluded.actor "
            "WHERE " + guard,
            (kind, object_id, json.dumps(payload), now, now, user_id, actor),
        )
        await self._publish_board(user_id, "object_put",
                                  {"kind": kind, "object_id": object_id, "actor": actor})

    async def get_objects(self, kind: str | None = None, user_id: str | None = None) -> list[dict]:
        """Light rows [{kind, object_id, payload, created_at, updated_at}], newest-first."""
        clause, params = _scope(user_id)
        wheres = ([f"{clause}"] if clause else []) + (["kind = ?"] if kind is not None else [])
        args = list(params) + ([kind] if kind is not None else [])
        sql = "SELECT kind, object_id, payload, created_at, updated_at, actor FROM canvas_objects "
        if wheres:
            sql += "WHERE " + " AND ".join(wheres) + " "
        rows = await self._all(sql + "ORDER BY created_at DESC", args)
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    async def all_objects_of_kind(self, kind: str) -> list[dict]:
        """Every object of a kind across ALL owners, WITH user_id — the background-job read (the
        calendar ICS sync iterates these like the dreamer iterates tenants, then writes each back
        scoped to its owner via put_object(user_id=...)). Not an API path; callers are trusted
        in-process jobs, so it deliberately ignores the per-user _scope guard."""
        rows = await self._all(
            "SELECT kind, object_id, payload, user_id, created_at, updated_at "
            "FROM canvas_objects WHERE kind = ? ORDER BY created_at DESC", (kind,))
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    async def delete_object(self, kind: str, object_id: str, user_id: str | None = None) -> bool:
        clause, params = _scope(user_id)
        if self._pool is not None:
            await self._open_pool()
            async with self._pool.connection() as c:
                cur = await c.execute(_db_mod._PgConn._t(
                    "DELETE FROM canvas_objects WHERE kind = ? AND object_id = ?"
                    + (f" AND {clause}" if clause else "")),
                    (kind, object_id, *params))
                changed = cur.rowcount > 0
        else:
            with self._lock:
                cur = self._db.execute(
                    "DELETE FROM canvas_objects WHERE kind = ? AND object_id = ?"
                    + (f" AND {clause}" if clause else ""),
                    (kind, object_id, *params),
                )
                self._db.commit()
                changed = cur.rowcount > 0
        if changed:
            # Cascade the object's position row(s) — a position must not outlive its object (an
            # orphan row is a phantom Mission Control dot / pile badge). Owner-scoped, same as
            # the delete above. Own-table kinds (list) cascade in their own delete path already.
            clause2, params2 = _scope(user_id)
            await self._exec(
                "DELETE FROM canvas_positions WHERE kind = ? AND object_id = ?"
                + (f" AND {clause2}" if clause2 else ""), (kind, object_id, *params2))
            await self._publish_board(user_id, "object_deleted",
                                      {"kind": kind, "object_id": object_id})
        return changed
