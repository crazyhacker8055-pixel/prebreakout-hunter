from __future__ import annotations
import gzip, io, os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta, datetime
from urllib.parse import quote
import numpy as np
import pandas as pd
import requests
import streamlit as st
import plotly.graph_objects as go

APP='StockPilot AI'; BASE='https://api.upstox.com/v3'
MASTER_URLS=['https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz','https://assets.upstox.com/market-quote/instruments/exchange/complete.json']
st.set_page_config(page_title=APP,page_icon='📈',layout='wide',initial_sidebar_state='expanded')
st.markdown('''<style>.block-container{padding:1rem;max-width:1500px}[data-testid="stSidebar"]{background:#07111d}div[data-testid="stMetric"]{background:#0b1420;border:1px solid #1d2b3a;padding:10px;border-radius:12px}@media(max-width:700px){.block-container{padding:.55rem}h1{font-size:1.7rem}}</style>''',unsafe_allow_html=True)

def token():
    try:
        x=st.secrets.get('UPSTOX_ACCESS_TOKEN')
        if x:return str(x)
    except Exception:pass
    return os.getenv('UPSTOX_ACCESS_TOKEN')
TOKEN=token()
class Upstox:
    def __init__(self,t):self.h={'Accept':'application/json','Authorization':f'Bearer {t}'}
    def get(self,path,params=None):
        r=requests.get(BASE+path,headers=self.h,params=params,timeout=30);r.raise_for_status();j=r.json()
        if j.get('status') not in (None,'success'):raise RuntimeError(str(j))
        return j
    def candles(self,key,days=420):
        e=date.today();s=e-timedelta(days=days);k=quote(key,safe='')
        return candles_df(self.get(f'/historical-candle/{k}/days/1/{e:%Y-%m-%d}/{s:%Y-%m-%d}'))
    def quotes(self,keys):return self.get('/market-quote/quotes',{'instrument_key':','.join(keys)}).get('data',{})
    def ohlc(self,keys):return self.get('/market-quote/ohlc',{'instrument_key':','.join(keys),'interval':'1d'}).get('data',{})

def candles_df(p):
    rows=p.get('data',{}).get('candles',[]); cols=['timestamp','open','high','low','close','volume','open_interest']
    d=pd.DataFrame([x[:7] for x in rows if len(x)>=6],columns=cols)
    if d.empty:return d
    d.timestamp=pd.to_datetime(d.timestamp,errors='coerce')
    for c in cols[1:]:d[c]=pd.to_numeric(d[c],errors='coerce')
    return d.dropna(subset=['timestamp','close']).sort_values('timestamp').drop_duplicates('timestamp').reset_index(drop=True)

def indicators(d):
    x=d.copy();x['sma50']=x.close.rolling(50).mean();x['sma150']=x.close.rolling(150).mean();x['ema220']=x.close.ewm(span=220,adjust=False).mean();x['low52']=x.low.rolling(252,min_periods=200).min();x['high52']=x.high.rolling(252,min_periods=200).max();x['avgvol20']=x.volume.rolling(20).mean();x['range_pct']=(x.high-x.low)/x.close.replace(0,np.nan);x['avg_range20']=x.range_pct.rolling(20).mean();return x

def evaluate(d,price=None,volume=None):
    x=indicators(d)
    if len(x)<260:return {'status':'INSUFFICIENT DATA','score':0,'reason':f'{len(x)} daily candles; need 260'}
    r=x.iloc[-1];p=float(price if price is not None else r.close);v=float(volume if volume is not None else r.volume)
    cond={'150 SMA > 220 EMA':r.sma150>r.ema220,'Price > 50 SMA':p>r.sma50,'50 SMA > 150 SMA':r.sma50>r.sma150,'Price > 1.25 x 52W low':pd.notna(r.low52) and p>1.25*r.low52,'220 EMA dip in last 90 sessions':(x.tail(90).low<x.tail(90).ema220).any()}
    completed=x.iloc[:-1]
    if len(completed) < 253:
        return {'status':'INSUFFICIENT DATA','score':0,'reason':'Need at least 253 completed daily candles.'}
    last_completed=completed.iloc[-1]
    breakout=float(completed.iloc[:-1].tail(252).high.max())
    last_close=float(last_completed.close)
    confirmed=last_close>breakout
    above=p>breakout
    dist=(p/breakout-1)*100 if breakout else np.nan
    near=not confirmed and .97*breakout<=p<=1.03*breakout
    vol_ok=pd.notna(r.avgvol20) and v>=.8*r.avgvol20;contract=pd.notna(r.avg_range20) and pd.notna(r.range_pct) and r.range_pct<=1.25*r.avg_range20
    score=int(round(min(100,sum(cond.values())/5*70+(10 if vol_ok else 0)+(10 if contract else 0)+(10 if confirmed else 5 if near else 0))))
    if confirmed and all(cond.values()):status='TRIGGERED'
    elif all(cond.values()) and near:status='READY'
    elif sum(cond.values())>=4 and above:status='BREAKOUT WATCH'
    elif sum(cond.values())>=4:status='FORMING'
    else:status='NEAR MISS'
    swing=float(x.tail(20).low.min());stop=max(0,min(p*.85,swing*.99))
    return {'status':status,'score':score,'price':p,'breakout_level':breakout,'distance_pct':dist,'stop':stop,'target1':max(breakout,p)*1.08,'target2':max(breakout,p)*1.15,'ema220':float(r.ema220),'sma50':float(r.sma50),'sma150':float(r.sma150),'low52':float(r.low52),'high52':float(r.high52),'volume':v,'avgvol20':float(r.avgvol20) if pd.notna(r.avgvol20) else np.nan,'confirmed_close':confirmed,'conditions':cond,'volume_ratio':(v/r.avgvol20 if pd.notna(r.avgvol20) and r.avgvol20 else np.nan),'range_ratio':(r.range_pct/r.avg_range20 if pd.notna(r.avg_range20) and r.avg_range20 else np.nan)}

@st.cache_data(ttl=3600,show_spinner=False)
def master():
    err=None
    for u in MASTER_URLS:
        try:
            raw=requests.get(u,timeout=60).content
            if u.endswith('.gz'):raw=gzip.decompress(raw)
            return pd.read_json(io.BytesIO(raw)).drop_duplicates('instrument_key').reset_index(drop=True)
        except Exception as e:err=e
    raise RuntimeError(f'Instrument master failed: {err}')

def equity_universe(m):
    x=m[m.segment.astype(str).eq('NSE_EQ')].copy() if 'segment' in m else m.iloc[0:0].copy()
    if 'instrument_type' in x:x=x[x.instrument_type.astype(str).eq('EQ')]
    return x.drop_duplicates('instrument_key').reset_index(drop=True)

def index_keys(m):
    x=m[m.segment.astype(str).eq('NSE_INDEX')].copy() if 'segment' in m else m.iloc[0:0].copy();out={'NIFTY 50':'NSE_INDEX|Nifty 50','BANK NIFTY':'NSE_INDEX|Nifty Bank','NIFTY MIDCAP 100':'NSE_INDEX|Nifty Midcap 100','NIFTY SMALLCAP 100':'NSE_INDEX|Nifty Smallcap 100','INDIA VIX':'NSE_INDEX|India VIX'}
    if not x.empty:
        cols=[c for c in ['name','short_name','trading_symbol'] if c in x]
        if cols:
            txt=x[cols].fillna('').astype(str).agg(' | '.join,axis=1)
            aliases={'NIFTY 50':['Nifty 50','NIFTY 50'], 'BANK NIFTY':['Nifty Bank','NIFTY BANK'], 'NIFTY MIDCAP 100':['Nifty Midcap 100','NIFTY MIDCAP 100','NIFTYMIDCAP100'], 'NIFTY SMALLCAP 100':['Nifty Smallcap 100','Nifty Smlcap 100','NIFTY SMLCAP 100','NIFTY SMALLCAP 100','NIFTYSMLCAP100'], 'INDIA VIX':['India VIX','INDIA VIX']}
            for lab,alts in aliases.items():
                for a in alts:
                    h=x[txt.str.contains(a,case=False,regex=False)]
                    if not h.empty:
                        out[lab]=str(h.iloc[0].instrument_key);break
    return out

def fields(item):
    if not item:return None,None,None
    lo=item.get('live_ohlc') or item.get('ohlc') or {}
    p=item.get('last_price')
    if p is None:p=lo.get('close')
    v=item.get('volume')
    if v is None:v=lo.get('volume')
    prev=item.get('prev_ohlc') or {}
    pr=prev.get('close')
    if pr is None:pr=item.get('prev_close_price')
    return (float(p) if p is not None else None,float(v) if v is not None else None,float(pr) if pr is not None else None)

def normalize_quote_map(raw):
    out={}
    for response_key,item in (raw or {}).items():
        if not isinstance(item,dict):
            continue
        out[str(response_key)] = item
        token_key=item.get('instrument_token') or item.get('instrument_key')
        if token_key:
            token_key=str(token_key)
            out[token_key]=item
            out[token_key.replace('|',':')]=item
    return out

def quote_for(qmap,key):
    k=str(key)
    return qmap.get(k) or qmap.get(k.replace('|',':')) or {}

@st.cache_data(ttl=45,show_spinner=False)
def all_quotes(_token,keys):
    u=Upstox(_token);raw={};ks=list(keys)
    for i in range(0,len(ks),500):raw.update(u.quotes(ks[i:i+500]))
    return normalize_quote_map(raw)
@st.cache_data(ttl=45,show_spinner=False)
def index_quotes(_token,keys):
    return normalize_quote_map(Upstox(_token).ohlc(list(keys)))
@st.cache_data(ttl=1800,show_spinner=False)
def history(_token,key):return Upstox(_token).candles(key)

def candidates(uni,quotes,limit):
    rows=[]
    for r in uni.itertuples(index=False):
        q=quote_for(quotes,str(r.instrument_key));p,v,_=fields(q)
        if p is None:continue
        yl=q.get('year_low');yh=q.get('year_high')
        if yl is None or yh is None or float(yl)<=0 or float(yh)<=0:continue
        yl=float(yl);yh=float(yh)
        if p>1.25*yl and p/yh>=.90:rows.append({'row':r,'quote':q,'near_high':p/yh,'volume':v or 0})
    rows.sort(key=lambda z:(z['near_high'],z['volume']),reverse=True)
    return rows[:limit]

def scan_one(token,row,q):
    try:
        d=history(token,str(row.instrument_key));p,v,_=fields(q)
        if d.empty:return {'symbol':str(row.trading_symbol),'status':'NO DATA','score':0,'instrument_key':str(row.instrument_key)}
        r=evaluate(d,p,v);r.update(symbol=str(row.trading_symbol),instrument_key=str(row.instrument_key));return r
    except Exception as e:return {'symbol':str(row.trading_symbol),'status':'ERROR','score':0,'instrument_key':str(row.instrument_key),'reason':str(e)[:180]}

def full_scan(token,uni,limit,workers):
    quotes=all_quotes(token,tuple(uni.instrument_key.astype(str)));cand=candidates(uni,quotes,limit);results=[]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        fs=[ex.submit(scan_one,token,z['row'],z['quote']) for z in cand]
        for f in as_completed(fs):results.append(f.result())
    if not results:return pd.DataFrame(),len(quotes),len(cand)
    order={'TRIGGERED':0,'READY':1,'BREAKOUT WATCH':2,'FORMING':3,'NEAR MISS':4,'INSUFFICIENT DATA':5,'NO DATA':6,'ERROR':7};df=pd.DataFrame(results);df['_o']=df.status.map(order).fillna(99);return df.sort_values(['_o','score','distance_pct'],ascending=[True,False,False]).drop(columns='_o').reset_index(drop=True),len(uni),len(cand)

with st.sidebar:
    st.markdown('## 📈 StockPilot AI');st.caption('Indian Market Intelligence')
    page=st.radio('Navigation',['Dashboard','Live Scanner','Top Setups','Watchlist','Portfolio','SMC Charts','News & Alerts','Results Calendar','Corporate Actions','FII/DII Data','Sector Heatmap','Backtesting','Settings'],label_visibility='collapsed')
    st.divider()
    if TOKEN:
        st.success('🟢 UPSTOX CONNECTED')
    else:
        st.error('🔴 UPSTOX TOKEN REQUIRED')
st.title(APP);st.caption('Pre-breakout intelligence • SMC • market structure • risk-aware setups')
if not TOKEN:st.warning('Add UPSTOX_ACCESS_TOKEN to Streamlit Secrets. No fake market data is used.');st.stop()
try:m=master();uni=equity_universe(m);iks=index_keys(m)
except Exception as e:st.error(f'Instrument master error: {e}');st.stop()
labels=['NIFTY 50','BANK NIFTY','NIFTY MIDCAP 100','NIFTY SMALLCAP 100','INDIA VIX']
try:iq=index_quotes(TOKEN,tuple(iks[x] for x in labels))
except Exception as e:iq={};st.error(f'Index quote error: {e}')
cs=st.columns(5)
for c,l in zip(cs,labels):
    p,_,pr=fields(quote_for(iq,iks[l]));d=(p-pr)/pr*100 if p is not None and pr else None
    with c:st.metric(l,f'{p:,.2f}' if p is not None else 'N/A',f'{d:+.2f}%' if d is not None else None)
st.divider()

def status_badge(status):
    icons={'TRIGGERED':'🟢','READY':'🟡','BREAKOUT WATCH':'🔵','FORMING':'🟣','NEAR MISS':'⚪'}
    return f"{icons.get(status,'⚪')} {status}"

def money(x):
    return f'₹{x:,.2f}' if pd.notna(x) else '—'

def setup_chart(token,key,symbol,days=180):
    d=history(token,key).tail(days).copy();x=indicators(d)
    fig=go.Figure()
    fig.add_trace(go.Candlestick(x=x.timestamp,open=x.open,high=x.high,low=x.low,close=x.close,name='Price'))
    fig.add_trace(go.Scatter(x=x.timestamp,y=x.sma50,name='SMA 50',line={'width':1.4}))
    fig.add_trace(go.Scatter(x=x.timestamp,y=x.sma150,name='SMA 150',line={'width':1.4}))
    fig.add_trace(go.Scatter(x=x.timestamp,y=x.ema220,name='EMA 220',line={'width':1.8}))
    return x,fig

if page=='Dashboard':
    st.markdown('### 🔥 Highest Conviction Setups')
    df=st.session_state.get('scan_results')
    if df is None or df.empty:
        st.info('Run the Full NSE Live Scan to populate fresh Upstox setups.')
    else:
        ranked=df.copy().head(10)
        st.caption('Strict ranking: strategy conditions → breakout proximity → volume/range quality. Only the strongest setups are shown.')
        for _,r in ranked.iterrows():
            with st.container(border=True):
                a,b,c,d=st.columns([1.5,1.2,1.2,1.2])
                a.markdown(f"### {r.symbol}")
                a.caption(status_badge(r.status))
                b.metric('Score',int(r.score))
                c.metric('Live price',money(r.price))
                d.metric('Breakout',money(r.breakout_level),f"{r.distance_pct:+.2f}%")
                e,f,g,h=st.columns(4)
                e.metric('Stop',money(r.stop))
                f.metric('Target 1',money(r.target1))
                g.metric('Target 2',money(r.target2))
                vr=r.get('volume_ratio',np.nan)
                h.metric('Vol / Avg20',f"{vr:.2f}x" if pd.notna(vr) else '—')
                st.caption(f"EMA220 {money(r.ema220)} • SMA50 {money(r.sma50)} • SMA150 {money(r.sma150)} • 52W high {money(r.high52)} • 52W low {money(r.low52)}")
        st.markdown('#### Quick ranking')
        show=ranked[['symbol','status','score','price','breakout_level','distance_pct','stop','target1','target2']].copy()
        show.columns=['Stock','Status','Score','Live Price','Breakout','Distance %','Stop','Target 1','Target 2']
        for col in ['Live Price','Breakout','Stop','Target 1','Target 2']:show[col]=show[col].map(lambda x:round(float(x),2) if pd.notna(x) else np.nan)
        st.dataframe(show,use_container_width=True,hide_index=True)
    st.success(f'🟢 Live Upstox connection • {datetime.now():%d-%m-%Y %H:%M:%S}')
elif page=='Live Scanner':
    st.markdown('### ⚡ Live Scanner');st.caption('Every run pulls fresh Upstox quotes for the NSE equity universe, then calculates the strict strategy only for the strongest live candidates.')
    st.write(f'NSE equity universe: **{len(uni):,} instruments** • full quotes: **500 instruments/request**')
    st.caption('Pipeline: full NSE universe → live Upstox quote filter → strongest candidates → daily-history validation.')
    st.info('Pre-filter: live price > 1.25× trailing year low and within 10% of trailing year high. Then the full daily-history strategy is evaluated. No fake data is inserted.')
    limit=st.slider('Historical candidates after live pre-filter',50,300,150,25);workers=st.slider('Concurrent historical requests',2,8,6)
    if st.button('🚀 RUN FULL NSE LIVE SCAN',type='primary',use_container_width=True):
        with st.spinner('Pulling live Upstox quotes and calculating candidates...'):
            try:df,nq,ncand=full_scan(TOKEN,uni,limit,workers)
            except Exception as e:df=pd.DataFrame();nq=ncand=0;st.error(f'Scan failed: {e}')
        st.session_state.scan_results=df;st.session_state.scan_time=datetime.now();st.session_state.quote_count=nq;st.session_state.candidate_count=ncand
        if df.empty:
            st.warning('No strategy candidates returned. Live quotes were received, but none passed the live 52-week pre-filter/history stage. No fake data was inserted.')
        else:st.success(f'Scan complete: {nq:,} NSE instruments checked → {ncand:,} live candidates selected → historical strategy evaluated.')
    df=st.session_state.get('scan_results')
    if df is not None and not df.empty:
        cols=[c for c in ['symbol','status','score','price','breakout_level','distance_pct','stop','target1','target2','ema220','sma50','sma150','low52','high52','confirmed_close'] if c in df];st.dataframe(df[cols],use_container_width=True,hide_index=True)
        a,b,c=st.columns(3);a.metric('Triggered',int((df.status=='TRIGGERED').sum()));b.metric('Ready',int((df.status=='READY').sum()));c.metric('Forming',int((df.status=='FORMING').sum()));st.caption(f"Last scan: {st.session_state.get('scan_time')} • Source: Upstox V3")
elif page=='Top Setups':
    df=st.session_state.get('scan_results')
    if df is None or df.empty:st.info('Run the Live Scanner first.')
    else:
        st.markdown('### 🏆 Top Setups')
        for _,r in df.head(20).iterrows():
            st.markdown(f"**{r.symbol}** — {status_badge(r.status)} — Score **{int(r.score)}** — Entry/live **{money(r.price)}** — Breakout **{money(r.breakout_level)}** — Stop **{money(r.stop)}** — T1 **{money(r.target1)}** — T2 **{money(r.target2)}**")
            st.divider()
elif page=='SMC Charts':
    df=st.session_state.get('scan_results')
    if df is None or df.empty:st.info('Run the Live Scanner first.')
    else:
        st.markdown('### 📊 SMC / Market Structure Chart')
        s=st.selectbox('Select scanned stock',df.symbol.astype(str).tolist())
        r=df[df.symbol.astype(str)==s].iloc[0]
        x,fig=setup_chart(TOKEN,str(r.instrument_key),s,180)
        # Breakout reference from the evaluated completed history.
        fig.add_hline(y=float(r.breakout_level),line_dash='dash',annotation_text='52W breakout',annotation_position='top left')
        fig.add_hline(y=float(r.ema220),line_dash='dot',annotation_text='EMA220',annotation_position='bottom right')
        # Lightweight, transparent structure markers: confirmed higher-high break and recent swing points.
        if len(x)>=12:
            highs=x.high.rolling(5,center=True).max();lows=x.low.rolling(5,center=True).min()
            swing_hi=x[(x.high==highs)&highs.notna()].tail(8);swing_lo=x[(x.low==lows)&lows.notna()].tail(8)
            if not swing_hi.empty:
                fig.add_trace(go.Scatter(x=swing_hi.timestamp,y=swing_hi.high,mode='markers',name='Swing High',marker={'size':7,'symbol':'triangle-up'}))
            if not swing_lo.empty:
                fig.add_trace(go.Scatter(x=swing_lo.timestamp,y=swing_lo.low,mode='markers',name='Swing Low',marker={'size':7,'symbol':'triangle-down'}))
        fig.update_layout(template='plotly_dark',height=560,margin={'l':10,'r':10,'t':45,'b':10},title=f'{s} • Daily structure')
        st.plotly_chart(fig,use_container_width=True)
        a,b,c,d=st.columns(4);a.metric('Status',r.status);b.metric('Score',int(r.score));c.metric('Live',money(r.price));d.metric('Breakout',money(r.breakout_level),f"{r.distance_pct:+.2f}%")
        st.markdown('#### Strategy checklist')
        cond=r.get('conditions',{})
        for label,ok in cond.items():st.write(('✅' if bool(ok) else '❌')+f' {label}')
        st.caption('Chart overlays show SMA50, SMA150, EMA220, breakout reference and swing structure. FVG/order-block/BOS/CHOCH detection will be expanded in the next SMC build; no pattern is fabricated.')
elif page=='Settings':
    st.markdown('### ⚙️ Settings');st.write(f'NSE equity universe: **{len(uni):,}**');st.write('Upstox API: **V3**');st.write('Order execution: **Disabled**');st.write('Token display: **Never**')
else:st.info(f'{page} module is queued for the next build stage. Live Upstox connectivity remains active.')
st.caption('StockPilot AI • Upstox V3 • No orders are executed automatically.')
