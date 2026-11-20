"""登录与令牌认证。

认证器每次请求都按 user_id 查库，角色与模型授权以数据库为准，
因此停用账号或撤销授权对已签发但未过期的令牌立即生效。
"""

from datetime import datetime

from app.auth.passwords import hash_password, verify_password
from app.auth.store import AuthStore, check_password
from app.auth.tokens import issue_token, read_user_id
from app.contracts import DomainError, Principal

# 用户不存在时也做一次同等成本的校验，避免通过响应时间枚举用户名。
_DUMMY_HASH = hash_password("timing-equalizer-not-a-real-password")

INVALID_CREDENTIALS = "Invalid username or password."


class JwtAuthService:
    def __init__(self, store: AuthStore, secret: str, ttl_minutes: int = 30):
        if not secret:
            raise ValueError("JWT secret must not be empty")
        self.store, self.secret, self.ttl_minutes = store, secret, ttl_minutes

    def login(self, username: str, password: str, *, now: datetime | None = None) -> dict:
        user = self.store.user_by_username(username)
        if user is None:
            verify_password(_DUMMY_HASH, password or "")
            self.store.record_login("anonymous", username, False, "unknown_user")
            raise DomainError(401, "UNAUTHENTICATED", INVALID_CREDENTIALS)
        if not check_password(user, password or ""):
            self.store.record_login("anonymous", username, False, "bad_password")
            raise DomainError(401, "UNAUTHENTICATED", INVALID_CREDENTIALS)
        if user["status"] != "active":
            self.store.record_login(user["user_id"], username, False, "disabled")
            raise DomainError(401, "UNAUTHENTICATED", INVALID_CREDENTIALS)
        token, expires_at = issue_token(user["user_id"], self.secret, self.ttl_minutes, now=now)
        self.store.record_login(user["user_id"], username, True)
        return {"access_token": token, "token_type": "Bearer", "expires_at": expires_at}

    def authenticate(self, token: str) -> Principal:
        user_id = read_user_id(token, self.secret)
        principal = self.store.principal(user_id)
        if principal is None:
            raise DomainError(401, "UNAUTHENTICATED", "A valid bearer token is required.")
        return principal
