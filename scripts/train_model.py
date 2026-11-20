"""A2: train and package one fixed version; never overwrite a published version."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
from pathlib import Path
import warnings

import joblib
import numpy as np
from sklearn.exceptions import ConvergenceWarning
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (accuracy_score, brier_score_loss, confusion_matrix, f1_score,
                             log_loss, precision_score, recall_score, roc_auc_score)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits

from app.modules.models.bundle import (DEFAULT_ARTIFACT_ROOT, PACKAGE_FILES, ModelBundle,
                                      environment, load_bundle, sha256, version_directory)
from app.modules.models.schema import FEATURES, feature_schema, normalize_input, numeric_row
from app.modules.scoring.predict import score_one
from scripts.generate_data import DEFAULT_OUTPUT, LABEL
from scripts.validate_data import validate


def write_json(path: Path, content) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(content, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")


def read_csv(path: Path) -> tuple[list[dict], np.ndarray]:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    inputs = [{k: v for k, v in row.items() if k != LABEL} for row in rows]
    matrix = np.asarray([numeric_row(normalize_input(row)) for row in inputs], dtype=np.float64)
    return rows, matrix


def distribution(values: np.ndarray) -> dict:
    present = values[np.isfinite(values)]
    cuts = np.unique(np.quantile(present, np.arange(0.1, 1.0, 0.1))) if len(present) else np.array([])
    counts = np.bincount(np.searchsorted(cuts, present, side="right"), minlength=len(cuts) + 1)
    missing = len(values) - len(present)
    return {"cutpoints": cuts.tolist(), "intervals": "[-infinity,c0),[c0,c1),...,[last,+infinity)",
            "counts": counts.tolist(), "missing_count": missing, "total_count": len(values),
            "proportions_including_missing": [float(c / len(values)) for c in [*counts, missing]]}


def build_bundle(version: str, data_root: Path = DEFAULT_OUTPUT, root: Path = DEFAULT_ARTIFACT_ROOT,
                 *, regularization_c: float = 1.0, threshold: float = 0.5, seed: int = 20261104) -> dict:
    if not math.isfinite(regularization_c) or regularization_c <= 0:
        raise ValueError("regularization C must be finite and positive")
    if not math.isfinite(threshold) or not 0 < threshold < 1:
        raise ValueError("threshold must be finite and strictly between 0 and 1")
    folder = version_directory(root, version)
    if folder.exists():
        raise FileExistsError(f"immutable version already exists: {folder}")
    registry_path = Path(root) / "trusted_registry.json"
    if registry_path.is_symlink():
        raise ValueError("unsafe registry path")
    registry = (json.loads(registry_path.read_text(encoding="utf-8")) if registry_path.exists()
                else {"registry_format": "1.0", "versions": {}})
    if version in registry["versions"]:
        raise ValueError("version already registered")
    report = validate(data_root)
    if not report["passed"]:
        raise ValueError("data validation failed: " + "; ".join(report["errors"][:5]))
    train_rows, train_x = read_csv(data_root / "train.csv")
    test_rows, test_x = read_csv(data_root / "test.csv")
    reference_rows, reference_x = read_csv(data_root / "reference.csv")
    train_y = np.asarray([int(r[LABEL]) for r in train_rows])
    test_y = np.asarray([int(r[LABEL]) for r in test_rows])
    if not np.array_equal(np.unique(train_y), [0, 1]) or not np.array_equal(np.unique(test_y), [0, 1]):
        raise ValueError("train and held-out test sets must each contain both classes")
    pipeline = Pipeline([("imputer", SimpleImputer(strategy="median", keep_empty_features=True)),
                         ("scaler", StandardScaler()),
                         ("classifier", LogisticRegression(C=regularization_c, solver="lbfgs",
                                                           max_iter=1000, tol=1e-8, random_state=seed))])
    with threadpool_limits(limits=1), warnings.catch_warnings():
        warnings.simplefilter("error", ConvergenceWarning)
        pipeline.fit(train_x, train_y)  # Only training data fits the entire Pipeline.
        probabilities = pipeline.predict_proba(test_x)[:, 1]
        reference_probabilities = pipeline.predict_proba(reference_x)[:, 1]
    predictions = (probabilities >= threshold).astype(int)
    evaluation = {"dataset": "held_out_synthetic_test", "rows": len(test_rows), "threshold": threshold,
                  "roc_auc": float(roc_auc_score(test_y, probabilities)),
                  "accuracy": float(accuracy_score(test_y, predictions)),
                  "precision": float(precision_score(test_y, predictions, zero_division=0)),
                  "recall": float(recall_score(test_y, predictions, zero_division=0)),
                  "f1": float(f1_score(test_y, predictions, zero_division=0)),
                  "brier_score": float(brier_score_loss(test_y, probabilities)),
                  "log_loss": float(log_loss(test_y, probabilities, labels=[0, 1])),
                  "confusion_matrix_true_rows_predicted_columns": confusion_matrix(test_y, predictions, labels=[0, 1]).tolist(),
                  "majority_class_accuracy": float(max(np.bincount(test_y)) / len(test_y)),
                  "limitation": "Synthetic data only; no real credit performance or calibration claim."}
    config = {"model_version": version, "feature_order": list(FEATURES), "seed": seed,
              "training_recipe_version": "1.0",
              "decision_threshold": threshold, "fit_dataset": "train.csv",
              "test_usage": "evaluation only; no model/threshold selection",
              "reference_usage": "fixed bins only; not used in fitting",
              "classifier": {"name": "LogisticRegression", "C": regularization_c, "solver": "lbfgs",
                             "max_iter": 1000, "tol": 1e-8, "random_state": seed},
              "preprocessing": {"imputer": "median", "keep_empty_features": True, "scaler": "StandardScaler"},
              "thread_limit": 1, "source_sha256": {name: sha256(data_root / name)
                  for name in ("train.csv", "test.csv", "reference.csv", "manifest.json")}}
    source_root = Path(__file__).resolve().parents[1]
    config["implementation_sha256"] = {name: sha256(source_root / name) for name in (
        "scripts/train_model.py", "app/modules/models/schema.py", "app/modules/models/bundle.py",
        "app/modules/scoring/predict.py")}
    reference = {"reference_rows": len(reference_rows), "reference_csv_sha256": sha256(data_root / "reference.csv"),
                 "binning": "fixed reference deciles; duplicate cutpoints removed", "psi_epsilon": 1e-6,
                 "psi_smoothing": "add epsilon to each raw proportion, then renormalize separately",
                 "features": {f: distribution(reference_x[:, i]) for i, f in enumerate(FEATURES)},
                 "risk_probability": distribution(reference_probabilities)}
    explanation = {"method": "standardized_linear_log_odds", "top_k": 3,
                   "baseline": "zero standardized vector; intercept is its log-odds",
                   "formula": "log_odds = intercept + sum(standardized_feature * coefficient)",
                   "missing": "median imputation; preserve original missing fields",
                   "not_causal": True, "units": "log_odds"}
    classifier = pipeline.named_steps["classifier"]
    parameters = {"feature_order": list(FEATURES), "classes": classifier.classes_.tolist(),
                  "intercept": classifier.intercept_.tolist(), "coefficients": classifier.coef_[0].tolist(),
                  "imputation_medians": pipeline.named_steps["imputer"].statistics_.tolist(),
                  "scaler_mean": pipeline.named_steps["scaler"].mean_.tolist(),
                  "scaler_scale": pipeline.named_steps["scaler"].scale_.tolist(),
                  "iterations": classifier.n_iter_.tolist()}
    smoke_inputs = [{k: v for k, v in r.items() if k != LABEL} for r in test_rows[:3]]
    smoke_inputs.extend([{**smoke_inputs[0], "monthly_income": None}, {f: None for f in FEATURES}])
    provisional = ModelBundle(version, "pending", pipeline, config, reference, explanation, [])
    smoke_cases = [{"input": raw, "expected_probability": score_one(provisional, raw)["risk_probability"],
                    "expected_decision": score_one(provisional, raw)["decision"]} for raw in smoke_inputs]
    env = environment()
    folder.mkdir(parents=True, exist_ok=False)
    # An interrupted build leaves no complete manifest and is never silently reused.
    joblib.dump(pipeline, folder / "pipeline.joblib", compress=3, protocol=5)
    for name, obj in {"feature_schema.json": feature_schema(), "training_config.json": config,
                      "environment.json": env, "reference_distribution.json": reference,
                      "explanation_config.json": explanation, "model_parameters.json": parameters,
                      "evaluation.json": evaluation, "smoke_cases.json": smoke_cases}.items():
        write_json(folder / name, obj)
    with (folder / "requirements.lock.txt").open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(f"{name}=={v}" for name, v in sorted(env["packages"].items())) + "\n")
    write_json(folder / "manifest.json", {"bundle_format": "1.0", "model_version": version,
               "synthetic_only": True, "files": {name: sha256(folder / name) for name in sorted(PACKAGE_FILES)}})
    digest = sha256(folder / "manifest.json")
    loaded = load_bundle(version, digest, root)
    errors = [abs(score_one(loaded, c["input"])["risk_probability"] - c["expected_probability"]) for c in smoke_cases]
    if max(errors) > 1e-8:
        raise ValueError("saved model failed reload verification")
    registry["versions"][version] = {"manifest_sha256": digest,
                                      "pipeline_sha256": sha256(folder / "pipeline.joblib")}
    # Offline, single-writer registry for A2; production registration is B's responsibility.
    registry_temp = registry_path.with_suffix(".json.tmp")
    with registry_temp.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(registry, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
    os.replace(registry_temp, registry_path)
    return {"version": version, "manifest_sha256": digest, "evaluation": evaluation,
            "reload_max_absolute_error": max(errors), "smoke_cases": len(smoke_cases)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="credit-logreg-v1")
    parser.add_argument("--data", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--artifacts", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--regularization-c", type=float, default=1.0)
    parser.add_argument("--threshold", type=float, default=0.5)
    parser.add_argument("--seed", type=int, default=20261104)
    args = parser.parse_args()
    try:
        result = build_bundle(args.version, args.data, args.artifacts,
                              regularization_c=args.regularization_c, threshold=args.threshold, seed=args.seed)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"training failed: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
