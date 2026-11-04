"""Generate deterministic, entirely fictional credit-monitoring fixtures.

Run from the repository root: python -m scripts.generate_data
This is an offline data preparation tool, not a trained credit model.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import platform
import random
from decimal import Decimal, ROUND_HALF_UP

GENERATOR_VERSION = "1.0.0"
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "data" / "synthetic" / "v1"
FEATURES = (
    "age", "monthly_income", "debt_income_ratio", "past_due_count",
    "loan_amount", "employment_years",
)
LABEL = "synthetic_default_label"
DATASETS = ("train", "test", "reference", "stream_normal", "stream_drift", "stream_missing")
LABEL_FORMULA = (
    "z = -2.4 + 2.4 * debt_income_ratio + 0.45 * past_due_count "
    "+ 0.8 * min(loan_amount / (monthly_income * 12), 5) "
    "- 0.07 * employment_years; p = 1 / (1 + exp(-z)); label = Bernoulli(p)"
)


def derived_seed(seed: int, name: str) -> int:
    digest = hashlib.sha256(f"{GENERATOR_VERSION}:{seed}:{name}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def money(value: float) -> str:
    return str(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def make_rows(name: str, count: int, seed: int, missing_rate: float) -> list[dict]:
    rng = random.Random(derived_seed(seed, name))
    label_rng = random.Random(derived_seed(seed, name + ":label"))
    rows = []
    for index in range(count):
        age = rng.randint(18, 80)
        income = min(150000.0, max(1500.0, rng.lognormvariate(math.log(8000), 0.55)))
        debt_ratio = min(2.5, max(0.0, rng.betavariate(2, 5) * 1.8))
        past_due = min(12, int(rng.expovariate(1.5)))
        employment = round(rng.uniform(0, min(age - 18, 60)), 2)
        loan = min(5000000.0, max(1000.0, income * rng.uniform(2, 24)))
        if name == "stream_drift":
            income *= 0.55
            debt_ratio = min(3.0, debt_ratio + 0.6)
        row = {
            "customer_ref": f"SYN-{name}-{index + 1:06d}",
            "age": str(age),
            "monthly_income": money(income),
            "debt_income_ratio": f"{debt_ratio:.6f}",
            "past_due_count": str(past_due),
            "loan_amount": money(loan),
            "employment_years": f"{employment:.2f}",
        }
        if name in ("train", "test"):
            # Use the serialized features so the label recipe is auditable.
            annual_income = float(row["monthly_income"]) * 12
            z = (-2.4 + 2.4 * float(row["debt_income_ratio"])
                 + 0.45 * int(row["past_due_count"])
                 + 0.8 * min(float(row["loan_amount"]) / annual_income, 5)
                 - 0.07 * float(row["employment_years"]))
            row[LABEL] = str(int(label_rng.random() < 1 / (1 + math.exp(-z))))
        rows.append(row)
    if name == "stream_missing":
        mask_rng = random.Random(derived_seed(seed, name + ":missing_mask"))
        # Explicit half-up rounding, rather than independently dropping each row.
        missing_count = int((Decimal(str(missing_rate)) * count).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP))
        for index in mask_rng.sample(range(count), missing_count):
            rows[index]["monthly_income"] = ""
    return rows


def csv_bytes(rows: list[dict], labeled: bool) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=["customer_ref", *FEATURES, *([LABEL] if labeled else [])],
                            lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def describe_rows(rows: list[dict]) -> dict:
    missing_counts = {key: sum(row[key] == "" for row in rows) for key in FEATURES}
    numeric = {}
    for key in FEATURES:
        values = [Decimal(row[key]) for row in rows if row[key] != ""]
        numeric[key] = ({"min": str(min(values)), "max": str(max(values)),
                         "mean": str((sum(values) / len(values)).quantize(Decimal("0.000001")))}
                        if values else None)
    result = {"row_count": len(rows), "missing_counts": missing_counts,
              "missing_rates": {k: round(v / len(rows), 6) for k, v in missing_counts.items()},
              "numeric_summary": numeric}
    if LABEL in rows[0]:
        positive = sum(int(row[LABEL]) for row in rows)
        result["label_counts"] = {"0": len(rows) - positive, "1": positive}
    return result


def generate(output: Path, *, seed: int = 20261104, train_rows: int = 5000,
             sample_rows: int = 1000, missing_rate: float = 0.1, overwrite: bool = False) -> dict:
    if train_rows < 1 or sample_rows < 1:
        raise ValueError("row counts must be positive")
    if not math.isfinite(missing_rate) or not 0 <= missing_rate <= 1:
        raise ValueError("missing_rate must be finite and within [0, 1]")
    output = Path(output)
    filenames = [f"{name}.csv" for name in DATASETS] + ["manifest.json"]
    for name in filenames:
        path = output / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError(f"refusing non-regular output: {path}")
        if path.exists() and not overwrite:
            raise FileExistsError(f"already exists: {path}; use a new --output or explicit --overwrite")
    content = {}
    entries = {}
    for name in DATASETS:
        count = train_rows if name == "train" else sample_rows
        rows = make_rows(name, count, seed, missing_rate)
        payload = csv_bytes(rows, name in ("train", "test"))
        content[f"{name}.csv"] = payload
        entries[name] = {"filename": f"{name}.csv", "seed": derived_seed(seed, name),
                         "sha256": hashlib.sha256(payload).hexdigest(),
                         "has_label": name in ("train", "test"), **describe_rows(rows)}
    manifest = {
        "generator_version": GENERATOR_VERSION,
        "python_version": platform.python_version(),
        "synthetic_only": True,
        "base_seed": seed,
        "parameters": {"train_rows": train_rows, "sample_rows": sample_rows,
                       "missing_rate": missing_rate},
        "feature_columns": list(FEATURES), "label_column": LABEL,
        "missing_representation": "empty CSV field; convert to null/NaN when loading",
        "label_formula": LABEL_FORMULA,
        "datasets": entries,
    }
    content["manifest.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True)
                                + "\n").encode("utf-8")
    output.mkdir(parents=True, exist_ok=True)
    # Write the manifest last: interrupted runs are detectable through its hashes.
    for name, payload in content.items():
        target = output / name
        temp = target.with_suffix(target.suffix + ".tmp")
        if temp.exists() or temp.is_symlink():
            raise FileExistsError(f"temporary file already exists: {temp}")
        try:
            with temp.open("xb") as handle:
                handle.write(payload)
            os.replace(temp, target)
        finally:
            if temp.is_file() and not temp.is_symlink():
                temp.unlink()
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--seed", type=int, default=20261104)
    parser.add_argument("--train-rows", type=int, default=5000)
    parser.add_argument("--sample-rows", type=int, default=1000)
    parser.add_argument("--missing-rate", type=float, default=0.1)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    try:
        manifest = generate(args.output, seed=args.seed, train_rows=args.train_rows,
                            sample_rows=args.sample_rows, missing_rate=args.missing_rate,
                            overwrite=args.overwrite)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"generation failed: {exc}\n")
    print(json.dumps({"output": str(args.output.resolve()),
                      "total_rows": sum(d["row_count"] for d in manifest["datasets"].values()),
                      "generator_version": GENERATOR_VERSION}, ensure_ascii=True))


if __name__ == "__main__":
    main()
