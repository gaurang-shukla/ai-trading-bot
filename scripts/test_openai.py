#!/usr/bin/env python3
"""Minimal OpenAI smoke test using exactly the model configured for Signal."""
import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tradebot.config import load_project_env


def main() -> int:
    load_project_env()
    model = os.getenv("OPENAI_MODEL", "") or "gpt-4o-mini"
    try:
        from openai import OpenAI
        print(f"OpenAI model: {model}")
        print(f"OpenAI key loaded: {bool(os.getenv('OPENAI_API_KEY'))}")
        response = OpenAI().responses.create(model=model, input="Say hello.")
        print(response.output_text)
        return 0
    except Exception:
        print("FULL PYTHON TRACEBACK", file=sys.stderr)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
