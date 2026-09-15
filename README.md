# Pre-Breakout Hunter V23.0

LIVE-FIRST NSE/NIFTY 500 pre-breakout scanner using Upstox Analytics Token.

## What V23 changes
- Full NIFTY 500 live snapshot from Upstox V3.
- Completed daily candles establish the pre-breakout structure.
- Current-session Upstox daily OHLC + LTP regrades every mapped stock.
- Fresh 1-minute OHLC pressure is included when available.
- **LIVE PRIORITY** ranking emphasizes what is closest to breaking out now, not simply the highest historical structure score.
- Historical success is shown as raw %, but tiny samples receive a neutral-sample adjustment for ranking.
- Evidence grade prevents 100% from one event from looking like validated evidence.
- Top candidate cards are phone-friendly.
- Formation charts show completed daily candles plus today's live candle.
- Historical validation uses precomputed rolling features and candidate-only replay.

## LIVE PRIORITY formula
- 35% live breakout pressure
- 25% established structure score
- 15% distance to resistance
- 10% live position in today's range
- 10% live range/compression quality
- 5% historical evidence strength

## Data
- Upstox Analytics Token only.
- No Yahoo Finance.
- No order/trading API.
- No automated order placement.

## Setup
1. Put your Upstox Analytics Token in Streamlit Secrets as `UPSTOX_ACCESS_TOKEN`.
2. Install `requirements.txt`.
3. Run `streamlit run app.py`.

This is a research/scanning tool, not a guarantee of future returns. Historical rates are event statistics, not probabilities of the next trade.
