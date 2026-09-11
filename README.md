# Pre-Breakout Hunter V11.0

Upstox-only NIFTY 500 daily pre-breakout scanner.

## Design
- Exact NIFTY 500 universe and Upstox NSE_EQ mapping.
- Upstox V3 completed daily candles only.
- No Yahoo Finance, WebSocket, intraday polling, or trading APIs.
- Live LTP is fetched only after a completed-daily scan.
- Scanner: trend + progressive contraction + volatility/volume compression + higher lows + resistance tests + relative strength + resistance still ahead + breakout protection + distribution filter.
- Current incomplete daily candle is excluded automatically; after the NSE session closes, the completed day's candle becomes eligible.

## Streamlit Secret
`UPSTOX_ACCESS_TOKEN`
