"""访问令牌的签发与校验。

令牌只携带 user_id，角色与模型授权每次从数据库读取，因此撤销立即生效。
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt

from app.contracts import DomainError
from app.modules.monitoring.time_utils import stamp

ALGORITHM = "HS256"


def issue_token(user_id: str, secret: str, ttl_minutes: int, *, now: datetime | None = None):
    """返回 (token, expires_at 字符串)。"""
    if not secret:
        raise ValueError("JWT secret is required to issue tokens")
    issued = now or datetime.now(timezone.utc)
    expires = issued + timedelta(minutes=ttl_minutes)
    payload = {
        "sub": user_id,
        "iat": int(issued.timestamp()),
        "exp": int(expires.timestamp()),
        "jti": str(uuid4()),
    }
    return jwt.encode(payload, secret, algorithm=ALGORITHM), stamp(expires)


def read_user_id(token: str, secret: str) -> str:
    """校验签名与有效期，返回 user_id；任何失败统一按未认证处理。"""
    if not token or not secret:
        raise DomainError(401, "UNAUTHENTICATED", "A valid bearer token is required.")
    try:
        payload = jwt.decode(token, secret, algorithms=[ALGORITHM],
                             options={"require": ["exp", "sub"]})
    except jwt.PyJWTError:
        raise DomainError(401, "UNAUTHENTICATED", "A valid bearer token is required.") from None
    subject = payload.get("sub")
    if not isinstance(subject, str) or not subject:
        raise DomainError(401, "UNAUTHENTICATED", "A valid bearer token is required.")
    return subject
