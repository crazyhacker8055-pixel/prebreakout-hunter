# Pre-Breakout Hunter V9.5.3

V9.5.3 fixes Streamlit rerun persistence for the Upstox live market engine. The live engine state and lock are stored with `st.cache_resource`, so pressing the daily scanner no longer resets an already-running 500-stock live engine to STOPPED.

Architecture:
- NIFTY 500 lightweight live market data through Upstox V3 REST.
- Detailed 1-minute history only for the focused radar watchlist (daily strict candidates + strongest near-misses, max 30).
- Daily scan and live radar can now coexist across Streamlit reruns.
- Read-only market-data APIs only; no order APIs.
