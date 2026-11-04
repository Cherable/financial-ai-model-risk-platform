"""Score a local JSON sample with an explicitly registered fixed model version."""

import argparse
import json
from pathlib import Path

from app.modules.models.bundle import DEFAULT_ARTIFACT_ROOT, load_registered
from app.modules.scoring.predict import score_one


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="credit-logreg-v1")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        raw = json.loads(args.input.read_text(encoding="utf-8"))
        result = score_one(load_registered(args.version, args.artifacts), raw)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"scoring failed: {exc}\n")
    text = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
