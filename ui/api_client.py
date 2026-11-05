"""Session-scoped HTTP boundary. No database access or automatic POST retries."""

import json
from urllib.parse import urlsplit
from uuid import uuid4

import httpx


class ApiError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code
        # Never echo request bodies, tokens, raw HTTP exceptions or HTML responses.
        super().__init__(f"API 请求未完成（HTTP {status or '—'} / {code}）")


class ApiClient:
    def __init__(self, base_url, token, *, transport=None):
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("API_BASE_URL must be a trusted HTTP(S) server URL")
        self._base_url, self._token, self._transport = base_url.rstrip("/"), token, transport

    def request(self, method, path, *, params=None, body=None, key=None):
        if not path.startswith("/api/v1/") or ".." in path or "?" in path or "#" in path:
            raise ValueError("Only fixed API paths are supported")
        headers = {"Authorization": f"Bearer {self._token}"}
        if method == "POST":
            if not key:
                raise ValueError("Mutations require an idempotency key")
            headers["Idempotency-Key"] = key
        try:
            with httpx.Client(base_url=self._base_url, timeout=httpx.Timeout(10, connect=3),
                              trust_env=False, follow_redirects=False, transport=self._transport) as client:
                response = client.request(method, path, params=params, json=body, headers=headers)
        except httpx.RequestError:
            raise ApiError(None, "NETWORK_UNAVAILABLE") from None
        if not response.is_success:
            allowed = {401: "UNAUTHENTICATED", 403: "FORBIDDEN", 404: "NOT_FOUND",
                       409: "STATE_OR_KEY_CONFLICT", 422: "INVALID_REQUEST", 503: "SERVICE_UNAVAILABLE"}
            raise ApiError(response.status_code, allowed.get(response.status_code, "HTTP_ERROR"))
        try:
            return response.json()
        except ValueError:
            raise ApiError(response.status_code, "INVALID_RESPONSE") from None

    def page(self, path, version_id, offset=0, limit=30):
        return self.request("GET", path, params={"model_version_id": version_id, "offset": offset, "limit": limit})["items"]


def command_key(state, path, body):
    """Retain the pending command per operation across uncertain network outcomes."""
    fingerprint = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    commands = state.setdefault("commands", {})
    if path not in commands or commands[path][0] != fingerprint:
        commands[path] = (fingerprint, str(uuid4()))
    return commands[path][1]
