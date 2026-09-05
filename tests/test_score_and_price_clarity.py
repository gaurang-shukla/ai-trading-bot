from pathlib import Path


APP_JS = Path("src/tradebot/web/app.js").read_text()


def test_opportunity_score_is_distinct_from_a_trade_recommendation():
    assert "Screen score" not in APP_JS
    assert "Signal score</th>" not in APP_JS
    assert "Opportunity score" in APP_JS
    assert "Opportunity score is not a trade recommendation" in APP_JS
    assert "A high opportunity score means this asset is worth watching" in APP_JS
    assert "A trade opens only when Quick Signal finds an active setup" in APP_JS


def test_rankings_allow_high_hold_and_lower_buy_and_explain_the_difference():
    assert "${actionStatus(r)}" in APP_JS
    assert "HOLD (watch only)" in APP_JS
    assert "a lower-score asset can have BUY while a higher-score asset remains HOLD" in APP_JS
    assert "Paper trade available" in APP_JS


def test_tiny_prices_use_adaptive_precision_on_every_price_surface():
    assert "if(n>=.01)return {minimum:4,maximum:6,precision:6}" in APP_JS
    assert "if(n>=.0001)return {minimum:6,maximum:8,precision:8}" in APP_JS
    assert "maximumSignificantDigits:10" in APP_JS
    assert "chartPriceFormat(candles.at(-1)?.close??data.live_price)" in APP_JS
    assert "${price(level.price)}" in APP_JS  # chart legend
    assert "${safe(price(item.value))}" in APP_JS  # fallback chart labels
    assert "${price(data.live_price,market,symbol)}" in APP_JS  # confirmation
    assert "${price(x.entry_price,x.market,x.symbol)}" in APP_JS  # dashboard/trades
    assert "${price(x.latest_price,x.market,x.symbol)}" in APP_JS  # watchlist


def test_hold_has_one_clear_paper_message_and_no_open_button():
    panel = APP_JS.split("function paperActions", 1)[1].split("function bindPaperActions", 1)[0]
    assert panel.count("No active trade setup") == 1
    assert "No active trade setup — watch support/resistance for confirmation." in panel
    assert "copy.active?`<button id=\"open-paper\"" in panel
    assert '<button id="watch-asset"' in panel
