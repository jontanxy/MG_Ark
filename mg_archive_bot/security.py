from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 5  # OWASP: N=2^14 (16 MiB), r=8, p=5
_DKLEN = 32

TOKEN_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I ambiguity
TOKEN_PREFIX = "MG-"

MIN_PASSWORD_LENGTH = 12
# Values that ship in .env.example / documentation or are too common to be a shared secret.
PLACEHOLDER_PASSWORDS = frozenset(
    {"change-me-please", "changeme", "change-me", "password", "password1", "password123", "123456789012", "qwertyuiop12", "letmein12345"}
)


class WeakPasswordError(ValueError):
    pass


def validate_new_password(password: str) -> str:
    """Return *password* if it is acceptable as the shared access password, else raise WeakPasswordError."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise WeakPasswordError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    lowered = password.lower()
    if lowered in PLACEHOLDER_PASSWORDS or lowered.replace(" ", "") in PLACEHOLDER_PASSWORDS:
        raise WeakPasswordError("That password is a well-known placeholder; choose a real passphrase.")
    if len(set(lowered)) < 4:
        raise WeakPasswordError("Password is too repetitive; choose a real passphrase.")
    return password


def hash_password(password: str) -> str:
    if not password:
        raise ValueError("password must not be empty")
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_DKLEN)
    return "scrypt${}${}${}${}${}".format(
        _SCRYPT_N,
        _SCRYPT_R,
        _SCRYPT_P,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(dk).decode("ascii"),
    )


def verify_password(password: str, stored: str | None) -> bool:
    if not stored or not password:
        return False
    try:
        algo, n, r, p, salt_b64, dk_b64 = stored.split("$")
        if algo != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def generate_provisioning_token() -> str:
    body = "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(8))
    return f"{TOKEN_PREFIX}{body[:4]}-{body[4:]}"


def normalise_token(raw: str) -> str:
    cleaned = "".join(ch for ch in raw.upper() if ch.isalnum())
    if cleaned.startswith("MG"):
        cleaned = cleaned[2:]
    if len(cleaned) != 8:
        return ""
    return f"{TOKEN_PREFIX}{cleaned[:4]}-{cleaned[4:]}"
