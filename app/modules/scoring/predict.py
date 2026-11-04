"""Pure local inference and an additive log-odds explanation."""

import numpy as np
from threadpoolctl import threadpool_limits

from app.modules.models.bundle import ModelBundle
from app.modules.models.schema import FEATURES, normalize_input, numeric_row


def score_one(bundle: ModelBundle, raw: dict) -> dict:
    normalized = normalize_input(raw)
    matrix = np.asarray([numeric_row(normalized)], dtype=np.float64)
    with threadpool_limits(limits=1):
        probability = float(bundle.pipeline.predict_proba(matrix)[0, 1])
        imputed = bundle.pipeline.named_steps["imputer"].transform(matrix)[0]
        transformed = bundle.pipeline.named_steps["scaler"].transform([imputed])[0]
        classifier = bundle.pipeline.named_steps["classifier"]
        contributions = transformed * classifier.coef_[0]
    intercept = float(classifier.intercept_[0])
    all_items = [{"feature": field, "original_value": normalized[field],
                  "was_missing": normalized[field] is None, "imputed_value": float(imputed[i]),
                  "standardized_value": float(transformed[i]), "coefficient": float(classifier.coef_[0, i]),
                  "log_odds_contribution": float(contributions[i])} for i, field in enumerate(FEATURES)]
    top = sorted(all_items, key=lambda item: -abs(item["log_odds_contribution"]))[:bundle.explanation_config["top_k"]]
    threshold = bundle.training_config["decision_threshold"]
    return {"model_version": bundle.version, "manifest_sha256": bundle.manifest_sha256,
            "risk_probability": probability, "decision_threshold": threshold,
            "decision": "at_or_above_threshold" if probability >= threshold else "below_threshold",
            "original_input": dict(raw), "normalized_input": normalized,
            "missing_fields": [f for f in FEATURES if normalized[f] is None],
            "explanation": {"method": "standardized_linear_log_odds", "intercept": intercept,
                            "log_odds": intercept + float(contributions.sum()),
                            "contributions": all_items, "top_factors": top,
                            "caveat": "Additive log-odds contributions, not causal effects or probability changes."}}
