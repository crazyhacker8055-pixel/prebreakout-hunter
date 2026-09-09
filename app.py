import streamlit as st
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime, timedelta

st.set_page_config(page_title='Pre-Breakout Hunter V6.1', page_icon='🎯', layout='wide')

MIN_BARS = 230
DEFAULTS = {
    'range5': 4.5, 'range10': 7.5, 'range20': 12.0,
    'atr_ratio': 0.72, 'vol_ratio': 0.70,
    'pivot_min': 0.5, 'pivot_max': 4.0, 'min_score': 80,
}

@st.cache_data(ttl=86400, show_spinner=False)
def get_nifty500_symbols():
    import io, requests
    github_url = ('https://raw.githubusercontent.com/ganeshbiyer/Nse_Historical_Data/'
                  'refs/heads/main/nifty500_symbols.csv')
    github_msg = 'unknown'
    try:
        r = requests.get(github_url, timeout=20, headers={'User-Agent': 'Mozilla/5.0'})
        r.raise_for_status()
        t = pd.read_csv(io.BytesIO(r.content))
        col = next((c for c in t.columns if str(c).strip().lower() == 'symbol'), None)
        if col is None:
            raise RuntimeError('GitHub CSV has no Symbol column.')
        syms = list(dict.fromkeys(t[col].astype(str).str.strip().tolist()))
        syms = [s for s in syms if s and s.lower() != 'nan' and s.upper() != 'SYMBOL']
        if len(syms) >= 450:
            return syms
    except Exception as e:
        github_msg = str(e)
    nse_url = 'https://archives.nseindia.com/content/indices/ind_nifty500list.csv'
    try:
        r = requests.get(nse_url, timeout=20, headers={
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'text/csv,application/csv,*/*', 'Referer': 'https://www.nseindia.com/'})
        r.raise_for_status()
        t = pd.read_csv(io.BytesIO(r.content))
        col = next((c for c in t.columns if str(c).strip().lower() == 'symbol'), None)
        if col is None:
            raise RuntimeError('NSE CSV has no Symbol column.')
        syms = list(dict.fromkeys(t[col].astype(str).str.strip().tolist()))
        if len(syms) >= 450:
            return syms
        raise RuntimeError(f'Only {len(syms)} symbols returned.')
    except Exception as e:
        raise RuntimeError(f'NIFTY 500 list failed. GitHub: {github_msg}; NSE: {e}')

@st.cache_data(ttl=900, show_spinner=False)
def download_batch(symbols, days):
    """Download a manageable batch to avoid Streamlit Cloud timeouts."""
    tickers=[f'{s}.NS' for s in symbols]
    end=datetime.now()
    start=end-timedelta(days=days)
    return yf.download(
        tickers=tickers, start=start.strftime('%Y-%m-%d'),
        end=(end+timedelta(days=1)).strftime('%Y-%m-%d'), interval='1d',
        auto_adjust=False, group_by='column', threads=False, progress=False
    )

@st.cache_data(ttl=900, show_spinner=False)
def download_index(days):
    end=datetime.now(); start=end-timedelta(days=days)
    return yf.download(
        tickers=['^NSEI'], start=start.strftime('%Y-%m-%d'),
        end=(end+timedelta(days=1)).strftime('%Y-%m-%d'), interval='1d',
        auto_adjust=False, group_by='column', threads=False, progress=False
    )

def download_market_data_batched(symbols, days, progress_callback=None, batch_size=40):
    """Download stocks in small batches. This is slower per request but much more reliable."""
    parts=[]; total=(len(symbols)+batch_size-1)//batch_size
    for n,start_i in enumerate(range(0,len(symbols),batch_size),1):
        batch=tuple(symbols[start_i:start_i+batch_size])
        try:
            x=download_batch(batch,days)
            if x is not None and not x.empty:
                parts.append(x)
        except Exception:
            pass
        if progress_callback: progress_callback(n/total)
    idx=download_index(days)
    if parts:
        data=pd.concat(parts,axis=1)
    else:
        data=pd.DataFrame()
    if idx is not None and not idx.empty:
        data=pd.concat([data,idx],axis=1) if not data.empty else idx
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
    return pd.DataFrame({k.lower(): col(data, k, ticker) for k in ['Open','High','Low','Close','Volume']}).dropna()

def prepare_features(d, nifty):
    d = d.sort_index().copy()
    c, h, l, o, v = d['close'], d['high'], d['low'], d['open'], d['volume']
    sma50 = c.rolling(50).mean()
    sma150 = c.rolling(150).mean()
    sma200 = c.rolling(200).mean()
    ema20 = c.ewm(span=20, adjust=False).mean()
    ema220 = c.ewm(span=220, adjust=False).mean()

    tr = pd.concat([h-l, (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    atr5, atr20 = tr.rolling(5).mean(), tr.rolling(20).mean()
    r5 = (h.rolling(5).max()-l.rolling(5).min())/l.rolling(5).min()*100
    r10 = (h.rolling(10).max()-l.rolling(10).min())/l.rolling(10).min()*100
    r20 = (h.rolling(20).max()-l.rolling(20).min())/l.rolling(20).min()*100
    vol5, vol20 = v.rolling(5).mean(), v.rolling(20).mean()

    # Setup-day pivot: highest high from the prior 20 sessions, so today's bar cannot set its own pivot.
    pivot = h.rolling(20).max().shift(1)
    distance = (pivot-c)/pivot*100

    # Recent breakout uses prior pivots only.
    recent_breakout = (c >= pivot).rolling(3).max().fillna(False).astype(bool)

    # Higher-low support structure.
    low10 = l.rolling(10).min()
    prev_low10 = low10.shift(10)
    higher_low = low10 >= prev_low10 * 0.985

    # Distribution: bearish volume meaningfully outweighing bullish volume.
    down_vol = v.where(c < o).rolling(20).mean()
    up_vol = v.where(c >= o).rolling(20).mean()
    distribution = (down_vol > up_vol*1.35).fillna(False)

    nn = nifty.reindex(d.index).ffill()
    stock_ret20 = c/c.shift(20)-1
    nifty_ret20 = nn/nn.shift(20)-1
    rs = (stock_ret20-nifty_ret20)*100
    rs_prev = rs.shift(10)
    rs_accel = rs-rs_prev

    # Resistance-pressure features: repeated tests of prior pivot without breaking it.
    near_res = (distance >= 0) & (distance <= 4.0)
    resistance_tests = near_res.rolling(15).sum()
    close_position20 = (c-l.rolling(20).min())/(h.rolling(20).max()-l.rolling(20).min()).replace(0,np.nan)*100

    # Candle quality: compression should not be dominated by large bearish bodies or rejection wicks.
    body_pct = (c-o).abs()/o*100
    upper_wick_pct = (h-pd.concat([o,c],axis=1).max(axis=1))/o*100
    lower_wick_pct = (pd.concat([o,c],axis=1).min(axis=1)-l)/o*100
    bearish_body = ((c<o) & (body_pct>2.0)).rolling(10).sum()
    rejection_count = ((upper_wick_pct>1.2) & (upper_wick_pct>body_pct*1.5)).rolling(10).sum()
    avg_body20 = body_pct.rolling(20).mean()

    # Volume architecture: dry-up during setup, but avoid chronic illiquidity.
    volume_ratio = vol5/vol20.replace(0,np.nan)
    volume_slope = volume_ratio - volume_ratio.shift(5)

    # Progressive contraction. Use both the raw ranges and volatility ratios.
    trend = (c>sma50)&(sma50>sma150)&(sma150>sma200)&(sma200>sma200.shift(20))&(c>ema20)
    base = (r5<=DEFAULTS['range5']*1.25)&(r10<=DEFAULTS['range10']*1.20)&(r20<=DEFAULTS['range20']*1.20)
    pre = (c<pivot)&(distance>=0.20)&(distance<=7.0)
    structural = trend & base & pre & (~recent_breakout) & higher_low & (~distribution)

    return locals()

def score_at(f, i, cfg):
    keys = ['c','sma50','sma150','sma200','ema20','atr5','atr20','r5','r10','r20',
            'vol5','vol20','pivot','distance','rs','rs_accel','resistance_tests',
            'close_position20','bearish_body','rejection_count','avg_body20']
    if any(i >= len(f[k]) or not np.isfinite(f[k].iloc[i]) for k in keys):
        return None
    c = f['c'].iloc[i]
    r5, r10, r20 = f['r5'].iloc[i], f['r10'].iloc[i], f['r20'].iloc[i]
    ar = f['atr5'].iloc[i]/f['atr20'].iloc[i] if f['atr20'].iloc[i] else np.inf
    vr = f['vol5'].iloc[i]/f['vol20'].iloc[i] if f['vol20'].iloc[i] else np.inf
    dist, rs = f['distance'].iloc[i], f['rs'].iloc[i]
    rs_accel = f['rs_accel'].iloc[i]
    tests = f['resistance_tests'].iloc[i]
    cp = f['close_position20'].iloc[i]
    bear = f['bearish_body'].iloc[i]
    rej = f['rejection_count'].iloc[i]
    avg_body = f['avg_body20'].iloc[i]

    # V6 readiness score: structure + pressure + behavior. Maximum = 100.
    score = 0
    # Trend quality (25)
    score += 7 if c>f['sma50'].iloc[i] else 0
    score += 6 if f['sma50'].iloc[i]>f['sma150'].iloc[i] else 0
    score += 5 if f['sma150'].iloc[i]>f['sma200'].iloc[i] else 0
    score += 4 if f['sma200'].iloc[i]>f['sma200'].iloc[i-20] else 0
    score += 3 if c>f['ema20'].iloc[i] else 0
    # Compression quality (25)
    score += 4 if r5<=4 else (2 if r5<=cfg['range5'] else 0)
    score += 4 if r10<=7 else (2 if r10<=cfg['range10'] else 0)
    score += 4 if r20<=12 else (2 if r20<=cfg['range20'] else 0)
    score += 8 if r5<r10<r20 else (4 if r5<r10 or r10<r20 else 0)
    score += 5 if ar<=0.65 else (3 if ar<=cfg['atr_ratio'] else (1 if ar<=0.85 else 0))
    # Volume behavior (12)
    score += 6 if vr<=0.60 else (4 if vr<=cfg['vol_ratio'] else (2 if vr<=0.85 else 0))
    score += 3 if f['volume_slope'].iloc[i] <= 0.05 else 0
    score += 3 if 0.35 <= vr <= 0.85 else 0
    # Support/candle quality (13)
    score += 4 if bool(f['higher_low'].iloc[i]) else 0
    score += 3 if bear <= 2 else (1 if bear <= 4 else 0)
    score += 3 if rej <= 2 else (1 if rej <= 4 else 0)
    score += 3 if avg_body <= 1.8 else (1 if avg_body <= 2.5 else 0)
    # Resistance pressure + RS (25)
    score += 7 if 3 <= tests <= 8 else (4 if 1 <= tests <= 10 else 0)
    score += 6 if 0.5 <= dist <= 2.5 else (4 if cfg['pivot_min']<=dist<=cfg['pivot_max'] else (2 if 0.2<=dist<=7 else 0))
    score += 5 if cp >= 75 else (3 if cp >= 60 else 0)
    score += 4 if rs >= 6 else (2 if rs >= 3 else (1 if rs >= 0 else 0))
    score += 3 if rs_accel >= 0 else 0

    score = min(100, int(score))
    strict = bool(f['structural'].iloc[i]) and score >= cfg['min_score']
    return {
        'date': f['d'].index[i], 'close': float(c), 'pivot': float(f['pivot'].iloc[i]),
        'score': score, 'distance': float(dist), 'rs': float(rs), 'rs_accel': float(rs_accel),
        'tests': float(tests), 'close_position20': float(cp), 'atr_ratio': float(ar),
        'vol_ratio': float(vr), 'strict': strict,
    }

def evaluate(df, nifty, cfg):
    if len(df)<MIN_BARS: return None
    f=prepare_features(df,nifty)
    x=score_at(f,len(df)-1,cfg)
    if x is None:return None
    i=len(df)-1
    r5,r10,r20=f['r5'].iloc[i],f['r10'].iloc[i],f['r20'].iloc[i]
    reasons=[]
    if not(r5<r10<r20): reasons.append('Contraction incomplete')
    if x['atr_ratio']>cfg['atr_ratio']: reasons.append('ATR contraction weak')
    if x['vol_ratio']>cfg['vol_ratio']: reasons.append('Volume dry-up weak')
    if x['distance']>7: reasons.append('Too far below resistance')
    elif x['distance']<0.2: reasons.append('Already too close / nearly breaking')
    elif not(cfg['pivot_min']<=x['distance']<=cfg['pivot_max']): reasons.append('Resistance distance not ideal')
    if x['rs']<0: reasons.append('Relative strength below NIFTY')
    if x['rs_accel']<0: reasons.append('Relative strength weakening')
    if x['tests']<2: reasons.append('Too few resistance tests')
    if x['close_position20']<60: reasons.append('Close not pressing upper base')
    if f['bearish_body'].iloc[i]>4: reasons.append('Too many large bearish candles')
    if f['rejection_count'].iloc[i]>4: reasons.append('Repeated upper-wick rejection')
    if x['score']<cfg['min_score']: reasons.append(f"Score below {cfg['min_score']}")
    if not bool(f['trend'].iloc[i]): reasons.insert(0,'Trend not strong enough')
    if not bool(f['base'].iloc[i]): reasons.insert(0,'Base ranges too wide')
    if bool(f['recent_breakout'].iloc[i]): reasons.append('Recent breakout detected')
    if not bool(f['higher_low'].iloc[i]): reasons.append('Higher-low structure weak')
    if bool(f['distribution'].iloc[i]): reasons.append('Distribution pressure detected')
    return {
        'Stock':'','Close':round(x['close'],2),'Score':x['score'],
        '5D Range %':round(r5,2),'10D Range %':round(r10,2),'20D Range %':round(r20,2),
        'ATR5/ATR20':round(x['atr_ratio'],2),'Vol5/Vol20':round(x['vol_ratio'],2),
        'Pivot':round(x['pivot'],2),'To Pivot %':round(x['distance'],2),
        'RS vs Nifty %':round(x['rs'],2),'RS Accel %':round(x['rs_accel'],2),
        'Resistance Tests':int(round(x['tests'])),'Close Position %':round(x['close_position20'],1),
        'Status':'🔥 PRE-BREAKOUT' if x['strict'] else '👀 NEAR-MISS',
        'Why it missed':'PASS — structure + readiness score' if x['strict'] else ' • '.join(reasons[:5]),
        '_strict':x['strict']
    }

def trade_outcome(d, setup_i, setup, breakout_window, hold_window):
    if setup_i+2>=len(d): return None
    end=min(len(d)-1,setup_i+breakout_window)
    bi=None
    for j in range(setup_i+1,end+1):
        if d['close'].iloc[j]>setup['pivot']:
            bi=j; break
    if bi is None or bi+1>=len(d):
        return {'breakout':False,'days_to_breakout':np.nan,'entry':np.nan,'exit':np.nan,
                'return_pct':np.nan,'max_gain_pct':np.nan,'max_drawdown_pct':np.nan,
                'exit_reason':'NO BREAKOUT','breakout_volume_ratio':np.nan}
    ei=bi+1
    entry=float(d['open'].iloc[ei]); stop=entry*0.85
    if not np.isfinite(entry) or entry<=0:return None
    ema220=d['close'].ewm(span=220,adjust=False).mean()
    end_hold=min(len(d)-2,ei+hold_window)
    trigger_i=None; reason='TIME CAP'
    for k in range(ei,end_hold+1):
        stop_hit=float(d['low'].iloc[k])<=stop
        ema_hit=float(d['close'].iloc[k])<float(ema220.iloc[k])
        if stop_hit or ema_hit:
            trigger_i=k
            if stop_hit and ema_hit: reason='-15% STOP + CLOSE < 220 EMA'
            elif stop_hit: reason='-15% STOP'
            else: reason='CLOSE < 220 EMA'
            break
    if trigger_i is not None and trigger_i+1<len(d):
        exit_i=trigger_i+1; exit_price=float(d['open'].iloc[exit_i]); exit_type='NEXT OPEN'
    else:
        exit_i=end_hold; exit_price=float(d['close'].iloc[exit_i]); exit_type='TIME CAP CLOSE'
    ret=(exit_price/entry-1)*100
    future=d.iloc[ei:end_hold+1]
    max_gain=(future['high'].max()/entry-1)*100
    max_dd=(future['low'].min()/entry-1)*100
    vol20=d['volume'].iloc[max(0,bi-19):bi+1].mean()
    brvol=float(d['volume'].iloc[bi]/vol20) if vol20 else np.nan
    return {'breakout':True,'days_to_breakout':bi-setup_i,'entry':entry,'exit':exit_price,
            'return_pct':ret,'max_gain_pct':max_gain,'max_drawdown_pct':max_dd,
            'exit_reason':reason+' / '+exit_type,'breakout_volume_ratio':brvol}

def run_validation(symbols,data,nifty,cfg,lookback,breakout_window,hold_window,progress_callback=None):
    rows=[]; total=len(symbols)
    for si,sym in enumerate(symbols):
        try:
            d=ohlcv_from_download(data,f'{sym}.NS')
            if len(d)<MIN_BARS+breakout_window+hold_window+2: continue
            f=prepare_features(d,nifty.reindex(d.index).ffill())
            last=len(d)-breakout_window-hold_window-2
            first=max(MIN_BARS-1,last-lookback+1)
            next_allowed=first
            for i in range(first,last+1):
                if i<next_allowed: continue
                s=score_at(f,i,cfg)
                if s is None or not s['strict']: continue
                out=trade_outcome(d,i,s,breakout_window,hold_window)
                if out is None: continue
                rows.append({'Stock':sym,'Setup Date':s['date'].strftime('%Y-%m-%d'),
                             'Score':s['score'],'To Pivot %':round(s['distance'],2),
                             'RS vs Nifty %':round(s['rs'],2),'RS Accel %':round(s['rs_accel'],2),
                             'Resistance Tests':int(round(s['tests'])),**out})
                next_allowed=i+breakout_window+hold_window+1
        except Exception:
            pass
        if progress_callback: progress_callback((si+1)/total)
    return pd.DataFrame(rows)

def summarize(bt):
    if bt.empty: return {}
    br=bt[bt['breakout']==True]
    trades=br.dropna(subset=['return_pct'])
    wins=(trades['return_pct']>0).mean()*100 if len(trades) else np.nan
    return {
        'setups':len(bt), 'breakout_rate':bt['breakout'].mean()*100,
        'win_rate':wins, 'avg_return':trades['return_pct'].mean() if len(trades) else np.nan,
        'median_return':trades['return_pct'].median() if len(trades) else np.nan,
        'avg_gain':trades['max_gain_pct'].mean() if len(trades) else np.nan,
        'avg_dd':trades['max_drawdown_pct'].mean() if len(trades) else np.nan,
        'strong_vol':(br['breakout_volume_ratio']>=1.5).mean()*100 if len(br) else np.nan,
    }

def render_validation(bt):
    if bt is None:return
    if bt.empty:
        st.warning('No historical setups passed the current V6 structural gates in this window.')
        return
    s=summarize(bt)
    cols=st.columns(6)
    vals=[
        ('Setups',f"{s['setups']}"),('Breakout rate',f"{s['breakout_rate']:.1f}%"),
        ('Trade win rate',f"{s['win_rate']:.1f}%"),('Avg trade return',f"{s['avg_return']:.2f}%"),
        ('Median return',f"{s['median_return']:.2f}%"),('Avg max drawdown',f"{s['avg_dd']:.2f}%"),
    ]
    for c,(a,b) in zip(cols,vals):
        c.metric(a,b)
    st.write(f"Average max gain: **{s['avg_gain']:.2f}%** · Breakouts with ≥1.5× volume: **{s['strong_vol']:.1f}%**")

    st.markdown('#### 🧪 Walk-forward / out-of-sample check')
    bt2=bt.copy(); bt2['Setup Date']=pd.to_datetime(bt2['Setup Date'])
    cutoff=bt2['Setup Date'].quantile(0.60)
    train=bt2[bt2['Setup Date']<=cutoff]; test=bt2[bt2['Setup Date']>cutoff]
    tr=summarize(train); te=summarize(test)
    wf=pd.DataFrame([
        {'Period':'Earlier 60% (development)','Setups':tr.get('setups',0),'Breakout %':round(tr.get('breakout_rate',np.nan),1),'Win %':round(tr.get('win_rate',np.nan),1),'Avg trade %':round(tr.get('avg_return',np.nan),2),'Median trade %':round(tr.get('median_return',np.nan),2)},
        {'Period':'Recent 40% (out-of-sample)','Setups':te.get('setups',0),'Breakout %':round(te.get('breakout_rate',np.nan),1),'Win %':round(te.get('win_rate',np.nan),1),'Avg trade %':round(te.get('avg_return',np.nan),2),'Median trade %':round(te.get('median_return',np.nan),2)},
    ])
    st.dataframe(wf,use_container_width=True,hide_index=True)

    st.markdown('#### 🏆 Performance by V6 score band')
    bands=[(80,84),(85,89),(90,94),(95,100)]
    rows=[]
    for lo,hi in bands:
        q=bt2[(bt2['Score']>=lo)&(bt2['Score']<=hi)]
        z=summarize(q)
        rows.append({'Score Band':f'{lo}-{hi}','Setups':z.get('setups',0),'Breakout %':None if not z else round(z.get('breakout_rate',np.nan),1),'Win %':None if not z else round(z.get('win_rate',np.nan),1),'Avg trade %':None if not z else round(z.get('avg_return',np.nan),2),'Median trade %':None if not z else round(z.get('median_return',np.nan),2)})
    st.dataframe(pd.DataFrame(rows),use_container_width=True,hide_index=True)

    st.markdown('#### 📋 Historical V6 trade events')
    show=bt.sort_values(['Score','Setup Date'],ascending=[False,False]).head(30).copy()
    st.dataframe(show,use_container_width=True,hide_index=True)
    st.download_button('⬇️ Download V6 validation CSV',bt.to_csv(index=False).encode(),'prebreakout_v6_1_validation.csv','text/csv')

def main():
    st.title('🎯 Pre-Breakout Hunter V6.1')
    st.caption('Selective NSE scanner — structure + breakout readiness, with long walk-forward validation. No broker API required.')
    st.info('V6.1 downloads market data in small batches to avoid Yahoo/Streamlit Cloud timeouts. Normal scan uses recent data; historical validation downloads the longer history only when requested.')
    with st.sidebar:
        st.header('Scanner settings')
        min_score=st.slider('Minimum score',70,100,DEFAULTS['min_score'])
        max_range5=st.number_input('5D max range %',2.0,8.0,DEFAULTS['range5'],0.1)
        max_range10=st.number_input('10D max range %',4.0,12.0,DEFAULTS['range10'],0.1)
        max_range20=st.number_input('20D max range %',6.0,20.0,DEFAULTS['range20'],0.1)
        max_atr=st.number_input('Max ATR5/ATR20',0.50,1.00,DEFAULTS['atr_ratio'],0.01)
        max_vol=st.number_input('Max Vol5/Vol20',0.40,1.00,DEFAULTS['vol_ratio'],0.01)
        pivot_min=st.number_input('Min distance to pivot %',0.0,3.0,DEFAULTS['pivot_min'],0.1)
        pivot_max=st.number_input('Max distance to pivot %',1.0,8.0,DEFAULTS['pivot_max'],0.1)
        scan=st.button('🚀 SCAN NIFTY 500',type='primary',use_container_width=True)

    st.markdown('''### Exact setup being hunted
**V6.1:** strong trend → controlled base → progressive contraction → higher lows → repeated resistance pressure → improving relative strength → quiet volume → breakout still ahead.

**Breakout-readiness scoring** now rewards resistance tests, closing position inside the base, cleaner candles, relative-strength acceleration and healthier volume behavior. Historical testing uses only setup-day information.''')
    cfg={'range5':max_range5,'range10':max_range10,'range20':max_range20,'atr_ratio':max_atr,'vol_ratio':max_vol,'pivot_min':pivot_min,'pivot_max':pivot_max,'min_score':min_score}
    defaults={'scan_done':False,'scan_cfg':None,'scan_symbols':None,'scan_data':None,'scan_nifty':None,'scan_all_out':None,'validation_result':None,'validation_error':None}
    for k,v in defaults.items():
        if k not in st.session_state: st.session_state[k]=v

    if scan:
        st.session_state.scan_done=False; st.session_state.validation_result=None; st.session_state.validation_error=None
        try: symbols=get_nifty500_symbols()
        except Exception as e: st.error(f'Could not load NIFTY 500 list: {e}'); return
        with st.spinner('Downloading recent daily data in small batches...'):
            dp=st.progress(0)
            data=download_market_data_batched(tuple(symbols), 420, lambda x: dp.progress(min(1,max(0,x))), 40)
            dp.empty()
        if data.empty:
            st.error('Market data download returned no usable data. Please retry in a minute.'); return
        nifty=ohlcv_from_download(data,'^NSEI')['close']; results=[]; p=st.progress(0)
        for i,sym in enumerate(symbols):
            try:
                r=evaluate(ohlcv_from_download(data,f'{sym}.NS'),nifty,cfg)
                if r: r['Stock']=sym; results.append(r)
            except Exception: pass
            p.progress((i+1)/len(symbols))
        p.empty(); all_out=pd.DataFrame(results)
        if all_out.empty: st.error('No stocks had enough usable daily data to evaluate.'); return
        st.session_state.update(scan_done=True,scan_cfg=cfg,scan_symbols=tuple(symbols),scan_data=data,scan_nifty=nifty,scan_all_out=all_out)

    if not st.session_state.scan_done:
        st.info('Press **SCAN NIFTY 500** to run the scanner. It uses public Yahoo Finance market data; no broker API key is needed.'); return

    cfg=st.session_state.scan_cfg; symbols=st.session_state.scan_symbols; data=st.session_state.scan_data; nifty=st.session_state.scan_nifty; all_out=st.session_state.scan_all_out
    st.write(f'Universe: **{len(symbols)} stocks**')
    strict=all_out[all_out['_strict']].sort_values(['Score','To Pivot %'],ascending=[False,True]).reset_index(drop=True)
    near=all_out[~all_out['_strict']].sort_values(['Score','To Pivot %'],ascending=[False,True]).head(20)
    if strict.empty:
        st.warning('No A-grade V6 pre-breakout setups found today. That is intentional — zero can be a valid result.')
    else:
        st.success(f'Found **{len(strict)}** strict V6 pre-breakout candidate(s).')
        st.dataframe(strict[['Stock','Score','Close','5D Range %','10D Range %','20D Range %','ATR5/ATR20','Vol5/Vol20','To Pivot %','RS vs Nifty %','RS Accel %','Resistance Tests','Close Position %','Status']],use_container_width=True,hide_index=True)
        st.download_button('⬇️ Download A-grade results CSV',strict.drop(columns=['_strict']).to_csv(index=False).encode(),'prebreakout_v6_1_results.csv','text/csv')
    st.markdown('### 🔎 Near-Miss Diagnostic')
    st.caption('Not buy signals. These are the strongest almost-qualified structures and the readiness feature that is missing.')
    if near.empty: st.info('No near-miss stocks were available.')
    else:
        st.dataframe(near[['Stock','Score','Close','5D Range %','10D Range %','20D Range %','ATR5/ATR20','Vol5/Vol20','To Pivot %','RS vs Nifty %','RS Accel %','Resistance Tests','Close Position %','Why it missed']],use_container_width=True,hide_index=True)
        st.download_button('⬇️ Download diagnostic CSV',near.drop(columns=['_strict']).to_csv(index=False).encode(),'prebreakout_v6_1_diagnostic.csv','text/csv')

    st.markdown('### 📈 V6.1 Long Walk-Forward Trade Validation')
    st.caption('Uses up to ~3 years of daily data. Setup signals use only information available on the setup date. Breakout = first close above setup-day pivot. Entry = next-day open. Exit = next-day open after close below 220 EMA or -15% price-risk trigger. No future information is used to score the setup.')
    with st.expander('Run V6.1 historical trade validation',expanded=False):
        lookback=st.slider('Historical setup days',120,700,500,20,key='v6_lookback')
        bw=st.slider('Breakout window (days)',5,20,10,1,key='v6_breakout')
        hw=st.slider('Maximum observation / trade days',10,60,30,5,key='v6_hold')
        run=st.button('🧪 RUN V6.1 TRADE VALIDATION',use_container_width=True)
        if run:
            st.session_state.validation_result=None; st.session_state.validation_error=None; p=st.progress(0)
            try:
                # Validation needs much more history than the live scanner. Download it separately
                # in small batches so a single huge Yahoo request cannot stall/restart the app.
                with st.spinner('Downloading historical data in batches, then testing trades...'):
                    vp=st.progress(0)
                    hist=download_market_data_batched(tuple(symbols), 1150, lambda x: vp.progress(min(0.65,max(0,x))), 40)
                    vp.progress(0.68)
                    if hist.empty:
                        raise RuntimeError('Historical market-data download returned no data.')
                    hist_nifty=ohlcv_from_download(hist,'^NSEI')['close']
                    res=run_validation(tuple(symbols),hist,hist_nifty,cfg,lookback,bw,hw,lambda x: vp.progress(min(1,0.68+0.32*max(0,x))))
                    vp.progress(1.0)
                    vp.empty()
                st.session_state.validation_result=res
            except Exception as e:
                st.session_state.validation_error=str(e)
            p.empty()
        if st.session_state.validation_error:
            st.error('V6 validation failed: '+st.session_state.validation_error)
        render_validation(st.session_state.validation_result)

    st.markdown('### ⚠️ How to use the result')
    st.warning('A scanner result is a watchlist candidate, not an automatic buy. Wait for the daily closing breakout and confirm liquidity/volume, entry, stop and position-size rules before trading.')

if __name__ == '__main__':
    main()
