# Pre-Breakout Hunter V22.0 — LIVE-FIRST

Full NIFTY 500 live-first pre-breakout scanner. Completed daily candles establish historical structure; every mapped stock then receives a fresh Upstox V3 current-session OHLC/LTP and latest 1-minute snapshot at scan time.

V22 adds a 0–100 Live Breakout Pressure score, a reason for each near-miss, and a 10% risk guard. Near-misses are watch-only; calculated risk above 10% is explicitly not actionable.

Outputs: LIVE BREAKOUT NOW, READY NOW, and the closest NEAR-MISS NOW candidates, with entry/stop/2R/3R/4R planning levels and historical validation where available.

No Yahoo, no order API, no WebSocket dependency.
