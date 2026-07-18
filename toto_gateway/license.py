"""Offline-verifiable distribution license (W3-C7).

A customer running Toto inside their own perimeter cannot phone home, so entitlement is proven by a
signed token, not a license server. The operator sets `TOTO_GW_LICENSE_KEY` to a compact token that
Toto verifies at boot against a public key baked into THIS module (the matching private key lives
only with the vendor; see scripts/make_license.py). No network, no clock tricks — the only inputs are
the token and the local system time.

Token wire format (JWT-shaped, but Ed25519 only): `<b64url(claims_json)>.<b64url(signature)>`. The
signature covers the ASCII bytes of the first segment, so re-encoding can't smuggle claims past it.
Claims: {"org": str, "exp": "YYYY-MM-DD", "nbf"?: "YYYY-MM-DD", "ent"?: {..}}.

Posture — HARD-GATE WITH GRACE (Alex's ruling):
  * unlicensed dev/test (TOTO_GW_LICENSE_REQUIRED unset AND no key) → gate is skipped entirely; the
    OSS/dev experience is byte-identical to before.
  * valid, unexpired                       → serve.
  * valid, expired within GRACE_DAYS       → serve, but log a loud warning and surface it.
  * valid, past grace                      → REFUSE chat-plane traffic (503 license_expired). /healthz,
    /statusz and the admin console stay up so the operator can see and fix the license.
  * missing / invalid / not-yet-valid key when required → refuse (no grace — there is no trustworthy
    expiry to grant grace from).

Clock handling: system time only, no phone-home. A key whose `nbf` is in the future by less than
SKEW_HOURS is accepted anyway (clock skew between the vendor's signer and the customer's host). The
signature is verified ONCE at boot; the time-dependent verdict (grace → expired) is recomputed live
per request off the parsed dates, so a deploy that boots inside grace starts refusing at the grace
boundary with no restart.
"""

from __future__ import annotations

import base64
import binascii
import datetime as _dt
import json
import logging
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

log = logging.getLogger(__name__)

# Grace after the embedded expiry date during which the gateway keeps serving (with warnings) before
# it refuses. Clock-skew tolerance on a future notBefore. Both are fixed policy, not env-tunable.
GRACE_DAYS = 14
SKEW_HOURS = 48

# The vendor's Ed25519 public key, baked in. The matching PRIVATE key is never in this repo — it
# signs licenses via scripts/make_license.py from a key the operator supplies out-of-band.
_PUBLIC_KEY_PEM = b"""-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEASk2CHRutLB/GkKQ2B8V1mUzweENtAmmbxA4soeLK2rE=
-----END PUBLIC KEY-----
"""

_baked_public_key: Ed25519PublicKey | None = None


def public_key() -> Ed25519PublicKey:
    global _baked_public_key
    if _baked_public_key is None:
        _baked_public_key = serialization.load_pem_public_key(_PUBLIC_KEY_PEM)  # type: ignore[assignment]
    return _baked_public_key  # type: ignore[return-value]


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def b64url_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def sign(claims: dict, private_key) -> str:  # noqa: ANN001 — Ed25519PrivateKey, avoid import at call sites
    """Produce a license token from claims + an Ed25519 private key. Used by scripts/make_license.py
    (operator tool) and the tests. The signature covers the encoded claims segment's ASCII bytes."""
    payload = b64url_encode(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    sig = private_key.sign(payload.encode("ascii"))
    return f"{payload}.{b64url_encode(sig)}"


def _parse_date(v) -> _dt.date | None:  # noqa: ANN001
    if not isinstance(v, str):
        return None
    try:
        return _dt.date.fromisoformat(v)
    except ValueError:
        return None


@dataclass(frozen=True)
class LicenseStatus:
    """The boot-time verification result. Signature is checked once (`valid`); the serving verdict is
    time-dependent and computed live via `state()`/`blocked()` off the parsed dates."""

    required: bool
    valid: bool                      # signature verified against the baked key
    org: str | None = None
    expiry: _dt.date | None = None
    not_before: _dt.date | None = None
    entitlements: dict = field(default_factory=dict)
    error: str | None = None         # reason the token was rejected (bad_signature, malformed, ...)

    # ---- time-dependent verdict -------------------------------------------------------------
    def state(self, now: _dt.date | None = None) -> str:
        """One of: unlicensed | ok | grace | expired | not_yet_valid | missing | invalid."""
        now = now or _dt.datetime.now(_dt.UTC).date()
        if not self.valid:
            if not self.required and self.error == "missing":
                return "unlicensed"      # dev/OSS: no key, none required → gate skipped
            return self.error or "invalid"
        # notBefore with skew tolerance: a future nbf within SKEW_HOURS is accepted.
        if self.not_before is not None:
            skew = _dt.timedelta(hours=SKEW_HOURS)
            if _dt.datetime.combine(now, _dt.time()) < _dt.datetime.combine(self.not_before, _dt.time()) - skew:
                return "not_yet_valid"
        if self.expiry is None:
            return "invalid"
        if now <= self.expiry:
            return "ok"
        if now <= self.expiry + _dt.timedelta(days=GRACE_DAYS):
            return "grace"
        return "expired"

    def blocked(self, now: _dt.date | None = None) -> bool:
        """True → the chat plane must be refused. Only ever True when a license is REQUIRED. Allow-list
        the serving states (ok/grace/unlicensed); ANY other verdict — expired, missing, malformed,
        bad_signature, not_yet_valid — refuses. Fail-closed: a state we didn't foresee blocks."""
        if not self.required:
            return False
        return self.state(now) not in {"ok", "grace", "unlicensed"}

    def snapshot(self, now: _dt.date | None = None, *, include_org: bool = True) -> dict:
        st = self.state(now)
        out: dict = {"state": st, "required": self.required, "blocked": self.blocked(now)}
        if self.expiry is not None:
            out["expiry"] = self.expiry.isoformat()
            out["days_remaining"] = (self.expiry - (now or _dt.datetime.now(_dt.UTC).date())).days
        if include_org and self.org:
            out["org"] = self.org
        if include_org and self.entitlements:
            out["entitlements"] = self.entitlements
        return out


def verify(token: str, pub: Ed25519PublicKey | None = None) -> LicenseStatus:
    """Verify a token's signature against `pub` (default: the baked key) and parse its claims. Returns
    a LicenseStatus with valid=False + an error reason on any failure — never raises. `required` is
    left False here; the caller (evaluate) stamps it."""
    pub = pub or public_key()
    if not token:
        return LicenseStatus(required=False, valid=False, error="missing")
    try:
        payload_b64, sig_b64 = token.strip().split(".", 1)
        sig = _b64url_decode(sig_b64)
        pub.verify(sig, payload_b64.encode("ascii"))
    except (ValueError, binascii.Error):
        return LicenseStatus(required=False, valid=False, error="malformed")
    except InvalidSignature:
        return LicenseStatus(required=False, valid=False, error="bad_signature")
    try:
        claims = json.loads(_b64url_decode(payload_b64))
        if not isinstance(claims, dict):
            raise ValueError
    except (ValueError, binascii.Error):
        return LicenseStatus(required=False, valid=False, error="malformed")
    expiry = _parse_date(claims.get("exp"))
    if expiry is None:
        return LicenseStatus(required=False, valid=False, error="malformed")
    ent = claims.get("ent") if isinstance(claims.get("ent"), dict) else {}
    return LicenseStatus(
        required=False, valid=True,
        org=str(claims["org"]) if claims.get("org") else None,
        expiry=expiry, not_before=_parse_date(claims.get("nbf")), entitlements=ent,
    )


def evaluate(settings, *, pub: Ed25519PublicKey | None = None) -> LicenseStatus:  # noqa: ANN001
    """Boot-time entry point: read TOTO_GW_LICENSE_KEY, verify once, stamp `required`, and log the
    posture loudly. Returns a LicenseStatus stored on app.state for the middleware + status endpoints."""
    required = bool(getattr(settings, "license_required", False))
    st = verify(getattr(settings, "license_key", "") or "", pub=pub)
    # Re-stamp `required` (verify() can't know it): frozen dataclass → rebuild.
    st = LicenseStatus(required=required, valid=st.valid, org=st.org, expiry=st.expiry,
                       not_before=st.not_before, entitlements=st.entitlements, error=st.error)
    state = st.state()
    if state == "unlicensed":
        log.info("license: no key set and none required — running unlicensed (dev/OSS mode)")
    elif state == "ok":
        log.info("license: valid for org=%s until %s", st.org, st.expiry)
    elif state == "grace":
        log.warning("LICENSE EXPIRED on %s but within %d-day grace — org=%s. Renew now; the gateway "
                    "will refuse traffic after grace.", st.expiry, GRACE_DAYS, st.org)
    elif required:
        log.error("LICENSE %s — chat-plane traffic will be REFUSED (503). Set a valid "
                  "TOTO_GW_LICENSE_KEY. /healthz, /statusz and /console stay up.", state.upper())
    else:  # invalid/missing key but not required → warn, keep serving
        log.warning("license: key present but %s (not required — still serving)", state)
    return st
