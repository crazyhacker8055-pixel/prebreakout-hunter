# Pre-Breakout Hunter V9.5.4

NIFTY 500 pre-breakout scanner with Upstox read-only market data.

V9.5.4 adds a live-data integrity layer to the focused intraday radar:
- Parses Upstox 1-minute candle timestamps as timezone-aware values.
- Shows LIVE / DELAYED / STALE status and candle age.
- Rejects radar candidates whose latest 1-minute candle is older than 150 seconds.
- Keeps the existing selective V8 daily scanner and V9.5 focused radar logic unchanged.
- Uses detailed 1-minute OHLCV only for the focused daily candidates + strongest near-misses.
- No trading/order API is used.
