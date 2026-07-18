"""HTTP-surface helpers: the app-wide security headers/CSP and the SPA static-file mounts."""

from __future__ import annotations

from pathlib import Path

from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

# App-wide CSP for the same-origin SPA + API. 'unsafe-inline' covers SvelteKit's hydration
# bootstrap script/style; everything else is clamped to 'self' so injected external scripts
# can't load and exfil is blocked. ponytail: tighten to nonces/hashes if we drop unsafe-inline.
_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; "
        # media-src: companion TTS plays blob: object-URLs; without an explicit media-src,
        # <audio> falls back to default-src and the browser SILENTLY drops the source
        # (play() resolves, no error, no sound). blob: for media only — never scripts.
        "media-src 'self' blob:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


def _apply_security_headers(headers) -> None:
    """Set the app-wide security headers on a response's (Mutable)Headers in place. Routes that
    ship their own Content-Security-Policy (the bindle sandbox, deliberately frameable
    same-origin) opt out of the app policy AND its frame ban — we never clobber their CSP."""
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if "content-security-policy" not in headers:
        headers["Content-Security-Policy"] = _CSP
        headers["X-Frame-Options"] = "DENY"


def _find_build(rel: str) -> Path | None:
    """Locate a built SPA dir: CWD-relative in both dev (repo root) and the Docker runtime (/app,
    where the node stage's output is copied); fall back to the source-tree location next to the
    package."""
    return next((p for p in (Path(rel), Path(__file__).resolve().parent.parent.parent / rel)
                 if p.is_dir()), None)


class SpaStaticFiles(StaticFiles):
    """adapter-static writes prerendered pages as <route>.html — map deep links
    (/console/catalog → catalog.html) so hard reloads and shared URLs work. Starlette
    RAISES HTTPException(404) for missing files (it doesn't return a 404 response),
    so the fallback must catch, not inspect status codes."""

    async def get_response(self, path, scope):  # type: ignore[override]
        try:
            resp = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404 and path and "." not in path.rsplit("/", 1)[-1]:
                resp = await super().get_response(f"{path}.html", scope)
            else:
                raise
        # /_app/immutable/* is content-hashed → cache forever; .html entry points → never.
        if "/immutable/" in path:
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif path.endswith(".html") or "." not in path.rsplit("/", 1)[-1]:
            resp.headers["Cache-Control"] = "no-cache"
        return resp


if __name__ == "__main__":
    # Self-check for the security-header logic: normal responses get the full policy + frame
    # ban; a route with its own CSP (the bindle sandbox) keeps it and is NOT frame-banned.
    # Run as `python -m toto_gateway.app.http` — a direct file run would put this dir on
    # sys.path and shadow stdlib `http`, which starlette imports.
    from starlette.datastructures import MutableHeaders

    normal = MutableHeaders()
    _apply_security_headers(normal)
    assert normal["x-content-type-options"] == "nosniff"
    assert normal["referrer-policy"] == "strict-origin-when-cross-origin"
    assert normal["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in normal["content-security-policy"]

    bindle = MutableHeaders(headers={"content-security-policy": "sandbox allow-scripts",
                                     "x-content-type-options": "nosniff"})
    _apply_security_headers(bindle)
    assert bindle["content-security-policy"] == "sandbox allow-scripts"  # not clobbered
    assert "x-frame-options" not in bindle                              # stays frameable
    assert bindle["referrer-policy"] == "strict-origin-when-cross-origin"
    print("app security-header self-check OK")
