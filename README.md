# Pre-Breakout Hunter V15.0

Upstox-only NIFTY 500 pre-breakout scanner for mobile-friendly Streamlit deployment.

## Architecture
1. Load NIFTY 500 universe and exact Upstox NSE_EQ instrument keys.
2. Scan completed daily candles for the strict pre-breakout structure.
3. Read the **current trading-session OHLC + LTP** for the mapped universe using Upstox Market Quote V3.
4. Enrich candidates with live range, live compression, pivot pressure, 7D/20D formation ranges and current distance to resistance.
5. Separate results into **READY**, **NEAR-MISS**, and **TRIGGERED**.
6. Backtest only the strongest candidates against their own historical formations.
7. Show true 5-session and 10-session breakout rates, selected-window validated success rate, gain/drawdown statistics and formation charts.

## Important validation rules
- Structural scan uses completed daily candles only, so the unfinished current candle cannot contaminate the historical indicators.
- The current trading session is added only through Upstox V3 live OHLC/LTP.
- Historical validation always observes 10 future sessions so the displayed 5D and 10D breakout rates are genuine horizons.
- The sidebar's 2/3/5-year validation choice controls the historical window.
- The success window can be 5, 7 or 10 sessions.
- A candidate at/above its resistance is **TRIGGERED**, not READY.
- Historical percentages are event-study statistics, not guaranteed future probabilities.

## Strategy structure
- Close > SMA50 > SMA150 > SMA200, rising SMA200, Close > EMA20.
- Progressive 5D < 10D < 20D range contraction.
- ATR and volume contraction plus candle-body compression.
- Higher lows and repeated resistance tests.
- Price just below prior 20-session resistance and near the 52-week high.
- No recent breakout/distribution.
- Relative strength versus NIFTY 50.
- Live price/range pressure is evaluated separately from the completed setup.

## Streamlit Secret
Set:
`UPSTOX_ACCESS_TOKEN`

The app intentionally uses no Yahoo Finance, no WebSocket dependency, no intraday-history polling engine and no order/trading API.
