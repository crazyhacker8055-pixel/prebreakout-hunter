import streamlit as st
import pandas as pd
import numpy as np
import requests, gzip, json, hashlib, time
from io import StringIO
from datetime import datetime, timedelta, timezone, time as dtime
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote
from zoneinfo import ZoneInfo

st.set_page_config(page_title="Pre-Breakout Hunter V11.0", page_icon="🎯", layout="wide")

APP_VERSION = "V11.0"
IST = ZoneInfo("Asia/Kolkata")
MIN_BARS = 230
HISTORY_CAL_DAYS = 390
HISTORY_CACHE_TTL = 900
WORKERS = 12
MAX_REQUESTS_PER_SEC = 35
REQUEST_INTERVAL = 1.0 / MAX_REQUESTS_PER_SEC

NIFTY500_URLS = [
    "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv",
    "https://raw.githubusercontent.com/pkjmesra/PKScreener/main/results/Indices/ind_nifty500list.csv",
]
INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
SEARCH_URL = "https://api.upstox.com/v2/instruments/search"
HISTORY_URL = "https://api.upstox.com/v3/historical-candle"
LTP_URL = "https://api.upstox.com/v3/market-quote/ltp"

DEFAULTS = dict(min_score=84, range5=5.0, range10=8.5, range20=13.5, atr_ratio=0.78, vol_ratio=0.78, pivot_min=0.20, pivot_max=3.50)


def now_ist():
    return datetime.now(timezone.utc).astimezone(IST)


def completed_session_date():
    n = now_ist()
    # During the NSE session and pre-open, today's candle is incomplete.
    # After 15:30 IST, today's completed daily candle can be used.
    if n.weekday() >= 5 or n.time() >= dtime(15, 35):
        return n.date()
    return (n - timedelta(days=1)).date()


def token():
    try:
        return str(st.secrets.get("UPSTOX_ACCESS_TOKEN", "")).strip()
    except Exception:
        return ""


def token_fp():
    return hashlib.sha256(token().encode()).hexdigest()[:12]


def auth_headers():
    return {"Accept": "application/json", "Authorization": f"Bearer {token()}"}


@st.cache_data(ttl=86400, show_spinner=False)
def get_universe():
    last = None
    for url in NIFTY500_URLS:
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 Pre-Breakout-Hunter-V11"}, timeout=20)
            r.raise_for_status()
            t = pd.read_csv(StringIO(r.text))
            cols = {str(c).strip().lower(): c for c in t.columns}
            sc = next((cols[k] for k in ("symbol", "ticker") if k in cols), None)
            sec = next((cols[k] for k in ("industry", "sector", "industry name", "industry_name") if k in cols), None)
            if not sc: raise RuntimeError("No Symbol column")
            out = pd.DataFrame({"Symbol": t[sc].astype(str).str.strip().str.upper()})
            out["Sector"] = t[sec].astype(str).str.strip() if sec else "Other / Unclassified"
            out = out[(out.Symbol != "") & (out.Symbol.str.lower() != "nan") & ~out.Symbol.str.startswith("DUMMY")]
            out = out.drop_duplicates("Symbol").head(500).reset_index(drop=True)
            if len(out) >= 490: return out
            last = f"only {len(out)} symbols"
        except Exception as e:
            last = str(e)
    raise RuntimeError(f"Could not load NIFTY 500 list: {last}")


@st.cache_data(ttl=86400, show_spinner=False)
def get_instruments():
    r = requests.get(INSTRUMENTS_URL, headers={"User-Agent":"Mozilla/5.0 Pre-Breakout-Hunter-V11"}, timeout=30)
    r.raise_for_status()
    data = json.loads(gzip.decompress(r.content).decode("utf-8"))
    rows=[]
    for x in data:
        if not isinstance(x,dict) or x.get("segment") != "NSE_EQ": continue
        if str(x.get("instrument_type","")).upper() not in {"EQ","A","X"}: continue
        sym=str(x.get("trading_symbol","")).strip().upper(); key=str(x.get("instrument_key","")).strip()
        if sym and key: rows.append((sym,key))
    return pd.DataFrame(rows, columns=["Symbol","instrument_key"]).drop_duplicates("Symbol")


def repair_mapping(mapping):
    out=mapping.copy(); repaired=[]
    missing=out[out.instrument_key.isna()]["Symbol"].tolist()
    for sym in missing:
        try:
            r=requests.get(SEARCH_URL,headers=auth_headers(),params={"query":sym,"exchanges":"NSE","segments":"EQ","records":30,"page_number":1},timeout=8)
            if r.status_code!=200: continue
            for item in (r.json() or {}).get("data",[]):
                if isinstance(item,dict) and item.get("segment")=="NSE_EQ" and str(item.get("trading_symbol","")).upper()==sym:
                    key=str(item.get("instrument_key","")).strip()
                    if key:
                        out.loc[out.Symbol==sym,"instrument_key"]=key; repaired.append(sym)
                    break
        except Exception: pass
    return out,repaired


def build_mapping():
    c=get_universe(); m=get_instruments(); out=c.merge(m,on="Symbol",how="left"); return repair_mapping(out)


def parse_daily(payload):
    candles=((payload or {}).get("data") or {}).get("candles")
    if not isinstance(candles,list): return pd.DataFrame()
    rows=[]
    for row in candles:
        if not isinstance(row,(list,tuple)) or len(row)<6: continue
        try:
            ts=pd.to_datetime(row[0],utc=True).tz_convert(IST).date()
            vals=[float(row[i]) for i in range(1,6)]
            rows.append([pd.Timestamp(ts),*vals])
        except Exception: continue
    if not rows: return pd.DataFrame()
    return pd.DataFrame(rows,columns=["date","open","high","low","close","volume"]).drop_duplicates("date").set_index("date").sort_index()


def fetch_one(symbol,key,start,end):
    url=f"{HISTORY_URL}/{quote(key,safe='')}/days/1/{end}/{start}"
    try:
        r=requests.get(url,headers=auth_headers(),timeout=12)
        if r.status_code!=200: return symbol,pd.DataFrame(),f"HTTP {r.status_code}"
        df=parse_daily(r.json())
        # Never use today's incomplete candle. end is already the last completed session date.
        df=df[df.index.date<=end]
        if len(df)<MIN_BARS: return symbol,df,f"Only {len(df)} completed daily bars"
        return symbol,df,""
    except Exception as e: return symbol,pd.DataFrame(),str(e)[:160]


@st.cache_data(ttl=HISTORY_CACHE_TTL, show_spinner=False)
def fetch_histories(mapping, asof_date, token_fingerprint):
    end=asof_date
    start=end-timedelta(days=HISTORY_CAL_DAYS)
    jobs=list(mapping.dropna(subset=["instrument_key"])[["Symbol","instrument_key"]].itertuples(index=False,name=None))
    frames={}; errors={}; total=len(jobs); done=0; last_req=[0.0]
    def job(item):
        wait=REQUEST_INTERVAL-(time.monotonic()-last_req[0])
        if wait>0: time.sleep(wait)
        last_req[0]=time.monotonic()
        return fetch_one(item[0],item[1],start.isoformat(),end.isoformat())
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs=[ex.submit(job,x) for x in jobs]
        for fut in as_completed(futs):
            s,df,err=fut.result(); done+=1
            if not df.empty: frames[s]=df
            if err: errors[s]=err
    return frames,errors,start,end


@st.cache_data(ttl=HISTORY_CACHE_TTL, show_spinner=False)
def fetch_nifty(start,end,token_fingerprint):
    # Upstox index key used by the account/API.
    key="NSE_INDEX|Nifty 50"
    url=f"{HISTORY_URL}/{quote(key,safe='')}/days/1/{end}/{start}"
    r=requests.get(url,headers=auth_headers(),timeout=12)
    if r.status_code!=200: raise RuntimeError(f"NIFTY 50 HTTP {r.status_code}")
    df=parse_daily(r.json())
    df=df[df.index.date<=end]
    if len(df)<MIN_BARS: raise RuntimeError(f"NIFTY 50 returned only {len(df)} bars")
    return df


def atr(df,n):
    prev=df.close.shift(1)
    tr=pd.concat([(df.high-df.low),(df.high-prev).abs(),(df.low-prev).abs()],axis=1).max(axis=1)
    return tr.rolling(n,min_periods=n).mean()


def metrics(d,nifty):
    c,h,l,v=d.close,d.high,d.low,d.volume
    sma20=c.rolling(20).mean(); sma50=c.rolling(50).mean(); sma150=c.rolling(150).mean(); sma200=c.rolling(200).mean()
    ema20=c.ewm(span=20,adjust=False).mean(); a5=atr(d,5); a20=atr(d,20)
    r5=(h.iloc[-5:].max()-l.iloc[-5:].min())/l.iloc[-5:].min()*100
    r10=(h.iloc[-10:].max()-l.iloc[-10:].min())/l.iloc[-10:].min()*100
    r20=(h.iloc[-20:].max()-l.iloc[-20:].min())/l.iloc[-20:].min()*100
    pivot=h.iloc[-21:-1].max(); close=float(c.iloc[-1]); distance=(pivot-close)/pivot*100 if pivot else np.nan
    vr=float(v.iloc[-5:].mean()/v.iloc[-20:].mean()) if v.iloc[-20:].mean() else np.inf
    ar=float(a5.iloc[-1]/a20.iloc[-1]) if a20.iloc[-1] else np.inf
    nn=nifty.reindex(d.index).ffill(); rs=np.nan
    if len(nn.dropna())>=22: rs=((close/c.iloc[-21]-1)-(nn.iloc[-1]/nn.iloc[-21]-1))*100
    prev20=h.rolling(20).max().shift(1)
    near=((c.iloc[-20:]>=prev20.iloc[-20:]*0.985)&(c.iloc[-20:]<prev20.iloc[-20:])).sum()
    lows=[l.iloc[-5:].min(),l.iloc[-10:-5].min(),l.iloc[-15:-10].min()]
    higher_lows=lows[0]>=lows[1]*0.995 and lows[1]>=lows[2]*0.995
    bodies=(d.open-d.close).abs()/d.close*100; body_ratio=float(bodies.iloc[-5:].mean()/bodies.iloc[-20:].mean()) if bodies.iloc[-20:].mean() else np.inf
    base=d.iloc[-20:]; down=base.loc[base.close<base.open,"volume"].mean(); up=base.loc[base.close>=base.open,"volume"].mean(); dist_ratio=float(down/up) if up else np.inf
    high252=h.iloc[-252:].max(); low252=l.iloc[-252:].min(); pos52=(close-low252)/(high252-low252)*100 if high252>low252 else np.nan
    to_52w=(high252-close)/high252*100 if high252 else np.nan
    base_pos=(close-l.iloc[-20:].min())/(h.iloc[-20:].max()-l.iloc[-20:].min()) if h.iloc[-20:].max()>l.iloc[-20:].min() else 0
    return locals()


def score_stock(d,nifty,cfg):
    if len(d)<MIN_BARS:return None,None
    m=metrics(d.sort_index(),nifty); x=d.iloc[-1]
    trend=bool(x.close>m["sma50"].iloc[-1]>m["sma150"].iloc[-1]>m["sma200"].iloc[-1] and m["sma200"].iloc[-1]>m["sma200"].iloc[-21] and x.close>m["ema20"].iloc[-1])
    contraction=bool(m["r5"]<m["r10"]<m["r20"] and m["r5"]<=cfg["range5"] and m["r10"]<=cfg["range10"] and m["r20"]<=cfg["range20"])
    compression=bool(m["ar"]<=cfg["atr_ratio"] and m["vr"]<=cfg["vol_ratio"] and m["body_ratio"]<=0.85)
    pivot=bool(cfg["pivot_min"]<=m["distance"]<=cfg["pivot_max"] and x.close<m["pivot"])
    near_52w=bool(0.0 <= m["to_52w"] <= 6.0)
    no_break=bool((d.close.iloc[-5:]<d.high.rolling(20).max().shift(1).iloc[-5:]).all())
    gates={"Trend":trend,"Contraction":contraction,"Compression":compression,"Pivot":pivot,"No breakout":no_break,"Near 52W high":near_52w,"Higher lows":bool(m["higher_lows"]),"No distribution":m["dist_ratio"]<=1.20,"Relative strength":bool(m["rs"]>=0.5)}
    score=0
    score+=12 if trend else 0; score+=12 if contraction else 0; score+=12 if compression else 0
    score+=12 if m["near"]>=3 else (8 if m["near"]==2 else 4 if m["near"]==1 else 0)
    score+=10 if m["distance"]<=1.25 else (7 if m["distance"]<=2 else 4 if m["distance"]<=cfg["pivot_max"] else 0)
    score+=7 if m["to_52w"]<=2 else (4 if m["to_52w"]<=4 else 2 if m["to_52w"]<=6 else 0)
    score+=10 if m["ar"]<=0.65 else (6 if m["ar"]<=cfg["atr_ratio"] else 0)
    score+=8 if m["vr"]<=0.60 else (5 if m["vr"]<=cfg["vol_ratio"] else 0)
    score+=8 if m["higher_lows"] else 0; score+=8 if m["rs"]>=4 else (5 if m["rs"]>=2 else 2 if m["rs"]>=0 else 0)
    score+=5 if m["base_pos"]>=0.65 else (3 if m["base_pos"]>=0.55 else 0)
    score+=3 if m["dist_ratio"]<=0.90 else (1 if m["dist_ratio"]<=1.20 else 0)
    result={"Score":int(score),"Close":round(float(x.close),2),"Pivot":round(float(m["pivot"]),2),"To Pivot %":round(float(m["distance"]),2),"5D Range %":round(float(m["r5"]),2),"10D Range %":round(float(m["r10"]),2),"20D Range %":round(float(m["r20"]),2),"ATR5/ATR20":round(float(m["ar"]),2),"Vol5/Vol20":round(float(m["vr"]),2),"Resistance Tests":int(m["near"]),"RS vs Nifty %":round(float(m["rs"]),2),"52W Position %":round(float(m["pos52"]),1),"To 52W High %":round(float(m["to_52w"]),2),"Higher Lows":"YES" if m["higher_lows"] else "NO"}
    if all(gates.values()) and score>=cfg["min_score"]: return result,gates
    return None,gates


def near_miss(d,nifty,cfg):
    if len(d)<MIN_BARS:return None
    m=metrics(d.sort_index(),nifty); x=d.iloc[-1]
    score=0
    score+=12 if x.close>m["sma50"].iloc[-1]>m["sma150"].iloc[-1]>m["sma200"].iloc[-1] else 0
    score+=12 if m["r5"]<m["r10"]<m["r20"] else 0
    score+=10 if m["ar"]<=0.85 else 0; score+=10 if m["vr"]<=0.85 else 0; score+=10 if m["higher_lows"] else 0
    score+=10 if m["near"]>=2 else (5 if m["near"]==1 else 0); score+=10 if 0<m["distance"]<=5 else 0; score+=10 if m["rs"]>=0 else 0; score+=8 if m["base_pos"]>=0.60 else 0; score+=5 if m["body_ratio"]<=1 else 0
    if score<68:return None
    return {"Score":int(score),"Close":round(float(x.close),2),"To Pivot %":round(float(m["distance"]),2),"5D Range %":round(float(m["r5"]),2),"10D Range %":round(float(m["r10"]),2),"20D Range %":round(float(m["r20"]),2),"ATR5/ATR20":round(float(m["ar"]),2),"Vol5/Vol20":round(float(m["vr"]),2),"Resistance Tests":int(m["near"]),"RS vs Nifty %":round(float(m["rs"]),2)}


def fetch_ltp(keys):
    rows={}; errs=[]
    for i in range(0,len(keys),200):
        try:
            r=requests.get(LTP_URL,headers=auth_headers(),params={"instrument_key":",".join(keys[i:i+200])},timeout=10)
            if r.status_code!=200: errs.append(f"HTTP {r.status_code}"); continue
            for item in ((r.json() or {}).get("data") or {}).values():
                if isinstance(item,dict) and item.get("instrument_token"): rows[str(item["instrument_token"])]=item
        except Exception as e: errs.append(str(e)[:120])
    return rows,errs


def main():
    st.title(f"🎯 Pre-Breakout Hunter {APP_VERSION}")
    st.caption("Upstox-only • completed daily candles • deterministic pre-breakout structure • live LTP after scan")
    if not token(): st.error("UPSTOX_ACCESS_TOKEN is missing from Streamlit Secrets."); return
    try: universe=get_universe(); mapping,repaired=build_mapping()
    except Exception as e: st.error(f"Universe setup failed: {e}"); return
    mapped=mapping.dropna(subset=["instrument_key"]).copy()
    st.info(f"Universe: {len(universe)} NIFTY 500 constituents • Upstox mapping: {len(mapped)}/{len(universe)}")
    if repaired: st.success(f"Repaired exact Upstox mappings: {', '.join(repaired)}")
    missing=mapping[mapping.instrument_key.isna()]["Symbol"].tolist()
    if missing: st.warning(f"Unmapped and excluded: {', '.join(missing)}")
    if len(mapped)<490: st.error("Mapping coverage is below 490/500. Scan blocked to protect accuracy."); return
    with st.sidebar:
        st.header("Scanner settings")
        min_score=st.slider("Minimum score",80,100,DEFAULTS["min_score"])
        range5=st.number_input("5D max range %",2.0,8.0,DEFAULTS["range5"],0.25)
        range10=st.number_input("10D max range %",4.0,15.0,DEFAULTS["range10"],0.25)
        range20=st.number_input("20D max range %",7.0,25.0,DEFAULTS["range20"],0.5)
        atr_ratio=st.number_input("Max ATR5/ATR20",0.50,1.0,DEFAULTS["atr_ratio"],0.01)
        vol_ratio=st.number_input("Max Vol5/Vol20",0.40,1.0,DEFAULTS["vol_ratio"],0.01)
        pivot_min=st.number_input("Min distance to pivot %",0.10,3.0,DEFAULTS["pivot_min"],0.05)
        pivot_max=st.number_input("Max distance to pivot %",1.0,6.0,DEFAULTS["pivot_max"],0.25)
        cfg=dict(min_score=min_score,range5=range5,range10=range10,range20=range20,atr_ratio=atr_ratio,vol_ratio=vol_ratio,pivot_min=pivot_min,pivot_max=pivot_max)
        scan=st.button("🔎 SCAN NIFTY 500",type="primary",width="stretch")
    if "v11" not in st.session_state: st.session_state.v11=None
    if scan:
        asof=completed_session_date(); status=st.empty(); status.info(f"Scanning completed session {asof} using Upstox daily candles…")
        try:
            frames,errors,start,end=fetch_histories(mapping,asof,token_fp())
            nifty=fetch_nifty(start,end,token_fp())
            results=[]; near=[]
            for sym,df in frames.items():
                r,_=score_stock(df,nifty,cfg)
                if r: r["Stock"]=sym; results.append(r)
                else:
                    nm=near_miss(df,nifty,cfg)
                    if nm: nm["Stock"]=sym; near.append(nm)
            rdf=pd.DataFrame(results); ndf=pd.DataFrame(near)
            if not rdf.empty:rdf=rdf.sort_values(["Score","To Pivot %"],ascending=[False,True]).reset_index(drop=True)
            if not ndf.empty:ndf=ndf.sort_values(["Score","To Pivot %"],ascending=[False,True]).head(20).reset_index(drop=True)
            st.session_state.v11=dict(results=rdf,near=ndf,frames=frames,errors=errors,nifty=nifty,mapping=mapping,start=start,end=end,asof=asof,cfg=cfg,run_time=now_ist())
            status.success(f"Scan complete • {len(frames)}/{len(mapped)} usable histories • {len(rdf)} qualified setups • {len(ndf)} near-misses")
        except Exception as e:
            status.error(f"Scan failed: {e}"); return
    state=st.session_state.v11
    if state is None:
        st.markdown("### Final scanner design")
        st.markdown("**Trend → progressive contraction → ATR/volume compression → higher lows → repeated resistance → relative strength → resistance still ahead → no recent breakout.**")
        st.info("V11 uses only completed daily candles for the setup. Live LTP is used only after the scan to show the current market position.")
        return
    results,near,frames,errors,mapping=state["results"],state["near"],state["frames"],state["errors"],state["mapping"]
    live,live_err=fetch_ltp(mapping.dropna(subset=["instrument_key"]).instrument_key.astype(str).tolist())
    live_rows=[]
    for _,r in mapping.dropna(subset=["instrument_key"]).iterrows():
        q=live.get(str(r.instrument_key))
        if not q:continue
        lp,cp=q.get("last_price"),q.get("cp")
        if isinstance(lp,(int,float)) and isinstance(cp,(int,float)) and cp: live_rows.append({"Stock":r.Symbol,"Sector":r.Sector,"LTP":round(float(lp),2),"Live Change %":round((float(lp)/float(cp)-1)*100,2)})
    ldf=pd.DataFrame(live_rows)
    st.subheader("🟢 Upstox live market snapshot")
    a,b,c,d=st.columns(4); a.metric("Live quotes",f"{len(ldf)}/{len(mapped)}"); b.metric("A/A+ setups",len(results)); c.metric("Near-miss watchlist",len(near)); d.metric("Usable daily histories",f"{len(frames)}/{len(mapped)}")
    st.caption(f"Setup session: {state['asof']} • scan run: {state['run_time'].strftime('%Y-%m-%d %H:%M:%S %Z')}")
    if live_err: st.warning("Live LTP partial errors: "+"; ".join(live_err[:3]))
    if not results.empty:
        st.subheader("🎯 Best pre-breakout candidates")
        out=results.merge(ldf[["Stock","LTP","Live Change %"]],on="Stock",how="left") if not ldf.empty else results.copy()
        out["Live To Pivot %"]=(out["Pivot"]-out["LTP"])/out["Pivot"]*100 if "LTP" in out else np.nan
        cols=["Stock","Score","LTP","Live Change %","Close","Pivot","Live To Pivot %","To Pivot %","To 52W High %","5D Range %","10D Range %","20D Range %","ATR5/ATR20","Vol5/Vol20","Resistance Tests","RS vs Nifty %","52W Position %","Higher Lows"]
        st.dataframe(out[[c for c in cols if c in out.columns]],width="stretch",hide_index=True)
        st.success("These are watchlist candidates, not automatic buy signals. Confirmation remains a closing breakout above the pivot with acceptable volume/liquidity.")
    else: st.warning("No stock met every hard gate and the minimum score for this completed session.")
    if not near.empty:
        st.subheader("🟡 Near-miss watchlist")
        st.caption("Closest structural candidates that failed at least one final requirement. They are not buy signals.")
        st.dataframe(near,width="stretch",hide_index=True)
    with st.expander("🩺 Data health"):
        st.write(f"Usable completed histories: {len(frames)}/{len(mapped)}")
        st.write(f"Requested window: {state['start']} → {state['end']}")
        st.write(f"As-of completed session: {state['asof']}")
        if errors: st.dataframe(pd.DataFrame([{"Stock":k,"Reason":v} for k,v in errors.items()]),width="stretch",hide_index=True)
        else: st.success("All mapped stocks returned sufficient daily history.")
    with st.expander("📐 V11 rules"):
        st.markdown("""
- Trend: Close > SMA50 > SMA150 > SMA200, rising SMA200, Close > EMA20.
- Compression: 5D range < 10D range < 20D range, with configurable ceilings.
- Volatility: ATR5/ATR20 and 5D/20D volume contraction plus candle-body contraction.
- Structure: higher lows and repeated tests of the prior 20-session high.
- Position: price remains 0.2–3.5% below the prior 20-session pivot and within 6% of the 52-week high.
- Breakout protection: last five completed closes remain below their prior 20-session highs.
- Relative strength: stock must outperform NIFTY 50 over the recent 20-session window.
- Distribution: down-day volume cannot materially dominate up-day volume.
- Final score: minimum 84 by default; all hard gates must pass.
""")
    st.caption("V11 architecture: Upstox Analytics Token → exact NIFTY500 mapping → completed daily V3 history → deterministic scanner → fresh LTP snapshot. No Yahoo, WebSocket, intraday polling, or trading API.")

if __name__=="__main__": main()
