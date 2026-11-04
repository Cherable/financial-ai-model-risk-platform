"""Reload fixed model packages and verify their stored acceptance cases."""

import argparse
import json
import math
from pathlib import Path

from app.modules.models.bundle import DEFAULT_ARTIFACT_ROOT, load_registered
from app.modules.scoring.predict import score_one


def verify(version: str, root: Path = DEFAULT_ARTIFACT_ROOT) -> dict:
    bundle = load_registered(version, root)
    errors = []
    for case in bundle.smoke_cases:
        result = score_one(bundle, case["input"])
        error = abs(result["risk_probability"] - case["expected_probability"])
        reconstructed = 1 / (1 + math.exp(-result["explanation"]["log_odds"]))
        if error > 1e-8 or result["decision"] != case["expected_decision"]:
            raise ValueError("stored case prediction mismatch")
        if abs(reconstructed - result["risk_probability"]) > 1e-12:
            raise ValueError("additive explanation did not reconstruct the score")
        errors.append(error)
    if not errors:
        raise ValueError("bundle has no acceptance cases")
    return {"version": version, "manifest_sha256": bundle.manifest_sha256,
            "passed": True, "cases": len(errors), "max_absolute_error": max(errors),
            "environment_and_all_file_hashes_checked_before_loading": True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="credit-logreg-v1")
    parser.add_argument("--artifacts", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        result = verify(args.version, args.artifacts)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"verification failed: {exc}\n")
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
