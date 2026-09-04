from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from tradebot.adapters import (NormalizedMarketData, OpenBBClient,
                               YahooFinanceClient, research_symbol)
from tradebot.app import app
from tradebot.models import MarketKind, MarketSelection, MarketSnapshot
from tradebot.overview import MarketOverviewService, SEED_UNIVERSES
from tradebot.venues import default_registry

client = TestClient(app)


def test_indian_indices_market_route_and_defaults_exist():
    assert client.get('/market/indian_indices').status_code == 200
    choices = client.get('/api/markets').json()
    assert {'market': 'indian_indices', 'venue': 'openbb'} in choices
    assert SEED_UNIVERSES[MarketKind.INDIAN_INDICES][:4] == [
        'BANKNIFTY', 'NIFTY50', 'FINNIFTY', 'MIDCPNIFTY']


def test_bank_nifty_maps_to_research_symbol_but_preserves_identity():
    assert research_symbol('BANKNIFTY') == '^NSEBANK'
    upstream = Mock()
    upstream.snapshot.return_value = MarketSnapshot(
        '^NSEBANK', 51_250, 'now', 'Yahoo Finance')
    normalized = NormalizedMarketData(upstream)

    result = normalized.snapshot('BANKNIFTY')

    upstream.snapshot.assert_called_once_with('^NSEBANK')
    assert result.symbol == 'BANKNIFTY'
    assert 'BANK NIFTY' in client.get('/assets/app.js').text


def test_indian_indices_use_yahoo_then_openbb_and_never_weex():
    providers = MarketOverviewService()._providers(
        MarketKind.INDIAN_INDICES, 'BANKNIFTY')
    assert providers[0][0] == providers[1][0] == '^NSEBANK'
    assert isinstance(providers[0][1], YahooFinanceClient)
    assert isinstance(providers[1][1], OpenBBClient)
    adapter = default_registry().market_data(MarketSelection(
        MarketKind.INDIAN_INDICES, 'openbb', 'BANKNIFTY'))
    assert all('Weex' not in type(item.provider).__name__
               for item in adapter.providers)


def test_missing_indian_index_candles_fall_back_to_quick_signal_without_openai():
    provider = Mock()
    provider.snapshot.return_value = MarketSnapshot(
        'BANKNIFTY', 51_250, 'now', 'Yahoo Finance', 0.8, 1_000_000)
    provider.candles.side_effect = ValueError('interval unavailable')
    registry = Mock()
    registry.market_data.return_value = provider
    with patch('tradebot.app.default_registry', return_value=registry), \
         patch.dict('os.environ', {}, clear=True):
        response = client.post('/api/analyze/quick', json={
            'market': 'indian_indices', 'venue': 'openbb',
            'symbol': 'BANKNIFTY', 'equity': 100000})

    assert response.status_code == 200
    result = response.json()
    assert result['fallback'] is True
    assert result['signal']['symbol'] == 'BANKNIFTY'
    assert result['notice'].startswith('Deterministic quick signal')
    assert len(result['warnings']) == 4
    assert {call.args[1] for call in provider.candles.call_args_list} == {
        '5m', '15m', '1h', '1d'}


def test_hdfc_deep_ai_skips_incomplete_ohlcv_without_exposing_exception():
    provider = Mock()
    provider.snapshot.return_value = MarketSnapshot(
        'HDFCBANK.NS', 1_950, 'now', 'Yahoo Finance', 0.2, 1000)
    provider.candles.side_effect = RuntimeError(
        "NoMarketDataError: latest in-range OHLCV bar has no closing price")
    registry = Mock()
    registry.market_data.return_value = provider
    with patch('tradebot.app.default_registry', return_value=registry), \
         patch('tradebot.app.signals.analyze') as analyze:
        response = client.post('/api/analyze/deep', json={
            'market': 'indian_indices', 'venue': 'openbb',
            'symbol': 'HDFCBANK.NS', 'equity': 100000, 'refresh': True})

    assert response.status_code == 200
    result = response.json()
    assert result['ai_available'] is False
    assert result['ai_notice'] == ('Deep AI unavailable for this Indian asset because provider '
                                   'OHLCV data is incomplete. Quick Signal remains available.')
    assert 'NoMarketDataError' not in str(result)
    analyze.assert_not_called()
