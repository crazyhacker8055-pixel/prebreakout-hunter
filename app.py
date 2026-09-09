import streamlit as st
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime, timedelta

st.set_page_config(page_title='Pre-Breakout Hunter', page_icon='🎯', layout='wide')

# ---------------- CONFIG ----------------
MIN_BARS = 230

# Very strict defaults — intentionally designed for FEWER signals.
DEFAULTS = {
    'range5': 4.5,
    'range10': 7.5,
    'range20': 12.0,
    'atr_ratio': 0.72,
    'vol_ratio': 0.70,
    'pivot_min': 0.5,
    'pivot_max': 4.0,
    'min_score': 80,
}

@st.cache_data(ttl=86400, show_spinner=False)
def get_nifty500_symbols():
    """Get current NIFTY 500 constituents from a public webpage. No API key."""
    tables = pd.read_html('https://en.wikipedia.org/wiki/NIFTY_500')
    for t in tables:
        cols = [str(c).lower() for c in t.columns]
        if any('symbol' in c for c in cols):
            symbol_col = t.columns[[('symbol' in str(c).lower()) for c in t.columns].index(True)]
            syms = t[symbol_col].astype(str).str.strip().tolist()
            syms = [s for s in syms if s and s != 'nan' and s.upper() != 'SYMBOL']
            return syms
    raise RuntimeError('Could not find NIFTY 500 symbol table.')

@st.cache_data(ttl=900, show_spinner=False)
def download_market_data(symbols):
    """Download daily OHLCV in one public Yahoo Finance request. No broker API."""
    tickers = [f'{s}.NS' for s in symbols]
    tickers.append('^NSEI')
    end = datetime.now()
    start = end - timedelta(days=520)
    data = yf.download(
        tickers=tickers,
        start=start.strftime('%Y-%m-%d'),
        end=(end + timedelta(days=1)).strftime('%Y-%m-%d'),
        interval='1d',
        auto_adjust=False,
        group_by='column',
        threads=True,
        progress=False,
    )
    return data

def col(data, field, ticker):
    try:
        if isinstance(data.columns, pd.MultiIndex):
            if (field, ticker) in data.columns:
                return data[(field, ticker)].dropna()
            if (ticker, field) in data.columns:
                return data[(ticker, field)].dropna()
        return data[field][ticker].dropna()
    except Exception:
        return pd.Series(dtype=float)

def ohlcv_from_download(data, ticker):
    out = pd.DataFrame({
        'open': col(data, 'Open', ticker),
        'high': col(data, 'High', ticker),
        'low': col(data, 'Low', ticker),
        'close': col(data, 'Close', ticker),
        'volume': col(data, 'Volume', ticker),
    }).dropna()
    return out

def atr(df, n):
    prev = df['close'].shift(1)
    tr = pd.concat([
        df['high'] - df['low'],
        (df['high'] - prev).abs(),
        (df['low'] - prev).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=n).mean()

def evaluate(df, nifty, cfg):
    if len(df) < MIN_BARS or len(nifty) < MIN_BARS:
        return None

    d = df.copy().sort_index()
    n = nifty.reindex(d.index).ffill().dropna()
    if len(n) < MIN_BARS:
        return None

    c, h, l, v = d['close'], d['high'], d['low'], d['volume']
    d['sma20'] = c.rolling(20).mean()
    d['sma50'] = c.rolling(50).mean()
    d['sma150'] = c.rolling(150).mean()
    d['sma200'] = c.rolling(200).mean()
    d['ema20'] = c.ewm(span=20, adjust=False).mean()
    d['atr5'] = atr(d, 5)
    d['atr20'] = atr(d, 20)

    x = d.iloc[-1]
    sma200_20 = d['sma200'].iloc[-21]
    if not np.isfinite(sma200_20):
        return None

    # 1) Strong primary trend
    if not (
        x.close > x.sma50 > x.sma150 > x.sma200 and
        x.sma200 > sma200_20 and
        x.close > x.ema20
    ):
        return None

    # 2) Tightening price ranges
    r5 = (h.iloc[-5:].max() - l.iloc[-5:].min()) / l.iloc[-5:].min() * 100
    r10 = (h.iloc[-10:].max() - l.iloc[-10:].min()) / l.iloc[-10:].min() * 100
    r20 = (h.iloc[-20:].max() - l.iloc[-20:].min()) / l.iloc[-20:].min() * 100
    if not (r5 <= cfg['range5'] and r10 <= cfg['range10'] and r20 <= cfg['range20']):
        return None
    if not (r5 < r10 < r20):
        return None

    # 3) ATR contraction
    atr_ratio = x.atr5 / x.atr20 if x.atr20 else np.inf
    if not np.isfinite(atr_ratio) or atr_ratio > cfg['atr_ratio']:
        return None

    # 4) Volume dry-up
    vol5 = v.iloc[-5:].mean()
    vol20 = v.iloc[-20:].mean()
    vol_ratio = vol5 / vol20 if vol20 else np.inf
    if not np.isfinite(vol_ratio) or vol_ratio > cfg['vol_ratio']:
        return None

    # 5) Pivot: previous 20-session high. Must be close but NOT broken.
    pivot = h.iloc[-21:-1].max()
    distance = (pivot - x.close) / pivot * 100
    if not (cfg['pivot_min'] <= distance <= cfg['pivot_max']):
        return None
    if x.close >= pivot:
        return None

    # No close above its previous 20D high during the last 3 sessions.
    prior20 = h.rolling(20).max().shift(1)
    if (c.iloc[-3:] >= prior20.iloc[-3:]).any():
        return None

    # 6) Higher-low structure
    last10_low = l.iloc[-10:].min()
    prev10_low = l.iloc[-20:-10].min()
    if last10_low < prev10_low * 0.985:
        return None

    # 7) Reject obvious distribution in the base
    base = d.iloc[-20:]
    down_volume = base.loc[base['close'] < base['open'], 'volume'].mean()
    up_volume = base.loc[base['close'] >= base['open'], 'volume'].mean()
    if np.isfinite(down_volume) and np.isfinite(up_volume) and down_volume > up_volume * 1.35:
        return None

    # 8) Relative strength vs NIFTY over 20 sessions
    stock20 = c.iloc[-1] / c.iloc[-21] - 1
    nifty20 = n.iloc[-1] / n.iloc[-21] - 1
    rs = (stock20 - nifty20) * 100
    if rs < 0:
        return None

    # -------- QUALITY SCORE (only after hard filters) --------
    score = 0
    score += 15 if x.close > x.sma50 else 0
    score += 10 if x.sma50 > x.sma150 else 0
    score += 10 if x.sma150 > x.sma200 else 0
    score += 5 if x.sma200 > sma200_20 else 0
    score += 10 if r5 <= 4.0 else 5
    score += 10 if r10 <= 7.0 else 5
    score += 10 if atr_ratio <= 0.65 else 5
    score += 10 if vol_ratio <= 0.60 else 5
    score += 10 if distance <= 2.0 else 5
    score += 5 if rs >= 3 else 0
    score += 5 if rs >= 6 else 0

    if score < cfg['min_score']:
        return None

    # Trigger level is the pivot. This is NOT a buy signal yet.
    return {
        'Close': round(float(x.close), 2),
        'Score': int(score),
        '5D Range %': round(float(r5), 2),
        '10D Range %': round(float(r10), 2),
        '20D Range %': round(float(r20), 2),
        'ATR5/ATR20': round(float(atr_ratio), 2),
        'Vol5/Vol20': round(float(vol_ratio), 2),
        'Pivot': round(float(pivot), 2),
        'To Pivot %': round(float(distance), 2),
        'RS vs Nifty %': round(float(rs), 2),
        'Status': '🔥 PRE-BREAKOUT',
    }

def main():
    st.title('🎯 Pre-Breakout Hunter')
    st.caption('Strict NSE scanner — searches for the quiet compression BEFORE the breakout. No broker API required.')

    with st.sidebar:
        st.header('Scanner settings')
        min_score = st.slider('Minimum score', 70, 100, DEFAULTS['min_score'])
        max_range5 = st.number_input('5D max range %', 2.0, 8.0, DEFAULTS['range5'], 0.1)
        max_range10 = st.number_input('10D max range %', 4.0, 12.0, DEFAULTS['range10'], 0.1)
        max_range20 = st.number_input('20D max range %', 6.0, 20.0, DEFAULTS['range20'], 0.1)
        max_atr = st.number_input('Max ATR5/ATR20', 0.50, 1.00, DEFAULTS['atr_ratio'], 0.01)
        max_vol = st.number_input('Max Vol5/Vol20', 0.40, 1.00, DEFAULTS['vol_ratio'], 0.01)
        pivot_min = st.number_input('Min distance to pivot %', 0.0, 3.0, DEFAULTS['pivot_min'], 0.1)
        pivot_max = st.number_input('Max distance to pivot %', 1.0, 8.0, DEFAULTS['pivot_max'], 0.1)
        scan = st.button('🚀 SCAN NIFTY 500', type='primary', use_container_width=True)

    st.markdown('''
### Exact setup being hunted
**Strong trend → 20D base → 10D tighter → 5D very tight → ATR contraction → volume dry-up → higher lows → near pivot → no breakout yet → relative strength.**

The scanner intentionally produces **few results**. A result means *watch*, not automatic buy.
''')

    if not scan:
        st.info('Press **SCAN NIFTY 500** to run the scanner. It uses public Yahoo Finance market data; no Dhan/broker API key is needed.')
        return

    cfg = dict(DEFAULTS)
    cfg.update({
        'range5': max_range5,
        'range10': max_range10,
        'range20': max_range20,
        'atr_ratio': max_atr,
        'vol_ratio': max_vol,
        'pivot_min': pivot_min,
        'pivot_max': pivot_max,
        'min_score': min_score,
    })

    try:
        symbols = get_nifty500_symbols()
    except Exception as e:
        st.error(f'Could not load NIFTY 500 list: {e}')
        return

    st.write(f'Universe: **{len(symbols)} stocks**')
    with st.spinner('Downloading daily market data and checking strict setups...'):
        data = download_market_data(tuple(symbols))

    nifty = ohlcv_from_download(data, '^NSEI')['close']
    results = []
    progress = st.progress(0)

    for i, sym in enumerate(symbols):
        ticker = f'{sym}.NS'
        df = ohlcv_from_download(data, ticker)
        try:
            result = evaluate(df, nifty, cfg)
            if result:
                result['Stock'] = sym
                results.append(result)
        except Exception:
            pass
        progress.progress((i + 1) / len(symbols))

    progress.empty()
    out = pd.DataFrame(results)

    if out.empty:
        st.warning('No A-grade pre-breakout setups found today. That is intentional — the scanner is designed to be selective.')
        return

    out = out.sort_values(['Score', 'To Pivot %'], ascending=[False, True]).reset_index(drop=True)
    st.success(f'Found **{len(out)}** strict pre-breakout candidates.')

    st.dataframe(
        out[['Stock','Score','Close','5D Range %','10D Range %','20D Range %','ATR5/ATR20','Vol5/Vol20','Pivot','To Pivot %','RS vs Nifty %','Status']],
        use_container_width=True,
        hide_index=True,
    )

    st.download_button(
        '⬇️ Download results CSV',
        out.to_csv(index=False).encode('utf-8'),
        'prebreakout_results.csv',
        'text/csv',
    )

    st.markdown('### How to trade the result')
    st.warning('Do NOT buy just because a stock appears here. The scanner is detecting compression. Wait for a daily close above the Pivot with convincing volume, then apply your own entry/stop/risk rules.')

if __name__ == '__main__':
    main()
