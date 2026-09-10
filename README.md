# Pre-Breakout Hunter V9.5.4

V9.5.4 fixes the focused radar handoff in V9.5.3. The daily scan now attaches its strict candidates + strongest near-misses to the persistent Upstox live engine, seeds 1-minute history only for that focused watchlist (max 30), and keeps the live engine running across Streamlit reruns. Full NIFTY 500 current market data remains lightweight; detailed I1 OHLC is restricted to the focused radar watchlist. Read-only market-data APIs only; no order APIs are used.
