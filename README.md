# Pre-Breakout Hunter V10.1

Clean Upstox-only daily pre-breakout scanner for the NIFTY 500.

- Uses completed daily candles from Upstox V3 Historical Candle API.
- Excludes the current incomplete daily candle.
- Uses Upstox LTP only for the fresh market snapshot after the scan.
- No Yahoo Finance, WebSocket, 1-minute history, background polling, or trading APIs.
- Controlled historical request pacing stays below Upstox standard API limits.
- Completed daily histories are cached for 30 minutes so repeated scans do not redownload the universe.
- Missing NIFTY 500 instrument mappings are repaired only with an exact Upstox Instrument Search match; otherwise the symbol is excluded and reported.
