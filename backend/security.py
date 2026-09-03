from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from urllib.parse import urlsplit, urlunsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet, InvalidToken


USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,64}$")
_password_hasher = PasswordHasher()


def validate_username(value: str) -> str:
    value = value.strip()
    if not USERNAME_RE.fullmatch(value):
        raise ValueError("用户名须为 3-64 位字母、数字或 . _ -")
    return value


def validate_password(value: str) -> str:
    if not 8 <= len(value) <= 128:
        raise ValueError("密码长度须为 8-128 位")
    return value


def hash_password(value: str) -> str:
    return _password_hasher.hash(validate_password(value))


def verify_password(hashed: str, value: str) -> bool:
    try:
        return _password_hasher.verify(hashed, value)
    except (VerificationError, InvalidHashError):
        return False


def random_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class SecretCipher:
    def __init__(self, secret: str):
        key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
        self.fernet = Fernet(key)

    def encrypt(self, value: str) -> str:
        return self.fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        try:
            return self.fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            raise RuntimeError("敏感配置无法解密，请检查 APP_ENCRYPTION_KEY") from exc


def redact_rtsp(url: str) -> str:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        if parsed.username:
            host = f"***:***@{host}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except Exception:
        return "<redacted-video-source>"


def sign_webhook(secret: str, timestamp: str, payload: dict) -> str:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    digest = hmac.new(secret.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256)
    return f"sha256={digest.hexdigest()}"

