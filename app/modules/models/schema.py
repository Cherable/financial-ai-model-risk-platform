"""Shared feature contract, intentionally independent of ML libraries."""

import math
from decimal import Decimal, InvalidOperation

FEATURES = ("age", "monthly_income", "debt_income_ratio", "past_due_count",
            "loan_amount", "employment_years")
BOUNDS = {"age": (18, 80), "monthly_income": (0, 1000000), "debt_income_ratio": (0, 3),
          "past_due_count": (0, 100), "loan_amount": (0.01, 10000000), "employment_years": (0, 60)}
MONEY = {"monthly_income", "loan_amount"}
INTEGERS = {"age", "past_due_count"}


def feature_schema() -> dict:
    return {"schema_version": "1.0", "feature_order": list(FEATURES),
            "excluded_columns": ["customer_ref", "synthetic_default_label"],
            "features": [{"name": f, "nullable": True, "min": BOUNDS[f][0], "max": BOUNDS[f][1],
                          "type": "decimal_string" if f in MONEY else "integer" if f in INTEGERS else "number",
                          "unit": "CNY" if f in MONEY else "years" if f in ("age", "employment_years")
                          else "count" if f == "past_due_count" else "ratio"} for f in FEATURES]}


def normalize_input(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("input must be an object")
    unknown = set(raw) - set(FEATURES) - {"customer_ref"}
    if unknown:
        raise ValueError("unknown or forbidden columns: " + ", ".join(sorted(unknown)))
    result = {}
    for field in FEATURES:
        value = raw.get(field)
        if value is None or value == "":
            result[field] = None
            continue
        if isinstance(value, bool) or (field in MONEY and not isinstance(value, str)):
            raise ValueError(f"{field}: invalid type; money requires a decimal string")
        try:
            number = Decimal(str(value))
            low, high = BOUNDS[field]
            if not number.is_finite() or not Decimal(str(low)) <= number <= Decimal(str(high)):
                raise ValueError("out of range")
            if field in INTEGERS and number != number.to_integral_value():
                raise ValueError("integer required")
            if field in MONEY:
                if number != number.quantize(Decimal("0.01")):
                    raise ValueError("at most two decimal places")
                result[field] = format(number, ".2f")
            else:
                result[field] = int(number) if field in INTEGERS else float(number)
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError(f"{field}: invalid value {value!r}") from exc
    return result


def numeric_row(normalized: dict) -> list[float]:
    return [math.nan if normalized[f] is None else float(normalized[f]) for f in FEATURES]
