import streamlit as st
import pandas as pd
import numpy as np
import yfinance as yf
import requests
from io import StringIO
from datetime import datetime, timedelta
import threading
import time

st.set_page_config(page_title='Pre-Breakout Hunter V9.3.1', page_icon='🎯', layout='wide')

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
        'https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv',
        'https://raw.githubusercontent.com/pkjmesra/PKScreener/main/results/Indices/ind_nifty500list.csv',
        'https://raw.githubusercontent.com/sswapnil2/tradingview-mcp-india/main/src/tradingview_mcp/coinlist/nse.txt',
    ]
    headers = {'User-Agent': 'Mozilla/5.0 Pre-Breakout-Hunter/9.2.1'}
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
            syms = [s for s in syms if not s.upper().startswith('DUMMY')]
            if len(syms) >= 400:
                return syms
            last_error = f'Only {len(syms)} symbols returned from {url}'
        except Exception as e:
            last_error = e
    raise RuntimeError(f'Could not load NIFTY 500 list from public mirrors: {last_error}')

@st.cache_data(ttl=21600, show_spinner=False)
def download_one_batch(batch, start_date, end_date):
    """Small, fail-safe Yahoo download. Never spends a long time retrying a rate-limited request."""
    batch = tuple(batch)
    yahoo_batch = tuple(t if str(t).startswith('^') or str(t).endswith('.NS') else f'{t}.NS' for t in batch)
    try:
        d = yf.download(list(yahoo_batch), start=start_date, end=end_date, interval='1d',
                        auto_adjust=False, group_by='column', threads=False, progress=False, timeout=15)
        frames = {}
        for yt in yahoo_batch:
            df = ohlcv_from_download(d, yt)
            if not df.empty:
                frames[yt] = df
        return frames
    except Exception:
        return {}

@st.cache_data(ttl=86400, show_spinner=False)
def get_nifty500_sector_map():
    """Load a best-effort sector/industry classification from the same public NIFTY 500 mirror.
    The dashboard falls back to 'Other / Unclassified' when a sector field is unavailable.
    """
    urls = [
        'https://raw.githubusercontent.com/pkjmesra/PKScreener/main/results/Indices/ind_nifty500list.csv',
        'https://raw.githubusercontent.com/sswapnil2/tradingview-mcp-india/main/src/tradingview_mcp/coinlist/nse.txt',
    ]
    headers = {'User-Agent': 'Mozilla/5.0 Pre-Breakout-Hunter/8.7'}
    for url in urls:
        try:
            r=requests.get(url,headers=headers,timeout=20); r.raise_for_status()
            if 'ind_nifty500list.csv' not in url:
                return {}
            t=pd.read_csv(StringIO(r.text))
            cols={str(c).strip().lower():c for c in t.columns}
            sym_col=next((cols[k] for k in ('symbol','ticker') if k in cols),None)
            sector_col=next((cols[k] for k in ('sector','industry','industry name','industry_name') if k in cols),None)
            if sym_col is None or sector_col is None:
                return {}
            out={}
            for _,row in t.iterrows():
                sym=str(row[sym_col]).strip()
                sec=str(row[sector_col]).strip()
                if sym and sym.lower()!='nan':
                    out[sym]=sec if sec and sec.lower()!='nan' else 'Other / Unclassified'
            return out
        except Exception:
            continue
    return {}



@st.cache_data(ttl=900, show_spinner=False)
def get_nse_bulk_block_deals(days=3):
    """Fetch recent NSE bulk/block deals using the public NSE web endpoints.
    Returns a combined dataframe; failures are non-fatal so the scanner keeps working.
    """
    end = datetime.now(); start = end - timedelta(days=days)
    frm = start.strftime('%d-%m-%Y'); to = end.strftime('%d-%m-%Y')
    headers = {
        'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36',
        'Accept':'application/json, text/plain, */*', 'Accept-Language':'en-US,en;q=0.9',
        'Referer':'https://www.nseindia.com/'
    }
    rows=[]
    try:
        sess=requests.Session(); sess.headers.update(headers)
        sess.get('https://www.nseindia.com/', timeout=10)
        for deal_type, endpoint in [('Bulk','https://www.nseindia.com/api/historical/bulk-deals'),('Block','https://www.nseindia.com/api/historical/block-deals')]:
            try:
                r=sess.get(endpoint, params={'from':frm,'to':to}, timeout=12)
                r.raise_for_status(); payload=r.json()
                data=payload.get('data', payload) if isinstance(payload,dict) else payload
                if not isinstance(data,list): continue
                for x in data:
                    if not isinstance(x,dict): continue
                    rows.append({
                        'Deal Type':deal_type,
                        'Date':x.get('date',x.get('dealDate',x.get('DATE',''))),
                        'Stock':x.get('symbol',x.get('SYMBOL',x.get('Symbol',''))),
                        'Client':x.get('clientName',x.get('client',x.get('CLIENT_NAME',''))),
                        'Buy/Sell':x.get('buySell',x.get('buy_sell',x.get('BUY_SELL',''))),
                        'Quantity':x.get('quantity',x.get('qty',x.get('QUANTITY',''))),
                        'Price/VWAP':x.get('watp',x.get('price',x.get('WATP',''))),
                        'Remarks':x.get('remarks',x.get('REMARKS',''))
                    })
            except Exception:
                continue
    except Exception:
        return pd.DataFrame()
    df=pd.DataFrame(rows)
    if df.empty: return df
    df['Stock']=df['Stock'].astype(str).str.strip().str.upper()
    return df.drop_duplicates().reset_index(drop=True)


def _news_importance(subject, details=''):
    text=f'{subject} {details}'.lower()
    rules=[
        (5,['order win','large order','major order','contract win','work order','award of contract']),
        (5,['acquisition','merger','takeover','strategic investment','joint venture']),
        (5,['fund raise','fundraising','preferential','qip','rights issue','qualified institutional']),
        (4,['promoter','stake sale','stake acquisition','open offer','share purchase']),
        (4,['buyback','credit rating','downgrade','upgrade','default','insolvency']),
        (3,['financial results','results','revenue','profit','ebitda','guidance']),
        (3,['approval','regulatory','license','environment clearance','drug approval']),
        (2,['dividend','bonus','split','board meeting','capacity expansion','capex','expansion']),
        (2,['resignation','appointment','auditor','investigation','show cause'])
    ]
    score=0; category='General'
    for pts,keys in rules:
        if any(k in text for k in keys):
            score=max(score,pts)
            if pts>=5: category='Major Corporate Event'
            elif pts==4: category='Ownership / Risk'
            elif pts==3: category='Results / Business'
            elif pts==2: category='Corporate Action / Update'
    return score,category

@st.cache_data(ttl=900, show_spinner=False)
def get_nse_big_news(symbols, days=1):
    """Fetch recent NSE corporate announcements and rank likely market-moving items.
    Uses official public NSE announcement endpoints; no API key is required.
    """
    end=datetime.now(); start=end-timedelta(days=days)
    frm=start.strftime('%d-%m-%Y'); to=end.strftime('%d-%m-%Y')
    headers={
        'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36',
        'Accept':'application/json, text/plain, */*','Accept-Language':'en-US,en;q=0.9',
        'Referer':'https://www.nseindia.com/companies-listing/corporate-filings-announcements'
    }
    try:
        sess=requests.Session(); sess.headers.update(headers)
        sess.get('https://www.nseindia.com/',timeout=10)
        url='https://www.nseindia.com/api/corporate-announcements'
        r=sess.get(url,params={'index':'equities','from_date':frm,'to_date':to,'csv':'true'},timeout=15)
        r.raise_for_status()
        content=r.content
        df=None
        try:
            df=pd.read_csv(StringIO(content.decode('utf-8-sig')))
        except Exception:
            payload=r.json(); data=payload.get('data',payload) if isinstance(payload,dict) else payload
            df=pd.DataFrame(data)
        if df is None or df.empty: return pd.DataFrame()
        rename={
            'SYMBOL':'Stock','symbol':'Stock','COMPANY NAME':'Company','sm_name':'Company',
            'SUBJECT':'Subject','desc':'Subject','DETAILS':'Details','attchmntText':'Details',
            'BROADCAST DATE/TIME':'Time','an_dt':'Time','smIndustry':'Sector'
        }
        df=df.rename(columns={c:rename[c] for c in df.columns if c in rename})
        if 'Stock' not in df: return pd.DataFrame()
        df['Stock']=df['Stock'].astype(str).str.strip().str.upper()
        universe=set(str(x).upper() for x in symbols)
        df=df[df['Stock'].isin(universe)].copy()
        if df.empty: return df
        if 'Subject' not in df: df['Subject']=''
        if 'Details' not in df: df['Details']=''
        scores=df.apply(lambda r:_news_importance(str(r.get('Subject','')),str(r.get('Details',''))),axis=1)
        df['Importance']=scores.map(lambda x:x[0]); df['Category']=scores.map(lambda x:x[1])
        df=df[df['Importance']>0].copy()
        if df.empty: return df
        df['Headline']=df['Subject'].astype(str).str.replace(r'\s+',' ',regex=True).str.slice(0,180)
        keep=[c for c in ['Stock','Company','Sector','Headline','Category','Importance','Time','Details'] if c in df.columns]
        return df[keep].sort_values(['Importance','Time'],ascending=[False,False]).head(40).reset_index(drop=True)
    except Exception:
        return pd.DataFrame()

def market_dashboard(frames, symbols, sector_map):
    """Build a decision-oriented market dashboard from the same daily OHLCV data.

    Adds breadth, liquidity-aware gainers/volume leaders, sector strength, and a
    pre-breakout alignment list. All values are derived from the scanner's current
    daily OHLCV dataset; no broker API is required.
    """
    rows=[]
    for sym in symbols:
        df=frames.get(f'{sym}.NS',pd.DataFrame())
        if len(df)<55: continue
        try:
            df=df.sort_index()
            c=df.close; v=df.volume
            close=float(c.iloc[-1]); prev=float(c.iloc[-2])
            chg=(close/prev-1)*100 if prev else np.nan
            vol=float(v.iloc[-1]); v20=float(v.iloc[-21:-1].mean())
            vs=(vol/v20-1)*100 if v20 else np.nan
            avg_vol_value=float((c.iloc[-21:-1]*v.iloc[-21:-1]).mean())
            sma20=float(c.rolling(20).mean().iloc[-1]); sma50=float(c.rolling(50).mean().iloc[-1])
            high20=float(c.iloc[-21:-1].max()); dist_high=(high20-close)/high20*100 if high20 else np.nan
            rows.append({
                'Stock':sym,'Close':close,'Change %':chg,'Volume':vol,'Volume Surge %':vs,
                'Avg Traded Value Cr':avg_vol_value/1e7,'Above 20DMA':close>sma20,
                'Above 50DMA':close>sma50,'20D High Distance %':dist_high,
                'Sector':sector_map.get(sym,'Other / Unclassified')
            })
        except Exception:
            continue
    mkt=pd.DataFrame(rows)
    if mkt.empty: return tuple(pd.DataFrame() for _ in range(8))

    # Liquidity-aware lists: avoid letting tiny-volume stocks dominate the dashboard.
    liquid=mkt[mkt['Avg Traded Value Cr']>=5].copy()
    if liquid.empty: liquid=mkt.copy()
    gainers=liquid.sort_values(['Change %','Avg Traded Value Cr'],ascending=[False,False]).head(15).copy()
    losers=liquid.sort_values(['Change %','Avg Traded Value Cr'],ascending=[True,False]).head(15).copy()
    vg=liquid[liquid['Volume Surge %'].notna()].sort_values(['Volume Surge %','Avg Traded Value Cr'],ascending=[False,False]).head(15).copy()

    sec=mkt.assign(Advance=mkt['Change %']>0,Decline=mkt['Change %']<0)
    sector=sec.groupby('Sector',dropna=False).agg(
        Stocks=('Stock','count'), Advances=('Advance','sum'), Declines=('Decline','sum'),
        AvgChange=('Change %','mean'), MedianChange=('Change %','median'),
        Above20DMA=('Above 20DMA','sum'), Above50DMA=('Above 50DMA','sum')
    ).reset_index()
    sector['Unchanged']=sector['Stocks']-sector['Advances']-sector['Declines']
    sector['Advance %']=sector['Advances']/sector['Stocks']*100
    sector['Above20DMA %']=sector['Above20DMA']/sector['Stocks']*100
    sector['Above50DMA %']=sector['Above50DMA']/sector['Stocks']*100
    sector['Net Advance']=sector['Advances']-sector['Declines']
    # Strength combines breadth, average price change and trend participation.
    sector['Strength Score']=(
        sector['Advance %']*0.45 +
        sector['Above20DMA %']*0.25 +
        sector['Above50DMA %']*0.15 +
        sector['AvgChange'].clip(-5,5)*10*0.15
    ).round(1)
    sector=sector.sort_values(['Strength Score','Advance %','Net Advance'],ascending=[False,False,False]).reset_index(drop=True)

    # The most useful cross-filter: strict pre-breakout candidates living in healthy sectors.
    aligned=pd.DataFrame()
    if not mkt.empty:
        healthy=sector[(sector['Advance %']>=50) & (sector['AvgChange']>0)].copy()
        if not healthy.empty:
            healthy_secs=set(healthy['Sector'])
            aligned=mkt[mkt['Sector'].isin(healthy_secs)].copy()
            aligned=aligned[(aligned['Change %']>0) & (aligned['Volume Surge %']>=0) & (aligned['20D High Distance %']>=0)].copy()
            aligned['Alignment Score']=(
                aligned['Change %'].clip(-3,8)*3 +
                aligned['Volume Surge %'].clip(-50,300)/30 +
                (100-aligned['20D High Distance %'].clip(0,10))*0.5
            ).round(1)
            aligned=aligned.sort_values('Alignment Score',ascending=False).head(20)

    # Round display fields.
    for d in (gainers,losers,vg,aligned):
        if not d.empty:
            for colname in ('Close','Change %','Volume Surge %','Avg Traded Value Cr','20D High Distance %','Alignment Score'):
                if colname in d.columns: d[colname]=d[colname].round(2)
    sector['AvgChange']=sector['AvgChange'].round(2); sector['MedianChange']=sector['MedianChange'].round(2)
    sector['Advance %']=sector['Advance %'].round(1); sector['Above20DMA %']=sector['Above20DMA %'].round(1); sector['Above50DMA %']=sector['Above50DMA %'].round(1)
    return mkt,gainers,losers,vg,sector,aligned

def download_batches_resumable(symbols, period_days=520, batch_size=100, progress=None):
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

def backtest_full_history(frames,nifty,cfg,breakout_window=10,trade_days=30,progress=None):
    """Full-history walk-forward validation.

    Unlike V8.5, this does NOT sample setup dates. Every valid historical trading
    day in the downloaded window is tested. The setup rules are unchanged from
    the live scanner, and only prices after the setup date are used to evaluate
    breakout/trade outcomes.
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
            usable=feat.index[:-breakout_window-trade_days]
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


def validation_batch_symbols(symbols, batch_no, batch_size=25):
    """Return one small historical batch. Keeping each click small prevents long-lived
    Streamlit websocket requests and makes validation resumable on free hosting."""
    start=(batch_no-1)*batch_size
    return tuple(symbols[start:start+batch_size])


def run_validation_batch(symbols, nifty, cfg, batch_no, batch_size, _unused, bw, td, progress=None):
    batch=validation_batch_symbols(symbols,batch_no,batch_size)
    if not batch:
        return pd.DataFrame(), [], batch
    end=datetime.now(); start=end-timedelta(days=1825)
    start_s=start.strftime('%Y-%m-%d'); end_s=(end+timedelta(days=1)).strftime('%Y-%m-%d')
    frames=download_one_batch(batch,start_s,end_s)
    if progress is not None:
        progress.progress(0.55, text=f'Downloaded {len(frames)}/{len(batch)} stocks; validating…')
    if not frames:
        return pd.DataFrame(), list(batch), batch
    bt=backtest_full_history(frames,nifty,cfg,bw,td,progress=None)
    failed=[s for s in batch if f'{s}.NS' not in frames and s not in frames]
    return bt,failed,batch


def upstox_connection_test():
    """Non-trading, read-only Upstox Analytics Token connectivity test.
    This deliberately tests REST market data first; WebSocket integration comes next.
    """
    token = ""
    try:
        token = str(st.secrets.get("UPSTOX_ACCESS_TOKEN", "")).strip()
    except Exception:
        token = ""

    st.markdown("## 🔌 Upstox Live Data Connection Test")
    st.caption(
        "V9.0 test only: verifies the Upstox Analytics Token and V3 market-data API. "
        "It does not place, modify, or cancel orders."
    )

    if not token:
        st.error("UPSTOX_ACCESS_TOKEN is not available in Streamlit Secrets.")
        st.info('Add: UPSTOX_ACCESS_TOKEN = "your_token" under Streamlit → Settings → Secrets.')
        return

    col1, col2 = st.columns([1, 3])
    test = col1.button("🧪 TEST UPSTOX DATA", width="stretch")
    col2.caption("Test instrument: NHPC (NSE) • API: Upstox Market Quote V3 LTP")

    if not test:
        st.info("Press **TEST UPSTOX DATA** to verify the token and market-data connection.")
        return

    url = "https://api.upstox.com/v3/market-quote/ltp"
    params = {"instrument_key": "NSE_EQ|INE848E01016"}
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }

    try:
        r = requests.get(url, headers=headers, params=params, timeout=15)
        if r.status_code != 200:
            st.error(f"Upstox returned HTTP {r.status_code}.")
            try:
                st.json(r.json())
            except Exception:
                st.code(r.text[:1000])
            return

        payload = r.json()
        data = payload.get("data", {})
        row = next(iter(data.values()), None) if isinstance(data, dict) else None

        if not row:
            st.warning("Upstox responded successfully, but no quote data was returned.")
            st.json(payload)
            return

        ltp = row.get("last_price")
        ltq = row.get("ltq")
        volume = row.get("volume")
        cp = row.get("cp")

        a, b, c, d = st.columns(4)
        a.metric("NHPC LTP", f"₹{ltp:,.2f}" if isinstance(ltp, (int, float)) else str(ltp))
        b.metric("Prev Close", f"₹{cp:,.2f}" if isinstance(cp, (int, float)) else str(cp))
        c.metric("Day Volume", f"{int(volume):,}" if isinstance(volume, (int, float)) else str(volume))
        d.metric("Last Trade Qty", f"{int(ltq):,}" if isinstance(ltq, (int, float)) else str(ltq))

        st.success("✅ UPSTOX CONNECTION SUCCESSFUL — authenticated market data is reaching Streamlit.")
        st.caption(f"Response received at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} server time.")
        st.caption(
            "This is a connection/permission test only. The existing V8.9 scanner still uses its "
            "existing data engine until the WebSocket integration is completed."
        )
    except requests.RequestException as e:
        st.error(f"Could not reach Upstox: {e}")
    except Exception as e:
        st.error(f"Unexpected Upstox test error: {e}")



# V9.3.3: NIFTY 500 -> proven LTPC WebSocket + V3 REST 1-minute OHLC polling.
# This hybrid avoids the FULL-feed WebSocket 403 seen on some Analytics-token sessions while preserving real-time LTPC streaming.
# Read-only market data only. No order API is used.
UPSTOX_INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
UPSTOX_WS_STATE = {
    "streamer": None, "thread": None, "running": False, "connected": False,
    "rest_live": False, "ohlc_symbols": 0, "ohlc_candles": 0,
    "error": "", "opened_at": None, "last_message_at": None,
    "updates": {}, "mapped": {}, "unmapped": [], "recovered": [], "universe_size": 0,
    "candles": {}, "last_day_volume": {},
    "ohlc_thread": None, "ohlc_running": False, "ohlc_error": "", "ohlc_last_poll": None,
    "seed_thread": None, "seed_running": False, "seed_done": False, "seed_ok": 0, "seed_total": 0, "seed_error": "", "radar_watchlist": [], "radar_watchlist_source": "",
}
UPSTOX_WS_LOCK = threading.Lock()

@st.cache_data(ttl=86400, show_spinner=False)
def get_upstox_nse_equity_instruments():
    """Download Upstox's daily NSE instrument master and keep NSE_EQ cash equities."""
    import gzip, json
    r = requests.get(UPSTOX_INSTRUMENTS_URL, headers={"User-Agent": "Mozilla/5.0 Pre-Breakout-Hunter/9.2"}, timeout=30)
    r.raise_for_status()
    raw = gzip.decompress(r.content).decode("utf-8")
    data = json.loads(raw)
    if not isinstance(data, list):
        raise RuntimeError("Unexpected Upstox NSE instrument-file format.")
    rows = []
    for x in data:
        if not isinstance(x, dict):
            continue
        if str(x.get("segment", "")) != "NSE_EQ":
            continue
        symbol = str(x.get("trading_symbol", "")).strip().upper()
        key = str(x.get("instrument_key", "")).strip()
        if symbol and key:
            rows.append({
                "symbol": symbol,
                "instrument_key": key,
                "name": str(x.get("name", "")).strip(),
                "isin": str(x.get("isin", "")).strip(),
                "short_name": str(x.get("short_name", "")).strip(),
            })
    if not rows:
        raise RuntimeError("No NSE_EQ instruments found in Upstox instrument master.")
    return pd.DataFrame(rows).drop_duplicates("symbol").reset_index(drop=True)


@st.cache_data(ttl=21600, show_spinner=False)
def search_upstox_equity_fallback(symbol, token):
    """Repair a valid NSE equity missing from the downloaded Upstox BOD master."""
    try:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        }
        r = requests.get(
            "https://api.upstox.com/v2/instruments/search",
            headers=headers,
            params={
                "query": symbol, "exchanges": "NSE", "segments": "EQ",
                "page_number": 1, "records": 30,
            },
            timeout=10,
        )
        r.raise_for_status()
        data = r.json().get("data", [])
        if not isinstance(data, list):
            return {}
        for x in data:
            if not isinstance(x, dict):
                continue
            if str(x.get("segment", "")) != "NSE_EQ":
                continue
            if str(x.get("trading_symbol", "")).strip().upper() != str(symbol).strip().upper():
                continue
            key = str(x.get("instrument_key", "")).strip()
            if key:
                return {
                    "symbol": str(x.get("trading_symbol", symbol)).strip().upper(),
                    "instrument_key": key,
                    "name": str(x.get("name", "")).strip(),
                    "isin": str(x.get("isin", "")).strip(),
                    "short_name": str(x.get("short_name", "")).strip(),
                }
    except Exception:
        pass
    return {}


def build_upstox_nifty500_mapping(symbols):
    """Map NIFTY 500 symbols to current Upstox NSE_EQ keys with repair fallback."""
    inst = get_upstox_nse_equity_instruments()
    by_symbol = dict(zip(inst["symbol"], inst["instrument_key"]))
    mapped, unmapped, recovered = {}, [], []
    try:
        token = str(st.secrets.get("UPSTOX_ACCESS_TOKEN", "")).strip()
    except Exception:
        token = ""
    for raw in symbols:
        clean = str(raw).strip().upper().replace(".NS", "")
        if not clean or clean.startswith("DUMMY"):
            continue
        key = by_symbol.get(clean)
        if key:
            mapped[clean] = key
            continue
        if token:
            hit = search_upstox_equity_fallback(clean, token)
            if hit.get("instrument_key"):
                mapped[clean] = hit["instrument_key"]
                recovered.append(clean)
                continue
        unmapped.append(clean)
    return mapped, unmapped, inst, recovered


def _as_dict(obj):
    if isinstance(obj, dict):
        return obj
    for method in ("to_dict", "toDict"):
        try:
            fn = getattr(obj, method, None)
            if callable(fn):
                out = fn()
                if isinstance(out, dict):
                    return out
        except Exception:
            pass
    return {}


def _find_ltpc(obj):
    if isinstance(obj, dict):
        if isinstance(obj.get("ltpc"), dict):
            return obj["ltpc"]
        for v in obj.values():
            found = _find_ltpc(v)
            if found:
                return found
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            found = _find_ltpc(v)
            if found:
                return found
    return None


def _find_first_key(obj, wanted):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k) == wanted:
                return v
            found = _find_first_key(v, wanted)
            if found is not None:
                return found
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            found = _find_first_key(v, wanted)
            if found is not None:
                return found
    return None


def _extract_market_ohlc(obj):
    return _find_first_key(obj, "marketOhlc") or _find_first_key(obj, "marketOHLC")


def _extract_efeed(obj):
    return _find_first_key(obj, "eFeedDetails") or _find_first_key(obj, "eFeedDetails")


def _extract_live_updates(message, reverse):
    payload = _as_dict(message)
    feeds = payload.get("feeds", {}) if isinstance(payload, dict) else {}
    if not isinstance(feeds, dict):
        return {}, {}
    out, candles = {}, {}
    for instrument_key, feed in feeds.items():
        name = reverse.get(instrument_key, instrument_key)
        ltpc = _find_ltpc(feed)
        if not isinstance(ltpc, dict):
            continue
        ltp, cp = ltpc.get("ltp"), ltpc.get("cp")
        change_pct = None
        try:
            if float(cp) != 0:
                change_pct = (float(ltp) / float(cp) - 1.0) * 100.0
        except Exception:
            pass
        row = {
            "Symbol": name, "Instrument Key": instrument_key, "LTP": ltp,
            "Prev Close": cp, "Change %": change_pct, "Last Qty": ltpc.get("ltq"),
            "Trade Time": ltpc.get("ltt"), "Received": datetime.now().strftime("%H:%M:%S"),
        }
        ef = _extract_efeed(feed)
        if isinstance(ef, dict):
            row["Day Volume"] = ef.get("vtt") or ef.get("volume")
            row["ATP"] = ef.get("atp")
        mo = _extract_market_ohlc(feed)
        arr = mo.get("ohlc", []) if isinstance(mo, dict) else []
        if isinstance(arr, list):
            for item in arr:
                if not isinstance(item, dict):
                    continue
                interval = item.get("interval")
                if interval == "I1":
                    c = {
                        "Symbol": name, "Instrument Key": instrument_key,
                        "1m Timestamp": item.get("ts"), "1m Open": item.get("open"),
                        "1m High": item.get("high"), "1m Low": item.get("low"),
                        "1m Close": item.get("close"), "1m Volume": item.get("vol"),
                    }
                    candles[name] = c
                    row.update(c)
                elif interval == "I30":
                    row["30m Close"] = item.get("close")
        out[name] = row
    return out, candles


def _update_candle_history(candles):
    """Merge one or more candle rows into per-symbol rolling history."""
    if not candles:
        return
    with UPSTOX_WS_LOCK:
        hist = UPSTOX_WS_STATE.setdefault("candles", {})
        for symbol, rows in candles.items():
            if isinstance(rows, dict):
                rows = [rows]
            if not isinstance(rows, (list, tuple)):
                continue
            series = hist.setdefault(symbol, [])
            for c in rows:
                if not isinstance(c, dict):
                    continue
                ts = c.get("1m Timestamp")
                if ts is None:
                    continue
                clean = dict(c)
                replaced = False
                for i, old in enumerate(series):
                    if str(old.get("1m Timestamp")) == str(ts):
                        series[i] = clean
                        replaced = True
                        break
                if not replaced:
                    series.append(clean)
            series.sort(key=lambda x: str(x.get("1m Timestamp", "")))
            if len(series) > 120:
                del series[:-120]


def _normalise_ohlc_candle(symbol, ikey, candle):
    """Convert an Upstox V3 OHLC candle to our internal 1-minute row."""
    if not isinstance(candle, dict):
        return None
    ts = candle.get("ts") or candle.get("timestamp")
    if ts is None:
        return None
    return {
        "Symbol": symbol,
        "Instrument Key": ikey,
        "1m Timestamp": ts,
        "1m Open": candle.get("open"),
        "1m High": candle.get("high"),
        "1m Low": candle.get("low"),
        "1m Close": candle.get("close"),
        "1m Volume": candle.get("volume", candle.get("vol", 0)),
    }


def _extract_i1_response(payload, reverse):
    """Parse Upstox V3 I1 OHLC safely across dict/list response shapes."""
    rows, latest = {}, {}
    parsed = 0
    shape = []
    if not isinstance(payload, dict):
        return rows, latest, parsed, f"unexpected top-level JSON type: {type(payload).__name__}"

    data = payload.get("data", {})
    shape.append(f"data={type(data).__name__}")

    if isinstance(data, dict):
        entries = list(data.items())
    elif isinstance(data, list):
        entries = [(str(i), value) for i, value in enumerate(data)]
    else:
        return rows, latest, parsed, "; ".join(shape)

    for api_key, raw_item in entries:
        item_list = raw_item if isinstance(raw_item, list) else [raw_item]
        for obj in item_list:
            if not isinstance(obj, dict):
                continue
            shape.append(f"item={type(obj).__name__}")

            raw_key = obj.get("instrument_token") or (api_key if isinstance(api_key, str) else "")
            ikey = str(raw_key).replace(":", "|")
            sym = reverse.get(ikey)
            if not sym and isinstance(api_key, str):
                sym = reverse.get(api_key.replace(":", "|"))
            if not sym:
                # Last-resort match using the NSE_EQ:<SYMBOL> response key.
                candidate = str(api_key).split(":", 1)[-1].strip().upper()
                if candidate in reverse:
                    sym = candidate
            if not sym:
                continue

            candle_list = []
            for field in ("prev_ohlc", "live_ohlc", "ohlc"):
                value = obj.get(field)
                if isinstance(value, dict):
                    candle_list.append(value)
                elif isinstance(value, list):
                    candle_list.extend(v for v in value if isinstance(v, dict))

            # De-duplicate candles by timestamp, preferring live_ohlc when both exist.
            seen = set()
            out = []
            for candle in candle_list:
                row = _normalise_ohlc_candle(sym, ikey, candle)
                if not row:
                    continue
                ts = str(row.get("1m Timestamp"))
                if ts in seen:
                    continue
                seen.add(ts)
                out.append(row)
                parsed += 1

            if out:
                out.sort(key=lambda x: str(x.get("1m Timestamp", "")))
                rows[sym] = out
                latest[sym] = dict(out[-1])

            last_price = obj.get("last_price")
            if last_price is not None:
                prev_close = None
                prev_obj = obj.get("prev_ohlc")
                if isinstance(prev_obj, dict):
                    prev_close = prev_obj.get("close")
                elif isinstance(prev_obj, list):
                    for po in reversed(prev_obj):
                        if isinstance(po, dict) and po.get("close") is not None:
                            prev_close = po.get("close")
                            break
                latest.setdefault(sym, {})
                change_pct = None
                try:
                    if prev_close is not None and float(prev_close) != 0:
                        change_pct = (float(last_price) / float(prev_close) - 1.0) * 100.0
                except Exception:
                    pass
                latest[sym].update({
                    "Symbol": sym,
                    "Instrument Key": ikey,
                    "LTP": last_price,
                    "Prev Close": prev_close,
                    "Change %": change_pct,
                    "Received": datetime.now().strftime("%H:%M:%S"),
                })

    return rows, latest, parsed, "; ".join(shape[-8:])


def _seed_upstox_intraday_history(mapped):
    """Seed today's 1-minute history only for the focused radar watchlist.

    V9.5 deliberately does NOT request historical candles for all 500 stocks.
    The full NIFTY 500 feed supplies current market data; only the small daily
    pre-breakout/near-miss watchlist gets 1-minute history for intraday confirmation.
    """
    try:
        token = str(st.secrets.get("UPSTOX_ACCESS_TOKEN", "")).strip()
    except Exception:
        token = ""
    if not token or not mapped:
        with UPSTOX_WS_LOCK:
            UPSTOX_WS_STATE["seed_error"] = "Missing token or no focused radar watchlist is mapped."
            UPSTOX_WS_STATE["seed_running"] = False
            UPSTOX_WS_STATE["seed_done"] = True
        return
    items = list(mapped.items())
    total = len(items); ok = 0; errors = 0
    headers = {"Accept":"application/json", "Authorization":f"Bearer {token}"}
    for idx, (symbol, ikey) in enumerate(items, start=1):
        if not UPSTOX_WS_STATE.get("seed_running", False):
            break
        try:
            url = "https://api.upstox.com/v3/historical-candle/intraday/{}/minutes/1".format(
                requests.utils.quote(str(ikey), safe="")
            )
            r = requests.get(url, headers=headers, timeout=12)
            r.raise_for_status()
            payload = r.json()
            data = payload.get("data", {}) if isinstance(payload, dict) else {}
            raw = data.get("candles", []) if isinstance(data, dict) else []
            rows = []
            for c in raw:
                if isinstance(c, (list, tuple)) and len(c) >= 6:
                    rows.append({
                        "Symbol":symbol, "Instrument Key":ikey, "1m Timestamp":c[0],
                        "1m Open":c[1], "1m High":c[2], "1m Low":c[3],
                        "1m Close":c[4], "1m Volume":c[5]
                    })
            rows.sort(key=lambda x: str(x["1m Timestamp"]))
            if rows:
                _update_candle_history({symbol: rows[-120:]})
                ok += 1
            else:
                errors += 1
        except Exception as exc:
            errors += 1
            with UPSTOX_WS_LOCK:
                if not UPSTOX_WS_STATE.get("seed_error"):
                    UPSTOX_WS_STATE["seed_error"] = f"{symbol}: {type(exc).__name__}: {exc}"
        with UPSTOX_WS_LOCK:
            UPSTOX_WS_STATE["seed_ok"] = ok
            UPSTOX_WS_STATE["seed_total"] = total
        # Small pacing gap prevents a burst and leaves headroom for the 15s OHLC polling.
        time.sleep(0.15)
    with UPSTOX_WS_LOCK:
        UPSTOX_WS_STATE["seed_running"] = False
        UPSTOX_WS_STATE["seed_done"] = True
        UPSTOX_WS_STATE["seed_ok"] = ok
        UPSTOX_WS_STATE["seed_total"] = total
        if errors and not UPSTOX_WS_STATE.get("seed_error"):
            UPSTOX_WS_STATE["seed_error"] = f"{errors} watchlist symbols returned no intraday candles."


def _make_radar_watchlist(daily_candidates, near_miss, limit=30):
    """Build a small, high-priority live radar universe from the daily scan."""
    names=[]
    if isinstance(daily_candidates, pd.DataFrame) and not daily_candidates.empty and "Stock" in daily_candidates.columns:
        for x in daily_candidates["Stock"].astype(str).tolist():
            x=x.upper().strip()
            if x and x not in names: names.append(x)
    if isinstance(near_miss, pd.DataFrame) and not near_miss.empty and "Stock" in near_miss.columns:
        for x in near_miss["Stock"].astype(str).tolist():
            x=x.upper().strip()
            if x and x not in names: names.append(x)
    return names[:limit]


def _poll_upstox_i1_ohlc_loop(mapped):
    """Near-live 1-minute OHLCV polling for the full mapped universe.

    Current OHLCV is cheap and bounded: five 100-instrument requests per cycle.
    Historical 1-minute seeding is handled separately and only for the focused
    radar watchlist.
    """
    try:
        token = str(st.secrets.get("UPSTOX_ACCESS_TOKEN", "")).strip()
    except Exception:
        token = ""
    keys = list(mapped.values()) if isinstance(mapped, dict) else []
    reverse = {v: k for k, v in mapped.items()} if isinstance(mapped, dict) else {}
    if not token or not keys:
        with UPSTOX_WS_LOCK:
            UPSTOX_WS_STATE["ohlc_error"] = "Missing token or mapped instruments for V3 I1 OHLC."
            UPSTOX_WS_STATE["rest_live"] = False
        return

    headers = {"Accept":"application/json", "Authorization":f"Bearer {token}"}
    batch_size = 100
    while True:
        with UPSTOX_WS_LOCK:
            if not UPSTOX_WS_STATE.get("ohlc_running"):
                break
        try:
            total_rows = 0
            total_candles = 0
            successful_batches = 0
            errors = []
            batch_count = (len(keys) + batch_size - 1) // batch_size
            for offset in range(0, len(keys), batch_size):
                batch = keys[offset:offset + batch_size]
                params = {"instrument_key": ",".join(str(x) for x in batch), "interval": "I1"}
                r = requests.get("https://api.upstox.com/v3/market-quote/ohlc", headers=headers, params=params, timeout=15)
                r.raise_for_status()
                payload = r.json()
                candle_rows, latest_rows, parsed, shape = _extract_i1_response(payload, reverse)
                _update_candle_history(candle_rows)
                with UPSTOX_WS_LOCK:
                    updates = UPSTOX_WS_STATE.setdefault("updates", {})
                    for sym, row in latest_rows.items():
                        base = updates.get(sym, {"Symbol": sym})
                        base.update(row)
                        series = UPSTOX_WS_STATE.get("candles", {}).get(sym, [])
                        if series:
                            base.update(series[-1])
                        updates[sym] = base
                total_rows += len(latest_rows)
                total_candles += parsed
                successful_batches += 1
                if not latest_rows and len(errors) < 2:
                    errors.append(f"batch {offset//batch_size + 1}: no rows ({shape})")

            with UPSTOX_WS_LOCK:
                UPSTOX_WS_STATE["ohlc_last_poll"] = datetime.now().strftime("%H:%M:%S")
                UPSTOX_WS_STATE["ohlc_symbols"] = total_rows
                UPSTOX_WS_STATE["ohlc_candles"] = total_candles
                UPSTOX_WS_STATE["rest_live"] = bool(total_rows)
                if errors:
                    UPSTOX_WS_STATE["ohlc_error"] = f"I1 poll partial: {successful_batches}/{batch_count} batches; " + " | ".join(errors)
                else:
                    UPSTOX_WS_STATE["ohlc_error"] = ""
        except Exception as exc:
            with UPSTOX_WS_LOCK:
                UPSTOX_WS_STATE["ohlc_error"] = f"I1 OHLC poll error: {type(exc).__name__}: {exc}"
                UPSTOX_WS_STATE["rest_live"] = False
        time.sleep(15)


def _start_radar_seed_for_watchlist(watchlist):
    """Start a small sequential history seed for the selected radar stocks."""
    watchlist=[str(x).upper().strip() for x in (watchlist or []) if str(x).strip()]
    with UPSTOX_WS_LOCK:
        mapped_all=dict(UPSTOX_WS_STATE.get("mapped", {}))
        if UPSTOX_WS_STATE.get("seed_running"):
            return False, "radar history seed is already running"
    mapped={s:mapped_all[s] for s in watchlist if s in mapped_all}
    if not mapped:
        return False, "start the NIFTY 500 live engine first so the watchlist is mapped"
    with UPSTOX_WS_LOCK:
        UPSTOX_WS_STATE["candles"] = {k:v for k,v in UPSTOX_WS_STATE.get("candles", {}).items() if k in mapped}
        UPSTOX_WS_STATE["seed_running"] = True
        UPSTOX_WS_STATE["seed_done"] = False
        UPSTOX_WS_STATE["seed_ok"] = 0
        UPSTOX_WS_STATE["seed_total"] = len(mapped)
        UPSTOX_WS_STATE["seed_error"] = ""
        UPSTOX_WS_STATE["radar_watchlist"] = list(mapped.keys())
        UPSTOX_WS_STATE["radar_watchlist_source"] = "daily strict candidates + near-misses"
    t=threading.Thread(target=_seed_upstox_intraday_history, args=(mapped,), name="upstox-v3-radar-seed", daemon=True)
    with UPSTOX_WS_LOCK:
        UPSTOX_WS_STATE["seed_thread"] = t
    t.start()
    return True, f"seeding {len(mapped)} focused radar stocks"

def _start_upstox_nifty500_websocket(symbols):
    """Start the proven V3 LTPC stream, then start V3 REST I1 polling.

    V9.3.4 keeps the WebSocket on LTPC as an optional enhancement because the same
    Analytics Token already proved stable for the 500-stock LTPC feed.
    The richer I1 OHLCV data is obtained from the documented V3 OHLC REST
    endpoint in one request for the 500 instruments. This avoids the FULL
    WebSocket handshake 403 seen in some Analytics-token sessions while
    retaining live streaming LTP data.
    """
    with UPSTOX_WS_LOCK:
        if UPSTOX_WS_STATE["running"]:
            return False, "already running"
    try:
        token = str(st.secrets.get("UPSTOX_ACCESS_TOKEN", "")).strip()
    except Exception:
        token = ""
    if not token:
        with UPSTOX_WS_LOCK:
            UPSTOX_WS_STATE["error"] = "UPSTOX_ACCESS_TOKEN is missing from Streamlit Secrets."
        return False, "missing token"

    try:
        mapped, unmapped, _, recovered = build_upstox_nifty500_mapping(symbols)
    except Exception as exc:
        with UPSTOX_WS_LOCK:
            UPSTOX_WS_STATE["error"] = f"Instrument master error: {type(exc).__name__}: {exc}"
        return False, "instrument mapping failed"

    if not mapped:
        with UPSTOX_WS_LOCK:
            UPSTOX_WS_STATE["error"] = "No NIFTY 500 symbols could be mapped to Upstox NSE_EQ keys."
        return False, "no mapped instruments"

    with UPSTOX_WS_LOCK:
        UPSTOX_WS_STATE.update({
            "running": True, "connected": False, "error": "", "opened_at": None,
            "last_message_at": None, "updates": {}, "candles": {}, "last_day_volume": {},
    "ohlc_thread": None, "ohlc_running": False, "ohlc_error": "", "ohlc_last_poll": None, "ohlc_symbols": 0, "ohlc_candles": 0, "rest_live": False,
            "seed_thread": None, "seed_running": False, "seed_done": False, "seed_ok": 0, "seed_total": 0, "seed_error": "", "radar_watchlist": [], "radar_watchlist_source": "", "mapped": mapped,
            "unmapped": unmapped, "recovered": recovered, "universe_size": len(symbols),
        })

    def worker():
        ws = None
        try:
            import json
            import uuid
            import websocket
            from google.protobuf import json_format
            from upstox_client.feeder.proto import MarketDataFeedV3_pb2

            auth_url = "https://api.upstox.com/v3/feed/market-data-feed/authorize"
            auth_headers = {
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            }
            auth_response = requests.get(auth_url, headers=auth_headers, timeout=20, allow_redirects=False)
            if auth_response.status_code != 200:
                raise RuntimeError(
                    f"Upstox WebSocket authorization failed: HTTP {auth_response.status_code} "
                    f"{auth_response.text[:300]}"
                )
            auth_json = auth_response.json()
            ws_url = ((auth_json.get("data") or {}).get("authorized_redirect_uri") or "").strip()
            if not ws_url.startswith("wss://"):
                raise RuntimeError("Upstox authorization response did not contain a valid wss:// URI.")

            reverse = {v: k for k, v in mapped.items()}
            keys = list(mapped.values())

            def on_open(sock):
                with UPSTOX_WS_LOCK:
                    UPSTOX_WS_STATE["connected"] = True
                    UPSTOX_WS_STATE["opened_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    UPSTOX_WS_STATE["error"] = ""
                # Subscribe in modest chunks after the socket is open.
                # This keeps the handshake independent of the NIFTY 500 payload.
                for i in range(0, len(keys), 100):
                    chunk = keys[i:i + 100]
                    request = {
                        "guid": str(uuid.uuid4()),
                        "method": "sub",
                        "data": {"mode": "ltpc", "instrumentKeys": chunk},
                    }
                    sock.send(json.dumps(request).encode("utf-8"), opcode=websocket.ABNF.OPCODE_BINARY)

            def on_message(sock, message):
                try:
                    if isinstance(message, str):
                        return
                    decoded = MarketDataFeedV3_pb2.FeedResponse.FromString(message)
                    payload = json_format.MessageToDict(decoded)
                    updates, candles = _extract_live_updates(payload, reverse)
                    with UPSTOX_WS_LOCK:
                        UPSTOX_WS_STATE["last_message_at"] = datetime.now().strftime("%H:%M:%S")
                        if updates:
                            UPSTOX_WS_STATE["updates"].update(updates)
                    _update_candle_history(candles)
                except Exception as exc:
                    with UPSTOX_WS_LOCK:
                        UPSTOX_WS_STATE["error"] = f"Feed decode error: {type(exc).__name__}: {exc}"

            def on_error(sock, error):
                with UPSTOX_WS_LOCK:
                    UPSTOX_WS_STATE["error"] = str(error)
                    UPSTOX_WS_STATE["connected"] = False

            def on_close(sock, status_code, close_msg):
                with UPSTOX_WS_LOCK:
                    UPSTOX_WS_STATE["connected"] = False
                    if close_msg and not UPSTOX_WS_STATE.get("error"):
                        UPSTOX_WS_STATE["error"] = f"WebSocket closed: {status_code} {close_msg}"

            ws = websocket.WebSocketApp(
                ws_url,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            with UPSTOX_WS_LOCK:
                UPSTOX_WS_STATE["streamer"] = ws

            # The authorized URI is already authenticated; do not send the
            # access token as a second handshake header.
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as exc:
            with UPSTOX_WS_LOCK:
                UPSTOX_WS_STATE["error"] = f"{type(exc).__name__}: {exc}"
                UPSTOX_WS_STATE["connected"] = False
        finally:
            with UPSTOX_WS_LOCK:
                UPSTOX_WS_STATE["streamer"] = None
                # Keep the overall engine running while the REST I1 fallback is alive.
                if not UPSTOX_WS_STATE.get("ohlc_running"):
                    UPSTOX_WS_STATE["running"] = False

    thread = threading.Thread(target=worker, name="upstox-v3-nifty500-ltpc", daemon=True)
    ohlc_thread = threading.Thread(target=_poll_upstox_i1_ohlc_loop, args=(mapped,), name="upstox-v3-i1-ohlc", daemon=True)
    with UPSTOX_WS_LOCK:
        UPSTOX_WS_STATE["thread"] = thread
        UPSTOX_WS_STATE["ohlc_thread"] = ohlc_thread
        UPSTOX_WS_STATE["seed_thread"] = None
        UPSTOX_WS_STATE["ohlc_running"] = True
        UPSTOX_WS_STATE["seed_running"] = False
        UPSTOX_WS_STATE["seed_done"] = False
        UPSTOX_WS_STATE["seed_ok"] = 0
        UPSTOX_WS_STATE["seed_total"] = 0
        UPSTOX_WS_STATE["seed_error"] = ""
    ohlc_thread.start()
    # V9.5.1: do not seed 500 stocks at startup. The focused seed is triggered
    # after the daily scan creates the actual radar watchlist.
    return True, f"started with {len(mapped)} mapped instruments + V3 I1 OHLC polling"

def _stop_upstox_nifty500_websocket():
    with UPSTOX_WS_LOCK:
        streamer = UPSTOX_WS_STATE.get("streamer")
    if streamer is not None:
        try:
            streamer.disconnect()
        except Exception:
            pass
    with UPSTOX_WS_LOCK:
        UPSTOX_WS_STATE["running"] = False
        UPSTOX_WS_STATE["ohlc_running"] = False
        UPSTOX_WS_STATE["seed_running"] = False
        UPSTOX_WS_STATE["connected"] = False


if hasattr(st, "fragment"):
    @st.fragment(run_every="2s")
    def render_upstox_nifty500_panel():
        with UPSTOX_WS_LOCK:
            connected = UPSTOX_WS_STATE["connected"]
            running = UPSTOX_WS_STATE["running"]
            error = UPSTOX_WS_STATE["error"]
            ohlc_error = UPSTOX_WS_STATE.get("ohlc_error", "")
            ohlc_last_poll = UPSTOX_WS_STATE.get("ohlc_last_poll")
            rest_live = UPSTOX_WS_STATE.get("rest_live", False)
            ohlc_symbols = UPSTOX_WS_STATE.get("ohlc_symbols", 0)
            ohlc_candles = UPSTOX_WS_STATE.get("ohlc_candles", 0)
            seed_running = UPSTOX_WS_STATE.get("seed_running", False)
            seed_done = UPSTOX_WS_STATE.get("seed_done", False)
            seed_ok = UPSTOX_WS_STATE.get("seed_ok", 0)
            seed_total = UPSTOX_WS_STATE.get("seed_total", 0)
            seed_error = UPSTOX_WS_STATE.get("seed_error", "")
            opened_at = UPSTOX_WS_STATE["opened_at"]
            last_message_at = UPSTOX_WS_STATE["last_message_at"]
            updates = dict(UPSTOX_WS_STATE["updates"])
            mapped = dict(UPSTOX_WS_STATE["mapped"])
            unmapped = list(UPSTOX_WS_STATE["unmapped"])
            recovered = list(UPSTOX_WS_STATE.get("recovered", []))
            universe_size = UPSTOX_WS_STATE["universe_size"]
        if connected and rest_live:
            st.success("🟢 LIVE — Upstox V3 I1 REST (stable mode)")
        elif connected:
            st.success("🟢 LIVE — Upstox Upstox V3 REST market engine")
        elif rest_live:
            st.success("🟢 LIVE — Upstox V3 I1 REST market engine (stable REST mode)")
        elif running:
            st.info("🟡 Starting live market engine…")
        else:
            st.warning("⚪ NIFTY 500 market engine is stopped")
        a,b,c,d = st.columns(4)
        a.metric("Connection", "LIVE" if (connected or rest_live) else ("STARTING" if running else "STOPPED"))
        b.metric("Mapped", f"{len(mapped)}/{universe_size or 0}")
        c.metric("Updating", str(len(updates)))
        d.metric("Last Feed", last_message_at or "—")
        if opened_at:
            st.caption(f"Connected at {opened_at} server time. Feed mode: LTPC (read-only) • live LTP stream. 1-minute OHLCV is polled from Upstox V3 REST.")
        if error:
            st.caption(f"WebSocket optional status: {error}")
        if ohlc_last_poll:
            st.caption(f"V3 I1 OHLC last poll: {ohlc_last_poll} • {ohlc_symbols}/500 symbols • {ohlc_candles} candle rows parsed • five 100-key REST requests per cycle")
        if ohlc_error:
            st.warning(ohlc_error)
        if seed_running:
            st.info(f"⏳ Seeding focused radar history: {seed_ok}/{seed_total} stocks loaded. V9.5 seeds only the daily candidates/near-misses, not all 500.")
        elif seed_done and seed_total:
            st.success(f"✅ Focused intraday history seeded: {seed_ok}/{seed_total} stocks. Radar has 1-minute context for the selected watchlist.")
        with UPSTOX_WS_LOCK:
            wl=list(UPSTOX_WS_STATE.get("radar_watchlist", []))
        if wl:
            st.caption(f"🎯 Radar watchlist: {len(wl)} stocks • {', '.join(wl[:12])}{' …' if len(wl)>12 else ''}")
        if seed_error:
            st.caption(f"History seed note: {seed_error[:300]}")
        if unmapped:
            st.warning(f"Unmapped NIFTY 500 symbols: {len(unmapped)} — {', '.join(unmapped[:20])}{' …' if len(unmapped)>20 else ''}")
        if recovered:
            st.info(f"🔧 Repaired via Upstox instrument search: {len(recovered)} — {', '.join(recovered[:20])}{' …' if len(recovered)>20 else ''}")
        if updates:
            rows=list(updates.values())
            df=pd.DataFrame(rows)
            # REST I1 rows may not contain a Change % field. Build it safely
            # from LTP and previous close before sorting, and never assume a
            # column exists in a partial market-data response.
            if "LTP" in df.columns:
                df["LTP"] = pd.to_numeric(df["LTP"], errors="coerce")
            if "Prev Close" in df.columns:
                df["Prev Close"] = pd.to_numeric(df["Prev Close"], errors="coerce")
            if "Change %" not in df.columns:
                df["Change %"] = np.nan
            if "LTP" in df.columns and "Prev Close" in df.columns:
                valid = df["Prev Close"].notna() & df["Prev Close"].ne(0) & df["LTP"].notna()
                df.loc[valid, "Change %"] = (df.loc[valid, "LTP"] / df.loc[valid, "Prev Close"] - 1.0) * 100.0
            df["Change %"] = pd.to_numeric(df["Change %"], errors="coerce").round(2)
            preferred=[c for c in ["Symbol","LTP","Prev Close","Change %","Day Volume","ATP","1m Open","1m High","1m Low","1m Close","1m Volume","1m Timestamp","Last Qty","Received"] if c in df.columns]
            if "Change %" in df.columns:
                df=df.sort_values("Change %",ascending=False,na_position="last")
            st.dataframe(df[preferred] if preferred else df, use_container_width=True, hide_index=True, height=560)
            with UPSTOX_WS_LOCK:
                ch=dict(UPSTOX_WS_STATE.get("candles", {}))
            if ch:
                # Candle history is stored as {symbol: [candle, ...]}.
                # Build a flat display dataframe from the latest candle per symbol
                # instead of passing the nested lists directly to pandas.
                latest_candles=[]
                for symbol, series in ch.items():
                    if isinstance(series, dict):
                        series=[series]
                    if isinstance(series, (list, tuple)) and series:
                        last=series[-1]
                        if isinstance(last, dict):
                            latest_candles.append(dict(last))
                cdf=pd.DataFrame(latest_candles)
                st.markdown("### ⏱️ Live 1-Minute Candle Engine")
                st.caption("Upstox V3 OHLC supplies the current and previous 1-minute OHLCV candle. The app polls all 500 keys in five 100-key REST requests per cycle and keeps the latest 120 one-minute candles per stock in memory for the next intraday scanner stage.")
                if not cdf.empty:
                    for col in ["1m Open","1m High","1m Low","1m Close","1m Volume"]:
                        if col in cdf.columns:
                            cdf[col]=pd.to_numeric(cdf[col],errors="coerce")
                    if all(c in cdf.columns for c in ["1m High","1m Low","1m Open"]):
                        cdf["1m Range %"]=(cdf["1m High"]-cdf["1m Low"])/cdf["1m Open"].replace(0,np.nan)*100
                    if "1m Volume" in cdf.columns:
                        cdf=cdf.sort_values("1m Volume",ascending=False,na_position="last")
                    display_cols=[c for c in ["Symbol","1m Timestamp","1m Open","1m High","1m Low","1m Close","1m Volume","1m Range %"] if c in cdf.columns]
                    st.dataframe(cdf[display_cols].head(30) if display_cols else cdf.head(30), use_container_width=True, hide_index=True, height=420)
                else:
                    st.info("1-minute candle history is still being assembled. The REST market feed is connected.")
        else:
            st.caption("Waiting for the first NIFTY 500 market-data snapshot…")



def build_intraday_radar(candles, daily_candidates=None, max_rows=25):
    """Build a lightweight 1-minute pre-breakout confirmation radar.

    This is deliberately a confirmation layer, not a replacement for the V8 daily
    setup. It looks for a tight intraday base that is pressing resistance while
    volatility and volume contract and the last candles maintain higher lows.
    """
    rows = []
    daily_set = set()
    if isinstance(daily_candidates, pd.DataFrame) and not daily_candidates.empty and "Stock" in daily_candidates.columns:
        daily_set = set(daily_candidates["Stock"].astype(str).str.upper())

    for symbol, series in (candles or {}).items():
        if isinstance(series, dict):
            series = [series]
        if not isinstance(series, (list, tuple)) or len(series) < 20:
            continue
        try:
            d = pd.DataFrame([x for x in series if isinstance(x, dict)])
            if d.empty:
                continue
            cols = ["1m Open", "1m High", "1m Low", "1m Close", "1m Volume"]
            if any(c not in d.columns for c in cols):
                continue
            for c in cols:
                d[c] = pd.to_numeric(d[c], errors="coerce")
            d = d.dropna(subset=cols).tail(120)
            if len(d) < 20:
                continue
            close = d["1m Close"]
            high = d["1m High"]
            low = d["1m Low"]
            vol = d["1m Volume"]
            latest = float(close.iloc[-1])
            pivot = float(high.iloc[-21:-1].max())
            if pivot <= 0 or latest <= 0:
                continue
            r5 = (float(high.tail(5).max()) - float(low.tail(5).min())) / float(low.tail(5).min()) * 100
            r10 = (float(high.tail(10).max()) - float(low.tail(10).min())) / float(low.tail(10).min()) * 100
            r20 = (float(high.tail(20).max()) - float(low.tail(20).min())) / float(low.tail(20).min()) * 100
            atr1 = (high - low).rolling(5).mean().iloc[-1]
            atr20 = (high - low).rolling(20).mean().iloc[-1]
            ar = float(atr1 / atr20) if pd.notna(atr20) and atr20 else np.nan
            vr = float(vol.tail(5).mean() / vol.tail(20).mean()) if vol.tail(20).mean() else np.nan
            dist = (pivot - latest) / pivot * 100
            pos = (latest - float(low.tail(20).min())) / max(float(high.tail(20).max()) - float(low.tail(20).min()), 1e-9)
            low5 = float(low.tail(5).min())
            low10 = float(low.iloc[-10:-5].min())
            low15 = float(low.iloc[-15:-10].min())
            higher_lows = low5 >= low10 * 0.997 and low10 >= low15 * 0.997
            no_break = bool((close.tail(5) < pivot).all())
            score = 0
            score += 18 if r5 < r10 < r20 else 0
            score += 15 if r5 <= 0.80 else (8 if r5 <= 1.20 else 0)
            score += 12 if ar <= 0.70 else (6 if ar <= 0.85 else 0)
            score += 12 if vr <= 0.65 else (6 if vr <= 0.85 else 0)
            score += 12 if higher_lows else 0
            score += 16 if 0.10 <= dist <= 1.50 else (8 if 0 < dist <= 3.0 else 0)
            score += 8 if pos >= 0.70 else (4 if pos >= 0.55 else 0)
            score += 7 if no_break else 0
            if score < 60:
                continue
            rows.append({
                "Stock": str(symbol).upper(),
                "Score": int(score),
                "LTP": round(latest, 2),
                "To 20m Pivot %": round(dist, 2),
                "5m Range %": round(r5, 2),
                "10m Range %": round(r10, 2),
                "20m Range %": round(r20, 2),
                "ATR5/ATR20": round(ar, 2) if np.isfinite(ar) else np.nan,
                "Vol5/Vol20": round(vr, 2) if np.isfinite(vr) else np.nan,
                "Higher Lows": "YES" if higher_lows else "NO",
                "Daily Setup": "YES" if str(symbol).upper() in daily_set else "NO",
            })
        except Exception:
            continue
    if not rows:
        return pd.DataFrame()
    out = pd.DataFrame(rows).sort_values(["Daily Setup", "Score", "To 20m Pivot %"], ascending=[False, False, True])
    return out.head(max_rows).reset_index(drop=True)

def upstox_nifty500_stream_test(symbols):
    st.markdown("## 🌐 Upstox NIFTY 500 Live Universe")
    st.caption(
        "V9.5.1 uses the official NSE Indices NIFTY 500 constituent file first, then Upstox NSE_EQ mapping with instrument-search repair for valid equities. "
        "Read-only — no order API is used."
    )
    c1,c2,c3 = st.columns(3)
    if c1.button("▶️ START NIFTY 500 LIVE", type="primary", width="stretch"):
        ok,msg=_start_upstox_nifty500_websocket(symbols)
        if ok: st.toast(msg)
        else: st.error(f"Could not start NIFTY 500 stream: {msg}")
    if c2.button("⏹️ STOP NIFTY 500", width="stretch"):
        _stop_upstox_nifty500_websocket(); st.toast("NIFTY 500 stream stopped")
    c3.caption("Stable design: current OHLCV is polled for NIFTY 500; 1-minute history is seeded only for the focused radar watchlist.")
    if st.session_state.get("radar_watchlist"):
        if st.button("🎯 SEED / REFRESH RADAR HISTORY", width="stretch"):
            ok_seed,msg_seed=_start_radar_seed_for_watchlist(st.session_state.radar_watchlist)
            if ok_seed: st.toast(msg_seed)
            else: st.warning(msg_seed)
    render_upstox_nifty500_panel()
    st.markdown("### 🎯 Live Intraday Pre-Breakout Radar")
    st.caption("Confirmation layer for the focused daily watchlist. V9.5.1 does not seed all 500 stocks; it seeds strict daily candidates + strongest near-misses, then confirms them with live 1-minute structure.")
    with UPSTOX_WS_LOCK:
        radar_candles = {k: list(v) for k, v in UPSTOX_WS_STATE.get("candles", {}).items()}
    daily_candidates = st.session_state.get("scan_out", pd.DataFrame())
    radar = build_intraday_radar(radar_candles, daily_candidates)
    if radar.empty:
        st.info("No intraday pre-breakout confirmation setup is ready yet. This is expected when the conditions are intentionally strict.")
    else:
        st.dataframe(radar, use_container_width=True, hide_index=True, height=520)
        st.caption("Radar logic: progressive 5m/10m/20m contraction + low ATR + volume dry-up + higher lows + price pressing the prior 20-minute pivot + no recent 1-minute breakout.")

def main():
    st.title('🎯 Pre-Breakout Hunter V9.5.1')
    upstox_connection_test()
    st.divider()
    # NIFTY 500 is loaded before the live-universe test so the exact scanner universe is used.
    try:
        v92_symbols = get_nifty500_symbols()
    except Exception as e:
        st.error(f'Could not load NIFTY 500 list for Upstox V9.2: {e}')
        v92_symbols = []
    if v92_symbols:
        upstox_nifty500_stream_test(tuple(v92_symbols))
    st.divider()
    st.caption('Selective NSE scanner + live market breadth dashboard — resistance pressure + progressive compression + higher lows + improving relative strength. Upstox is used for the read-only live-data test; no trading API is used.')

    if 'scan_out' not in st.session_state:
        st.session_state.scan_out = pd.DataFrame()
    if 'scan_diag' not in st.session_state:
        st.session_state.scan_diag = pd.DataFrame()
    if 'scan_ran' not in st.session_state:
        st.session_state.scan_ran = False
    if 'radar_watchlist' not in st.session_state:
        st.session_state.radar_watchlist = []
    if 'validation_result' not in st.session_state:
        st.session_state.validation_result = pd.DataFrame()
    if 'validation_status' not in st.session_state:
        st.session_state.validation_status = ''
    if 'market_frames' not in st.session_state:
        st.session_state.market_frames = {}
    if 'market_data' not in st.session_state:
        st.session_state.market_data = pd.DataFrame()
    if 'market_gainers' not in st.session_state:
        st.session_state.market_gainers = pd.DataFrame()
    if 'market_losers' not in st.session_state:
        st.session_state.market_losers = pd.DataFrame()
    if 'market_volume_gainers' not in st.session_state:
        st.session_state.market_volume_gainers = pd.DataFrame()
    if 'market_sector' not in st.session_state:
        st.session_state.market_sector = pd.DataFrame()
    if 'market_aligned' not in st.session_state:
        st.session_state.market_aligned = pd.DataFrame()
    if 'market_deals' not in st.session_state:
        st.session_state.market_deals = pd.DataFrame()
    if 'market_news' not in st.session_state:
        st.session_state.market_news = pd.DataFrame()

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

    cfg={'range5':r5,'range10':r10,'range20':r20,'atr_ratio':atrmax,'vol_ratio':volmax,'pivot_min':pmin,'pivot_max':pmax,'min_score':min_score}

    st.markdown('''### Exact V8 setup being hunted
**Strong trend → progressive range contraction → quiet volume → higher lows → repeated resistance tests → improving relative strength → price near resistance → breakout still ahead.**

V8 deliberately rewards *readiness* rather than simply giving points for a good-looking trend. Results are **watchlist candidates, not automatic buy signals**.''')

    # Load the universe once. Validation no longer depends on the scan button.
    try:
        symbols=get_nifty500_symbols()
    except Exception as e:
        st.error(f'Could not load NIFTY 500 list: {e}')
        return
    st.write(f'Universe: **{len(symbols)} stocks**')
        

    if scan_btn:
        st.session_state.validation_result = pd.DataFrame()
        st.session_state.validation_status = ''
        with st.spinner('Downloading current daily data in small batches...'):
            dlp=st.progress(0, text='Downloading current market data…')
            frames=download_batches_resumable(tuple(symbols),period_days=520,batch_size=10,progress=dlp)
            dlp.empty()
        st.session_state.market_frames=frames
        sector_map=get_nifty500_sector_map()
        md=market_dashboard(frames,tuple(symbols),sector_map)
        st.session_state.market_data=md[0]
        st.session_state.market_gainers=md[1]
        st.session_state.market_losers=md[2]
        st.session_state.market_volume_gainers=md[3]
        st.session_state.market_sector=md[4]
        st.session_state.market_aligned=md[5]
        with st.spinner('Loading recent NSE bulk/block deals and market-moving corporate announcements…'):
            st.session_state.market_deals=get_nse_bulk_block_deals(days=3)
            st.session_state.market_news=get_nse_big_news(tuple(symbols),days=1)
        st.info(f'Yahoo data received for {len(frames)}/{len(symbols)} NSE stocks.')
        nifty_frames=download_one_batch(('^NSEI',), (datetime.now()-timedelta(days=520)).strftime('%Y-%m-%d'), (datetime.now()+timedelta(days=1)).strftime('%Y-%m-%d'))
        nifty=nifty_frames.get('^NSEI', pd.DataFrame()).get('close', pd.Series(dtype=float))
        out,diag=scan(symbols,frames,nifty,cfg)
        if not out.empty:
            sector_map=get_nifty500_sector_map()
            out['Sector']=out['Stock'].map(lambda x: sector_map.get(x,'Other / Unclassified'))
            # Contextual flag: candidate is in a sector with positive breadth today.
            if not st.session_state.market_sector.empty:
                sm=st.session_state.market_sector.set_index('Sector')
                out['Sector Strength']=out['Sector'].map(sm['Strength Score']).fillna(0)
                out['Sector Breadth %']=out['Sector'].map(sm['Advance %']).fillna(0)
                out['Sector Aligned']=np.where((out['Sector Breadth %']>=50)&(out['Sector Strength']>=50),'YES','NO')
        st.session_state.scan_out=out
        st.session_state.scan_diag=diag
        st.session_state.scan_ran=True
        # V9.5.1: after the daily scan, create the focused intraday radar universe.
        watchlist=_make_radar_watchlist(out, diag, limit=30)
        st.session_state.radar_watchlist=watchlist
        if watchlist:
            ok_seed,msg_seed=_start_radar_seed_for_watchlist(watchlist)
            if ok_seed:
                st.toast(f"🎯 {msg_seed}")

    if st.session_state.scan_ran and not st.session_state.market_data.empty:
        st.markdown('### 📊 Market Dashboard')
        st.caption('Decision layer built from the same current daily OHLCV data used by the scanner. Use breadth and sector strength to prioritize — not to chase already-extended gainers.')
        md=st.session_state.market_data
        adv=int((md['Change %']>0).sum()); dec=int((md['Change %']<0).sum()); unch=int((md['Change %']==0).sum())
        breadth_pct=(adv-dec)/max(len(md),1)*100
        above20=int(md['Above 20DMA'].sum()) if 'Above 20DMA' in md else 0
        above50=int(md['Above 50DMA'].sum()) if 'Above 50DMA' in md else 0
        a,b,c,d,e=st.columns(5)
        a.metric('Advancing',adv)
        b.metric('Declining',dec)
        c.metric('Unchanged',unch)
        d.metric('A/D ratio',f'{adv}:{dec}')
        e.metric('Breadth score',f'{breadth_pct:+.1f}%')
        st.caption(f'Trend participation: {above20}/{len(md)} above 20DMA • {above50}/{len(md)} above 50DMA')
        t1,t2,t3,t4,t5,t6,t7=st.tabs(['🔥 Pre-Breakout + Sector','🚀 Top Gainers','🔊 Volume Gainers','🗺️ Sector Strength','🔻 Top Losers','💰 Bulk / Block Deals','📰 Big News'])
        with t1:
            al=st.session_state.market_aligned.copy()
            if not al.empty:
                st.success('Stocks with positive price/volume behavior inside sectors showing positive breadth. These are a prioritization list, not buy signals.')
                st.dataframe(al[['Stock','Close','Change %','Volume Surge %','Avg Traded Value Cr','20D High Distance %','Sector']],width='stretch',hide_index=True)
                st.download_button('⬇️ Download sector-aligned watchlist',al.to_csv(index=False).encode(),'sector_aligned_watchlist.csv','text/csv',key='aligned_dl')
            else:
                st.info('No strong sector-aligned momentum candidates today.')
        with t2:
            g=st.session_state.market_gainers.copy()
            if not g.empty:
                st.dataframe(g[['Stock','Close','Change %','Volume','Volume Surge %','Avg Traded Value Cr','Sector']],width='stretch',hide_index=True)
                st.download_button('⬇️ Download top gainers CSV',g.to_csv(index=False).encode(),'top_gainers.csv','text/csv',key='top_gainers_dl')
        with t3:
            vg=st.session_state.market_volume_gainers.copy()
            if not vg.empty:
                st.dataframe(vg[['Stock','Close','Change %','Volume','Volume Surge %','Avg Traded Value Cr','Sector']],width='stretch',hide_index=True)
                st.download_button('⬇️ Download volume gainers CSV',vg.to_csv(index=False).encode(),'volume_gainers.csv','text/csv',key='volume_gainers_dl')
        with t4:
            sec=st.session_state.market_sector.copy()
            if not sec.empty:
                tiles=[]
                for _,r in sec.iterrows():
                    pct=float(r['Advance %'])
                    bg='#1b7f3a' if pct>=60 else ('#7f8c32' if pct>=40 else '#a33a3a')
                    tiles.append(f"<div style='display:inline-block;vertical-align:top;width:30%;min-width:180px;margin:5px;padding:12px;border-radius:10px;background:{bg};color:white'><b>{r['Sector']}</b><br>Strength {float(r['Strength Score']):.1f}<br>Advance {pct:.1f}%<br>↑ {int(r['Advances'])} &nbsp; ↓ {int(r['Declines'])}<br>20DMA {float(r['Above20DMA %']):.0f}%</div>")
                st.markdown(''.join(tiles),unsafe_allow_html=True)
                st.dataframe(sec[['Sector','Stocks','Advances','Declines','Unchanged','Advance %','AvgChange','Above20DMA %','Above50DMA %','Strength Score']],width='stretch',hide_index=True)
        with t6:
            deals=st.session_state.market_deals.copy()
            if not deals.empty:
                st.caption('Recent NSE-disclosed bulk and block deals. These are context signals, not automatic buy/sell signals.')
                st.dataframe(deals,width='stretch',hide_index=True)
                st.download_button('⬇️ Download deals CSV',deals.to_csv(index=False).encode(),'nse_bulk_block_deals.csv','text/csv',key='deals_dl')
            else:
                st.info('No recent bulk/block deal data was returned by NSE, or the NSE endpoint was temporarily unavailable.')
        with t7:
            news=st.session_state.market_news.copy()
            if not news.empty:
                st.caption('NSE corporate announcements ranked for likely market relevance. The ranking is a filter, not a claim that the news is bullish.')
                st.dataframe(news,width='stretch',hide_index=True)
                st.download_button('⬇️ Download big news CSV',news.to_csv(index=False).encode(),'nse_big_news.csv','text/csv',key='news_dl')
            else:
                st.info('No high-priority NSE announcements were returned for the selected period, or the NSE endpoint was temporarily unavailable.')
        with t5:
            lo=st.session_state.market_losers.copy()
            if not lo.empty:
                st.dataframe(lo[['Stock','Close','Change %','Volume Surge %','Avg Traded Value Cr','Sector']],width='stretch',hide_index=True)

    if st.session_state.scan_ran:
        out=st.session_state.scan_out
        diag=st.session_state.scan_diag
        if out.empty:
            st.warning('No strict V8 setups found today. This is intentional.')
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

    st.markdown('### 📈 V8.6 Full-History Walk-Forward Trade Validation')
    st.caption('V8.6 tests every valid historical trading day in a 5-year window. No setup-date sampling is used. Validation remains in small resumable batches to protect the Streamlit connection and Yahoo Finance limits.')
    with st.expander('Run V8.6 full-history trade validation', expanded=bool(st.session_state.validation_status)):
        bw=st.slider('Breakout window (days)',5,20,10,key='val_bw')
        td=st.slider('Maximum observation / trade days',15,40,30,key='val_td')
        batch_size=3
        total_batches=(len(symbols)+batch_size-1)//batch_size
        if 'val_batch' not in st.session_state: st.session_state.val_batch=1
        if 'val_events' not in st.session_state: st.session_state.val_events=pd.DataFrame()
        if 'val_failed' not in st.session_state: st.session_state.val_failed=[]
        if 'val_config' not in st.session_state: st.session_state.val_config=None

        st.write(f'**Validation batches:** {total_batches} × {batch_size} stocks  •  **Current batch:** {st.session_state.val_batch}/{total_batches}')
        b1,b2,b3=st.columns(3)
        run_next=b1.button('🧪 RUN CURRENT BATCH',width='stretch')
        reset=b2.button('♻️ RESET VALIDATION',width='stretch')
        run_all=b3.button('▶️ RUN ALL BATCHES',width='stretch')

        if reset:
            st.session_state.val_batch=1
            st.session_state.val_events=pd.DataFrame()
            st.session_state.val_failed=[]
            st.session_state.val_config=None
            st.session_state.validation_status='Reset'
            st.rerun()

        def do_one(batch_no):
            progress=st.progress(0,text=f'Batch {batch_no}/{total_batches}: downloading {batch_size} stocks…')
            # NIFTY historical data is downloaded once and cached separately.
            hist_nifty_frames=download_one_batch(('^NSEI',), (datetime.now()-timedelta(days=1825)).strftime('%Y-%m-%d'), (datetime.now()+timedelta(days=1)).strftime('%Y-%m-%d'))
            hist_nifty=hist_nifty_frames.get('^NSEI',pd.DataFrame()).get('close',pd.Series(dtype=float))
            bt,failed,batch=run_validation_batch(symbols,hist_nifty,cfg,batch_no,batch_size,None,bw,td,progress)
            progress.progress(1.0,text=f'Batch {batch_no}/{total_batches} complete: {len(bt)} setup events; {len(failed)} download failures')
            return bt,failed,batch

        if run_next:
            bt,failed,batch=do_one(st.session_state.val_batch)
            if not bt.empty:
                st.session_state.val_events=pd.concat([st.session_state.val_events,bt],ignore_index=True)
            st.session_state.val_failed=sorted(set(st.session_state.val_failed+failed))
            if st.session_state.val_batch<total_batches:
                st.session_state.val_batch+=1
                st.session_state.validation_status=f'Completed batch {st.session_state.val_batch-1}/{total_batches}'
            else:
                st.session_state.validation_status='All batches completed'
            st.rerun()

        if run_all:
            # Run all batches sequentially, but each network call is only 3 stocks.
            for batch_no in range(st.session_state.val_batch,total_batches+1):
                bt,failed,batch=do_one(batch_no)
                if not bt.empty:
                    st.session_state.val_events=pd.concat([st.session_state.val_events,bt],ignore_index=True)
                st.session_state.val_failed=sorted(set(st.session_state.val_failed+failed))
                st.session_state.val_batch=batch_no+1
                if batch_no<total_batches:
                    st.write(f'Completed batch {batch_no}/{total_batches}. Starting the next small batch…')
            st.session_state.validation_status='All batches completed'
            st.rerun()

        bt=st.session_state.val_events
        if not bt.empty:
            # Deduplicate in case a batch is accidentally rerun.
            bt=bt.drop_duplicates(subset=['Stock','Setup Date'],keep='last').copy()
            st.session_state.val_events=bt
            br=bt[bt.Breakout=='YES']
            trades=bt.dropna(subset=['Trade Return %'])
            a,b,c,d=st.columns(4)
            a.metric('Setups',len(bt))
            b.metric('Breakout rate',f'{len(br)/len(bt)*100:.1f}%')
            c.metric('Trade win rate',f'{(trades["Trade Return %"]>0).mean()*100:.1f}%' if not trades.empty else '—')
            d.metric('Avg trade',f'{trades["Trade Return %"].mean():.2f}%' if not trades.empty else '—')
            if not trades.empty:
                st.write(f'**Median trade:** {trades["Trade Return %"].median():.2f}%  •  **Average max gain:** {trades["Max Gain %"].mean():.2f}%  •  **Average max drawdown:** {trades["Max Drawdown %"].mean():.2f}%')
                # Score-band diagnostics help distinguish whether stronger setups behave better.
                bands=pd.cut(bt['Score'], bins=[-np.inf,89,93,np.inf], labels=['88–89','90–93','94+'])
                band_rows=[]
                for label,g in bt.assign(ScoreBand=bands).groupby('ScoreBand',observed=False):
                    gt=g.dropna(subset=['Trade Return %'])
                    band_rows.append({'Score band':str(label),'Setups':len(g),'Breakout %':round((g['Breakout']=='YES').mean()*100,1),'Win %':round((gt['Trade Return %']>0).mean()*100,1) if not gt.empty else np.nan,'Avg trade %':round(gt['Trade Return %'].mean(),2) if not gt.empty else np.nan})
                st.markdown('**Score-band results**')
                st.dataframe(pd.DataFrame(band_rows),width='stretch',hide_index=True)
                st.dataframe(bt.sort_values('Setup Date',ascending=False).head(300),width='stretch',hide_index=True)
                st.download_button('⬇️ Download V8.6 validation CSV',bt.to_csv(index=False).encode(),'v8_6_validation.csv','text/csv')
        else:
            st.info('No historical setup events collected yet. Run the current batch; V8.6 evaluates every valid historical day in the 5-year window for each successfully downloaded stock.')
        if st.session_state.val_failed:
            st.caption(f'Yahoo data unavailable for {len(st.session_state.val_failed)} symbols so far; these are skipped, not treated as zero-performance setups.')

    st.markdown('### ⚠️ How to use the result')
    st.warning('A scanner result is a watchlist candidate, not an automatic buy. Wait for a daily closing breakout above the Pivot and confirm volume/liquidity, entry, stop and position-size rules before trading.')

if __name__=='__main__': main()
