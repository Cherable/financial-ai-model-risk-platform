"""Verify local trusted packages before deserializing their model.

SHA-256 detects modification relative to a separately trusted manifest digest.
It does not establish the authenticity of arbitrary downloaded pickle files.
"""

from dataclasses import dataclass
import hashlib
from importlib.metadata import version as package_version
import json
from pathlib import Path
import platform
import re
import warnings

import joblib
import numpy as np
from sklearn.exceptions import InconsistentVersionWarning
from sklearn.pipeline import Pipeline

from app.modules.models.schema import FEATURES, feature_schema

DEFAULT_ARTIFACT_ROOT = Path(__file__).resolve().parents[3] / "artifacts"
PACKAGES = ("cloudpickle", "joblib", "narwhals", "numpy", "scikit-learn", "scipy", "threadpoolctl")
PACKAGE_FILES = {"pipeline.joblib", "feature_schema.json", "training_config.json", "environment.json",
                 "requirements.lock.txt", "reference_distribution.json", "explanation_config.json",
                 "model_parameters.json", "evaluation.json", "smoke_cases.json"}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def environment() -> dict:
    return {"python": platform.python_version(), "implementation": platform.python_implementation(),
            "system": platform.system(), "machine": platform.machine(),
            "packages": {p: package_version(p) for p in PACKAGES}}


def version_directory(root: Path, version: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", version):
        raise ValueError("invalid version identifier")
    root = Path(root).resolve()
    target = root / version
    if target.is_symlink() or (target.exists() and target.resolve().parent != root):
        raise ValueError("version directory must not be a symlink")
    return target


@dataclass(frozen=True)
class ModelBundle:
    version: str
    manifest_sha256: str
    pipeline: Pipeline
    training_config: dict
    reference_distribution: dict
    explanation_config: dict
    smoke_cases: list


def load_bundle(version: str, expected_manifest_sha256: str,
                root: Path = DEFAULT_ARTIFACT_ROOT) -> ModelBundle:
    """The caller must supply an approved manifest digest, e.g. from B's registry."""
    folder = version_directory(root, version)
    manifest_path = folder / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("model manifest is missing or unsafe")
    if not re.fullmatch(r"[a-f0-9]{64}", expected_manifest_sha256 or ""):
        raise ValueError("an independently trusted manifest SHA-256 is required")
    if sha256(manifest_path) != expected_manifest_sha256:
        raise ValueError("manifest SHA-256 mismatch")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("model_version") != version or manifest.get("bundle_format") != "1.0":
        raise ValueError("bundle identity or format mismatch")
    if set(manifest.get("files", {})) != PACKAGE_FILES:
        raise ValueError("unexpected package file inventory")
    for name, digest in manifest["files"].items():
        path = folder / name
        if path.is_symlink() or not path.is_file() or sha256(path) != digest:
            raise ValueError(f"package integrity check failed: {name}")
    read = lambda name: json.loads((folder / name).read_text(encoding="utf-8"))
    if read("environment.json") != environment():
        raise ValueError("environment mismatch; restore the pinned environment before loading")
    if read("feature_schema.json") != feature_schema():
        raise ValueError("feature schema mismatch")
    config = read("training_config.json")
    if config.get("model_version") != version or config.get("feature_order") != list(FEATURES):
        raise ValueError("training configuration mismatch")
    # Loading is last: no pickle execution before inventory, digest and environment checks.
    with warnings.catch_warnings():
        warnings.simplefilter("error", InconsistentVersionWarning)
        pipeline = joblib.load(folder / "pipeline.joblib")
    if not isinstance(pipeline, Pipeline) or list(pipeline.named_steps) != ["imputer", "scaler", "classifier"]:
        raise ValueError("unexpected pipeline structure")
    if pipeline.n_features_in_ != len(FEATURES) or not np.array_equal(pipeline.classes_, [0, 1]):
        raise ValueError("unexpected feature count or target classes")
    return ModelBundle(version, expected_manifest_sha256, pipeline, config,
                       read("reference_distribution.json"), read("explanation_config.json"), read("smoke_cases.json"))


def load_registered(version: str, root: Path = DEFAULT_ARTIFACT_ROOT) -> ModelBundle:
    """Development-only registry; B will copy approved hashes into controlled metadata."""
    registry_path = Path(root) / "trusted_registry.json"
    if registry_path.is_symlink():
        raise ValueError("registry must be a trusted regular file")
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    if version not in registry.get("versions", {}):
        raise ValueError("version is not registered; no fallback is allowed")
    return load_bundle(version, registry["versions"][version]["manifest_sha256"], root)
