# Pre-Breakout Hunter V9.2.1

V9.2.1 hardens the NIFTY 500 live-universe mapping before live data is connected to the pre-breakout engine.

## What changed
- Uses the official NSE Indices NIFTY 500 constituent CSV as the primary universe source.
- Keeps public mirrors only as fallbacks if the official download is temporarily unavailable.
- Filters obvious `DUMMY*` index-calculation placeholders so they cannot consume live WebSocket slots or become scanner candidates.
- Uses the Upstox NSE BOD instrument master for exact `NSE_EQ` mapping.
- If a valid equity is temporarily missing from the downloaded BOD file, the app uses Upstox Instrument Search as a per-symbol repair path.
- Displays repaired symbols separately from genuinely unmapped symbols.
- Keeps the read-only LTPC WebSocket architecture from V9.2.
- No order/trading API is used.

NSE Indices publishes the NIFTY 500 constituent file, and Upstox recommends the unique `instrument_key` from its instrument data for market-data APIs. Upstox's V3 LTPC feed currently allows up to 5,000 instrument keys per individual subscription, so the NIFTY 500 universe is within the documented limit.

## Deploy
Replace `app.py` and `requirements.txt` in the existing GitHub `main` branch. Keep the existing Streamlit Secret:

`UPSTOX_ACCESS_TOKEN = "your_token"`

Do not put the token in GitHub.
