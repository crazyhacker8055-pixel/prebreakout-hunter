# Pre-Breakout Hunter V21.0 — LIVE-FIRST

This version implements the requested live-first architecture for NSE/NIFTY500.

### Scan flow
1. Completed daily candles establish the stable context: trend, contraction, ATR/volume compression, higher lows, prior 20-session resistance, relative strength and 52-week context.
2. Every **RUN FULL SCANNER** makes a **fresh, uncached Upstox V3 `1d` OHLC request** for the whole mapped universe.
3. It also makes a fresh V3 **`I1` (1-minute) OHLC request** for the whole mapped universe and uses the latest minute for intraday pressure.
4. The live decision is recalculated from the current LTP/current-session OHLC. The historical daily score is **not a universe pre-filter**.
5. Results are classified as **LIVE BREAKOUT NOW**, **READY NOW**, and **NEAR-MISS NOW**. If fewer than five strict near-misses exist, the closest live WATCH names are promoted to a clearly labelled NEAR-MISS watchlist instead of showing an empty list.
6. Historical validation is performed only for the strongest live candidates.
7. Trade plans show planned entry, stop loss and 2R/3R/4R targets. They are planning levels, not guarantees.

### Freshness
During the NSE session, a current-session quote is considered fresh only when its Upstox candle timestamp is no more than 180 seconds old. Outside the session, the last quote can be displayed but is not treated as an active market signal.

### Interpretation
`LIVE BREAKOUT NOW` is an **intraday** condition: current LTP has reached/crossed prior resistance. It is not the same as the original daily-close confirmation rule. If you follow the original rule, wait for the daily close and enter the next session.

### Upstox
Requires `UPSTOX_ACCESS_TOKEN` in Streamlit Secrets. No Yahoo Finance, order API, or WebSocket is required.
