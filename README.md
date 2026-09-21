# Pre-Breakout Hunter V27

V27 keeps the strict pre-breakout scanner unchanged and adds a separate Live Event Radar plus a Fast Live Refresh path.

## Tabs
- Strict Scanner: READY / NEAR-MISS / LIVE BREAKOUT NOW only from the established daily structure rules.
- Live Event Radar: independent live resistance-test/breakout detection. It never promotes a stock into the strict tab.
- Formation Chart: completed daily candles plus the current live candle.

## Speed
- 24 concurrent workers
- 48 requests/sec pacing for historical requests, below Upstox's documented 50 req/sec standard API limit
- 100-stock resumable history batches
- cached per-symbol daily history
- after a full scan, `LIVE REFRESH — no history download` refreshes only current 1D/I1 live data and recalculates the results

## Upstox
The app uses V3 OHLC for current 1D/I1 data and does not use Yahoo or order/trading APIs.


V28 Event Radar: BREAKOUT NOW (0–2% above resistance), BREAKOUT CONFIRMING (2–3%), BREAKOUT TESTED, BREAKOUT RECLAIMING, EXTENDED BREAKOUT (>3%), and FAILED BREAKOUT. The strict scanner thresholds are unchanged.
