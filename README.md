# Pre-Breakout Hunter V26.0 — FINAL STRICT + EVENT RADAR

Phone-first NSE/NIFTY500 pre-breakout scanner using the Upstox Analytics Token.

## Design
- **STRICT scanner remains strict:** READY and NEAR-MISS use the established trend, contraction, compression, resistance, 52-week, higher-low, distribution and relative-strength rules.
- **LIVE EVENT RADAR is separate:** exceptional intraday resistance/volume/range events are displayed in a separate tab and NEVER promote a stock into READY/NEAR-MISS.
- **News is separate context:** Upstox News is shown in a separate News Radar tab and can explain/reinforce an event, but news alone cannot create a strict candidate.
- Current-session Upstox V3 1D OHLC/LTP and I1 data are read fresh on every scan.
- Historical validation is candidate-only.
- Formation charts use completed daily candles plus the current live candle.

## Speed improvements
- Upstox standard APIs permit up to 50 requests/sec; V26 uses 48 requests/sec with 24 workers, remaining just below the documented limit.
- Daily history is fetched in **100-stock batches** instead of 25.
- Each stock's daily history is cached independently for 30 minutes, so repeated scans on the same completed session reuse the downloaded history.
- Live 1D and I1 data use only four OHLC requests for a ~500-stock universe (250 keys/request), plus batched news requests.
- The first scan of a new trading session downloads the completed history; subsequent scans on that same session should be substantially faster.

Upstox documents 50 requests/sec for standard APIs and up to 500 instruments in Full Market Quotes. See the official Upstox documentation for current limits.

## Deployment
1. Put `UPSTOX_ACCESS_TOKEN` in Streamlit Secrets.
2. Deploy `app.py` and `requirements.txt`.
3. Run during NSE market hours for live detection.
4. A strict scan may legitimately return zero READY/NEAR-MISS names.
5. Use **LIVE EVENT RADAR** to inspect exceptional intraday moves without weakening the strategy.
