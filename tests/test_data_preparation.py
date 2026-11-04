"""A1 acceptance checks: reproducibility, leakage, perturbation and data integrity."""

import csv
import math
from pathlib import Path
import tempfile
import unittest

from scripts.generate_data import DATASETS, generate
from scripts.validate_data import validate


class DataPreparationTests(unittest.TestCase):
    def setUp(self):
        # Use the writable repository rather than an OS sandbox temp folder.
        parent = Path(__file__).resolve().parent
        self.temp = tempfile.TemporaryDirectory(dir=parent, prefix=".a1-test-")
        self.root = Path(self.temp.name)
        self.assertTrue(self.root.resolve().is_relative_to(parent))
        self.addCleanup(self.cleanup_temp)

    def cleanup_temp(self):
        parent = Path(__file__).resolve().parent
        if not self.root.resolve().is_relative_to(parent):
            raise RuntimeError("test cleanup target escaped the tests directory")
        self.temp.cleanup()

    def build(self, name="data", **kwargs):
        path = self.root / name
        generate(path, train_rows=80, sample_rows=40, **kwargs)
        return path

    def test_identical_parameters_are_byte_reproducible(self):
        left, right = self.build("left"), self.build("right")
        for filename in [*(f"{name}.csv" for name in DATASETS), "manifest.json"]:
            self.assertEqual((left / filename).read_bytes(), (right / filename).read_bytes())

    def test_different_seed_changes_each_dataset(self):
        left, right = self.build("left", seed=1), self.build("right", seed=2)
        for name in DATASETS:
            self.assertNotEqual((left / f"{name}.csv").read_bytes(), (right / f"{name}.csv").read_bytes())

    def test_generated_data_passes_independent_validation(self):
        report = validate(self.build())
        self.assertTrue(report["passed"], report["errors"])
        self.assertEqual(report["total_rows"], 280)

    def test_missing_rate_endpoints_and_half_up_rounding(self):
        for index, rate in enumerate((0, 0.1, 0.0125, 1)):
            report = validate(self.build(f"case{index}", missing_rate=rate))
            self.assertTrue(report["passed"], report["errors"])
            expected = (0, 4, 1, 40)[index]
            self.assertEqual(report["datasets"]["stream_missing"]["missing_counts"]["monthly_income"], expected)

    def test_drift_scenario_has_lower_income_and_higher_debt(self):
        manifest = generate(self.root / "large", train_rows=100, sample_rows=1000)
        normal = manifest["datasets"]["stream_normal"]["numeric_summary"]
        drift = manifest["datasets"]["stream_drift"]["numeric_summary"]
        self.assertLess(float(drift["monthly_income"]["mean"]), float(normal["monthly_income"]["mean"]) * 0.7)
        self.assertGreater(float(drift["debt_income_ratio"]["mean"]), float(normal["debt_income_ratio"]["mean"]) + 0.4)

    def test_overwrite_requires_explicit_flag(self):
        path = self.build()
        original = (path / "train.csv").read_bytes()
        with self.assertRaises(FileExistsError):
            generate(path)
        self.assertEqual(original, (path / "train.csv").read_bytes())
        generate(path, train_rows=80, sample_rows=40, overwrite=True)
        self.assertEqual(original, (path / "train.csv").read_bytes())

    def test_invalid_parameters_do_not_create_output(self):
        for i, kwargs in enumerate(({"missing_rate": math.nan}, {"missing_rate": math.inf},
                                    {"missing_rate": -0.1}, {"missing_rate": 1.1},
                                    {"train_rows": 0}, {"sample_rows": 0})):
            path = self.root / str(i)
            with self.assertRaises(ValueError):
                generate(path, **kwargs)
            self.assertFalse(path.exists())

    def test_validator_detects_tampered_range_id_and_hash(self):
        path = self.build()
        file = path / "test.csv"
        with file.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            fields, rows = reader.fieldnames, list(reader)
        rows[0]["age"] = "17"
        rows[1]["customer_ref"] = rows[0]["customer_ref"]
        with file.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        report = validate(path)
        self.assertFalse(report["passed"])
        self.assertTrue(any("SHA-256" in e for e in report["errors"]))
        self.assertTrue(any("invalid age" in e for e in report["errors"]))
        self.assertTrue(any("repeated synthetic ID" in e for e in report["errors"]))

    def test_validator_rejects_labels_in_online_stream(self):
        path = self.build()
        file = path / "stream_normal.csv"
        file.write_text(file.read_text(encoding="utf-8").replace(
            "employment_years\n", "employment_years,synthetic_default_label\n", 1), encoding="utf-8")
        report = validate(path)
        self.assertFalse(report["passed"])
        self.assertTrue(any("label leakage" in e for e in report["errors"]))

    def test_validator_rejects_wrong_column_count(self):
        path = self.build()
        file = path / "reference.csv"
        lines = file.read_text(encoding="utf-8").splitlines()
        lines[1] += ",unexpected"
        file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        report = validate(path)
        self.assertFalse(report["passed"])
        self.assertTrue(any("column count" in e for e in report["errors"]))


if __name__ == "__main__":
    unittest.main()
