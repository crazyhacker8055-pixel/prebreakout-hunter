import streamlit as st
import pandas as pd
import numpy as np
import yfinance as yf
import requests
from io import StringIO
from datetime import datetime, timedelta

st.set_page_config(page_title='Pre-Breakout Hunter V8', page_icon='🎯', layout='wide')

MIN_BARS = 230
DEFAULTS = {
    'range5': 4.5, 'range10': 7.5, 'range20': 12.0,
    'atr_ratio': 0.72, 'vol_ratio': 0.70,
    'pivot_min': 0.25, 'pivot_max': 3.0, 'min_score': 88,
}

@st.cache_data(ttl=86400, show_spinner=False)
def get_nifty500_symbols():
    # Do NOT use Wikipedia here: Streamlit Cloud can receive HTTP 403 from Wikipedia.
    # Use a public GitHub mirror of the NSE NIFTY 500 constituent CSV instead.
    urls = [
        'https://raw.githubusercontent.com/pkjmesra/PKScreener/main/results/Indices/ind_nifty500list.csv',
        'https://raw.githubusercontent.com/sswapnil2/tradingview-mcp-india/main/src/tradingview_mcp/coinlist/nse.txt',
    ]
    headers = {'User-Agent': 'Mozilla/5.0 Pre-Breakout-Hunter/7.1'}
    last_error = None
    for url in urls:
        try:
            r = requests.get(url, headers=headers, timeout=20)
            r.raise_for_status()
            text = r.text
            if 'ind_nifty500list.csv' in url:
                t = pd.read_csv(StringIO(text))
                colname = next((c for c in t.columns if str(c).strip().lower() in {'symbol','ticker'}), None)
                if colname is not None:
                    syms = t[colname].astype(str).str.strip().tolist()
                else:
                    syms = []
            else:
                syms = [x.strip().replace('NSE:', '') for x in text.splitlines()]
            syms = [s for s in syms if s and s.lower() != 'nan' and s.upper() != 'SYMBOL']
            syms = list(dict.fromkeys(syms))
            if len(syms) >= 400:
                return syms
            last_error = f'Only {len(syms)} symbols returned from {url}'
        except Exception as e:
            last_error = e
    raise RuntimeError(f'Could not load NIFTY 500 list from public mirrors: {last_error}')

@st.cache_data(ttl=1800, show_spinner=False)
def download_one_batch(batch, start_date, end_date):
    """Download one Yahoo batch. Cached independently so completed batches survive reruns."""
    batch = tuple(batch)
    last_error = None
    for attempt in range(3):
        try:
            d = yf.download(list(batch), start=start_date, end=end_date, interval='1d',
                            auto_adjust=False, group_by='column', threads=False, progress=False)
            frames = {}
            for t in batch:
                df = ohlcv_from_download(d, t)
                if not df.empty:
                    frames[t] = df
            return frames
        except Exception as e:
            last_error = e
            if attempt < 2:
                import time
                time.sleep(2 * (attempt + 1))
    return {}

def download_batches_resumable(symbols, period_days=520, batch_size=50, progress=None, pause_seconds=1.0):
    end = datetime.now()
    start = end - timedelta(days=period_days)
    start_s = start.strftime('%Y-%m-%d'); end_s = (end + timedelta(days=1)).strftime('%Y-%m-%d')
    frames = {}
    batches = [tuple(symbols[i:i+batch_size]) for i in range(0, len(symbols), batch_size)]
    total = len(batches)
    for bi, batch in enumerate(batches, start=1):
        if progress is not None:
            progress.progress((bi-1)/total, text=f'Downloading batch {bi}/{total} ({len(batch)} stocks)…')
        got = download_one_batch(batch, start_s, end_s)
        frames.update(got)
        if pause_seconds and bi < total:
            import time
            time.sleep(pause_seconds)
    if progress is not None:
        progress.progress(1.0, text=f'Download complete: {len(frames)} stocks with data')
    return frames

def col(data, field, ticker):
    try:
        if isinstance(data.columns, pd.MultiIndex):
            if (field, ticker) in data.columns:
                return data[(field, ticker)].dropna()
            if (ticker, field) in data.columns:
                return data[(ticker, field)].dropna()
        if field in data.columns:
            return data[field].dropna()
    except Exception:
        pass
    return pd.Series(dtype=float)

def ohlcv_from_download(data, ticker):
    if data is None or len(data) == 0:
        return pd.DataFrame()
    out = pd.DataFrame({
        'open': col(data, 'Open', ticker), 'high': col(data, 'High', ticker),
        'low': col(data, 'Low', ticker), 'close': col(data, 'Close', ticker),
        'volume': col(data, 'Volume', ticker)
    }).dropna()
    return out

def atr(df, n):
    prev = df.close.shift(1)
    tr = pd.concat([df.high-df.low, (df.high-prev).abs(), (df.low-prev).abs()], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=n).mean()

def metrics(d, nifty):
    c,h,l,v = d.close,d.high,d.low,d.volume
    sma50=c.rolling(50).mean(); sma150=c.rolling(150).mean(); sma200=c.rolling(200).mean()
    ema20=c.ewm(span=20, adjust=False).mean(); atr5=atr(d,5); atr20=atr(d,20)
    r5=(h.iloc[-5:].max()-l.iloc[-5:].min())/l.iloc[-5:].min()*100
    r10=(h.iloc[-10:].max()-l.iloc[-10:].min())/l.iloc[-10:].min()*100
    r20=(h.iloc[-20:].max()-l.iloc[-20:].min())/l.iloc[-20:].min()*100
    pivot=h.iloc[-21:-1].max()
    distance=(pivot-c.iloc[-1])/pivot*100
    vol5=v.iloc[-5:].mean(); vol20=v.iloc[-20:].mean()
    vr=vol5/vol20 if vol20 else np.inf
    ar=atr5.iloc[-1]/atr20.iloc[-1] if atr20.iloc[-1] else np.inf
    n=nifty.reindex(d.index).ffill()
    rs=((c.iloc[-1]/c.iloc[-21]-1)-(n.iloc[-1]/n.iloc[-21]-1))*100 if len(n.dropna())>=22 else np.nan
    # Resistance pressure: count closes within 2% of pivot without breaking it.
    prev20=h.rolling(20).max().shift(1)
    near=(c.iloc[-15:] >= prev20.iloc[-15:]*0.98) & (c.iloc[-15:] < prev20.iloc[-15:])
    tests=int(near.sum())
    # Closing position inside recent base; higher means closer to upper boundary.
    base_hi=h.iloc[-20:].max(); base_lo=l.iloc[-20:].min()
    pos=(c.iloc[-1]-base_lo)/(base_hi-base_lo) if base_hi>base_lo else 0
    # Candle/body compression in the last 5 sessions.
    bodies=(d.open-d.close).abs()/d.close*100
    body5=bodies.iloc[-5:].mean(); body20=bodies.iloc[-20:].mean()
    body_ratio=body5/body20 if body20 else np.inf
    # Higher-low structure: three windows rising.
    low_a=l.iloc[-5:].min(); low_b=l.iloc[-10:-5].min(); low_c=l.iloc[-15:-10].min()
    higher_lows=(low_a>=low_b*0.995) and (low_b>=low_c*0.995)
    # Distribution balance.
    base=d.iloc[-20:]
    down=base.loc[base.close<base.open,'volume'].mean(); up=base.loc[base.close>=base.open,'volume'].mean()
    dist_ratio=down/up if np.isfinite(down) and np.isfinite(up) and up else 0
    return locals()

def evaluate(df, nifty, cfg, date=None):
    if len(df)<MIN_BARS or len(nifty)<MIN_BARS: return None
    d=df.sort_index().copy()
    if date is not None: d=d.loc[:date]
    if len(d)<MIN_BARS: return None
    n=nifty.reindex(d.index).ffill()
    if len(n.dropna())<MIN_BARS: return None
    m=metrics(d,n); x=d.iloc[-1]; c=d.close; h=d.high; l=d.low; v=d.volume
    if not np.isfinite(m['sma200'].iloc[-1]) if False else False: pass
    sma50=m['sma50'].iloc[-1]; sma150=m['sma150'].iloc[-1]; sma200=m['sma200'].iloc[-1]
    sma200_20=m['sma200'].iloc[-21]
    ar=m['ar']; vr=m['vr']; distance=m['distance']; rs=m['rs']
    # HARD STRUCTURAL GATES
    trend=(x.close>sma50>sma150>sma200 and sma200>sma200_20 and x.close>m['ema20'].iloc[-1])
    ranges=(m['r5']<=cfg['range5'] and m['r10']<=cfg['range10'] and m['r20']<=cfg['range20'] and m['r5']<m['r10']<m['r20'])
    compression=(ar<=cfg['atr_ratio'] and vr<=cfg['vol_ratio'] and m['body_ratio']<=0.80)
    pivot_ok=(cfg['pivot_min']<=distance<=cfg['pivot_max'] and x.close<m['pivot'])
    prev20=h.rolling(20).max().shift(1)
    no_break=(c.iloc[-5:]<prev20.iloc[-5:]).all()
    hl=m['higher_lows']
    dist_ok=m['dist_ratio']<=1.20
    rs_ok=rs>=1.0
    if not all([trend,ranges,compression,pivot_ok,no_break,hl,dist_ok,rs_ok]): return None
    # Readiness score, deliberately dominated by pre-breakout pressure rather than generic trend.
    score=0
    score += 12 if m['r5']<=4.0 else 7
    score += 10 if m['r10']<=7.0 else 6
    score += 8 if m['r20']<=10.0 else 5
    score += 10 if ar<=0.65 else 6
    score += 8 if vr<=0.60 else 5
    score += 8 if m['body_ratio']<=0.65 else 4
    score += 12 if m['tests']>=3 else (8 if m['tests']==2 else 4)
    score += 10 if distance<=1.25 else (7 if distance<=2 else 4)
    score += 7 if m['pos']>=0.70 else (4 if m['pos']>=0.55 else 2)
    score += 7 if rs>=4 else (4 if rs>=2 else 2)
    score += 5 if m['higher_lows'] else 0
    score += 3 if m['dist_ratio']<=0.90 else 1
    if score<cfg['min_score']: return None
    readiness='🔥 A+ READY' if score>=94 and m['tests']>=3 and distance<=1.5 else '🟢 A WATCH'
    return {'Stock':'', 'Score':int(score), 'Close':round(float(x.close),2),
            '5D Range %':round(float(m['r5']),2),'10D Range %':round(float(m['r10']),2),'20D Range %':round(float(m['r20']),2),
            'ATR5/ATR20':round(float(ar),2),'Vol5/Vol20':round(float(vr),2),'Resistance Tests':m['tests'],
            'Pivot':round(float(m['pivot']),2),'To Pivot %':round(float(distance),2),'Base Position %':round(float(m['pos']*100),1),
            'RS vs Nifty %':round(float(rs),2),'Body Ratio':round(float(m['body_ratio']),2),'Status':readiness}

def scan(symbols, frames, nifty, cfg):
    results=[]; near=[]
    for sym in symbols:
        df=frames.get(f'{sym}.NS',pd.DataFrame())
        if len(df)<MIN_BARS: continue
        try:
            r=evaluate(df,nifty,cfg)
            if r:
                r['Stock']=sym; results.append(r)
            else:
                # Diagnostic using a relaxed score but retaining data-quality requirements.
                rr=diagnostic(df,nifty,cfg)
                if rr: rr['Stock']=sym; near.append(rr)
        except Exception: continue
    out=pd.DataFrame(results); diag=pd.DataFrame(near)
    if not out.empty: out=out.sort_values(['Score','To Pivot %'],ascending=[False,True]).reset_index(drop=True)
    if not diag.empty: diag=diag.sort_values(['Score','To Pivot %'],ascending=[False,True]).head(15).reset_index(drop=True)
    return out,diag

def diagnostic(df,nifty,cfg):
    d=df.sort_index();
    if len(d)<MIN_BARS:return None
    n=nifty.reindex(d.index).ffill(); m=metrics(d,n); x=d.iloc[-1]
    sma50=m['sma50'].iloc[-1]; sma150=m['sma150'].iloc[-1]; sma200=m['sma200'].iloc[-1]
    if not np.isfinite(sma200): return None
    score=0
    score+=12 if x.close>sma50>sma150>sma200 else 0
    score+=10 if m['r5']<m['r10']<m['r20'] else 0
    score+=10 if m['ar']<=0.85 else 0
    score+=10 if m['vr']<=0.85 else 0
    score+=10 if m['higher_lows'] else 0
    score+=10 if m['tests']>=2 else (5 if m['tests']==1 else 0)
    score+=10 if 0<m['distance']<=5 else 0
    score+=10 if m['rs']>=0 else 0
    score+=8 if m['pos']>=0.60 else 0
    score+=5 if m['body_ratio']<=1 else 0
    if score<68:return None
    return {'Score':int(score),'Close':round(float(x.close),2),'5D Range %':round(float(m['r5']),2),'10D Range %':round(float(m['r10']),2),'20D Range %':round(float(m['r20']),2),'ATR5/ATR20':round(float(m['ar']),2),'Vol5/Vol20':round(float(m['vr']),2),'Resistance Tests':m['tests'],'To Pivot %':round(float(m['distance']),2)}

def backtest_fast(frames,nifty,cfg,setup_days=250,breakout_window=10,trade_days=30,progress=None):
    """Fast walk-forward validation.
    Indicators are calculated once per stock, then historical setup dates are tested
    from those precomputed features. This avoids recalculating hundreds of rolling
    windows for every setup date and is much safer on Streamlit Cloud.
    """
    events=[]
    items=list(frames.items())
    total=len(items)
    for stock_i,(sym,df) in enumerate(items, start=1):
        if len(df)<MIN_BARS+breakout_window+trade_days:
            continue
        try:
            d=df.sort_index().copy()
            n=nifty.reindex(d.index).ffill()
            if len(n.dropna())<MIN_BARS:
                continue
            c,h,l,v=d.close,d.high,d.low,d.volume
            sma50=c.rolling(50).mean(); sma150=c.rolling(150).mean(); sma200=c.rolling(200).mean()
            ema20=c.ewm(span=20,adjust=False).mean(); a5=atr(d,5); a20=atr(d,20)
            r5=(h.rolling(5).max()-l.rolling(5).min())/l.rolling(5).min()*100
            r10=(h.rolling(10).max()-l.rolling(10).min())/l.rolling(10).min()*100
            r20=(h.rolling(20).max()-l.rolling(20).min())/l.rolling(20).min()*100
            pivot=h.rolling(20).max().shift(1)
            distance=(pivot-c)/pivot*100
            vr=v.rolling(5).mean()/v.rolling(20).mean()
            ar=a5/a20
            rs=((c/c.shift(20)-1)-(n/n.shift(20)-1))*100
            prev20=h.rolling(20).max().shift(1)
            near=((c>=prev20*0.98)&(c<prev20)).rolling(15).sum()
            base_hi=h.rolling(20).max(); base_lo=l.rolling(20).min()
            pos=(c-base_lo)/(base_hi-base_lo)
            bodies=(d.open-d.close).abs()/c*100
            body_ratio=bodies.rolling(5).mean()/bodies.rolling(20).mean()
            low5=l.rolling(5).min(); low10=l.shift(5).rolling(5).min(); low15=l.shift(10).rolling(5).min()
            higher_lows=(low5>=low10*0.995)&(low10>=low15*0.995)
            downvol=d.volume.where(d.close<d.open).rolling(20).mean()
            upvol=d.volume.where(d.close>=d.open).rolling(20).mean()
            dist_ratio=downvol/upvol
            sma200_20=sma200.shift(20)
            feat=pd.DataFrame({
                'close':c,'sma50':sma50,'sma150':sma150,'sma200':sma200,'sma200_20':sma200_20,
                'ema20':ema20,'r5':r5,'r10':r10,'r20':r20,'ar':ar,'vr':vr,'pivot':pivot,
                'distance':distance,'tests':near,'pos':pos,'body_ratio':body_ratio,
                'higher_lows':higher_lows,'dist_ratio':dist_ratio,'rs':rs
            }).dropna()
            dates=feat.index
            usable=dates[:-breakout_window-trade_days]
            if len(usable)>setup_days:
                # Evenly sample setup dates, preserving a broad historical sample.
                idx=np.linspace(0,len(usable)-1,setup_days,dtype=int)
                usable=usable[idx]
            for dt in usable:
                m=feat.loc[dt]
                trend=(m.close>m.sma50>m.sma150>m.sma200 and m.sma200>m.sma200_20 and m.close>m.ema20)
                ranges=(m.r5<=cfg['range5'] and m.r10<=cfg['range10'] and m.r20<=cfg['range20'] and m.r5<m.r10<m.r20)
                compression=(m.ar<=cfg['atr_ratio'] and m.vr<=cfg['vol_ratio'] and m.body_ratio<=0.80)
                pivot_ok=(cfg['pivot_min']<=m.distance<=cfg['pivot_max'] and m.close<m.pivot)
                loc=d.index.get_loc(dt)
                no_break=(c.iloc[loc-4:loc+1]<prev20.iloc[loc-4:loc+1]).all() if loc>=4 else False
                if not all([trend,ranges,compression,pivot_ok,no_break,bool(m.higher_lows),m.dist_ratio<=1.20,m.rs>=1.0]):
                    continue
                score=0
                score += 12 if m.r5<=4.0 else 7
                score += 10 if m.r10<=7.0 else 6
                score += 8 if m.r20<=10.0 else 5
                score += 10 if m.ar<=0.65 else 6
                score += 8 if m.vr<=0.60 else 5
                score += 8 if m.body_ratio<=0.65 else 4
                score += 12 if m.tests>=3 else (8 if m.tests==2 else 4)
                score += 10 if m.distance<=1.25 else (7 if m.distance<=2 else 4)
                score += 7 if m.pos>=0.70 else (4 if m.pos>=0.55 else 2)
                score += 7 if m.rs>=4 else (4 if m.rs>=2 else 2)
                score += 5
                score += 3 if m.dist_ratio<=0.90 else 1
                if score<cfg['min_score']:
                    continue
                after=d.loc[dt:].iloc[1:breakout_window+trade_days+1]
                if len(after)<breakout_window+1:
                    continue
                br=None
                for j,(ix,row) in enumerate(after.iloc[:breakout_window].iterrows(),start=1):
                    if row.close>m.pivot:
                        br=(j,ix); break
                ev={'Stock':sym.replace('.NS',''),'Setup Date':dt.strftime('%Y-%m-%d'),'Score':int(score),'To Pivot %':round(float(m.distance),2)}
                if br is None:
                    ev.update({'Breakout':'NO','Days to Breakout':np.nan,'Entry':np.nan,'Trade Return %':np.nan,'Max Gain %':np.nan,'Max Drawdown %':np.nan,'Win':'NO'})
                else:
                    j,bdate=br
                    bidx=d.index.get_loc(bdate)
                    if bidx+1>=len(d): continue
                    entry=float(d.open.iloc[bidx+1])
                    endidx=min(bidx+trade_days+1,len(d)-1)
                    exit_price=None
                    for k in range(bidx,endidx):
                        close_k=float(c.iloc[k])
                        sma220_k=float(c.iloc[max(0,k-219):k+1].mean()) if k>=219 else np.nan
                        trigger=(np.isfinite(sma220_k) and close_k<sma220_k) or close_k<=entry*0.85
                        if trigger and k+1<len(d):
                            exit_price=float(d.open.iloc[k+1]); break
                    if exit_price is None:
                        exit_price=float(c.iloc[endidx])
                    trade_ret=(exit_price/entry-1)*100
                    path=d.iloc[bidx:min(endidx+1,len(d))]
                    max_gain=(path.high.max()/entry-1)*100
                    max_dd=(path.low.min()/entry-1)*100
                    ev.update({'Breakout':'YES','Days to Breakout':j,'Entry':round(entry,2),'Trade Return %':round(trade_ret,2),'Max Gain %':round(max_gain,2),'Max Drawdown %':round(max_dd,2),'Win':'YES' if trade_ret>0 else 'NO'})
                events.append(ev)
        except Exception:
            continue
        if progress is not None:
            progress.progress(stock_i/total, text=f'Validating stock {stock_i}/{total}…')
    return pd.DataFrame(events)

def main():
    if 'scan_done' not in st.session_state:
        st.session_state.scan_done = False
    if 'scan_out' not in st.session_state:
        st.session_state.scan_out = pd.DataFrame()
    if 'scan_diag' not in st.session_state:
        st.session_state.scan_diag = pd.DataFrame()
    if 'symbols' not in st.session_state:
        st.session_state.symbols = []

    st.title('🎯 Pre-Breakout Hunter V8.1')
    st.caption('Selective NSE scanner — resistance pressure + progressive compression + higher lows + improving relative strength. No broker API required.')
    with st.sidebar:
        st.header('Scanner settings')
        min_score=st.slider('Minimum score',80,100,DEFAULTS['min_score'])
        r5=st.number_input('5D max range %',2.0,8.0,DEFAULTS['range5'],0.1)
        r10=st.number_input('10D max range %',4.0,12.0,DEFAULTS['range10'],0.1)
        r20=st.number_input('20D max range %',6.0,20.0,DEFAULTS['range20'],0.1)
        atrmax=st.number_input('Max ATR5/ATR20',0.50,1.00,DEFAULTS['atr_ratio'],0.01)
        volmax=st.number_input('Max Vol5/Vol20',0.40,1.00,DEFAULTS['vol_ratio'],0.01)
        pmin=st.number_input('Min distance to pivot %',0.0,3.0,DEFAULTS['pivot_min'],0.1)
        pmax=st.number_input('Max distance to pivot %',1.0,8.0,DEFAULTS['pivot_max'],0.1)
        scan_btn=st.button('🚀 SCAN NIFTY 500',type='primary',width='stretch')
    st.markdown('''### Exact V8 setup being hunted
**Strong trend → progressive range contraction → quiet volume → higher lows → repeated resistance tests → improving relative strength → price near resistance → breakout still ahead.**

V8 deliberately rewards *readiness* rather than simply giving points for a good-looking trend. Results are **watchlist candidates, not automatic buy signals**.''')
    cfg={'range5':r5,'range10':r10,'range20':r20,'atr_ratio':atrmax,'vol_ratio':volmax,'pivot_min':pmin,'pivot_max':pmax,'min_score':min_score}
    if scan_btn:
        try: symbols=get_nifty500_symbols()
        except Exception as e: st.error(f'Could not load NIFTY 500 list: {e}'); return
        st.session_state.symbols = list(symbols)
        st.write(f'Universe: **{len(symbols)} stocks**')
        with st.spinner('Downloading current daily data in small batches...'):
            dlp=st.progress(0, text='Downloading current market data…')
            frames=download_batches_resumable(tuple(symbols),period_days=520,batch_size=50,progress=dlp,pause_seconds=1.0)
            dlp.empty()
        nifty_frames=download_one_batch(('^NSEI',), (datetime.now()-timedelta(days=520)).strftime('%Y-%m-%d'), (datetime.now()+timedelta(days=1)).strftime('%Y-%m-%d'))
        nifty=nifty_frames.get('^NSEI', pd.DataFrame()).get('close', pd.Series(dtype=float))
        out,diag=scan(symbols,frames,nifty,cfg)
        st.session_state.scan_out = out
        st.session_state.scan_diag = diag
        st.session_state.scan_done = True

    if not st.session_state.scan_done:
        st.info('Press **SCAN NIFTY 500** to run the scanner. It uses public Yahoo Finance market data; no broker API key is needed.')
        return

    out=st.session_state.scan_out
    diag=st.session_state.scan_diag
    symbols=st.session_state.symbols
    st.write(f'Universe: **{len(symbols)} stocks**')
    if out.empty: st.warning('No strict V8 setups found today. This is intentional.')
    else:
        st.success(f'Found **{len(out)}** strict V8 pre-breakout candidate(s).')
        st.dataframe(out,width='stretch',hide_index=True)
        st.download_button('⬇️ Download A-grade CSV',out.to_csv(index=False).encode(),'v8_prebreakout_results.csv','text/csv')
    st.markdown('### 🔎 Near-Miss Diagnostic')
    st.caption('Not buy signals. These are the strongest almost-qualified structures and the readiness feature that is missing.')
    if diag.empty: st.info('No strong near-misses.')
    else:
        st.dataframe(diag,width='stretch',hide_index=True)
        st.download_button('⬇️ Download diagnostic CSV',diag.to_csv(index=False).encode(),'v8_near_miss.csv','text/csv')
    st.markdown('### 📈 V8.1 Long Walk-Forward Trade Validation')
    st.caption('Uses only information available on each historical setup date. Breakout = first close above setup pivot. Entry = next-day open. Exit = next-day open after a close below 220 SMA or -15% price risk, with a time cap.')
    with st.expander('Run V8 historical trade validation'):
        setup_days=st.slider('Historical setup days',100,400,200,50)
        bw=st.slider('Breakout window (days)',5,20,10)
        td=st.slider('Maximum observation / trade days',15,40,30)
        if st.button('🧪 RUN V8 TRADE VALIDATION',width='stretch'):
            st.info('V8.1 validation uses smaller independently cached batches with a pause between Yahoo requests. Completed batches are reused on reruns. Keep this tab open while it runs.')
            dlp=st.progress(0, text='Downloading historical data…')
            hist_frames=download_batches_resumable(tuple(symbols),period_days=1100,batch_size=50,progress=dlp,pause_seconds=1.5)
            dlp.empty()
            hist_nifty_frames=download_one_batch(('^NSEI',), (datetime.now()-timedelta(days=1100)).strftime('%Y-%m-%d'), (datetime.now()+timedelta(days=1)).strftime('%Y-%m-%d'))
            hist_nifty=hist_nifty_frames.get('^NSEI', pd.DataFrame()).get('close', pd.Series(dtype=float))
            progress=st.progress(0, text='Preparing historical validation…')
            with st.spinner('Testing historical V8 setups using only setup-day information...'):
                bt=backtest_fast(hist_frames,hist_nifty,cfg,setup_days,bw,td,progress=progress)
            progress.empty()
            if bt.empty: st.warning('No historical V7 setups were found in this sample.')
            else:
                br=bt[bt.Breakout=='YES']; trades=bt.dropna(subset=['Trade Return %']);
                st.success(f'Validation completed — **{len(bt)}** historical setup event(s) tested.')
                a,b,c,d=st.columns(4)
                a.metric('Setups',len(bt)); b.metric('Breakout rate',f'{len(br)/len(bt)*100:.1f}%'); c.metric('Trade win rate',f'{(trades["Trade Return %"]>0).mean()*100:.1f}%'); d.metric('Avg trade',f'{trades["Trade Return %"].mean():.2f}%')
                if not trades.empty:
                    st.write(f'**Average max gain:** {trades["Max Gain %"].mean():.2f}%  •  **Average max drawdown:** {trades["Max Drawdown %"].mean():.2f}%')
                    st.dataframe(bt.sort_values('Setup Date',ascending=False).head(100),width='stretch',hide_index=True)
                    st.download_button('⬇️ Download V8 validation CSV',bt.to_csv(index=False).encode(),'v8_validation.csv','text/csv')
    st.markdown('### ⚠️ How to use the result')
    st.warning('A scanner result is a watchlist candidate, not an automatic buy. Wait for a daily closing breakout above the Pivot and confirm volume/liquidity, entry, stop and position-size rules before trading.')

if __name__=='__main__': main()
