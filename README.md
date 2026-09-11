# Pre-Breakout Hunter V13

## Deployment
1. Upload `app.py` and `requirements.txt` to the Streamlit app repository root.
2. In Streamlit Cloud → Settings → Secrets, keep:
   `UPSTOX_ACCESS_TOKEN = "your Analytics Token"`
3. Deploy/reboot.
4. Press **RUN FULL SCANNER**.

## Architecture
NIFTY 500 completed daily history → minimum strategy filter → live Upstox V3 OHLC → READY/NEAR-MISS → candidate-only historical event validation → charts/statistics.

The historical validator is deliberately not run against all 500 stocks. It only runs for the strongest READY and NEAR-MISS candidates.
