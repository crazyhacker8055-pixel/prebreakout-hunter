# Pre-Breakout Hunter V14.0

Upstox-only NIFTY 500 pre-breakout scanner.

Flow:
1. Scan completed daily candles for the minimum strategy structure.
2. Read current Upstox V3 live daily OHLC for the mapped universe.
3. Analyze today's evolving range against the historical formation.
4. Rank READY and NEAR-MISS candidates.
5. Run historical event validation only for those candidates.
6. Show breakout rate, validated success rate, maximum gain/drawdown and formation charts.

Streamlit Secret required:
`UPSTOX_ACCESS_TOKEN`

The scanner uses completed candles for structural calculations and Upstox V3 live OHLC for today's current market state. It does not silently substitute yesterday's close for the live snapshot.
