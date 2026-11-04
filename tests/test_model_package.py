"""A2 acceptance tests for training isolation, trusted loading and exact replay."""

import csv
import json
import math
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from app.modules.models.bundle import load_bundle, load_registered
from app.modules.models.schema import FEATURES
from app.modules.scoring.predict import score_one
from scripts.generate_data import generate
from scripts.train_model import build_bundle
from scripts.verify_model import verify


class ModelPackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parent = Path(__file__).resolve().parent
        cls.temp = tempfile.TemporaryDirectory(dir=cls.parent, prefix=".a2-test-")
        cls.root = Path(cls.temp.name)
        cls.addClassCleanup(cls.cleanup_temp)
        cls.data = cls.root / "data"
        cls.artifacts = cls.root / "artifacts"
        generate(cls.data, train_rows=300, sample_rows=100)
        cls.result = build_bundle("test-v1", cls.data, cls.artifacts)
        build_bundle("test-v2", cls.data, cls.artifacts, regularization_c=0.5)
        cls.bundle = load_registered("test-v1", cls.artifacts)
        cls.sample = cls.bundle.smoke_cases[0]["input"]

    @classmethod
    def cleanup_temp(cls):
        if not cls.root.resolve().is_relative_to(cls.parent):
            raise RuntimeError("cleanup escaped test workspace")
        cls.temp.cleanup()

    def copy_bundle(self, name):
        root = self.root / name
        shutil.copytree(self.artifacts / "test-v1", root / "test-v1")
        return root

    def test_pipeline_fits_preprocessing_on_training_only(self):
        with (self.data / "train.csv").open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        matrix = np.array([[float(row[f]) for f in FEATURES] for row in rows])
        np.testing.assert_allclose(self.bundle.pipeline.named_steps["imputer"].statistics_, np.median(matrix, axis=0))
        np.testing.assert_allclose(self.bundle.pipeline.named_steps["scaler"].mean_, matrix.mean(axis=0))
        self.assertEqual(self.bundle.pipeline.n_features_in_, 6)

    def test_dictionary_order_does_not_change_inference(self):
        original = score_one(self.bundle, self.sample)
        reversed_input = dict(reversed(list(self.sample.items())))
        self.assertEqual(original, score_one(self.bundle, reversed_input))

    def test_identifier_is_not_a_feature_and_label_is_rejected(self):
        first = score_one(self.bundle, self.sample)
        changed = score_one(self.bundle, {**self.sample, "customer_ref": "different-customer"})
        self.assertEqual(first["risk_probability"], changed["risk_probability"])
        with self.assertRaisesRegex(ValueError, "forbidden"):
            score_one(self.bundle, {**self.sample, "synthetic_default_label": "1"})

    def test_missing_fields_are_preserved_and_imputed(self):
        result = score_one(self.bundle, {f: None for f in FEATURES})
        self.assertEqual(result["missing_fields"], list(FEATURES))
        self.assertTrue(math.isfinite(result["risk_probability"]))
        self.assertTrue(all(x["was_missing"] for x in result["explanation"]["contributions"]))

    def test_explanation_reconstructs_score_and_selects_top_three(self):
        result = score_one(self.bundle, self.sample)
        explanation = result["explanation"]
        log_odds = explanation["intercept"] + sum(c["log_odds_contribution"] for c in explanation["contributions"])
        self.assertAlmostEqual(1 / (1 + math.exp(-log_odds)), result["risk_probability"], places=12)
        expected = sorted(explanation["contributions"], key=lambda c: -abs(c["log_odds_contribution"]))[:3]
        self.assertEqual(explanation["top_factors"], expected)

    def test_reference_bins_include_every_sample(self):
        reference = self.bundle.reference_distribution
        for description in [*reference["features"].values(), reference["risk_probability"]]:
            self.assertEqual(sum(description["counts"]) + description["missing_count"], 100)
            self.assertAlmostEqual(sum(description["proportions_including_missing"]), 1)
            self.assertEqual(description["cutpoints"], sorted(set(description["cutpoints"])))

    def test_reload_and_old_version_after_loading_new_version(self):
        before = score_one(self.bundle, self.sample)
        new_bundle = load_registered("test-v2", self.artifacts)
        score_one(new_bundle, self.sample)
        old_bundle = load_registered("test-v1", self.artifacts)
        self.assertEqual(before, score_one(old_bundle, self.sample))
        self.assertTrue(verify("test-v1", self.artifacts)["passed"])

    def test_same_training_recipe_reproduces_predictions(self):
        root = self.root / "retrained"
        result = build_bundle("test-v1", self.data, root)
        retrained = load_bundle("test-v1", result["manifest_sha256"], root)
        self.assertLessEqual(abs(score_one(retrained, self.sample)["risk_probability"]
                                 - score_one(self.bundle, self.sample)["risk_probability"]), 1e-12)

    def test_existing_version_cannot_be_overwritten(self):
        before = (self.artifacts / "test-v1" / "manifest.json").read_bytes()
        with self.assertRaises(FileExistsError):
            build_bundle("test-v1", self.data, self.artifacts, regularization_c=2)
        self.assertEqual(before, (self.artifacts / "test-v1" / "manifest.json").read_bytes())

    def test_model_tamper_is_rejected_before_deserialization(self):
        root = self.copy_bundle("tamper")
        path = root / "test-v1" / "pipeline.joblib"
        path.write_bytes(path.read_bytes() + b"changed")
        with patch("app.modules.models.bundle.joblib.load") as loader:
            with self.assertRaisesRegex(ValueError, "integrity"):
                load_bundle("test-v1", self.result["manifest_sha256"], root)
            loader.assert_not_called()

    def test_manifest_tamper_is_rejected_before_deserialization(self):
        root = self.copy_bundle("manifest-tamper")
        path = root / "test-v1" / "manifest.json"
        path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with patch("app.modules.models.bundle.joblib.load") as loader:
            with self.assertRaisesRegex(ValueError, "manifest SHA-256"):
                load_bundle("test-v1", self.result["manifest_sha256"], root)
            loader.assert_not_called()

    def test_missing_package_file_is_rejected_before_deserialization(self):
        root = self.copy_bundle("missing")
        (root / "test-v1" / "pipeline.joblib").rename(root / "test-v1" / "not-model.bin")
        with patch("app.modules.models.bundle.joblib.load") as loader:
            with self.assertRaisesRegex(ValueError, "integrity"):
                load_bundle("test-v1", self.result["manifest_sha256"], root)
            loader.assert_not_called()

    def test_environment_mismatch_is_rejected_before_deserialization(self):
        with patch("app.modules.models.bundle.environment", return_value={}), patch("app.modules.models.bundle.joblib.load") as loader:
            with self.assertRaisesRegex(ValueError, "environment mismatch"):
                load_bundle("test-v1", self.result["manifest_sha256"], self.artifacts)
            loader.assert_not_called()

    def test_unregistered_version_has_no_fallback(self):
        with self.assertRaisesRegex(ValueError, "not registered"):
            load_registered("absent-v1", self.artifacts)

    def test_path_traversal_and_untrusted_digest_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "identifier"):
            load_bundle("../test-v1", self.result["manifest_sha256"], self.artifacts)
        with self.assertRaisesRegex(ValueError, "trusted"):
            load_bundle("test-v1", "", self.artifacts)

    def test_nonfinite_invalid_and_overprecise_inputs_are_rejected(self):
        for changes in ({"age": True}, {"age": 18.5}, {"debt_income_ratio": float("inf")},
                        {"monthly_income": "NaN"}, {"loan_amount": "0"},
                        {"monthly_income": "1000.001"}, {"loan_amount": 1000}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                score_one(self.bundle, {**self.sample, **changes})

    def test_invalid_training_config_does_not_create_version(self):
        for c, threshold in ((0, 0.5), (float("nan"), 0.5), (1, 0), (1, float("inf"))):
            with self.assertRaises(ValueError):
                build_bundle("invalid", self.data, self.artifacts, regularization_c=c, threshold=threshold)
        self.assertFalse((self.artifacts / "invalid").exists())


if __name__ == "__main__":
    unittest.main()
