"""Per-user object store (interface 3, decision #8).

Two backends behind one Protocol:
  * FilesystemBackend — dev default, writes under TOTO_GW_STORAGE_DIR. No cloud.
  * S3Backend — any S3-compatible endpoint (MinIO, AWS, R2, …) via a thin httpx AWS
    SigV4 signer. No boto3 (LiteLLM-malware / dep-discipline rule): ~150 LOC of hmac-sha256.

IDOR is code structure here, not policy: every method takes user_id and internally prefixes
the object key with the owner ("{user_id}/{key}"). One user's calls can NEVER name another
user's object — there is no code path that reaches an unscoped key. Keys are validated to
block path traversal (`..`, absolute paths) so the filesystem backend can't escape the owner
prefix either. See docs/security/2026-07-04-idor-hardening-report.md.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import os
from pathlib import Path
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

import httpx

from .config import Settings, get_settings

# --- key scoping / validation -------------------------------------------------

_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def _validate_segment(value: str, what: str) -> str:
    """Reject anything that could escape the owner prefix (traversal / absolute / NUL)."""
    if not value:
        raise ValueError(f"{what} must be non-empty")
    if value.startswith("/") or "\x00" in value or ".." in value.split("/"):
        raise ValueError(f"unsafe {what}: {value!r}")
    return value


def _scoped_key(user_id: str, key: str) -> str:
    """The owner-prefixed key. This is the ONLY key that ever hits a backend."""
    user_id = _validate_segment(str(user_id), "user_id")
    key = _validate_segment(key.lstrip("/"), "key")
    return f"{user_id}/{key}"


@runtime_checkable
class ObjectStore(Protocol):
    def put(self, user_id: str, key: str, data: bytes, content_type: str = "application/octet-stream") -> str: ...
    def get(self, user_id: str, key: str) -> bytes: ...
    def delete(self, user_id: str, key: str) -> None: ...
    def url(self, user_id: str, key: str) -> str: ...


# --- filesystem backend (dev) -------------------------------------------------


class FilesystemBackend:
    """Dev default. Objects live at {base}/{user_id}/{key}. url() returns a file:// ref —
    ponytail: dev-only; production reads serve bytes through the app via get()."""

    def __init__(self, base_dir: str):
        self.base = Path(base_dir).resolve()

    def _path(self, user_id: str, key: str) -> Path:
        p = (self.base / _scoped_key(user_id, key)).resolve()
        # Defense in depth: even with validation, assert the resolved path stays under base.
        if not (p == self.base or self.base in p.parents):
            raise ValueError("resolved path escapes storage root")
        return p

    def put(self, user_id, key, data, content_type="application/octet-stream") -> str:
        p = self._path(user_id, key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return _scoped_key(user_id, key)

    def get(self, user_id, key) -> bytes:
        p = self._path(user_id, key)
        if not p.is_file():
            raise FileNotFoundError(_scoped_key(user_id, key))
        return p.read_bytes()

    def delete(self, user_id, key) -> None:
        self._path(user_id, key).unlink(missing_ok=True)

    def url(self, user_id, key) -> str:
        return self._path(user_id, key).as_uri()


# --- AWS SigV4 signer (no boto3) ---------------------------------------------


def _uri_encode(value: str, *, encode_slash: bool = True) -> str:
    """RFC 3986 unreserved-preserving percent-encoding (AWS SigV4 flavour)."""
    out = []
    for b in value.encode("utf-8"):
        c = chr(b)
        if c.isalnum() or c in "-_.~":
            out.append(c)
        elif c == "/" and not encode_slash:
            out.append(c)
        else:
            out.append(f"%{b:02X}")
    return "".join(out)


def _canonical_query(params: dict[str, str]) -> str:
    items = sorted((_uri_encode(k), _uri_encode(v)) for k, v in params.items())
    return "&".join(f"{k}={v}" for k, v in items)


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret: str, date_stamp: str, region: str, service: str) -> bytes:
    k = _sign(("AWS4" + secret).encode("utf-8"), date_stamp)
    k = _sign(k, region)
    k = _sign(k, service)
    return _sign(k, "aws4_request")


def sigv4_signature(
    *,
    method: str,
    canonical_uri: str,
    query: dict[str, str],
    headers: dict[str, str],
    payload_hash: str,
    amz_date: str,
    region: str,
    service: str,
    access_key: str,
    secret_key: str,
) -> tuple[str, str]:
    """Core SigV4. Returns (authorization_header_value, hex_signature). Header-signing form.

    Generic on purpose so a known AWS test vector can drive it directly (see test_storage.py)."""
    date_stamp = amz_date[:8]
    lower = {k.lower(): v.strip() for k, v in headers.items()}
    signed_headers = ";".join(sorted(lower))
    canonical_headers = "".join(f"{k}:{lower[k]}\n" for k in sorted(lower))
    canonical_request = "\n".join(
        [method, canonical_uri, _canonical_query(query), canonical_headers, signed_headers, payload_hash]
    )
    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    signature = hmac.new(
        _signing_key(secret_key, date_stamp, region, service), string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    auth = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return auth, signature


# --- S3 backend ---------------------------------------------------------------


class S3Backend:
    """S3-compatible object store. Header-signed PUT/GET/DELETE; presigned (query-signed) GET
    URL for reads. Path-style for MinIO (default), virtual-host style for AWS."""

    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        region: str,
        access_key: str,
        secret_key: str,
        force_path_style: bool = True,
        service: str = "s3",
        timeout: float = 30.0,
    ):
        self.endpoint = endpoint.rstrip("/")
        self.bucket = bucket
        self.region = region
        self.access_key = access_key
        self.secret_key = secret_key
        self.force_path_style = force_path_style
        self.service = service
        self.timeout = timeout
        parts = urlsplit(self.endpoint)
        self.scheme = parts.scheme
        self._endpoint_host = parts.netloc  # includes :port

    def _loc(self, scoped_key: str) -> tuple[str, str, str]:
        """Return (host, canonical_uri, full_url) for an object, honouring path vs vhost style."""
        enc_key = _uri_encode(scoped_key, encode_slash=False)
        if self.force_path_style:
            host = self._endpoint_host
            uri = f"/{self.bucket}/{enc_key}"
        else:
            host = f"{self.bucket}.{self._endpoint_host}"
            uri = f"/{enc_key}"
        return host, uri, f"{self.scheme}://{host}{uri}"

    @staticmethod
    def _amz_date() -> str:
        return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    def _signed_headers(self, method: str, host: str, uri: str, payload_hash: str) -> dict[str, str]:
        amz_date = self._amz_date()
        headers = {"host": host, "x-amz-date": amz_date, "x-amz-content-sha256": payload_hash}
        auth, _ = sigv4_signature(
            method=method,
            canonical_uri=uri,
            query={},
            headers=headers,
            payload_hash=payload_hash,
            amz_date=amz_date,
            region=self.region,
            service=self.service,
            access_key=self.access_key,
            secret_key=self.secret_key,
        )
        headers["Authorization"] = auth
        return headers

    def put(self, user_id, key, data, content_type="application/octet-stream") -> str:
        scoped = _scoped_key(user_id, key)
        host, uri, url = self._loc(scoped)
        payload_hash = hashlib.sha256(data).hexdigest()
        headers = self._signed_headers("PUT", host, uri, payload_hash)
        headers["Content-Type"] = content_type  # not signed; S3 doesn't require it in the signature
        with httpx.Client(timeout=self.timeout) as c:
            r = c.put(url, content=data, headers=headers)
            r.raise_for_status()
        return scoped

    def get(self, user_id, key) -> bytes:
        scoped = _scoped_key(user_id, key)
        host, uri, url = self._loc(scoped)
        headers = self._signed_headers("GET", host, uri, _EMPTY_SHA256)
        with httpx.Client(timeout=self.timeout) as c:
            r = c.get(url, headers=headers)
            if r.status_code == 404:
                raise FileNotFoundError(scoped)
            r.raise_for_status()
            return r.content

    def delete(self, user_id, key) -> None:
        scoped = _scoped_key(user_id, key)
        host, uri, url = self._loc(scoped)
        headers = self._signed_headers("DELETE", host, uri, _EMPTY_SHA256)
        with httpx.Client(timeout=self.timeout) as c:
            r = c.delete(url, headers=headers)
            if r.status_code not in (200, 204, 404):
                r.raise_for_status()

    def url(self, user_id, key, *, expires: int = 3600) -> str:
        """Presigned GET URL (query-signed). Owner-scoped; the presigned key is unforgeable."""
        scoped = _scoped_key(user_id, key)
        host, uri, base_url = self._loc(scoped)
        amz_date = self._amz_date()
        scope = f"{amz_date[:8]}/{self.region}/{self.service}/aws4_request"
        query = {
            "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
            "X-Amz-Credential": f"{self.access_key}/{scope}",
            "X-Amz-Date": amz_date,
            "X-Amz-Expires": str(expires),
            "X-Amz-SignedHeaders": "host",
        }
        _, signature = sigv4_signature(
            method="GET",
            canonical_uri=uri,
            query=query,
            headers={"host": host},
            payload_hash="UNSIGNED-PAYLOAD",
            amz_date=amz_date,
            region=self.region,
            service=self.service,
            access_key=self.access_key,
            secret_key=self.secret_key,
        )
        return f"{base_url}?{_canonical_query(query)}&X-Amz-Signature={signature}"


# --- selection factory --------------------------------------------------------


def get_object_store(settings: Settings | None = None) -> ObjectStore:
    """FilesystemBackend when no S3 endpoint is configured, else S3Backend."""
    s = settings or get_settings()
    if s.s3_endpoint:
        return S3Backend(
            endpoint=s.s3_endpoint,
            bucket=s.s3_bucket,
            region=s.s3_region,
            access_key=s.s3_access_key,
            secret_key=s.s3_secret,
            force_path_style=s.s3_force_path_style,
        )
    return FilesystemBackend(s.storage_dir)


def org_store_from_config(cfg: dict, secret_key: str) -> S3Backend | None:
    """An org's private S3Backend from its stored connector row (BYOS), or None when the row is
    disabled/incomplete. Decrypts the write-only secret here — the one place ciphertext meets
    the client. Shared by the resolver below and the admin test endpoint."""
    from .credentials import decrypt  # lazy: credentials imports settings machinery

    if not (cfg and cfg.get("enabled") and cfg.get("s3_endpoint") and cfg.get("s3_bucket")):
        return None
    enc = cfg.get("s3_secret_enc") or ""
    secret = decrypt(secret_key, enc) if (enc and secret_key) else ""
    if not secret:  # fail closed: a connector without a usable secret is not a connector
        return None
    return S3Backend(
        endpoint=cfg["s3_endpoint"],
        bucket=cfg["s3_bucket"],
        region=cfg.get("s3_region") or "us-east-1",
        access_key=cfg.get("s3_access_key") or "",
        secret_key=secret,
        force_path_style=bool(cfg.get("s3_force_path_style", True)),
    )


class _OrgFallbackStore:
    """An org's private store with read-through to the platform store: writes/deletes/urls go to
    the org bucket, a get() that misses falls back to the platform store so objects written
    BEFORE the org connected their bucket stay readable. No migration, no orphans.
    ponytail: fallback on read only; bulk migration is a separate admin feature if ever needed."""

    def __init__(self, org_store: ObjectStore, platform_store: ObjectStore):
        self._org = org_store
        self._platform = platform_store

    def put(self, user_id, key, data, content_type="application/octet-stream") -> str:
        return self._org.put(user_id, key, data, content_type)

    def get(self, user_id, key) -> bytes:
        try:
            return self._org.get(user_id, key)
        except FileNotFoundError:
            return self._platform.get(user_id, key)

    def delete(self, user_id, key) -> None:
        self._org.delete(user_id, key)

    def url(self, user_id, key) -> str:
        return self._org.url(user_id, key)


# The AuthStore that owns org_storage_configs + memberships, registered once at app boot
# (app.create_app). Module-level for the same reason config.get_settings is: the write path
# (sessions_service.write_document, the companion's save_document tool) sits below the route
# plane and holds no app handle. None (dev/tests/offline) → platform store only.
_org_config_source = None


def set_org_config_source(auth) -> None:
    """Register the AuthStore the resolvers read org connectors + memberships from."""
    global _org_config_source
    _org_config_source = auth


async def resolve_object_store(auth, settings: Settings | None = None,
                               org_id: str | None = None) -> ObjectStore:
    """The org-aware selection: an enabled org connector (BYOS) wins, else the platform store
    (TOTO_GW_S3_* env, else filesystem). Config is read per call — object writes are per-save,
    not a hot path, and no cache means a console edit takes effect on the very next save.
    Degrades to the platform store on ANY config/decrypt problem: a broken connector must never
    fail a document save."""
    platform = get_object_store(settings)
    if not (auth and org_id):
        return platform
    try:
        cfg = await auth.get_org_storage_config(org_id)
        if cfg is None:
            return platform
        from .credentials import credentials_secret

        org_store = org_store_from_config(cfg, credentials_secret(settings or get_settings()))
        if org_store is None:
            return platform
        return _OrgFallbackStore(org_store, platform)
    except Exception:  # noqa: BLE001 — storage selection must never crash a save path
        return platform


async def resolve_store_for_user(user_id: str | None,
                                 settings: Settings | None = None) -> ObjectStore:
    """The store a USER's objects home to: their PRIMARY membership org's connector, else the
    platform store. Used by BOTH the write path (document saves) and the read routes so reads
    and writes always agree — the multi-org switcher changes control-plane context, never where
    a user's documents live. No auth source registered (dev/tests) → platform store."""
    auth = _org_config_source
    if not (auth and user_id):
        return get_object_store(settings)
    try:
        m = await auth.get_membership(user_id)
        org_id = (m or {}).get("org_id")
    except Exception:  # noqa: BLE001 — membership lookup failure must never fail a save
        org_id = None
    return await resolve_object_store(auth, settings, org_id)


if __name__ == "__main__":  # pragma: no cover — runnable self-check
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        fs = FilesystemBackend(d)
        ref = fs.put("userA", "docs/hello.txt", b"hi", "text/plain")
        assert ref == "userA/docs/hello.txt"
        assert fs.get("userA", "docs/hello.txt") == b"hi"
        # per-user scoping: B cannot read A's key
        try:
            fs.get("userB", "docs/hello.txt")
            raise SystemExit("IDOR: B read A's object")
        except FileNotFoundError:
            pass
        fs.delete("userA", "docs/hello.txt")
        try:
            fs.get("userA", "docs/hello.txt")
            raise SystemExit("delete failed")
        except FileNotFoundError:
            pass
        # traversal blocked (a leading "/" is normalized+scoped, not an escape — only ".." escapes)
        for bad in ("../evil", "a/../../b", "x/../../../etc/passwd"):
            try:
                fs.put("userA", bad, b"x")
                raise SystemExit(f"traversal not blocked: {bad}")
            except ValueError:
                pass
    print("FilesystemBackend self-check OK")
