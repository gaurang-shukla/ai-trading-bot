from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import subprocess
import threading
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from tradebot.app import create_app
from tradebot.background import DEFAULTS, LocalState, PaperMonitor, Scanner, now
from tradebot.models import MarketKind
from tradebot.paper import PaperStore
from tradebot.weex_universe import WeexUniverseService


def local(tmp_path):
    return LocalState(PaperStore(tmp_path / 'paper.db').path)


def quick(market, symbol):
    return {'signal': {'side': 'BUY', 'confidence': .8, 'probability': .7},
            'risk_plan': {'risk_score': .2, 'stop_loss': 90, 'take_profit': 120, 'position_size_pct': .02},
            'live_price': 100, 'last_updated': now(),
            'plain_language_reason': 'Bullish momentum confirmed by candles.',
            'timeframe_breakdown': [{'timeframe': '1h', 'trend': 'Bullish'}]}


def universe(count=4):
    return WeexUniverseService(loader=lambda market, page, cursor: [
        {'symbol': f'COIN{i}USDT', 'lastPrice': 100, 'quoteVolume': 1000 * i, 'priceChangePercent': i,
         **({'fundingRate': 0} if market == MarketKind.CRYPTO_FUTURES else {})}
        for i in range(count)])


def test_scanner_separation_prefilter_ranking_missing_data(tmp_path):
    calls = []
    def analyze(market, symbol):
        calls.append((market, symbol))
        result = quick(market, symbol)
        result['signal']['confidence'] = .9 if symbol == 'COIN3USDT' else .7
        return result
    job = Scanner(local(tmp_path), universe(), analyze, candidates=2)
    assert job.run()
    rows = job.latest()['results']
    assert len(rows) == 4
    assert {m for m, _ in calls} == {MarketKind.CRYPTO_SPOT, MarketKind.CRYPTO_FUTURES}
    assert {s for _, s in calls} == {'COIN2USDT', 'COIN3USDT'}
    assert rows[0]['symbol'] == 'COIN3USDT'
    assert all(r['order_book_imbalance'] is None and r['open_interest'] is None for r in rows)
    assert all(r['confidence'] < .9 and r['why'] and r['multi_timeframe_agreement'] for r in rows)
    assert next(r for r in rows if r['market'] == 'crypto_futures')['funding_rate'] == 0
    assert next(r for r in rows if r['market'] == 'crypto_spot')['funding_rate'] is None
    assert job.status()['instruments_considered'] == 8


def test_bounded_concurrency_overlap_and_stale_restart(tmp_path):
    entered, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    active, maximum = 0, 0
    def analyze(market, symbol):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        entered.set()
        assert release.wait(5)
        with lock:
            active -= 1
        return quick(market, symbol)
    state = local(tmp_path)
    provider = universe()
    job = Scanner(state, provider, analyze, candidates=3, concurrency=2)
    assert job.request_run()
    assert entered.wait(2)
    assert not job.request_run() and not job.run()
    release.set()
    job.manual_thread.join(5)
    assert not job.manual_thread.is_alive() and maximum <= 2
    good = job.latest()['results']
    provider.loader = Mock(side_effect=RuntimeError('sensitive error'))
    provider._cache.clear()
    job.run()
    stale = job.latest()
    assert stale['stale'] and stale['results']
    assert {r['symbol'] for r in good} == {r['symbol'] for r in stale['results']}
    assert all(r['stale'] for r in stale['results'])
    restored = Scanner(state, provider, quick).latest()
    assert all(r['stale'] for r in restored['results'])
    assert 'sensitive' not in str(job.status())


@pytest.mark.parametrize('changes', [{'max_open_positions': 0}, {'futures_paper_leverage': 11},
    {'scanner_interval_seconds': 1}, {'monitor_interval_seconds': True}, {'live_execution': True},
    {'default_stop_loss_pct': float('nan')}, {'max_open_positions': 1.5}])
def test_setting_validation(tmp_path, changes):
    state = local(tmp_path)
    with pytest.raises(ValueError):
        state.update_settings(changes)
    assert state.settings() == DEFAULTS


def test_settings_persist_and_futures_only_leverage(tmp_path):
    state = local(tmp_path)
    state.update_settings({'futures_paper_leverage': 2})
    assert LocalState(state.path).settings()['futures_paper_leverage'] == 2
    store = PaperStore(state.path)
    for market, quantity in [('crypto_spot', 10), ('crypto_futures', 20)]:
        pos = store.open_position(market=market, symbol='TESTUSDT', display_name='Test', side='LONG',
            price=100, notional=1000, signal='BUY', settings=state.settings())
        assert pos['quantity'] == quantity
        assert pos['stop_loss'] == 98 and pos['take_profit'] == 104
        trade = store.close_position(pos['id'], 110)
        assert trade['realized_pnl'] == quantity * 10
    assert store.account()['cash_balance'] == 100300


def test_risk_limits_enforced_in_transaction(tmp_path):
    store = PaperStore(tmp_path / 'risk.db')
    settings = {**DEFAULTS, 'max_open_positions': 1, 'max_position_allocation_pct': 1}
    args = dict(market='crypto_spot', display_name='Test', side='LONG', price=100, signal='BUY', settings=settings)
    with pytest.raises(ValueError, match='allocation'):
        store.open_position(symbol='BIG', notional=2000, **args)
    store.open_position(symbol='ONE', notional=1000, **args)
    with pytest.raises(ValueError, match='simultaneous'):
        store.open_position(symbol='TWO', notional=500, **args)


@pytest.mark.parametrize('quote,reason,fill', [(80, 'stop_loss', 90), (130, 'take_profit', 120)])
def test_background_monitor_and_duplicate_closes(tmp_path, monkeypatch, quote, reason, fill):
    monkeypatch.setenv('SIGNAL_DB_PATH', str(tmp_path / 'monitor.db'))
    provider = Mock(snapshot=Mock(return_value=Mock(price=quote)))
    monkeypatch.setattr('tradebot.app.default_registry', lambda: Mock(market_data=Mock(return_value=provider)))
    app = create_app()
    store = app.state.paper_store
    pos = store.open_position(market='crypto_spot', symbol='TESTUSDT', display_name='Test', side='LONG',
        price=100, notional=1000, signal='BUY', risk_plan={'stop_loss': 90, 'take_profit': 120})
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda _: app.state.paper_monitor.run(), range(3)))
    trades = store.trades()
    assert len(trades) == 1 and trades[0]['close_reason'] == reason
    assert trades[0]['exit_price'] == fill and trades[0]['evaluated_price'] == quote
    assert trades[0]['price_provider'] == 'WEEX' and trades[0]['evaluated_at']
    with pytest.raises(KeyError):
        store.close_position(pos['id'], quote)
    assert app.state.paper_monitor.status()['last_successful_evaluation']


def test_quote_outage_retains_mark(tmp_path, monkeypatch):
    monkeypatch.setenv('SIGNAL_DB_PATH', str(tmp_path / 'outage.db'))
    monkeypatch.setattr('tradebot.app.default_registry', Mock(side_effect=RuntimeError('outage')))
    app = create_app()
    store = app.state.paper_store
    store.open_position(market='crypto_futures', symbol='TESTUSDT', display_name='Test', side='LONG',
        price=100, notional=1000, signal='BUY', risk_plan={'stop_loss': 90, 'take_profit': 120})
    app.state.paper_monitor.run()
    assert store.positions()[0]['current_price'] == 100
    assert not store.positions()[0]['price_available'] and not store.trades()
    assert app.state.paper_monitor.status()['stale']


def test_manual_api_duplicates_and_smoke(tmp_path, monkeypatch):
    monkeypatch.setenv('SIGNAL_DB_PATH', str(tmp_path / 'api.db'))
    app = create_app()
    client = TestClient(app)
    app.state.scanner.universe = universe()
    entered, release = threading.Event(), threading.Event()
    def analyze(market, symbol):
        entered.set()
        assert release.wait(5)
        return quick(market, symbol)
    app.state.scanner.analyze = analyze
    assert client.post('/api/scanner/run').status_code == 202
    assert entered.wait(2)
    assert client.post('/api/scanner/run').status_code == 409
    release.set()
    app.state.scanner.manual_thread.join(5)
    for route in ['/', '/scanner', '/market/crypto_futures', '/market/crypto_spot', '/paper',
                  '/api/status', '/api/scanner/status', '/api/scanner/results', '/api/paper/monitor/status', '/api/paper/settings']:
        assert client.get(route).status_code == 200, route
    assert client.put('/api/paper/settings', json={'live_execution': True}).status_code == 422
    assert client.put('/api/paper/settings', json={'monitor_interval_seconds': 10}).status_code == 200
    assert all('order' not in r.path and 'withdraw' not in r.path for r in app.routes)


def test_job_lifecycle_stops_without_network(tmp_path):
    job = PaperMonitor(local(tmp_path), lambda: [])
    job.start()
    job.stop()
    assert not job.thread.is_alive()
    assert not job.request_run()


def test_javascript_and_loading_recovery():
    for path in Path('src').rglob('*.js'):
        subprocess.run(['node', '--check', str(path)], check=True, capture_output=True)
    html = Path('src/tradebot/web/index.html').read_text()
    inline = html.split('<script>')[1].split('</script>')[0]
    subprocess.run(['node', '--check'], input=inline, text=True, check=True, capture_output=True)
    assert '15000' in inline and 'Signal could not start' in inline
    js = Path('src/tradebot/web/app.js').read_text()
    assert all(control in js for control in ['run-scan', 'scan-market', 'scan-action', 'scan-confidence',
        'scan-search', 'scan-watch', 'Why this setup?', 'paper-settings-form', 'Next scheduled evaluation', 'close-all-paper', 'refresh-paper'])
    assert 'scannerPage()' in js and 'href="/scanner"' in html
    sw = Path('src/tradebot/web/sw.js').read_text()
    assert 'v4-scanner' in sw and 'skipWaiting' in sw and "cache:'no-cache'" in sw


def test_exposure_and_daily_loss_limits(tmp_path):
    store = PaperStore(tmp_path / 'limits.db')
    args = dict(market='crypto_futures', symbol='TEST', display_name='Test', side='LONG', price=100, signal='BUY')
    with pytest.raises(ValueError, match='exposure'):
        store.open_position(notional=600, settings={**DEFAULTS, 'futures_paper_leverage': 2, 'max_total_exposure_pct': 1}, **args)
    pos = store.open_position(notional=2000, **args)
    store.close_position(pos['id'], 40)
    with pytest.raises(ValueError, match='Daily paper-loss'):
        store.open_position(notional=100, settings={**DEFAULTS, 'daily_loss_limit_pct': 1}, **args)


def test_empty_universe_keeps_last_good(tmp_path):
    provider = universe()
    job = Scanner(local(tmp_path), provider, quick, candidates=1)
    job.run()
    good = job.latest()['results']
    provider.loader = lambda *args: []
    provider._cache.clear()
    job.run()
    assert job.latest()['stale']
    assert len(job.latest()['results']) == len(good)


def test_lifespan_runs_and_stops_background_jobs(tmp_path, monkeypatch):
    monkeypatch.setenv('SIGNAL_DB_PATH', str(tmp_path / 'lifecycle.db'))
    monkeypatch.setattr('tradebot.app.startup_diagnostics', lambda: None)
    app = create_app()
    app.state.scanner.universe = universe(2)
    app.state.scanner.analyze = quick
    with TestClient(app) as client:
        assert client.get('/api/status').json()['mode'] == 'paper'
        assert app.state.scanner.thread.is_alive()
        assert app.state.paper_monitor.thread.is_alive()
    assert not app.state.scanner.thread.is_alive()
    assert not app.state.paper_monitor.thread.is_alive()
