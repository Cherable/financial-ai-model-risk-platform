"""Pure statistics with fixed reference bins; never fit a model or use labels."""

import math
import numpy as np

from app.modules.models.schema import FEATURES

CALCULATION_VERSION = "1.0"
MIN_PSI_SAMPLES = 200


def psi(values, reference, epsilon):
    values = np.asarray([np.nan if v is None else float(v) for v in values], dtype=float)
    present = values[np.isfinite(values)]
    if np.any(np.isinf(values)):
        raise ValueError("nonfinite monitoring input")
    cuts = np.asarray(reference["cutpoints"], dtype=float)
    counts = np.bincount(np.searchsorted(cuts, present, side="right"), minlength=len(cuts) + 1).tolist()
    counts.append(len(values) - len(present))
    p = np.asarray(reference["proportions_including_missing"], dtype=float)
    q = np.asarray(counts, dtype=float) / len(values)
    if p.shape != q.shape or not math.isfinite(epsilon) or epsilon <= 0:
        raise ValueError("invalid reference distribution")
    p, q = p + epsilon, q + epsilon
    p, q = p / p.sum(), q / q.sum()
    return float(np.sum((q - p) * np.log(q / p))), counts


def calculate(records: list[dict], reference: dict) -> list[dict]:
    successful = [r for r in records if r["status"] == "success"]
    attempted = [r for r in records if r["inference_attempted"]]
    result = []

    def add(name, feature, value, count, unit, status="ready", **details):
        result.append({"metric_name": name, "feature": feature, "value": value, "sample_count": count,
                       "unit": unit, "status": status, "details": details})

    for field in FEATURES:
        missing = sum(r["normalized_input"][field] is None for r in successful)
        add("missing_rate", field, missing / len(successful) if successful else None, len(successful), "ratio",
            "ready" if successful else "no_data", numerator=missing, denominator=len(successful))
        if len(successful) >= MIN_PSI_SAMPLES:
            value, bins = psi([r["normalized_input"][field] for r in successful], reference["features"][field], reference["psi_epsilon"])
            add("feature_psi", field, value, len(successful), "PSI", bin_counts_including_missing=bins)
        else:
            add("feature_psi", field, None, len(successful), "PSI", "insufficient_samples" if successful else "no_data",
                minimum_samples=MIN_PSI_SAMPLES)
    if len(successful) >= MIN_PSI_SAMPLES:
        value, bins = psi([r["risk_probability"] for r in successful], reference["risk_probability"], reference["psi_epsilon"])
        add("prediction_psi", "", value, len(successful), "PSI", bin_counts_including_missing=bins)
    else:
        add("prediction_psi", "", None, len(successful), "PSI", "insufficient_samples" if successful else "no_data",
            minimum_samples=MIN_PSI_SAMPLES)
    failures = sum(r["status"] == "failed" for r in attempted)
    add("failure_rate", "", failures / len(attempted) if attempted else None, len(attempted), "ratio",
        "ready" if attempted else "no_data", numerator=failures, denominator=len(attempted),
        excluded_load_failures=sum(r["status"] == "failed" and not r["inference_attempted"] for r in records))
    latency = [r["inference_ms"] for r in successful]
    add("inference_p95", "", float(np.quantile(latency, 0.95, method="linear")) if latency else None,
        len(latency), "ms", "ready" if latency else "no_data", method="linear")
    return result
