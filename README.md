# Pre-Breakout Hunter V24.0 — FINAL STRICT RELEASE

Phone-first NSE/NIFTY500 pre-breakout scanner using Upstox Analytics Token.

## Final design
- Whole mapped universe live Upstox snapshot.
- Completed daily candles establish the structural setup.
- Current-session Upstox 1D OHLC + LTP regrades the setup at scan time.
- Latest 1-minute pressure is used as a secondary live-pressure signal.
- Strict READY / NEAR-MISS / LIVE BREAKOUT classification.
- No artificial WATCH → NEAR-MISS promotion. Zero candidates is a valid result.
- Maximum 5 cards per stage for a readable phone dashboard.
- Historical success is sample-adjusted for ranking; raw rate, event count and evidence grade remain visible.
- Historical validation uses true 5-session and 10-session breakout horizons.
- Historical rolling features are precomputed once, avoiding the old O(n²) backtest slowdown.
- Candlestick formation charts use completed daily candles plus the current live candle.
- Rule-based planned entry, stop and 2R/3R/4R targets.

## Important interpretation
A LIVE BREAKOUT is an intraday/live condition and is not the strategy's confirmed daily-close entry. The original strategy remains close-confirmed with next-session entry. Historical percentages are research statistics, not guaranteed probabilities.

## Deployment
1. Put `UPSTOX_ACCESS_TOKEN` in Streamlit Secrets.
2. Deploy `app.py` with `requirements.txt`.
3. Run the scanner during NSE market hours for the live layer.
4. A strict scan may legitimately return zero READY setups.
