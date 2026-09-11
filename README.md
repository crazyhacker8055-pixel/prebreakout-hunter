# Pre-Breakout Hunter V10.0

Clean rebuild around the user's working Upstox Analytics Token.

## Data architecture
- NIFTY 500 universe from NSE constituent CSV with a public GitHub fallback.
- Upstox NSE equity instrument master for exact `NSE_EQ` instrument keys.
- Upstox V3 daily Historical Candle Data for completed daily setup analysis.
- Upstox V3 LTP for one fresh live market snapshot after the scan.
- No Yahoo Finance.
- No WebSocket.
- No 1-minute background engine.
- No intraday history seeding.
- No trading/order API.

## Scanner
Strong trend + progressive contraction + ATR/volume compression + higher lows + repeated resistance tests + relative strength + resistance still ahead + no recent breakout + distribution filter.

A candidate must pass all hard gates and the minimum score. Zero candidates is allowed.
