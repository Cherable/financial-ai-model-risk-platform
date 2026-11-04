"""Validate the generated fixtures independently of the generator's summaries."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from scripts.generate_data import DATASETS, DEFAULT_OUTPUT, FEATURES, LABEL

LIMITS = {
    "age": (Decimal(18), Decimal(80)),
    "monthly_income": (Decimal(0), Decimal(1000000)),
    "debt_income_ratio": (Decimal(0), Decimal(3)),
    "past_due_count": (Decimal(0), Decimal(100)),
    "loan_amount": (Decimal("0.01"), Decimal(10000000)),
    "employment_years": (Decimal(0), Decimal(60)),
}


def validate(directory: Path) -> dict:
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    errors = []
    seen_ids = set()
    seen_features = {}
    summaries = {}
    if manifest.get("synthetic_only") is not True:
        errors.append("manifest must mark synthetic_only=true")
    if set(manifest.get("datasets", {})) != set(DATASETS):
        errors.append("manifest dataset names differ from the six required datasets")
    for name in DATASETS:
        entry = manifest.get("datasets", {}).get(name)
        if not entry:
            errors.append(f"{name}: missing manifest entry")
            continue
        # Do not trust a manifest to select arbitrary files outside the directory.
        path = directory / f"{name}.csv"
        if not path.is_file():
            errors.append(f"{name}: file missing")
            continue
        if entry.get("filename") != path.name:
            errors.append(f"{name}: filename mismatch")
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry.get("sha256"):
            errors.append(f"{name}: SHA-256 mismatch")
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            expected_fields = ["customer_ref", *FEATURES, *([LABEL] if name in ("train", "test") else [])]
            if reader.fieldnames != expected_fields:
                errors.append(f"{name}: unexpected columns (possible label leakage)")
                continue
            rows = list(reader)
        expected_count = manifest["parameters"]["train_rows" if name == "train" else "sample_rows"]
        if not rows or len(rows) != expected_count or len(rows) != entry.get("row_count"):
            errors.append(f"{name}: row count mismatch or empty dataset")
        missing = dict.fromkeys(FEATURES, 0)
        labels = {"0": 0, "1": 0}
        income_sum = Decimal(0)
        income_count = 0
        for number, row in enumerate(rows, 1):
            if None in row or any(value is None for value in row.values()):
                errors.append(f"{name}:{number}: wrong column count")
                continue
            ref = row.get("customer_ref", "")
            if not ref.startswith(f"SYN-{name}-") or ref in seen_ids:
                errors.append(f"{name}:{number}: invalid or repeated synthetic ID")
            seen_ids.add(ref)
            signature = tuple(row.get(k) for k in FEATURES)
            if signature in seen_features:
                errors.append(f"{name}:{number}: duplicate feature row with {seen_features[signature]}")
            seen_features[signature] = name
            for field, (low, high) in LIMITS.items():
                raw = row.get(field)
                if raw == "":
                    missing[field] += 1
                    if name != "stream_missing" or field != "monthly_income":
                        errors.append(f"{name}:{number}: unexpected missing {field}")
                    continue
                try:
                    value = Decimal(raw)
                    if not value.is_finite() or not low <= value <= high:
                        raise ValueError("out of range")
                    if field in ("age", "past_due_count") and value != value.to_integral_value():
                        raise ValueError("integer required")
                    if field in ("monthly_income", "loan_amount") and value.as_tuple().exponent != -2:
                        raise ValueError("two decimal places required")
                    if field == "monthly_income":
                        income_sum += value
                        income_count += 1
                except (InvalidOperation, TypeError, ValueError):
                    errors.append(f"{name}:{number}: invalid {field}={raw!r}")
            try:
                if Decimal(row["employment_years"]) > Decimal(row["age"]) - 18:
                    errors.append(f"{name}:{number}: employment exceeds adult years")
            except (InvalidOperation, TypeError):
                pass  # Already reported by the field checks.
            if name in ("train", "test"):
                if row.get(LABEL) not in labels:
                    errors.append(f"{name}:{number}: label must be 0 or 1")
                else:
                    labels[row[LABEL]] += 1
        expected_missing = (int((Decimal(str(manifest["parameters"]["missing_rate"])) * len(rows))
                                .quantize(Decimal("1"), rounding=ROUND_HALF_UP))
                            if name == "stream_missing" else 0)
        if missing["monthly_income"] != expected_missing:
            errors.append(f"{name}: missing-rate injection mismatch")
        if missing != entry.get("missing_counts"):
            errors.append(f"{name}: missing-count summary mismatch")
        if name in ("train", "test") and labels != entry.get("label_counts"):
            errors.append(f"{name}: label-count summary mismatch")
        summaries[name] = {"rows": len(rows), "missing_counts": missing,
                           "monthly_income_mean": str((income_sum / income_count).quantize(Decimal("0.01")))
                           if income_count else None}
        if name in ("train", "test"):
            summaries[name]["label_counts"] = labels
    return {"passed": not errors, "synthetic_only": manifest.get("synthetic_only") is True, "datasets": summaries,
            "total_rows": sum(d["rows"] for d in summaries.values()),
            "checks": ["sha256", "schema_and_label_isolation", "row_counts", "feature_ranges",
                       "money_precision", "unique_ids_and_feature_rows", "exact_missing_counts",
                       "employment_age_consistency"],
            "errors": errors}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        report = validate(args.input)
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"validation failed: {exc}\n")
    serialized = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(serialized, encoding="utf-8")
    print(serialized)
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
