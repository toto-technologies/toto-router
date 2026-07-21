"""Sign-out must clear EVERY auth cookie — the user session AND the OSS operator cookie.

The operator console authenticates via the `toto_operator` cookie (routes/deps.py), which no
server session backs; if logout only revoked user sessions, Sign out silently left the operator
signed in (QA trust bug)."""

from __future__ import annotations

from toto_gateway.routes.deps import OPERATOR_COOKIE, SESSION_COOKIE


def _cleared(response, name: str) -> bool:
    """True when a Set-Cookie header expires `name` (empty value, immediate expiry)."""
    return any(
        h.startswith(f"{name}=") and ('Max-Age=0' in h or "expires" in h.lower())
        for h in response.headers.get_list("set-cookie")
    )


def test_logout_clears_operator_and_session_cookies(test_client):
    test_client.cookies.set(OPERATOR_COOKIE, "dev-operator-token")
    r = test_client.post("/v1/auth/logout")
    assert r.status_code == 204
    assert _cleared(r, OPERATOR_COOKIE)
    assert _cleared(r, SESSION_COOKIE)
