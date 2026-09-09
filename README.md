# Pre-Breakout Hunter V9.2

V9.2 adds a full NIFTY 500 Upstox live-universe test while retaining the V8.9 scanner and dashboards.

## Upstox V9.2
- Uses the read-only `UPSTOX_ACCESS_TOKEN` stored in Streamlit Secrets.
- Downloads the current Upstox NSE instrument master and maps NIFTY 500 trading symbols to `NSE_EQ` instrument keys.
- Streams the mapped universe through Upstox Market Data Feed V3 in `ltpc` mode.
- Displays mapped/unmapped counts, updating instruments, latest feed time, LTP, previous close, change %, last quantity and trade time.
- No order/trading API is used.

Upstox documents a 5,000-instrument individual LTPC subscription limit, so the NIFTY 500 universe is within the documented limit.

## Deploy
Replace `app.py` and `requirements.txt` in the existing GitHub `main` branch. Keep the existing Streamlit Secret:

`UPSTOX_ACCESS_TOKEN = "your_token"`

Do not put the token in GitHub.
