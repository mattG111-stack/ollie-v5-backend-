"""Per-user LLM credentials — stored encrypted, never returned to the client.

The key belongs to the user, not the deployment: they add it in Settings, it
persists, and it is used only to answer their own questions.

Encryption uses Fernet with a key derived from the app's existing JWT secret,
so there is no second secret to provision. That means rotating `jwt_secret`
makes stored API keys unreadable — decryption fails closed and the user is
asked to re-enter, which is the right failure mode for a credential.

The plaintext key is only ever held in memory for the duration of one request.
The API exposes whether a key is set and its last four characters; the value
itself has no read path.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from ..config import settings

PROVIDERS = ("anthropic", "openai")


def _fernet() -> Fernet:
    # Fernet needs a 32-byte urlsafe-base64 key; the JWT secret is an arbitrary
    # string, so hash it to the right shape.
    digest = hashlib.sha256(settings.jwt_secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(api_key: str) -> str:
    return _fernet().encrypt(api_key.encode()).decode()


def decrypt(stored: str | None) -> str | None:
    """Return the plaintext key, or None if it can't be read.

    Fails closed: a rotated jwt_secret or corrupted value yields None rather
    than an exception, so the caller reports "re-enter your key" instead of 500.
    """
    if not stored:
        return None
    try:
        return _fernet().decrypt(stored.encode()).decode()
    except (InvalidToken, ValueError):
        return None


def last_four(stored: str | None) -> str | None:
    """Enough for the user to recognise which key is saved. Never the whole thing."""
    plain = decrypt(stored)
    return plain[-4:] if plain and len(plain) >= 4 else None


def looks_valid(provider: str, api_key: str) -> str | None:
    """Cheap shape check so an obvious paste error is caught before a 401.

    Returns an error message, or None when the key looks plausible. This is a
    format check only — whether the key actually works is proven by the test
    call in the settings endpoint.
    """
    key = api_key.strip()
    if len(key) < 20:
        return "That key looks too short."
    if provider == "anthropic" and not key.startswith("sk-ant-"):
        return "Anthropic keys start with 'sk-ant-'."
    if provider == "openai" and not key.startswith("sk-"):
        return "OpenAI keys start with 'sk-'."
    return None
