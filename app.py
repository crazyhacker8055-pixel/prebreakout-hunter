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
    """Load NIFTY 500 symbols from a public GitHub CSV, with NSE fallback. No API key."""
    import io
    import requests

    # Primary source: public GitHub mirror. This avoids Wikipedia 403 blocks on Streamlit Cloud.
    github_url = (
        'https://raw.githubusercontent.com/ganeshbiyer/Nse_Historical_Data/'
        'refs/heads/main/nifty500_symbols.csv'
    )
    try:
        r = requests.get(github_url, timeout=20, headers={'User-Agent': 'Mozilla/5.0'})
        r.raise_for_status()
        t = pd.read_csv(io.BytesIO(r.content))
        symbol_col = next((c for c in t.columns if str(c).strip().lower() == 'symbol'), None)
        if symbol_col is None:
            raise RuntimeError('GitHub NIFTY 500 CSV has no Symbol column.')
        syms = t[symbol_col].astype(str).str.strip().tolist()
        syms = [s for s in syms if s and s.lower() != 'nan' and s.upper() != 'SYMBOL']
        syms = list(dict.fromkeys(syms))
        if len(syms) >= 450:
            return syms
    except Exception as github_error:
        github_msg = str(github_error)

    # Fallback: NSE's published CSV, with browser-like headers.
    nse_url = 'https://archives.nseindia.com/content/indices/ind_nifty500list.csv'
    try:
        r = requests.get(nse_url, timeout=20, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'text/csv,application/csv,*/*',
            'Referer': 'https://www.nseindia.com/'
        })
        r.raise_for_status()
        t = pd.read_csv(io.BytesIO(r.content))
        symbol_col = next((c for c in t.columns if str(c).strip().lower() in ('symbol', 'symbol ')), None)
        if symbol_col is None:
            raise RuntimeError('NSE CSV has no Symbol column.')
        syms = t[symbol_col].astype(str).str.strip().tolist()
        syms = [s for s in syms if s and s.lower() != 'nan']
        if len(syms) >= 450:
            return list(dict.fromkeys(syms))
        raise RuntimeError(f'Only {len(syms)} symbols returned.')
    except Exception as nse_error:
        raise RuntimeError(
            'NIFTY 500 list could not be loaded. GitHub and NSE sources failed. '
            f'GitHub: {github_msg if 'github_msg' in locals() else 'unknown'}; NSE: {nse_error}'
        )

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
    """Evaluate a stock using hard structural gates + continuous quality scoring."""
    if len(df) < MIN_BARS or len(nifty) < MIN_BARS:
        return None
    d = df.copy().sort_index()
    n = nifty.reindex(d.index).ffill().dropna()
    if len(n) < MIN_BARS:
        return None
    c, h, l, v = d['close'], d['high'], d['low'], d['volume']
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
    r5 = (h.iloc[-5:].max() - l.iloc[-5:].min()) / l.iloc[-5:].min() * 100
    r10 = (h.iloc[-10:].max() - l.iloc[-10:].min()) / l.iloc[-10:].min() * 100
    r20 = (h.iloc[-20:].max() - l.iloc[-20:].min()) / l.iloc[-20:].min() * 100
    atr_ratio = x.atr5 / x.atr20 if x.atr20 else np.inf
    vol5, vol20 = v.iloc[-5:].mean(), v.iloc[-20:].mean()
    vol_ratio = vol5 / vol20 if vol20 else np.inf
    pivot = h.iloc[-21:-1].max()
    distance = (pivot - x.close) / pivot * 100 if pivot else np.inf
    prior20 = h.rolling(20).max().shift(1)
    recent_breakout = bool((c.iloc[-3:] >= prior20.iloc[-3:]).any())
    last10_low, prev10_low = l.iloc[-10:].min(), l.iloc[-20:-10].min()
    higher_low = last10_low >= prev10_low * 0.985
    base = d.iloc[-20:]
    down_volume = base.loc[base['close'] < base['open'], 'volume'].mean()
    up_volume = base.loc[base['close'] >= base['open'], 'volume'].mean()
    distribution = bool(np.isfinite(down_volume) and np.isfinite(up_volume) and down_volume > up_volume * 1.35)
    stock20 = c.iloc[-1] / c.iloc[-21] - 1
    nifty20 = n.iloc[-1] / n.iloc[-21] - 1
    rs = (stock20 - nifty20) * 100

    # HARD STRUCTURAL GATES: trend, controlled base, still below pivot,
    # no recent breakout, higher-low structure, and no distribution.
    trend_ok = bool(x.close > x.sma50 > x.sma150 > x.sma200 and x.sma200 > sma200_20 and x.close > x.ema20)
    base_not_extreme = bool(r5 <= cfg['range5'] * 1.25 and r10 <= cfg['range10'] * 1.20 and r20 <= cfg['range20'] * 1.20)
    pre_breakout = bool(x.close < pivot and 0.20 <= distance <= 7.0)
    structural_ok = trend_ok and base_not_extreme and pre_breakout and (not recent_breakout) and higher_low and (not distribution)

    # 100-point QUALITY SCORE. Range/ATR/volume/pivot are scored continuously,
    # so small deviations no longer cause brittle rejection.
    score = 0
    score += 10 if x.close > x.sma50 else 0
    score += 8 if x.sma50 > x.sma150 else 0
    score += 8 if x.sma150 > x.sma200 else 0
    score += 5 if x.sma200 > sma200_20 else 0
    score += 4 if x.close > x.ema20 else 0
    score += 5 if r5 <= 4.0 else (3 if r5 <= cfg['range5'] else 0)
    score += 5 if r10 <= 7.0 else (3 if r10 <= cfg['range10'] else 0)
    score += 5 if r20 <= 12.0 else (3 if r20 <= cfg['range20'] else 0)
    score += 10 if r5 < r10 < r20 else (5 if r5 < r10 or r10 < r20 else 0)
    score += 6 if atr_ratio <= 0.65 else (4 if atr_ratio <= cfg['atr_ratio'] else (2 if atr_ratio <= 0.85 else 0))
    score += 6 if vol_ratio <= 0.60 else (4 if vol_ratio <= cfg['vol_ratio'] else (2 if vol_ratio <= 0.85 else 0))
    score += 4 if higher_low else 0
    score += 4 if not distribution else 0
    if 0.5 <= distance <= 2.5:
        score += 10
    elif cfg['pivot_min'] <= distance <= cfg['pivot_max']:
        score += 7
    elif 0.2 <= distance <= 7.0:
        score += 4
    score += 5 if rs >= 6 else (3 if rs >= 3 else (1 if rs >= 0 else 0))
    score += 5 if not recent_breakout else 0
    score = min(100, int(score))

    reasons = []
    if not trend_ok: reasons.append('Trend not strong enough')
    if not base_not_extreme: reasons.append('Base ranges too wide')
    if not (r5 < r10 < r20): reasons.append('Range contraction incomplete')
    if not (np.isfinite(atr_ratio) and atr_ratio <= cfg['atr_ratio']): reasons.append('ATR contraction weak')
    if not (np.isfinite(vol_ratio) and vol_ratio <= cfg['vol_ratio']): reasons.append('Volume dry-up weak')
    if x.close >= pivot: reasons.append('Already broke pivot')
    elif distance > 7.0: reasons.append('Too far below pivot')
    elif distance < 0.20: reasons.append('Too close to pivot')
    elif not (cfg['pivot_min'] <= distance <= cfg['pivot_max']): reasons.append('Pivot distance not ideal')
    if recent_breakout: reasons.append('Recent breakout detected')
    if not higher_low: reasons.append('Higher-low structure weak')
    if distribution: reasons.append('Distribution pressure detected')
    if rs < 0: reasons.append('Relative strength below NIFTY')
    if score < cfg['min_score']: reasons.append(f'Score below {cfg["min_score"]}')
    strict = structural_ok and score >= cfg['min_score']
    return {
        'Stock': '', 'Close': round(float(x.close), 2), 'Score': score,
        '5D Range %': round(float(r5), 2), '10D Range %': round(float(r10), 2),
        '20D Range %': round(float(r20), 2), 'ATR5/ATR20': round(float(atr_ratio), 2),
        'Vol5/Vol20': round(float(vol_ratio), 2), 'Pivot': round(float(pivot), 2),
        'To Pivot %': round(float(distance), 2), 'RS vs Nifty %': round(float(rs), 2),
        'Status': '🔥 PRE-BREAKOUT' if strict else '👀 NEAR-MISS',
        'Why it missed': 'PASS — structure + score' if strict else ' • '.join(reasons[:4]),
        '_strict': strict,
    }

def main():
    st.title('🎯 Pre-Breakout Hunter')
    st.caption('Selective NSE scanner — hard structural gates + quality scoring for quiet compression BEFORE the breakout. No broker API required.')

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
****Structure first:** strong trend + controlled base + no recent breakout + higher lows. **Then rank quality:** range contraction + ATR contraction + volume dry-up + pivot proximity + relative strength.**

The scanner intentionally produces **few results**. A result means *watch*, not automatic buy. A setup can score highly without passing every soft quality metric, but it must still pass the structural gates.
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
    all_out = pd.DataFrame(results)
    if all_out.empty:
        st.error('No stocks had enough usable daily data to evaluate.')
        return

    strict_out = all_out[all_out['_strict']].copy()
    near_out = all_out[~all_out['_strict']].copy()

    if strict_out.empty:
        st.warning('No A-grade pre-breakout setups found today. That is intentional — the scanner is designed to be selective.')
    else:
        strict_out = strict_out.sort_values(['Score', 'To Pivot %'], ascending=[False, True]).reset_index(drop=True)
        st.success(f'Found **{len(strict_out)}** strict pre-breakout candidate(s).')
        st.dataframe(
            strict_out[['Stock','Score','Close','5D Range %','10D Range %','20D Range %','ATR5/ATR20','Vol5/Vol20','Pivot','To Pivot %','RS vs Nifty %','Status']],
            use_container_width=True,
            hide_index=True,
        )
        st.download_button(
            '⬇️ Download A-grade results CSV',
            strict_out.drop(columns=['_strict']).to_csv(index=False).encode('utf-8'),
            'prebreakout_results.csv',
            'text/csv',
        )

    st.markdown('### 🔎 Near-Miss Diagnostic')
    st.caption('These stocks are NOT buy signals. They are the strongest almost-qualified setups, shown so we can see which filter is too restrictive or genuinely missing.')
    if near_out.empty:
        st.info('No near-miss stocks were available.')
    else:
        near_out = near_out.sort_values(['Score', 'To Pivot %'], ascending=[False, True]).reset_index(drop=True).head(20)
        st.dataframe(
            near_out[['Stock','Score','Close','5D Range %','10D Range %','20D Range %','ATR5/ATR20','Vol5/Vol20','To Pivot %','RS vs Nifty %','Why it missed']],
            use_container_width=True,
            hide_index=True,
        )
        st.download_button(
            '⬇️ Download diagnostic CSV',
            near_out.drop(columns=['_strict']).to_csv(index=False).encode('utf-8'),
            'prebreakout_diagnostic.csv',
            'text/csv',
        )

    st.markdown('### How to trade the result')
    st.warning('Do NOT buy just because a stock appears here. The scanner is detecting compression. Wait for a daily close above the Pivot with convincing volume, then apply your own entry/stop/risk rules.')

if __name__ == '__main__':
    main()
