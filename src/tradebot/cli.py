import argparse
import json
from datetime import date

from .adapters import PaperclipReporter, TradingAgentsClient
from .execution import PaperBroker
from .models import MarketKind, MarketSelection
from .risk import RiskEngine, RiskLimits
from .service import TradingService
from .venues import default_registry


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one guarded paper-trading decision")
    parser.add_argument("symbol")
    parser.add_argument("--market", choices=[item.value for item in MarketKind],
                        default=MarketKind.CRYPTO_FUTURES.value)
    parser.add_argument("--venue", default="weex")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--equity", type=float, default=100_000)
    args = parser.parse_args()
    selection = MarketSelection(MarketKind(args.market), args.venue, args.symbol.upper())
    data = default_registry().market_data(selection)
    service = TradingService(data, TradingAgentsClient(), RiskEngine(RiskLimits()),
                             PaperBroker(args.equity), PaperclipReporter())
    print(json.dumps(service.run(args.symbol, args.date, args.equity), indent=2, default=str))


if __name__ == "__main__":
    main()
