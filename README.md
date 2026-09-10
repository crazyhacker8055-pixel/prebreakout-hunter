# Pre-Breakout Hunter V9.5.2

V9.5.2 keeps the NIFTY 500 live Upstox V3 OHLC engine but changes the intraday history architecture: 1-minute history is seeded only for the focused daily strict-candidate + near-miss watchlist (up to 30 stocks), rather than all 500. This reduces server/API load and makes the live radar directly aligned with the final pre-breakout workflow.

Deploy `app.py`, `requirements.txt`, and this README to the GitHub repository. Keep `UPSTOX_ACCESS_TOKEN` in Streamlit Secrets.
