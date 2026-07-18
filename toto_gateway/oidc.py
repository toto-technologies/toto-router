"""OIDC relying-party primitives — discovery, JWKS, RS256 ID-token verify, PKCE, code exchange.

Zero new deps: `cryptography` (already vendored for Fernet) does RS256 verify by hand (~40 lines),
`hashlib`/`secrets`/`base64` do PKCE, and the house `httpx` client does the two HTTP calls. No JWT
library is pulled in — a maintained one buys nothing over this for a single, well-scoped flow.

The discovery doc and JWKS are cached in-process with a TTL (keyed by URL); a `kid` miss forces one
JWKS re-fetch to ride out key rotation. HTTP calls take a `transport` (httpx.BaseTransport) DI seam —
None in prod (real network), a MockTransport in tests — the same seam TotoClient uses. See W1-C6.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from urllib.parse import urlencode

import httpx

# in-process caches: {url: (value, expires_at)}. reset_caches() drops them (tests/rotation).
_DISCOVERY: dict[str, tuple[dict, float]] = {}
_JWKS: dict[str, tuple[dict, float]] = {}
_TTL = 3600.0  # discovery + JWKS live an hour; a kid miss forces an early JWKS re-fetch anyway
_HTTP_TIMEOUT = 8.0


class OIDCError(Exception):
    """Any relying-party failure (discovery, exchange, or token validation). `reason` is a short
    machine code (bad_signature, bad_iss, bad_aud, expired, bad_nonce, ...) — never IdP secrets."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def reset_caches() -> None:
    """Drop the discovery + JWKS caches (test/rotation helper)."""
    _DISCOVERY.clear()
    _JWKS.clear()


def _b64url_decode(s: str) -> bytes:
    """Decode base64url with the padding the encoder stripped (JWT/JWK convention)."""
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _b64url_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


# --- PKCE (RFC 7636, S256) --------------------------------------------------------------------

def new_pkce() -> tuple[str, str]:
    """(code_verifier, code_challenge). Verifier is a high-entropy secret held server-side in the
    login-state row; the challenge (S256 of it) rides the authorize redirect."""
    verifier = secrets.token_urlsafe(48)
    challenge = _b64url_encode(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def new_state() -> str:
    return secrets.token_urlsafe(24)


def new_nonce() -> str:
    return secrets.token_urlsafe(24)


# --- HTTP: discovery, JWKS, token exchange ----------------------------------------------------

async def _get_json(url: str, transport) -> dict:
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, transport=transport) as c:
        r = await c.get(url)
    if r.status_code != 200:
        raise OIDCError("http_error")
    return r.json()


async def discover(issuer: str, transport=None) -> dict:
    """The IdP's OpenID configuration (cached). Raises OIDCError if unreachable/non-200."""
    url = issuer.rstrip("/") + "/.well-known/openid-configuration"
    hit = _DISCOVERY.get(url)
    if hit is not None and hit[1] > time.time():
        return hit[0]
    doc = await _get_json(url, transport)
    _DISCOVERY[url] = (doc, time.time() + _TTL)
    return doc


async def _jwks(jwks_uri: str, transport=None, *, force: bool = False) -> dict:
    hit = _JWKS.get(jwks_uri)
    if not force and hit is not None and hit[1] > time.time():
        return hit[0]
    doc = await _get_json(jwks_uri, transport)
    _JWKS[jwks_uri] = (doc, time.time() + _TTL)
    return doc


async def exchange_code(token_endpoint: str, *, code: str, redirect_uri: str, client_id: str,
                        client_secret: str, code_verifier: str, transport=None) -> dict:
    """Swap the authorization code for tokens at the IdP token endpoint (client_secret + PKCE
    verifier). Returns the token response dict (must carry `id_token`). Raises OIDCError on failure."""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "client_secret": client_secret,
        "code_verifier": code_verifier,
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, transport=transport) as c:
        r = await c.post(token_endpoint, data=data,
                         headers={"Accept": "application/json"})
    if r.status_code != 200:
        raise OIDCError("token_exchange_failed")
    body = r.json()
    if not body.get("id_token"):
        raise OIDCError("no_id_token")
    return body


# --- RS256 ID-token verification (hand-rolled on `cryptography`) -------------------------------

def _verify_rs256(signing_input: bytes, signature: bytes, jwk: dict) -> None:
    """Verify an RS256 signature against an RSA JWK ({n, e}). Raises InvalidSignature on mismatch."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    n = int.from_bytes(_b64url_decode(jwk["n"]), "big")
    e = int.from_bytes(_b64url_decode(jwk["e"]), "big")
    pub = rsa.RSAPublicNumbers(e, n).public_key()
    pub.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())


def _find_key(jwks: dict, kid: str | None) -> dict | None:
    keys = [k for k in jwks.get("keys", []) if k.get("kty") == "RSA"]
    if kid is not None:
        for k in keys:
            if k.get("kid") == kid:
                return k
        return None
    return keys[0] if len(keys) == 1 else None  # no kid: only unambiguous when there's exactly one


def _verify_claims(id_token: str, jwk: dict, issuer: str, client_id: str,
                   nonce: str | None, leeway: float) -> dict:
    try:
        header_b64, payload_b64, sig_b64 = id_token.split(".")
    except ValueError as exc:
        raise OIDCError("malformed_token") from exc
    signing_input = f"{header_b64}.{payload_b64}".encode()
    try:
        from cryptography.exceptions import InvalidSignature
        _verify_rs256(signing_input, _b64url_decode(sig_b64), jwk)
    except InvalidSignature as exc:
        raise OIDCError("bad_signature") from exc
    except Exception as exc:  # noqa: BLE001 — malformed JWK/sig bytes are a rejection, not a 500
        raise OIDCError("bad_signature") from exc
    claims = json.loads(_b64url_decode(payload_b64))
    if claims.get("iss") != issuer:
        raise OIDCError("bad_iss")
    aud = claims.get("aud")
    auds = aud if isinstance(aud, list) else [aud]
    if client_id not in auds:
        raise OIDCError("bad_aud")
    if time.time() > float(claims.get("exp", 0)) + leeway:
        raise OIDCError("expired")
    if nonce is not None and claims.get("nonce") != nonce:
        raise OIDCError("bad_nonce")
    return claims


async def verify_id_token(id_token: str, *, issuer: str, client_id: str, nonce: str | None,
                          jwks_uri: str, transport=None, leeway: float = 60.0) -> dict:
    """Full ID-token check: RS256 signature (against the IdP JWKS, refetched once on a kid miss to
    ride key rotation), then iss/aud/exp/nonce. Returns the verified claims or raises OIDCError."""
    try:
        header = json.loads(_b64url_decode(id_token.split(".")[0]))
    except Exception as exc:  # noqa: BLE001
        raise OIDCError("malformed_token") from exc
    if header.get("alg") != "RS256":  # only RS256 supported (the ~universal OIDC default)
        raise OIDCError("unsupported_alg")
    kid = header.get("kid")
    jwk = _find_key(await _jwks(jwks_uri, transport), kid)
    if jwk is None:
        jwk = _find_key(await _jwks(jwks_uri, transport, force=True), kid)
    if jwk is None:
        raise OIDCError("unknown_kid")
    return _verify_claims(id_token, jwk, issuer, client_id, nonce, leeway)


def build_authorize_url(authorization_endpoint: str, *, client_id: str, redirect_uri: str,
                        state: str, nonce: str, code_challenge: str,
                        scope: str = "openid email profile") -> str:
    """The IdP authorize redirect (authorization-code + PKCE S256)."""
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    sep = "&" if "?" in authorization_endpoint else "?"
    return f"{authorization_endpoint}{sep}{urlencode(params)}"


def demo() -> None:
    """Self-check: sign a real RS256 JWT with a fresh key, verify it, and prove tamper + bad-aud +
    expiry + nonce mismatch each reject. No network — the JWK is built locally."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = key.public_key().public_numbers()
    jwk = {"kty": "RSA", "kid": "k1",
           "n": _b64url_encode(pub.n.to_bytes((pub.n.bit_length() + 7) // 8, "big")),
           "e": _b64url_encode(pub.e.to_bytes((pub.e.bit_length() + 7) // 8, "big"))}

    def _sign(claims: dict, kid: str = "k1", alg: str = "RS256") -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        h = _b64url_encode(json.dumps({"alg": alg, "kid": kid}).encode())
        p = _b64url_encode(json.dumps(claims).encode())
        sig = key.sign(f"{h}.{p}".encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{h}.{p}.{_b64url_encode(sig)}"

    iss, cid = "https://idp.example", "client-123"
    good = _sign({"iss": iss, "aud": cid, "exp": time.time() + 300, "nonce": "N",
                  "sub": "u1", "email": "a@x.com"})
    claims = _verify_claims(good, jwk, iss, cid, "N", 60)
    assert claims["sub"] == "u1" and claims["email"] == "a@x.com"
    # aud as a list containing the client id still passes
    _verify_claims(_sign({"iss": iss, "aud": [cid, "other"], "exp": time.time() + 300, "nonce": "N"}),
                   jwk, iss, cid, "N", 60)

    def _rejects(token, reason, *, nonce="N"):
        try:
            _verify_claims(token, jwk, iss, cid, nonce, 60)
        except OIDCError as e:
            assert e.reason == reason, (e.reason, reason)
        else:
            raise AssertionError(f"expected {reason}")

    tampered = good[:-4] + ("aaaa" if good[-4:] != "aaaa" else "bbbb")
    _rejects(tampered, "bad_signature")
    _rejects(_sign({"iss": "https://evil.example", "aud": cid, "exp": time.time() + 300}), "bad_iss")
    _rejects(_sign({"iss": iss, "aud": "someone-else", "exp": time.time() + 300}), "bad_aud")
    _rejects(_sign({"iss": iss, "aud": cid, "exp": time.time() - 3600}), "expired")
    _rejects(_sign({"iss": iss, "aud": cid, "exp": time.time() + 300, "nonce": "WRONG"}), "bad_nonce")

    v, c = new_pkce()
    assert _b64url_encode(hashlib.sha256(v.encode()).digest()) == c
    print("oidc demo ok")


if __name__ == "__main__":
    demo()
