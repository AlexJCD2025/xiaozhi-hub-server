"""管理后台的密码哈希与登录会话。"""

import base64
import hashlib
import hmac
import secrets
import time

_SCRYPT = {"n": 2**14, "r": 8, "p": 1}
MIN_PASSWORD_LENGTH = 6


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
    except (ValueError, AttributeError):
        return False
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)
    return hmac.compare_digest(digest, expected)


class SessionStore:
    """内存中的登录会话；服务重启后需要重新登录。"""

    def __init__(self, ttl_seconds: int = 12 * 3600):
        self.ttl = ttl_seconds
        self._sessions = {}

    def create(self) -> str:
        self._purge()
        token = secrets.token_urlsafe(32)
        self._sessions[token] = time.time() + self.ttl
        return token

    def valid(self, token) -> bool:
        if not token:
            return False
        expiry = self._sessions.get(token)
        if expiry is None or expiry < time.time():
            self._sessions.pop(token, None)
            return False
        return True

    def revoke(self, token) -> None:
        self._sessions.pop(token, None)

    def revoke_all(self) -> None:
        self._sessions.clear()

    def _purge(self) -> None:
        now = time.time()
        for token in [t for t, exp in self._sessions.items() if exp < now]:
            del self._sessions[token]
