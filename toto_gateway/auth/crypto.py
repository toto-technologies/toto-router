"""Password hashing and token digests. No auth/crypto library: `hashlib.scrypt` for passwords,
`secrets` for randomness, `hmac.compare_digest` for every compare."""

from __future__ import annotations

import hashlib
import hmac
import secrets

# scrypt params (OWASP-acceptable, memory-hard); stored self-describing so they can be raised
# later and stale hashes re-hashed on login. n must be a power of two.
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _SCRYPT_DKLEN = 2**14, 8, 1, 32


def hash_password(password: str) -> str:
    """`scrypt$<n>$<r>$<p>$<salt_hex>$<hash_hex>` — self-describing so params live with the hash."""
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
                        dklen=_SCRYPT_DKLEN)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verify against a self-describing scrypt string. False on any malformation."""
    try:
        algo, n, r, p, salt_hex, hash_hex = stored.split("$")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex),
                            n=int(n), r=int(r), p=int(p), dklen=len(hash_hex) // 2)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), hash_hex)


# A well-formed hash to burn on unknown-email login so the response time doesn't leak account
# existence. Computed once at import (same params as a real verify).
_DUMMY_HASH = hash_password("toto-dummy-password-for-timing")


def burn_dummy_hash() -> None:
    """Spend a scrypt on a dummy hash — call on unknown-email login to equalize timing."""
    verify_password("wrong", _DUMMY_HASH)


def _token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()
