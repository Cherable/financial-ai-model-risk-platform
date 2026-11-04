"""Replaceable boundaries for member B's storage and identity implementations."""

from dataclasses import dataclass
from typing import Protocol


class DomainError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message
        super().__init__(message)


class StorageUnavailable(DomainError):
    def __init__(self):
        super().__init__(503, "STORAGE_UNAVAILABLE", "Result was not committed; retry with the same idempotency key.")


@dataclass(frozen=True)
class Principal:
    principal_id: str
    roles: frozenset[str]
    model_ids: frozenset[str]


@dataclass(frozen=True)
class StoredResponse:
    body: dict
    http_status: int
    replayed: bool = False


class Authenticator(Protocol):
    def authenticate(self, token: str) -> Principal: ...


class PredictionStore(Protocol):
    def initialize(self, versions: list[dict]) -> None: ...
    def close(self) -> None: ...
    def healthy(self) -> bool: ...
    def version(self, version_id: str) -> dict: ...
    def versions(self, model_ids: frozenset[str]) -> list[dict]: ...
    def previous(self, actor: str, route: str, key: str, digest: str) -> StoredResponse | None: ...
    def save_prediction(self, command: dict) -> StoredResponse: ...
    def activate(self, command: dict) -> StoredResponse: ...
