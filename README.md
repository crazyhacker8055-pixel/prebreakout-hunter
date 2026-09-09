# Pre-Breakout Hunter — GitHub + Streamlit only

A strict NSE pre-breakout scanner designed from the user's chart pattern: strong trend, tight multi-day consolidation, volatility contraction, volume dry-up, higher-low base, relative strength, and price sitting just below resistance.

## No broker API
This version does **not** use Dhan, broker APIs, API keys, WebSockets, or a database.

It uses:
- GitHub for the code
- Streamlit Community Cloud for hosting
- Yahoo Finance public market data through `yfinance`
- Public NIFTY 500 constituent list

## Deploy from a phone
1. Create a GitHub repository, e.g. `prebreakout-hunter`.
2. Upload `app.py`, `requirements.txt`, and `README.md`.
3. Open Streamlit Community Cloud and create a new app from that GitHub repository.
4. Select `app.py` as the main file.
5. Deploy.
6. No secrets or API credentials are required.

## Scanner logic
Hard filters:
- Close > 50 SMA > 150 SMA > 200 SMA
- 200 SMA rising
- Close > 20 EMA
- 5D range <= 4.5%
- 10D range <= 7.5%
- 20D range <= 12%
- 5D range < 10D range < 20D range
- ATR5 / ATR20 <= 0.72
- 5D volume / 20D volume <= 0.70
- Close 0.5%–4% below the previous 20-session high
- No recent closing breakout
- Higher-low base
- No excessive down-volume distribution
- Stock's 20-session return >= NIFTY's 20-session return
- Quality score >= 80 by default

The result is a **watchlist candidate**, not an automatic buy signal.

## Important limitation
Yahoo Finance is a public third-party data source and can be delayed, rate-limited, or temporarily unavailable. This project is deliberately API-key-free. For serious live trading, validate every candidate on your broker/chart before entering.
