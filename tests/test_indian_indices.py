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


def test_bank_nifty_index_asset_keeps_quick_and_fast_ai_without_options_ui():
    javascript = client.get('/assets/app.js').text
    asset_renderer = javascript.split('function renderAssetSetup', 1)[1].split(
        'async function assetPage', 1)[0]

    assert "signalPanel(quick,'QUICK SIGNAL · NO AI')" in asset_renderer
    assert 'FAST AI EXPLANATION' in asset_renderer
    assert 'Explain Quick Signal' in asset_renderer
    assert 'OPTIONS PREPARATION' not in asset_renderer
    assert 'option-chain research requires' not in asset_renderer


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
    assert result['signal']
    availability = result['advanced_research_availability']
    assert availability['advanced_research_available'] is False
    assert availability['availability_status'] == 'unsupported_market'
    assert availability['recommended_action'] == 'Use Quick Signal and Fast AI Explanation'
    assert 'Indian index symbols' in availability['availability_reason']
    assert 'option-chain' not in availability['availability_reason']
    assert result['notice'].startswith('Deterministic quick signal')
    assert len(result['warnings']) == 4
    assert {call.args[1] for call in provider.candles.call_args_list} == {
        '5m', '15m', '1h', '1d'}
