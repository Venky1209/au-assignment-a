"""PII protection at rest.

Two keys, two jobs:
  * HMAC key   -> blind index. Deterministic, so the same phone always maps to the same token and
                  events can be joined to leads without storing the number. Keyed (not plain SHA-256)
                  because ~10^10 Indian mobiles are trivially brute-forced from an unkeyed hash.
  * AES key    -> AES-256-GCM with a random nonce for values we may need back (names, phone).
                  Randomised, so ciphertext cannot be joined on or compared.

Keys come from the environment / a local .env (never committed). In production they'd live in a KMS.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ENC_VAR, HMAC_VAR = "LEDGER_ENC_KEY", "LEDGER_HMAC_KEY"


def _read_dotenv(path: Path) -> dict[str, str]:
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


class Keys:
    def __init__(self, enc_key: bytes, hmac_key: bytes):
        if len(enc_key) != 32 or len(hmac_key) < 32:
            raise ValueError("LEDGER_ENC_KEY must be 32 bytes and LEDGER_HMAC_KEY >= 32 bytes (base64)")
        if enc_key == hmac_key:
            raise ValueError("encryption and HMAC keys must differ")
        self._aes = AESGCM(enc_key)
        self._hmac_key = hmac_key

    @classmethod
    def load(cls, env_file: Path = Path(".env"), create: bool = True) -> "Keys":
        env = {**_read_dotenv(env_file), **{k: v for k, v in os.environ.items() if k in (ENC_VAR, HMAC_VAR)}}
        if ENC_VAR not in env or HMAC_VAR not in env:
            if not create:
                raise RuntimeError(f"{ENC_VAR}/{HMAC_VAR} not set")
            env[ENC_VAR] = base64.b64encode(secrets.token_bytes(32)).decode()
            env[HMAC_VAR] = base64.b64encode(secrets.token_bytes(32)).decode()
            with open(env_file, "a", encoding="utf-8") as fh:
                fh.write(f"{ENC_VAR}={env[ENC_VAR]}\n{HMAC_VAR}={env[HMAC_VAR]}\n")
            print(f"[ledger] generated new keys in {env_file} (keep this file; it is gitignored)")
        return cls(base64.b64decode(env[ENC_VAR]), base64.b64decode(env[HMAC_VAR]))

    def blind_index(self, canonical_phone: str) -> str:
        return hmac.new(self._hmac_key, canonical_phone.encode(), hashlib.sha256).hexdigest()

    def fingerprint(self) -> str:
        """Stored in the DB so we refuse to mix data indexed under different HMAC keys."""
        return hashlib.sha256(b"fp:" + self._hmac_key).hexdigest()[:16]

    def encrypt(self, obj: dict, aad: str) -> str:
        """aad binds the ciphertext to its row, so a blob copied onto another lead fails to decrypt."""
        nonce = secrets.token_bytes(12)
        ct = self._aes.encrypt(nonce, json.dumps(obj, sort_keys=True).encode(), aad.encode())
        return base64.b64encode(nonce + ct).decode()

    def decrypt(self, blob: str, aad: str) -> dict:
        raw = base64.b64decode(blob)
        return json.loads(self._aes.decrypt(raw[:12], raw[12:], aad.encode()))
