# 🎯 Pre-Breakout Hunter — GitHub + Streamlit

Strict NSE pre-breakout scanner for the quiet compression **before** a breakout.

## No broker API
This version uses only:
- GitHub + Streamlit for the app/code
- Public GitHub mirror of the NIFTY 500 symbol list (NSE fallback)
- Yahoo Finance public daily OHLCV data

No Dhan, broker login, API key, or secret is required.

## Fix included
The previous version tried to read the NIFTY 500 list from Wikipedia and Streamlit Cloud received HTTP 403. This version uses a public GitHub CSV first, so the Wikipedia 403 is removed.

## Setup
1. Upload `app.py`, `requirements.txt`, and `README.md` to the GitHub repository root.
2. Streamlit Community Cloud: deploy the `app.py` file from the `main` branch.
3. No Secrets are required.

## Scanner logic
- Strong trend: Close > SMA50 > SMA150 > SMA200
- Rising SMA200
- Close above EMA20
- 20D range contraction
- 10D tighter than 20D
- 5D tighter than 10D
- ATR contraction
- Volume dry-up
- Close near previous 20-session high but still below it
- No recent closing breakout
- Higher-low structure
- Rejects obvious distribution
- Relative strength versus NIFTY
- Strict quality score

The scanner is intentionally selective. A result is a **watch candidate**, not an automatic buy signal.

## Data limitation
Yahoo Finance is a public third-party data source and can occasionally be delayed, rate-limited, or unavailable. The scanner should be validated before using it for live trading decisions.
