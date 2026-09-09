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

def _score_at(d, n, i, cfg):
    """Score one historical/current setup date using ONLY data available up to i."""
    if i < MIN_BARS - 1:
        return None
    c, h, l, v = d['close'], d['high'], d['low'], d['volume']
    sma50 = c.rolling(50).mean()
    sma150 = c.rolling(150).mean()
    sma200 = c.rolling(200).mean()
    ema20 = c.ewm(span=20, adjust=False).mean()
    atr5 = atr(d, 5)
    atr20 = atr(d, 20)
    xclose = c.iloc[i]
    xsma50, xsma150, xsma200, xema20 = sma50.iloc[i], sma150.iloc[i], sma200.iloc[i], ema20.iloc[i]
    sma200_20 = sma200.iloc[i-20] if i >= 20 else np.nan
    if not all(np.isfinite(z) for z in [xclose, xsma50, xsma150, xsma200, xema20, sma200_20, atr5.iloc[i], atr20.iloc[i]]):
        return None

    r5 = (h.iloc[i-4:i+1].max() - l.iloc[i-4:i+1].min()) / l.iloc[i-4:i+1].min() * 100
    r10 = (h.iloc[i-9:i+1].max() - l.iloc[i-9:i+1].min()) / l.iloc[i-9:i+1].min() * 100
    r20 = (h.iloc[i-19:i+1].max() - l.iloc[i-19:i+1].min()) / l.iloc[i-19:i+1].min() * 100
    atr_ratio = atr5.iloc[i] / atr20.iloc[i] if atr20.iloc[i] else np.inf
    vol20 = v.iloc[i-19:i+1].mean()
    vol5 = v.iloc[i-4:i+1].mean()
    vol_ratio = vol5 / vol20 if vol20 else np.inf
    pivot = h.iloc[i-20:i].max()
    distance = (pivot - xclose) / pivot * 100 if pivot else np.inf

    prior20 = h.rolling(20).max().shift(1)
    recent_breakout = bool((c.iloc[i-2:i+1] >= prior20.iloc[i-2:i+1]).any()) if i >= 2 else False
    last10_low = l.iloc[i-9:i+1].min()
    prev10_low = l.iloc[i-19:i-9].min()
    higher_low = last10_low >= prev10_low * 0.985

    base = d.iloc[i-19:i+1]
    down = base.loc[base['close'] < base['open'], 'volume']
    up = base.loc[base['close'] >= base['open'], 'volume']
    down_volume = down.mean() if len(down) else np.nan
    up_volume = up.mean() if len(up) else np.nan
    distribution = bool(np.isfinite(down_volume) and np.isfinite(up_volume) and down_volume > up_volume * 1.35)

    stock20 = c.iloc[i] / c.iloc[i-20] - 1
    nn = n.reindex(d.index).ffill()
    if not np.isfinite(nn.iloc[i]) or not np.isfinite(nn.iloc[i-20]):
        return None
    nifty20 = nn.iloc[i] / nn.iloc[i-20] - 1
    rs = (stock20 - nifty20) * 100

    trend_ok = bool(xclose > xsma50 > xsma150 > xsma200 and xsma200 > sma200_20 and xclose > xema20)
    base_not_extreme = bool(r5 <= cfg['range5'] * 1.25 and r10 <= cfg['range10'] * 1.20 and r20 <= cfg['range20'] * 1.20)
    pre_breakout = bool(xclose < pivot and 0.20 <= distance <= 7.0)
    structural_ok = trend_ok and base_not_extreme and pre_breakout and (not recent_breakout) and higher_low and (not distribution)

    score = 0
    score += 10 if xclose > xsma50 else 0
    score += 8 if xsma50 > xsma150 else 0
    score += 8 if xsma150 > xsma200 else 0
    score += 5 if xsma200 > sma200_20 else 0
    score += 4 if xclose > xema20 else 0
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
    strict = structural_ok and score >= cfg['min_score']
    return {
        'date': d.index[i], 'close': float(xclose), 'pivot': float(pivot), 'score': score,
        'r5': float(r5), 'r10': float(r10), 'r20': float(r20), 'atr_ratio': float(atr_ratio),
        'vol_ratio': float(vol_ratio), 'distance': float(distance), 'rs': float(rs),
        'strict': strict,
    }

def evaluate(df, nifty, cfg):
    if len(df) < MIN_BARS or len(nifty) < MIN_BARS:
        return None
    d = df.copy().sort_index()
    n = nifty.reindex(d.index).ffill().dropna()
    if len(n) < MIN_BARS:
        return None
    x = _score_at(d, n, len(d)-1, cfg)
    if x is None:
        return None
    reasons = []
    # Recompute compact reasons from the scored metrics.
    if x['r5'] >= x['r10'] or x['r10'] >= x['r20']: reasons.append('Range contraction incomplete')
    if x['atr_ratio'] > cfg['atr_ratio']: reasons.append('ATR contraction weak')
    if x['vol_ratio'] > cfg['vol_ratio']: reasons.append('Volume dry-up weak')
    if x['distance'] > 7.0: reasons.append('Too far below pivot')
    elif x['distance'] < 0.20: reasons.append('Too close to pivot')
    elif not (cfg['pivot_min'] <= x['distance'] <= cfg['pivot_max']): reasons.append('Pivot distance not ideal')
    if x['rs'] < 0: reasons.append('Relative strength below NIFTY')
    if x['score'] < cfg['min_score']: reasons.append(f"Score below {cfg['min_score']}")
    # Use structural gate flags from the original logic for clear current-day reasons.
    c, h, l, v = d['close'], d['high'], d['low'], d['volume']
    sma50, sma150, sma200 = c.rolling(50).mean(), c.rolling(150).mean(), c.rolling(200).mean()
    ema20 = c.ewm(span=20, adjust=False).mean()
    trend_ok = bool(c.iloc[-1] > sma50.iloc[-1] > sma150.iloc[-1] > sma200.iloc[-1] and sma200.iloc[-1] > sma200.iloc[-21] and c.iloc[-1] > ema20.iloc[-1])
    base_ok = bool(x['r5'] <= cfg['range5']*1.25 and x['r10'] <= cfg['range10']*1.20 and x['r20'] <= cfg['range20']*1.20)
    higher_low = l.iloc[-10:].min() >= l.iloc[-20:-10].min()*0.985
    prior20 = h.rolling(20).max().shift(1)
    recent_breakout = bool((c.iloc[-3:] >= prior20.iloc[-3:]).any())
    down = d.iloc[-20:].loc[d.iloc[-20:]['close'] < d.iloc[-20:]['open'], 'volume'].mean()
    up = d.iloc[-20:].loc[d.iloc[-20:]['close'] >= d.iloc[-20:]['open'], 'volume'].mean()
    distribution = bool(np.isfinite(down) and np.isfinite(up) and down > up*1.35)
    if not trend_ok: reasons.insert(0, 'Trend not strong enough')
    if not base_ok: reasons.insert(0, 'Base ranges too wide')
    if recent_breakout: reasons.append('Recent breakout detected')
    if not higher_low: reasons.append('Higher-low structure weak')
    if distribution: reasons.append('Distribution pressure detected')
    return {
        'Stock': '', 'Close': round(x['close'], 2), 'Score': x['score'],
        '5D Range %': round(x['r5'], 2), '10D Range %': round(x['r10'], 2), '20D Range %': round(x['r20'], 2),
        'ATR5/ATR20': round(x['atr_ratio'], 2), 'Vol5/Vol20': round(x['vol_ratio'], 2),
        'Pivot': round(x['pivot'], 2), 'To Pivot %': round(x['distance'], 2), 'RS vs Nifty %': round(x['rs'], 2),
        'Status': '🔥 PRE-BREAKOUT' if x['strict'] else '👀 NEAR-MISS',
        'Why it missed': 'PASS — structure + score' if x['strict'] else ' • '.join(reasons[:4]),
        '_strict': x['strict'],
    }

def historical_outcome(df, i, setup, breakout_window=10, holding_window=15):
    """Forward-test a setup. Breakout = first future close above setup-day pivot."""
    if i + 2 >= len(df):
        return None
    end_break = min(len(df)-1, i + breakout_window)
    breakout_i = None
    for j in range(i+1, end_break+1):
        if df['close'].iloc[j] > setup['pivot']:
            breakout_i = j
            break
    if breakout_i is None or breakout_i + 1 >= len(df):
        return {
            'breakout': False, 'days_to_breakout': np.nan, 'entry': np.nan,
            'max_gain_pct': np.nan, 'max_drawdown_pct': np.nan, 'false_breakout': False,
            'breakout_volume_ratio': np.nan,
        }
    entry_i = breakout_i + 1
    entry = float(df['open'].iloc[entry_i])
    if not np.isfinite(entry) or entry <= 0:
        return None
    end_hold = min(len(df)-1, entry_i + holding_window)
    future = df.iloc[entry_i:end_hold+1]
    max_gain = (future['high'].max()/entry - 1)*100
    max_dd = (future['low'].min()/entry - 1)*100
    # False breakout: closes back under pivot within 5 sessions and before +5% MFE.
    fb_end = min(len(df)-1, breakout_i+5)
    false_breakout = bool((df['close'].iloc[breakout_i:fb_end+1] < setup['pivot']).any() and max_gain < 5.0)
    vol20 = df['volume'].iloc[max(0, breakout_i-19):breakout_i+1].mean()
    br_vol = float(df['volume'].iloc[breakout_i] / vol20) if vol20 else np.nan
    return {
        'breakout': True, 'days_to_breakout': breakout_i-i, 'entry': entry,
        'max_gain_pct': max_gain, 'max_drawdown_pct': max_dd, 'false_breakout': false_breakout,
        'breakout_volume_ratio': br_vol,
    }

@st.cache_data(ttl=1800, show_spinner=False)
def run_historical_validation(symbols, data, nifty, cfg, lookback_days=120, breakout_window=10, holding_window=15):
    rows=[]
    for sym in symbols:
        df=ohlcv_from_download(data, f'{sym}.NS')
        if len(df)<MIN_BARS+breakout_window+2:
            continue
        d=df.sort_index()
        n=nifty.reindex(d.index).ffill()
        # Stop before the most recent breakout_window+1 bars so outcomes are observable.
        last_i=len(d)-breakout_window-2
        first_i=max(MIN_BARS-1, last_i-lookback_days+1)
        for i in range(first_i,last_i+1):
            setup=_score_at(d,n,i,cfg)
            if setup is None or not setup['strict']:
                continue
            outcome=historical_outcome(d,i,setup,breakout_window,holding_window)
            if outcome is None:
                continue
            rows.append({'Stock':sym,'Setup Date':setup['date'].strftime('%Y-%m-%d'),'Score':setup['score'],
                         'To Pivot %':round(setup['distance'],2),'RS vs Nifty %':round(setup['rs'],2),**outcome})
    return pd.DataFrame(rows)

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

    st.markdown('### 📈 Historical Validation')
    st.caption('Walk-forward test: each historical setup is evaluated using only information available on that setup day. A breakout means the closing price first moved above that day\'s pivot within the selected window; entry is the next day open.')
    with st.expander('Run historical validation', expanded=False):
        lookback = st.slider('Historical setup days', 30, 120, 90, 10, help='Number of recent eligible trading days per stock to test.')
        breakout_window = st.slider('Breakout window (days)', 5, 20, 10, 1)
        holding_window = st.slider('Post-entry measurement window (days)', 5, 30, 15, 1)
        run_bt = st.button('🧪 RUN HISTORICAL VALIDATION', use_container_width=True)
        if run_bt:
            with st.spinner('Testing historical pre-breakout setups across the NIFTY 500...'):
                bt = run_historical_validation(tuple(symbols), data, nifty, cfg, lookback, breakout_window, holding_window)
            if bt.empty:
                st.warning('No historical setups passed the current structural gates in the selected window. That is useful information — it means the current rules are extremely selective.')
            else:
                total=len(bt); br=int(bt['breakout'].sum()); br_rate=br/total*100
                avg_gain=bt.loc[bt['breakout'],'max_gain_pct'].mean() if br else np.nan
                med_gain=bt.loc[bt['breakout'],'max_gain_pct'].median() if br else np.nan
                avg_dd=bt.loc[bt['breakout'],'max_drawdown_pct'].mean() if br else np.nan
                false_rate=bt.loc[bt['breakout'],'false_breakout'].mean()*100 if br else np.nan
                strong_vol=(bt.loc[bt['breakout'],'breakout_volume_ratio']>=1.5).mean()*100 if br else np.nan
                c1,c2,c3,c4=st.columns(4)
                c1.metric('Historical setups', total)
                c2.metric('Breakout within window', f'{br_rate:.1f}%')
                c3.metric('Median max gain', f'{med_gain:.1f}%' if np.isfinite(med_gain) else '—')
                c4.metric('False-breakout rate', f'{false_rate:.1f}%' if np.isfinite(false_rate) else '—')
                st.write(f'Average max gain after next-day entry: **{avg_gain:.1f}%** · Average max drawdown: **{avg_dd:.1f}%** · Breakouts with ≥1.5× 20D volume: **{strong_vol:.1f}%**' if br else 'No observed breakouts in the selected window.')
                bt['Score Band']=pd.cut(bt['Score'], bins=[-1,79,84,89,94,100], labels=['<80','80–84','85–89','90–94','95–100'])
                band=bt.groupby('Score Band', observed=False).agg(Setups=('Stock','size'), Breakout_Rate=('breakout','mean'), Median_Max_Gain=('max_gain_pct','median'), False_Breakout_Rate=('false_breakout','mean')).reset_index()
                band['Breakout_Rate']=(band['Breakout_Rate']*100).round(1)
                band['False_Breakout_Rate']=(band['False_Breakout_Rate']*100).round(1)
                band['Median_Max_Gain']=band['Median_Max_Gain'].round(1)
                st.markdown('#### Performance by score band')
                st.dataframe(band, use_container_width=True, hide_index=True)
                detail=bt.sort_values(['Score','Setup Date'],ascending=[False,False]).copy()
                detail['Breakout']=detail['breakout'].map({True:'YES',False:'NO'})
                detail['Max Gain %']=detail['max_gain_pct'].round(1)
                detail['Max Drawdown %']=detail['max_drawdown_pct'].round(1)
                detail['Breakout Vol ×']=detail['breakout_volume_ratio'].round(2)
                detail['False Breakout']=detail['false_breakout'].map({True:'YES',False:'NO'})
                st.markdown('#### Historical setup events')
                st.dataframe(detail[['Stock','Setup Date','Score','To Pivot %','Breakout','days_to_breakout','Max Gain %','Max Drawdown %','Breakout Vol ×','False Breakout']], use_container_width=True, hide_index=True)
                st.download_button('⬇️ Download historical validation CSV', bt.to_csv(index=False).encode('utf-8'), 'prebreakout_historical_validation.csv', 'text/csv')

    st.markdown('### How to trade the result')
    st.warning('Do NOT buy just because a stock appears here. The scanner is detecting compression. Wait for a daily close above the Pivot with convincing volume, then apply your own entry/stop/risk rules.')

if __name__ == '__main__':
    main()
