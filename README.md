# Pre-Breakout Hunter V9.1

Phone-friendly Streamlit NSE scanner using public market data only. No broker API and no API key required.

## V9.1 additions
- Market breadth and sector intelligence
- Strict V8 pre-breakout scanner
- Liquidity-aware top gainers and volume surge lists
- Sector strength map
- Recent NSE bulk/block deal section
- Big News section using recent NSE corporate announcements, ranked by likely market relevance
- Full-history walk-forward validation remains resumable in small batches

### Data notes
Bulk/block deals and corporate announcements are fetched from NSE public web endpoints. If NSE temporarily rate-limits or blocks the public endpoint, the scanner continues and shows an unavailable-data message instead of crashing.


## V9.1 Upstox connection test

This version adds a non-trading Upstox Analytics Token connection test.
Store the token only in Streamlit Secrets:

```toml
UPSTOX_ACCESS_TOKEN = "YOUR_TOKEN"
```

The test calls Upstox Market Quote V3 for one NSE equity (NHPC) and displays LTP,
previous close, day volume and last-trade quantity. The existing V8.9 scanner
logic remains intact and still uses its existing data engine.

Next step: integrate Upstox Market Data Feed V3 WebSocket and then replace/augment
the scanner's live-data layer.


## V9.1 Upstox WebSocket test
The app now includes a small read-only Upstox MarketDataStreamerV3 test using the Analytics Token stored in Streamlit Secrets as `UPSTOX_ACCESS_TOKEN`. It subscribes to a small basket first and displays the live WebSocket status, LTP, previous close, change %, last trade quantity and feed timestamps. No order endpoints are used.

Upstox documents MarketDataStreamerV3 as the current market WebSocket interface; the V2 market feed is discontinued. The Analytics Token supports WebSocket market data and is read-only.
