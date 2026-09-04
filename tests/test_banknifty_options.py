from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from tradebot.app import app
from tradebot.models import IndexOption, MarketSnapshot
from tradebot.options import (atm_strike, filter_options, normalize_banknifty_option,
                              prepare_chain, score_option)

client = TestClient(app)

RAW = [
    {'expiration': '2026-09-10', 'strike': 51000, 'option_type': 'CE', 'last_price': 320,
     'change': 3.2, 'volume': 9000, 'open_interest': 25000, 'iv': 18.5,
     'delta': .55, 'gamma': .001, 'theta': -12, 'vega': 8, 'bid': 318, 'ask': 322},
    {'expiration': '2026-09-10', 'strike': 51500, 'option_type': 'PE', 'last_price': 410,
     'change': -1.1, 'volume': 7000, 'open_interest': 22000, 'iv': 19.1},
]


def test_banknifty_options_route_exists_and_card_is_visible():
    assert client.get('/market/banknifty_options').status_code == 200
    assert 'Bank Nifty Options' in client.get('/assets/app.js').text


def test_unavailable_provider_is_clean_and_contains_no_fake_data():
    with patch('tradebot.app.OpenBBClient.option_chain', side_effect=RuntimeError('secret raw failure')):
        response = client.get('/api/banknifty-options')
    assert response.status_code == 200
    result = response.json()
    assert result['available'] is False
    assert result['contracts'] == []
    assert result['message'] == 'Bank Nifty options data provider not configured yet.'
    assert 'secret raw failure' not in str(result)


def test_option_rows_normalize_atm_and_filter_ce_pe_moneyness():
    prepared = prepare_chain(RAW, 51240, 'BUY')
    assert prepared['atm_strike'] == 51000
    ce = filter_options(prepared['contracts'], option_type='CE')
    pe = filter_options(prepared['contracts'], option_type='PE')
    assert [row['option_type'] for row in ce] == ['CE']
    assert [row['option_type'] for row in pe] == ['PE']
    assert filter_options(prepared['contracts'], moneyness='ATM')[0]['strike'] == 51000
    assert ce[0]['underlying_price'] == 51240
    assert ce[0]['moneyness'] == 'ATM'
    assert ce[0]['distance_from_spot_pct'] > 0
    assert ce[0]['bid'] == 318 and ce[0]['ask'] == 322


def test_atm_calculation_and_option_score_schema_are_stable():
    assert atm_strike([50500, 51000, 51500], 51240) == 51000
    row = normalize_banknifty_option(RAW[0], 51240, 51000)
    assert isinstance(row, IndexOption)
    result = score_option(row, 'BUY')
    assert set(result) == {'action', 'confidence', 'risk_score', 'reason',
                           'suggested_stop_loss', 'suggested_target'}
    assert result['action'] in {'BUY CE', 'BUY PE', 'AVOID', 'WATCH'}


def test_available_api_normalizes_real_openbb_rows():
    spot = Mock()
    spot.snapshot.return_value = MarketSnapshot('BANKNIFTY', 51240, 'now', 'Yahoo Finance')
    registry = Mock()
    registry.market_data.return_value = spot
    with patch('tradebot.app.OpenBBClient.option_chain', return_value={
            'contracts': RAW, 'expiries': ['2026-09-10']}), \
         patch('tradebot.app.default_registry', return_value=registry):
        result = client.get('/api/banknifty-options?option_type=CE&underlying_side=BUY').json()
    assert result['available'] is True
    assert result['atm_strike'] == 51000
    assert len(result['contracts']) == 1
    assert result['contracts'][0]['signal']['action'] in {'BUY CE', 'AVOID'}
    assert result['notice'] == 'Research only. Live options trading is disabled.'
