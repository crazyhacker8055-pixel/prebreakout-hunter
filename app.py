import streamlit as st
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime, timedelta

st.set_page_config(page_title='Pre-Breakout Hunter V5', page_icon='🎯', layout='wide')

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
        if col is None: raise RuntimeError('GitHub CSV has no Symbol column.')
        syms = list(dict.fromkeys(t[col].astype(str).str.strip().tolist()))
        syms = [s for s in syms if s and s.lower() != 'nan' and s.upper() != 'SYMBOL']
        if len(syms) >= 450: return syms
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
        if col is None: raise RuntimeError('NSE CSV has no Symbol column.')
        syms = list(dict.fromkeys(t[col].astype(str).str.strip().tolist()))
        if len(syms) >= 450: return syms
        raise RuntimeError(f'Only {len(syms)} symbols returned.')
    except Exception as e:
        raise RuntimeError(f'NIFTY 500 list failed. GitHub: {github_msg}; NSE: {e}')

@st.cache_data(ttl=900, show_spinner=False)
def download_market_data(symbols):
    tickers = [f'{s}.NS' for s in symbols] + ['^NSEI']
    end = datetime.now(); start = end - timedelta(days=650)
    return yf.download(tickers=tickers, start=start.strftime('%Y-%m-%d'),
                       end=(end + timedelta(days=1)).strftime('%Y-%m-%d'), interval='1d',
                       auto_adjust=False, group_by='column', threads=True, progress=False)

def col(data, field, ticker):
    try:
        if isinstance(data.columns, pd.MultiIndex):
            if (field, ticker) in data.columns: return data[(field, ticker)].dropna()
            if (ticker, field) in data.columns: return data[(ticker, field)].dropna()
        return data[field][ticker].dropna()
    except Exception:
        return pd.Series(dtype=float)

def ohlcv_from_download(data, ticker):
    return pd.DataFrame({k.lower(): col(data, k, ticker) for k in ['Open','High','Low','Close','Volume']}).dropna()

def prepare_features(d, nifty):
    d = d.sort_index(); c,h,l,o,v = d['close'],d['high'],d['low'],d['open'],d['volume']
    sma50,sma150,sma200 = c.rolling(50).mean(),c.rolling(150).mean(),c.rolling(200).mean()
    ema20,ema220 = c.ewm(span=20,adjust=False).mean(),c.ewm(span=220,adjust=False).mean()
    tr = pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1)
    atr5,atr20 = tr.rolling(5).mean(),tr.rolling(20).mean()
    r5=(h.rolling(5).max()-l.rolling(5).min())/l.rolling(5).min()*100
    r10=(h.rolling(10).max()-l.rolling(10).min())/l.rolling(10).min()*100
    r20=(h.rolling(20).max()-l.rolling(20).min())/l.rolling(20).min()*100
    vol5,vol20=v.rolling(5).mean(),v.rolling(20).mean()
    pivot=h.rolling(20).max().shift(1); distance=(pivot-c)/pivot*100
    recent=(c>=pivot).rolling(3).max().fillna(False).astype(bool)
    last10=l.rolling(10).min(); prev10=last10.shift(10); higher_low=last10>=prev10*0.985
    down=v.where(c<o).rolling(20).mean(); up=v.where(c>=o).rolling(20).mean(); distribution=(down>up*1.35).fillna(False)
    nn=nifty.reindex(d.index).ffill(); rs=(c/c.shift(20)-1 - (nn/nn.shift(20)-1))*100
    trend=(c>sma50)&(sma50>sma150)&(sma150>sma200)&(sma200>sma200.shift(20))&(c>ema20)
    base=(r5<=DEFAULTS['range5']*1.25)&(r10<=DEFAULTS['range10']*1.20)&(r20<=DEFAULTS['range20']*1.20)
    pre=(c<pivot)&(distance>=0.20)&(distance<=7.0)
    structural=trend&base&pre&(~recent)&higher_low&(~distribution)
    return locals()

def score_at(f,i,cfg):
    keys=['c','sma50','sma150','sma200','ema20','atr5','atr20','r5','r10','r20','vol5','vol20','pivot','distance','rs']
    if any(not np.isfinite(f[k].iloc[i]) for k in keys): return None
    c=f['c'].iloc[i]; r5,r10,r20=f['r5'].iloc[i],f['r10'].iloc[i],f['r20'].iloc[i]
    ar=f['atr5'].iloc[i]/f['atr20'].iloc[i] if f['atr20'].iloc[i] else np.inf
    vr=f['vol5'].iloc[i]/f['vol20'].iloc[i] if f['vol20'].iloc[i] else np.inf
    dist,rs=f['distance'].iloc[i],f['rs'].iloc[i]
    score=(10 if c>f['sma50'].iloc[i] else 0)+(8 if f['sma50'].iloc[i]>f['sma150'].iloc[i] else 0)+(8 if f['sma150'].iloc[i]>f['sma200'].iloc[i] else 0)+(5 if f['sma200'].iloc[i]>f['sma200'].iloc[i-20] else 0)+(4 if c>f['ema20'].iloc[i] else 0)
    score += 5 if r5<=4 else (3 if r5<=cfg['range5'] else 0)
    score += 5 if r10<=7 else (3 if r10<=cfg['range10'] else 0)
    score += 5 if r20<=12 else (3 if r20<=cfg['range20'] else 0)
    score += 10 if r5<r10<r20 else (5 if r5<r10 or r10<r20 else 0)
    score += 6 if ar<=0.65 else (4 if ar<=cfg['atr_ratio'] else (2 if ar<=0.85 else 0))
    score += 6 if vr<=0.60 else (4 if vr<=cfg['vol_ratio'] else (2 if vr<=0.85 else 0))
    score += 4 if bool(f['higher_low'].iloc[i]) else 0
    score += 4 if not bool(f['distribution'].iloc[i]) else 0
    score += 10 if 0.5<=dist<=2.5 else (7 if cfg['pivot_min']<=dist<=cfg['pivot_max'] else (4 if 0.2<=dist<=7 else 0))
    score += 5 if rs>=6 else (3 if rs>=3 else (1 if rs>=0 else 0))
    score += 5 if not bool(f['recent'].iloc[i]) else 0
    score=min(100,int(score)); strict=bool(f['structural'].iloc[i]) and score>=cfg['min_score']
    return {'date':f['d'].index[i],'close':float(c),'pivot':float(f['pivot'].iloc[i]),'score':score,'distance':float(dist),'rs':float(rs),'strict':strict}

def evaluate(df,nifty,cfg):
    if len(df)<MIN_BARS: return None
    f=prepare_features(df,nifty); x=score_at(f,len(df)-1,cfg)
    if x is None:return None
    reasons=[]
    if x['r5'] if False else False: pass
    r5,r10,r20=f['r5'].iloc[-1],f['r10'].iloc[-1],f['r20'].iloc[-1]
    if not(r5<r10<r20): reasons.append('Range contraction incomplete')
    ar=f['atr5'].iloc[-1]/f['atr20'].iloc[-1]; vr=f['vol5'].iloc[-1]/f['vol20'].iloc[-1]
    if ar>cfg['atr_ratio']: reasons.append('ATR contraction weak')
    if vr>cfg['vol_ratio']: reasons.append('Volume dry-up weak')
    if x['distance']>7: reasons.append('Too far below pivot')
    elif x['distance']<0.2: reasons.append('Too close to pivot')
    elif not(cfg['pivot_min']<=x['distance']<=cfg['pivot_max']): reasons.append('Pivot distance not ideal')
    if x['rs']<0: reasons.append('Relative strength below NIFTY')
    if x['score']<cfg['min_score']: reasons.append(f"Score below {cfg['min_score']}")
    if not bool(f['trend'].iloc[-1]): reasons.insert(0,'Trend not strong enough')
    if not bool(f['base'].iloc[-1]): reasons.insert(0,'Base ranges too wide')
    if bool(f['recent'].iloc[-1]): reasons.append('Recent breakout detected')
    if not bool(f['higher_low'].iloc[-1]): reasons.append('Higher-low structure weak')
    if bool(f['distribution'].iloc[-1]): reasons.append('Distribution pressure detected')
    return {'Stock':'','Close':round(x['close'],2),'Score':x['score'],'5D Range %':round(r5,2),'10D Range %':round(r10,2),'20D Range %':round(r20,2),'ATR5/ATR20':round(ar,2),'Vol5/Vol20':round(vr,2),'Pivot':round(x['pivot'],2),'To Pivot %':round(x['distance'],2),'RS vs Nifty %':round(x['rs'],2),'Status':'🔥 PRE-BREAKOUT' if x['strict'] else '👀 NEAR-MISS','Why it missed':'PASS — structure + score' if x['strict'] else ' • '.join(reasons[:4]),'_strict':x['strict']}

def trade_outcome(d, setup_i, setup, breakout_window, hold_window):
    """Breakout is first close above setup-day pivot. Entry is next day's open.
    Exit follows user's intended rule: next-day-open exit after a close below 220 EMA
    OR price reaches -15% from entry. A time cap is used only to finish the test."""
    if setup_i+2>=len(d): return None
    end=min(len(d)-1,setup_i+breakout_window); bi=None
    for j in range(setup_i+1,end+1):
        if d['close'].iloc[j]>setup['pivot']: bi=j; break
    if bi is None or bi+1>=len(d):
        return {'breakout':False,'days_to_breakout':np.nan,'entry':np.nan,'exit':np.nan,'return_pct':np.nan,'max_gain_pct':np.nan,'max_drawdown_pct':np.nan,'exit_reason':'NO BREAKOUT','breakout_volume_ratio':np.nan}
    ei=bi+1; entry=float(d['open'].iloc[ei]); stop=entry*0.85
    if not np.isfinite(entry) or entry<=0:return None
    ema220=d['close'].ewm(span=220,adjust=False).mean()
    end_hold=min(len(d)-2,ei+hold_window); trigger_i=None; reason='TIME CAP'
    for k in range(ei,end_hold+1):
        stop_hit=float(d['low'].iloc[k])<=stop
        ema_hit=float(d['close'].iloc[k])<float(ema220.iloc[k])
        if stop_hit or ema_hit:
            trigger_i=k; reason='-15% STOP' if stop_hit else 'CLOSE < 220 EMA'
            if stop_hit and ema_hit: reason='-15% STOP + CLOSE < 220 EMA'
            break
    if trigger_i is not None and trigger_i+1<len(d):
        exit_i=trigger_i+1; exit_price=float(d['open'].iloc[exit_i]); exit_type='NEXT OPEN'
    else:
        exit_i=end_hold; exit_price=float(d['close'].iloc[exit_i]); exit_type='TIME CAP CLOSE'
    ret=(exit_price/entry-1)*100
    future=d.iloc[ei:end_hold+1]
    max_gain=(future['high'].max()/entry-1)*100; max_dd=(future['low'].min()/entry-1)*100
    vol20=d['volume'].iloc[max(0,bi-19):bi+1].mean(); brvol=float(d['volume'].iloc[bi]/vol20) if vol20 else np.nan
    return {'breakout':True,'days_to_breakout':bi-setup_i,'entry':entry,'exit':exit_price,'return_pct':ret,'max_gain_pct':max_gain,'max_drawdown_pct':max_dd,'exit_reason':reason+' / '+exit_type,'breakout_volume_ratio':brvol}

def run_validation(symbols,data,nifty,cfg,lookback,breakout_window,hold_window,progress_callback=None):
    rows=[]; total=len(symbols)
    for si,sym in enumerate(symbols):
        try:
            d=ohlcv_from_download(data,f'{sym}.NS')
            if len(d)<MIN_BARS+breakout_window+2: continue
            f=prepare_features(d,nifty.reindex(d.index).ffill())
            last=len(d)-breakout_window-2; first=max(MIN_BARS-1,last-lookback+1)
            # Avoid stacking identical signals: after a strict setup, skip forward until its trade is resolved.
            next_allowed=first
            for i in range(first,last+1):
                if i<next_allowed: continue
                s=score_at(f,i,cfg)
                if s is None or not s['strict']: continue
                out=trade_outcome(d,i,s,breakout_window,hold_window)
                if out is None: continue
                rows.append({'Stock':sym,'Setup Date':s['date'].strftime('%Y-%m-%d'),'Score':s['score'],'To Pivot %':round(s['distance'],2),'RS vs Nifty %':round(s['rs'],2),**out})
                # Don't count another signal while this test trade is inside its observation window.
                next_allowed=i+breakout_window+hold_window+1
        except Exception: pass
        if progress_callback: progress_callback((si+1)/total)
    return pd.DataFrame(rows)

def main():
    st.title('🎯 Pre-Breakout Hunter V5')
    st.caption('Selective NSE scanner — structure first, quality second, then realistic next-day-open trade validation. No broker API required.')
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
**Structure first:** strong trend + controlled base + no recent breakout + higher lows. **Then rank quality:** range contraction + ATR contraction + volume dry-up + pivot proximity + relative strength.

**V5 adds realistic validation:** breakout close → next-day-open entry → exit after close below 220 EMA or -15% risk trigger, with next-day-open exit. The backtest uses a time cap only so historical tests can finish.''')
    cfg={'range5':max_range5,'range10':max_range10,'range20':max_range20,'atr_ratio':max_atr,'vol_ratio':max_vol,'pivot_min':pivot_min,'pivot_max':pivot_max,'min_score':min_score}
    defaults={'scan_done':False,'scan_cfg':None,'scan_symbols':None,'scan_data':None,'scan_nifty':None,'scan_all_out':None,'validation_result':None,'validation_error':None}
    for k,v in defaults.items():
        if k not in st.session_state: st.session_state[k]=v
    if scan:
        st.session_state.scan_done=False; st.session_state.validation_result=None; st.session_state.validation_error=None
        try: symbols=get_nifty500_symbols()
        except Exception as e: st.error(f'Could not load NIFTY 500 list: {e}'); return
        with st.spinner('Downloading daily market data and checking strict setups...'): data=download_market_data(tuple(symbols))
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
    if strict.empty: st.warning('No A-grade pre-breakout setups found today. That is intentional — the scanner is designed to be selective.')
    else:
        st.success(f'Found **{len(strict)}** strict pre-breakout candidate(s).')
        st.dataframe(strict[['Stock','Score','Close','5D Range %','10D Range %','20D Range %','ATR5/ATR20','Vol5/Vol20','Pivot','To Pivot %','RS vs Nifty %','Status']],use_container_width=True,hide_index=True)
        st.download_button('⬇️ Download A-grade results CSV',strict.drop(columns=['_strict']).to_csv(index=False).encode(), 'prebreakout_results.csv','text/csv')
    st.markdown('### 🔎 Near-Miss Diagnostic'); st.caption('Not buy signals. These are the strongest almost-qualified structures.')
    if near.empty: st.info('No near-miss stocks were available.')
    else:
        st.dataframe(near[['Stock','Score','Close','5D Range %','10D Range %','20D Range %','ATR5/ATR20','Vol5/Vol20','To Pivot %','RS vs Nifty %','Why it missed']],use_container_width=True,hide_index=True)
        st.download_button('⬇️ Download diagnostic CSV',near.drop(columns=['_strict']).to_csv(index=False).encode(),'prebreakout_diagnostic.csv','text/csv')
    st.markdown('### 📈 V5 Realistic Trade Validation')
    st.caption('Walk-forward test. Setup uses only data available that day. Breakout = first close above the setup-day pivot. Entry = next day open. Exit = next day open after a close below 220 EMA OR a -15% price-risk trigger. A time cap is used only to prevent open-ended historical trades.')
    with st.expander('Run V5 historical trade validation',expanded=False):
        lookback=st.slider('Historical setup days',30,120,90,10,key='v5_lookback')
        bw=st.slider('Breakout window (days)',5,20,10,1,key='v5_breakout')
        hw=st.slider('Maximum observation / trade days',10,60,30,5,key='v5_hold')
        run=st.button('🧪 RUN V5 TRADE VALIDATION',use_container_width=True)
        if run:
            st.session_state.validation_result=None; st.session_state.validation_error=None; p=st.progress(0)
            try:
                with st.spinner('Testing realistic historical trades across the NIFTY 500...'):
                    res=run_validation(tuple(symbols),data,nifty,cfg,lookback,bw,hw,lambda x:p.progress(min(1,max(0,x))))
                st.session_state.validation_result=res
            except Exception as e: st.session_state.validation_error=str(e)
            p.empty()
        if st.session_state.validation_error: st.error('V5 validation failed: '+st.session_state.validation_error)
        bt=st.session_state.validation_result
        if bt is not None:
            st.success(f'V5 validation completed — {len(bt)} historical setup event(s) tested.')
            if bt.empty: st.warning('No historical setups passed the current structural gates in this window.')
            else:
                total=len(bt); br=bt['breakout'].sum(); br_rate=br/total*100
                trades=bt[bt['breakout']].copy(); wins=(trades['return_pct']>0).mean()*100 if len(trades) else np.nan
                avg_ret=trades['return_pct'].mean() if len(trades) else np.nan; med_ret=trades['return_pct'].median() if len(trades) else np.nan
                avg_gain=trades['max_gain_pct'].mean() if len(trades) else np.nan; avg_dd=trades['max_drawdown_pct'].mean() if len(trades) else np.nan
                strong=(trades['breakout_volume_ratio']>=1.5).mean()*100 if len(trades) else np.nan
                c1,c2,c3,c4,c5=st.columns(5); c1.metric('Setups',total); c2.metric('Breakout rate',f'{br_rate:.1f}%'); c3.metric('Trade win rate',f'{wins:.1f}%'); c4.metric('Avg trade return',f'{avg_ret:.2f}%'); c5.metric('Median trade return',f'{med_ret:.2f}%')
                st.write(f'Average max gain: **{avg_gain:.2f}%** · Average max drawdown: **{avg_dd:.2f}%** · Breakouts with ≥1.5× volume: **{strong:.1f}%**')
                b=bt.copy(); b['Score Band']=pd.cut(b['Score'],[-1,79,84,89,94,100],labels=['<80','80–84','85–89','90–94','95–100'])
                band=b.groupby('Score Band',observed=False).agg(Setups=('Stock','size'),Breakout_Rate=('breakout','mean'),Win_Rate=('return_pct',lambda x:(x>0).mean()),Median_Trade_Return=('return_pct','median'),Median_Max_Gain=('max_gain_pct','median'),Median_Max_Drawdown=('max_drawdown_pct','median')).reset_index()
                for k in ['Breakout_Rate','Win_Rate']: band[k]=(band[k]*100).round(1)
                for k in ['Median_Trade_Return','Median_Max_Gain','Median_Max_Drawdown']: band[k]=band[k].round(2)
                st.markdown('#### 🏆 Performance by score band'); st.dataframe(band,use_container_width=True,hide_index=True)
                detail=bt.sort_values(['Score','Setup Date'],ascending=[False,False]).copy(); detail['Breakout']=detail['breakout'].map({True:'YES',False:'NO'})
                detail['Trade Return %']=detail['return_pct'].round(2); detail['Max Gain %']=detail['max_gain_pct'].round(2); detail['Max Drawdown %']=detail['max_drawdown_pct'].round(2); detail['Breakout Vol ×']=detail['breakout_volume_ratio'].round(2)
                st.markdown('#### Historical trade events'); st.dataframe(detail[['Stock','Setup Date','Score','To Pivot %','Breakout','days_to_breakout','entry','exit','Trade Return %','Max Gain %','Max Drawdown %','exit_reason','Breakout Vol ×']],use_container_width=True,hide_index=True)
                st.download_button('⬇️ Download V5 trade validation CSV',bt.to_csv(index=False).encode(),'prebreakout_v5_trade_validation.csv','text/csv')
    st.markdown('### How to trade the result'); st.warning('A scanner result is a watchlist candidate, not an automatic buy. Wait for the daily closing breakout above the pivot and confirm volume/liquidity and your risk rules before entering.')

if __name__=='__main__': main()
