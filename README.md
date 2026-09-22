# Pre-Breakout Hunter V31

Strict NSE NIFTY500 pre-breakout scanner using Upstox API, with an independent live event radar and persistent continuity.

## V31 guarantees
- Strict READY / NEAR-MISS / STRICT BREAKOUT rules are unchanged.
- Live Event Radar is separate and never promotes an event into strict lists.
- Live Refresh reuses the exact full-scan structure snapshot; it does not download 500-stock daily history again.
- Current live data comes from Upstox V3 full market quotes + V3 OHLC (`1d` and `I1`).
- Full-scan strict candidates are retained for comparison if they stop qualifying after refresh.
- Live events are retained in a recent-event history so a detected event does not silently disappear.
- Event candidates are included in candidate-only historical validation.
- 5-session and 10-session breakout metrics are calculated on their true horizons even when the selected success window is 5 sessions.
- Mobile-friendly candidate cards and formation charts are included.

## Streamlit
Set `UPSTOX_ACCESS_TOKEN` in Streamlit Secrets, then deploy `app.py`.
