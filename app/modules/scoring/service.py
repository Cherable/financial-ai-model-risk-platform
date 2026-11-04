"""A3 request orchestration, independent of a specific auth or storage backend."""

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from app.contracts import DomainError, PredictionStore, Principal
from app.demo_auth import DEMO_MODEL_ID
from app.modules.models.bundle import load_bundle, load_registered
from app.modules.models.schema import normalize_input
from app.modules.scoring.predict import score_one

PREDICTION_ROUTE = "/api/v1/predictions"
ACTIVATION_ROUTE = "/api/v1/model-versions/activate"


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def json_digest(value):
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class PredictionFailure(Exception):
    def __init__(self, code: str, timing: dict | None = None):
        self.code, self.timing = code, timing or {}
        super().__init__(code)


class LocalModelPredictor:
    def __init__(self, root: Path, inference_timeout_ms=2000.0):
        if not math.isfinite(inference_timeout_ms) or inference_timeout_ms <= 0:
            raise ValueError("inference timeout must be finite and positive")
        self.root, self.inference_timeout_ms = Path(root), inference_timeout_ms

    def descriptors(self):
        registry = self.root / "trusted_registry.json"
        if registry.is_symlink():
            raise ValueError("registry must not be a symlink")
        names = json.loads(registry.read_text(encoding="utf-8"))["versions"]
        result = []
        for name in sorted(names):
            bundle = load_registered(name, self.root)
            result.append({"model_version_id": str(uuid5(NAMESPACE_URL, "financial-ai:model-version:" + name)),
                           "model_id": DEMO_MODEL_ID, "version": name, "manifest_sha256": bundle.manifest_sha256})
        if not result:
            raise ValueError("the trusted registry has no model versions")
        return result

    def validate(self, version):
        try:
            return load_bundle(version["version"], version["manifest_sha256"], self.root)
        except Exception as exc:
            code = "MODEL_ENVIRONMENT_MISMATCH" if str(exc).startswith("environment mismatch") else "MODEL_PACKAGE_UNAVAILABLE"
            raise PredictionFailure(code) from exc

    def score(self, version, raw):
        bundle = self.validate(version)
        timing = {}
        try:
            result = score_one(bundle, raw, timing=timing)
            if not math.isfinite(result["risk_probability"]) or not 0 <= result["risk_probability"] <= 1:
                raise ValueError("invalid probability")
            if timing["inference_ms"] > self.inference_timeout_ms:
                raise PredictionFailure("INFERENCE_TIMEOUT", timing)
            return result, timing
        except PredictionFailure:
            raise
        except Exception as exc:
            raise PredictionFailure("INFERENCE_FAILED", timing) from exc


class PredictionService:
    def __init__(self, store: PredictionStore, predictor: LocalModelPredictor):
        self.store, self.predictor = store, predictor

    @staticmethod
    def authorize(principal: Principal, version: dict, roles: set[str]):
        if not principal.roles.intersection(roles) or version["model_id"] not in principal.model_ids:
            raise DomainError(403, "FORBIDDEN", "The role or model permission is insufficient.")

    def activate(self, principal, version_id, key):
        version = self.store.version(version_id)
        self.authorize(principal, version, {"admin"})
        digest = json_digest({"model_version_id": version_id})
        previous = self.store.previous(principal.principal_id, ACTIVATION_ROUTE, key, digest)
        if previous:
            return previous
        try:
            self.predictor.validate(version)
        except PredictionFailure as exc:
            raise DomainError(503, exc.code, "The fixed model package cannot be activated.") from exc
        return self.store.activate({"principal_id": principal.principal_id, "version_id": version_id,
                                   "route": ACTIVATION_ROUTE, "key": key, "digest": digest, "at": utc_now()})

    def predict(self, principal, version_id, raw, key):
        received_at = utc_now()
        try:
            normalized = normalize_input(raw)
            if "customer_ref" in raw and (not isinstance(raw["customer_ref"], str) or len(raw["customer_ref"]) > 128):
                raise ValueError("invalid customer reference")
            digest = json_digest({"model_version_id": version_id, "input": raw})
        except (ValueError, TypeError) as exc:
            raise DomainError(422, "INVALID_INPUT", "Feature values or field names do not match the contract.") from exc
        version = self.store.version(version_id)
        self.authorize(principal, version, {"scorer"})
        previous = self.store.previous(principal.principal_id, PREDICTION_ROUTE, key, digest)
        if previous:
            return previous
        if not version["active"]:
            raise DomainError(409, "VERSION_NOT_ACTIVE", "New scoring requests require an active version.")
        try:
            result, timing = self.predictor.score(version, raw)
            outcome = {"status": "success", "risk_probability": result["risk_probability"],
                       "decision_threshold": result["decision_threshold"], "decision": result["decision"],
                       "explanation": result["explanation"], "error_code": None,
                       "inference_ms": timing["inference_ms"], "inference_attempted": True}
        except PredictionFailure as exc:
            outcome = {"status": "failed", "risk_probability": None, "decision_threshold": None,
                       "decision": None, "explanation": None, "error_code": exc.code,
                       "inference_ms": exc.timing.get("inference_ms"),
                       "inference_attempted": bool(exc.timing.get("inference_attempted", False))}
        return self.store.save_prediction({"principal_id": principal.principal_id, "route": PREDICTION_ROUTE,
                   "key": key, "digest": digest, "version_id": version_id, "version": version["version"],
                   "manifest_sha256": version["manifest_sha256"], "original_input": raw,
                   "normalized_input": normalized, "missing_fields": [f for f, v in normalized.items() if v is None],
                   "input_sha256": json_digest(raw), "received_at": received_at,
                   "completed_at": utc_now(), "outcome": outcome})
