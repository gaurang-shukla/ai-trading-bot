#!/usr/bin/env python3
"""Isolated TradingAgents graph smoke test, independent of the Signal server."""
import os
import sys
import traceback
from datetime import date
from importlib import metadata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tradebot.adapters import _bounded_tradingagents_config, _import_attribute
from tradebot.config import load_project_env


def main() -> int:
    load_project_env()
    try:
        graph_class = _import_attribute((
            ("tradingagents.graph.trading_graph", "TradingAgentsGraph"),
            ("tradingagents.graph", "TradingAgentsGraph"),
            ("tradingagents", "TradingAgentsGraph"),
        ))
        default = _import_attribute((
            ("tradingagents.default_config", "DEFAULT_CONFIG"),
            ("tradingagents.config", "DEFAULT_CONFIG"),
            ("tradingagents.config.default_config", "DEFAULT_CONFIG"),
            ("tradingagents", "DEFAULT_CONFIG"),
        ))
        config = _bounded_tradingagents_config(default.copy())
        model = os.getenv("OPENAI_MODEL", "") or config.get("quick_think_llm") or "gpt-4o-mini"
        config.update(llm_provider="openai", quick_think_llm=model,
                      deep_think_llm=os.getenv("OPENAI_DEEP_MODEL", "") or model)
        module = sys.modules[graph_class.__module__]
        print(f"module path: {Path(module.__file__).resolve()}")
        print(f"package version: {metadata.version('tradingagents')}")
        print(f"loaded classes: {graph_class.__module__}.{graph_class.__name__}")
        print(f"configured LLM: {model}")
        print(f"OpenAI key loaded: {bool(os.getenv('OPENAI_API_KEY'))}")
        graph = graph_class(debug=False, config=config)
        print("Prompt: Analyze BTCUSDT.\n\nReply with BUY SELL or HOLD.")
        _, response = graph.propagate("BTC-USD", date.today().isoformat())
        print("Raw response:")
        print(response)
        return 0
    except Exception:
        print("FULL PYTHON TRACEBACK", file=sys.stderr)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
