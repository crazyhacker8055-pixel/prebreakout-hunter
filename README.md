# Pre-Breakout Hunter V20.0

Live-at-scan-time NSE/NIFTY500 pre-breakout scanner using Upstox market data.

## What V20 does
- Uses completed daily candles only for the stable trend/base/resistance structure.
- Reads the **current Upstox LTP + current-session OHLC when RUN FULL SCANNER is pressed**.
- Recalculates live distance to resistance, live range, live compression and live stage from that exact scan-time price.
- Produces **LIVE BREAKOUT NOW**, **READY NOW**, or **NEAR-MISS NOW**.
- Does not wait for today's daily candle to close to identify an intraday breakout.
- Re-running the scanner later recalculates the result from the new live market price.
- Historical validation is candidate-only and uses the optimized historical feature engine.
- Includes planned entry, stop loss and 2R/3R/4R targets.

## Data/API
- Upstox Analytics Token
- Upstox V3 historical daily candles
- Upstox V3 current-session OHLC/LTP
- No Yahoo Finance
- No order/trading API
- No WebSocket dependency

## Important interpretation
LIVE BREAKOUT NOW means the current price is at/above the prior resistance at the scan time. It is an intraday condition, not a guaranteed daily-close breakout. The user can run the scanner again at any later time to recalculate the current state.
