"""Per-user WHERE predicates shared by every RunStore query family.

Two regimes, both fail-closed: `_scope` lets the operator/service credential (user_id None) see
everything while a real user sees strictly their own rows; `_mem_scope` never widens — even the
operator only reads NULL-owner rows.
"""

from __future__ import annotations


def _scope(user_id: str | None) -> tuple[str, tuple]:
    """A WHERE fragment restricting a real user to STRICTLY their own rows — never another
    user's, never NULL-owner (fail closed). Empty (no restriction) when user_id is None —
    operator or open-mode, which is the service-credential path and sees everything."""
    if user_id is None:
        return "", ()
    return "(user_id = ?)", (user_id,)


def _mem_scope(user_id: str | None) -> tuple[str, tuple]:
    """STRICT owner predicate for memory rows — no NULL grandfathering (two users share
    nothing). user_id None = the open-mode anonymous user's own rows."""
    if user_id is None:
        return "user_id IS NULL", ()
    return "user_id = ?", (user_id,)
