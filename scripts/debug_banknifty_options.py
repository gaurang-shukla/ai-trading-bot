#!/usr/bin/env python3
"""Probe Bank Nifty option providers without starting the web application."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tradebot.adapters import OpenBBClient
from tradebot.banknifty_options import (NSEOptionChainClient, build_chain,
    classify_openbb_failure, empty_provider_diagnostic)


def yes(value): return "yes" if value else "no"


def main():
    openbb = OpenBBClient(asset_class="index")
    od = empty_provider_diagnostic("openbb")
    od.update(attempted=True, final_url=f"{openbb.base_url}/api/v1/derivatives/options/chains?symbol=BANKNIFTY")
    print("OpenBB attempted: yes")
    try:
        raw = openbb.option_chain("BANKNIFTY")
        if not raw.get("contracts"):
            od["failure_category"] = "openbb_empty_chain"
        else:
            od["normalized_contract_count"] = len(raw["contracts"])
        print("OpenBB result or failure category:", od["failure_category"] or "contracts_received")
    except Exception as exc:
        od["failure_category"] = classify_openbb_failure(exc)
        od["sanitized_error"] = "Local OpenBB service refused the connection" if od["failure_category"] == "openbb_connection_refused" else "OpenBB option-chain request failed"
        print("OpenBB result or failure category:", od["failure_category"])
        print("OpenBB URL:", od["final_url"])
        print("OpenBB sanitized error:", od["sanitized_error"])

    nse = NSEOptionChainClient()
    print("NSE attempted: yes")
    contracts = []
    try:
        raw = nse.option_chain()
        contracts = build_chain(raw, raw["underlying_price"])["contracts"]
    except Exception as exc:
        if not nse.diagnostic["sanitized_error"]:
            nse.diagnostic["sanitized_error"] = "NSE option-chain request failed"
    d = nse.diagnostic
    print("NSE initial cookie request status:", nse.initial_status_code)
    print("NSE option-chain request status:", d["status_code"])
    print("content type:", d["content_type"])
    print("JSON received:", yes(d["got_json"]))
    print("top-level JSON keys:", nse.top_level_json_keys)
    print("records.data count:", d["raw_row_count"])
    print("CE count:", d["ce_count"])
    print("PE count:", d["pe_count"])
    print("normalized contract count:", len(contracts) or d["normalized_contract_count"])
    print("sample 3 normalized contracts:", json.dumps(contracts[:3], indent=2, default=str))
    print("NSE failure category:", d["failure_category"] or "connected")
    print("NSE sanitized error:", d["sanitized_error"])


if __name__ == "__main__": main()
