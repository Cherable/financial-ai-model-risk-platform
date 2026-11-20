"""Server-configured bearer tokens for A3; B can replace this with JWT/RBAC."""

import hmac
from uuid import NAMESPACE_URL, uuid5

from app.contracts import DomainError, Principal

DEMO_MODEL_ID = str(uuid5(NAMESPACE_URL, "financial-ai:credit-risk"))


class DemoTokenAuthenticator:
    def __init__(self, admin_token: str, scorer_token: str):
        if not all(isinstance(t, str) and len(t) >= 32 for t in (admin_token, scorer_token)):
            raise ValueError("APP_ADMIN_TOKEN and APP_SCORER_TOKEN must each contain at least 32 characters")
        if hmac.compare_digest(admin_token.encode("utf-8"), scorer_token.encode("utf-8")):
            raise ValueError("admin and scorer tokens must be different")
        self._tokens = [
            (admin_token, Principal(str(uuid5(NAMESPACE_URL, "financial-ai:demo-admin")),
                                    frozenset({"admin", "scorer"}), frozenset({DEMO_MODEL_ID}))),
            (scorer_token, Principal(str(uuid5(NAMESPACE_URL, "financial-ai:demo-scorer")),
                                     frozenset({"scorer"}), frozenset({DEMO_MODEL_ID}))),
        ]

    def authenticate(self, token: str) -> Principal:
        for known, principal in self._tokens:
            if hmac.compare_digest(token.encode("utf-8"), known.encode("utf-8")):
                return principal
        raise DomainError(401, "UNAUTHENTICATED", "A valid bearer token is required.")
