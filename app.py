import streamlit as st
import pandas as pd
import numpy as np
import requests, gzip, json, hashlib, time
from io import StringIO
from datetime import datetime, timedelta, timezone, time as dtime
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote
from zoneinfo import ZoneInfo

st.set_page_config(page_title="Pre-Breakout Hunter V15", page_icon="🎯", layout="wide")

APP_VERSION = "V15.0"
IST = ZoneInfo("Asia/Kolkata")
MIN_BARS = 230
HISTORY_CAL_DAYS = 760
BACKTEST_CAL_DAYS = 1825
HISTORY_CACHE_TTL = 1800
WORKERS = 16
REQUESTS_PER_SEC = 40
REQUEST_INTERVAL = 1.0 / REQUESTS_PER_SEC

NIFTY500_URLS = [
    "https://raw.githubusercontent.com/pkjmesra/PKScreener/main/results/Indices/ind_nifty500list.csv",
    "https://raw.githubusercontent.com/sswapnil2/tradingview-mcp-india/main/src/tradingview_mcp/coinlist/nse.txt",
]
INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
SEARCH_URL = "https://api.upstox.com/v2/instruments/search"
HISTORY_URL = "https://api.upstox.com/v3/historical-candle"
OHLC_URL = "https://api.upstox.com/v3/market-quote/ohlc"

DEFAULTS = dict(
    min_score=84, near_score=72, range5=5.0, range10=8.5, range20=13.5,
    atr_ratio=0.78, vol_ratio=0.78, pivot_min=0.20, pivot_max=3.50,
    backtest_days=1825, forward_days=10
)


def now_ist():
    return datetime.now(timezone.utc).astimezone(IST)


def last_completed_weekday():
    n = now_ist()
    # NSE regular equity session closes at 15:30 IST. Before the close, use the
    # previous weekday; on weekends, use the preceding Friday.
    if n.weekday() >= 5:
        return (n - timedelta(days=n.weekday() - 4)).date()
    if n.time() >= dtime(15, 35):
        return n.date()
    prev=n-timedelta(days=1)
    while prev.weekday()>=5:
        prev-=timedelta(days=1)
    return prev.date()


def token():
    try:
        return str(st.secrets.get("UPSTOX_ACCESS_TOKEN", "")).strip()
    except Exception:
        return ""


def token_fp():
    return hashlib.sha256(token().encode()).hexdigest()[:12]


def headers():
    return {"Accept": "application/json", "Authorization": f"Bearer {token()}"}


@st.cache_data(ttl=86400, show_spinner=False)
def get_universe():
    last = None
    for url in NIFTY500_URLS:
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 Pre-Breakout-Hunter"}, timeout=20)
            r.raise_for_status()
            text = r.text
            if url.endswith("nse.txt"):
                syms = [x.strip().upper() for x in text.splitlines() if x.strip() and not x.startswith("#")]
                out = pd.DataFrame({"Symbol": syms[:500], "Sector": "Other / Unclassified"})
            else:
                t = pd.read_csv(StringIO(text))
                cols = {str(c).strip().lower(): c for c in t.columns}
                sc = next((cols[k] for k in ("symbol", "ticker") if k in cols), None)
                sec = next((cols[k] for k in ("industry", "sector", "industry name", "industry_name") if k in cols), None)
                if not sc:
                    raise RuntimeError("NIFTY500 file has no Symbol column")
                out = pd.DataFrame({"Symbol": t[sc].astype(str).str.strip().str.upper()})
                out["Sector"] = t[sec].astype(str).str.strip() if sec else "Other / Unclassified"
            out = out[(out.Symbol != "") & (out.Symbol.str.lower() != "nan") & ~out.Symbol.str.startswith("DUMMY")]
            out = out.drop_duplicates("Symbol").head(500).reset_index(drop=True)
            if len(out) >= 490:
                return out
            last = f"only {len(out)} symbols"
        except Exception as e:
            last = str(e)
    raise RuntimeError(f"Could not load NIFTY 500 universe: {last}")


@st.cache_data(ttl=86400, show_spinner=False)
def get_instruments():
    r = requests.get(INSTRUMENTS_URL, headers={"User-Agent": "Mozilla/5.0 Pre-Breakout-Hunter"}, timeout=30)
    r.raise_for_status()
    data = json.loads(gzip.decompress(r.content).decode("utf-8"))
    rows = []
    for x in data:
        if not isinstance(x, dict) or x.get("segment") != "NSE_EQ":
            continue
        if str(x.get("instrument_type", "")).upper() not in {"EQ", "A", "X"}:
            continue
        sym = str(x.get("trading_symbol", "")).strip().upper()
        key = str(x.get("instrument_key", "")).strip()
        if sym and key:
            rows.append((sym, key))
    return pd.DataFrame(rows, columns=["Symbol", "instrument_key"]).drop_duplicates("Symbol")


def repair_mapping(mapping):
    out = mapping.copy()
    repaired = []
    for sym in out.loc[out.instrument_key.isna(), "Symbol"].tolist():
        try:
            r = requests.get(SEARCH_URL, headers=headers(), params={
                "query": sym, "exchanges": "NSE", "segments": "EQ", "records": 30, "page_number": 1
            }, timeout=8)
            if r.status_code != 200:
                continue
            for item in (r.json() or {}).get("data", []):
                if isinstance(item, dict) and item.get("segment") == "NSE_EQ" and str(item.get("trading_symbol", "")).upper() == sym:
                    key = str(item.get("instrument_key", "")).strip()
                    if key:
                        out.loc[out.Symbol == sym, "instrument_key"] = key
                        repaired.append(sym)
                    break
        except Exception:
            continue
    return out, repaired


def build_mapping():
    return repair_mapping(get_universe().merge(get_instruments(), on="Symbol", how="left"))


def parse_candles(payload):
    candles = ((payload or {}).get("data") or {}).get("candles")
    if not isinstance(candles, list):
        return pd.DataFrame()
    rows = []
    for row in candles:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        try:
            ts = pd.to_datetime(row[0], utc=True).tz_convert(IST).normalize().tz_localize(None)
            vals = [float(row[i]) for i in range(1, 6)]
            if not all(np.isfinite(vals)) or vals[3] <= 0:
                continue
            rows.append([ts, *vals])
        except Exception:
            continue
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"]).drop_duplicates("date").set_index("date").sort_index()


def fetch_history_one(symbol, key, start, end):
    url = f"{HISTORY_URL}/{quote(key, safe='')}/days/1/{end}/{start}"
    try:
        r = requests.get(url, headers=headers(), timeout=15)
        if r.status_code != 200:
            return symbol, pd.DataFrame(), f"HTTP {r.status_code}"
        df = parse_candles(r.json())
        end_ts = pd.Timestamp(end)
        df = df[df.index <= end_ts]
        if len(df) < MIN_BARS:
            return symbol, df, f"Only {len(df)} completed daily bars (need {MIN_BARS})"
        return symbol, df, ""
    except Exception as e:
        return symbol, pd.DataFrame(), str(e)[:180]


@st.cache_data(ttl=HISTORY_CACHE_TTL, show_spinner=False)
def fetch_scan_histories(mapping, asof, token_fingerprint):
    start = asof - timedelta(days=HISTORY_CAL_DAYS)
    jobs = list(mapping.dropna(subset=["instrument_key"])[["Symbol", "instrument_key"]].itertuples(index=False, name=None))
    frames, errors = {}, {}
    last_req = [0.0]
    lock = __import__("threading").Lock()

    def job(item):
        with lock:
            wait = REQUEST_INTERVAL - (time.monotonic() - last_req[0])
            if wait > 0:
                time.sleep(wait)
            last_req[0] = time.monotonic()
        return fetch_history_one(item[0], item[1], start.isoformat(), asof.isoformat())

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = [ex.submit(job, x) for x in jobs]
        for fut in as_completed(futures):
            sym, df, err = fut.result()
            if not df.empty and len(df) >= MIN_BARS:
                frames[sym] = df
            if err:
                errors[sym] = err
    return frames, errors, start, asof


@st.cache_data(ttl=HISTORY_CACHE_TTL, show_spinner=False)
def fetch_nifty(start, end, token_fingerprint):
    candidates = ["NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty 50"]
    for key in candidates:
        url = f"{HISTORY_URL}/{quote(key, safe='')}/days/1/{end}/{start}"
        r = requests.get(url, headers=headers(), timeout=15)
        if r.status_code == 200:
            df = parse_candles(r.json())
            df = df[df.index <= pd.Timestamp(end)]
            if len(df) >= MIN_BARS:
                return df
    raise RuntimeError("NIFTY 50 daily history could not be loaded from Upstox")


@st.cache_data(ttl=HISTORY_CACHE_TTL, show_spinner=False)
def fetch_nifty_range(start, end, token_fingerprint):
    key="NSE_INDEX|Nifty 50"
    url=f"{HISTORY_URL}/{quote(key,safe='')}/days/1/{end}/{start}"
    r=requests.get(url,headers=headers(),timeout=15)
    if r.status_code!=200:
        raise RuntimeError(f"NIFTY 50 historical validation HTTP {r.status_code}")
    df=parse_candles(r.json())
    df=df[df.index<=pd.Timestamp(end)]
    if len(df)<MIN_BARS:
        raise RuntimeError(f"NIFTY 50 historical validation returned only {len(df)} bars")
    return df


def atr(d, n):
    prev = d.close.shift(1)
    tr = pd.concat([(d.high-d.low), (d.high-prev).abs(), (d.low-prev).abs()], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=n).mean()


def metrics_at(d, idx, nifty):
    if idx < 220:
        return None
    x = d.iloc[:idx+1]
    c, h, l, v = x.close, x.high, x.low, x.volume
    sma50 = c.rolling(50).mean().iloc[-1]
    sma150 = c.rolling(150).mean().iloc[-1]
    sma200 = c.rolling(200).mean().iloc[-1]
    sma200_prev = c.rolling(200).mean().iloc[-22] if idx >= 221 else np.nan
    ema20 = c.ewm(span=20, adjust=False).mean().iloc[-1]
    a5, a20 = atr(x,5).iloc[-1], atr(x,20).iloc[-1]
    r5 = (h.iloc[-5:].max()-l.iloc[-5:].min())/l.iloc[-5:].min()*100
    r7 = (h.iloc[-7:].max()-l.iloc[-7:].min())/l.iloc[-7:].min()*100
    r10 = (h.iloc[-10:].max()-l.iloc[-10:].min())/l.iloc[-10:].min()*100
    r20 = (h.iloc[-20:].max()-l.iloc[-20:].min())/l.iloc[-20:].min()*100
    pivot = h.iloc[-21:-1].max()
    close = float(c.iloc[-1])
    distance = (pivot-close)/pivot*100 if pivot else np.nan
    vr = float(v.iloc[-5:].mean()/v.iloc[-20:].mean()) if v.iloc[-20:].mean() else np.inf
    ar = float(a5/a20) if a20 else np.inf
    nn = nifty["close"].reindex(x.index).ffill() if isinstance(nifty, pd.DataFrame) else nifty.reindex(x.index).ffill()
    rs = np.nan
    if len(nn.dropna()) >= 22:
        rs = ((close/c.iloc[-21]-1)-(float(nn.iloc[-1])/float(nn.iloc[-21])-1))*100
    prev20 = h.rolling(20).max().shift(1)
    near = int(((c.iloc[-20:] >= prev20.iloc[-20:]*0.985) & (c.iloc[-20:] < prev20.iloc[-20:])).sum())
    lows = [l.iloc[-5:].min(), l.iloc[-10:-5].min(), l.iloc[-15:-10].min()]
    higher_lows = lows[0] >= lows[1]*0.995 and lows[1] >= lows[2]*0.995
    bodies = (x.open-x.close).abs()/x.close*100
    body_ratio = float(bodies.iloc[-5:].mean()/bodies.iloc[-20:].mean()) if bodies.iloc[-20:].mean() else np.inf
    base = x.iloc[-20:]
    down = base.loc[base.close<base.open,"volume"].mean(); up = base.loc[base.close>=base.open,"volume"].mean()
    dist_ratio = float(down/up) if up else np.inf
    high252 = h.iloc[-252:].max(); low252 = l.iloc[-252:].min()
    pos52 = (close-low252)/(high252-low252)*100 if high252>low252 else np.nan
    to52 = (high252-close)/high252*100 if high252 else np.nan
    base_high, base_low = h.iloc[-20:].max(), l.iloc[-20:].min()
    base_pos = (close-base_low)/(base_high-base_low) if base_high>base_low else 0
    return dict(close=close,sma50=sma50,sma150=sma150,sma200=sma200,sma200_prev=sma200_prev,ema20=ema20,
                r5=r5,r7=r7,r10=r10,r20=r20,pivot=pivot,distance=distance,vr=vr,ar=ar,rs=rs,near=near,
                higher_lows=higher_lows,body_ratio=body_ratio,dist_ratio=dist_ratio,pos52=pos52,to52=to52,base_pos=base_pos)


def evaluate(d, nifty, cfg, idx=None):
    if idx is None: idx=len(d)-1
    m=metrics_at(d,idx,nifty)
    if m is None: return None
    trend=bool(m["close"]>m["sma50"]>m["sma150"]>m["sma200"] and m["sma200"]>m["sma200_prev"] and m["close"]>m["ema20"])
    contraction=bool(m["r5"]<m["r10"]<m["r20"] and m["r5"]<=cfg["range5"] and m["r10"]<=cfg["range10"] and m["r20"]<=cfg["range20"])
    compression=bool(m["ar"]<=cfg["atr_ratio"] and m["vr"]<=cfg["vol_ratio"] and m["body_ratio"]<=0.90)
    pivot=bool(cfg["pivot_min"]<=m["distance"]<=cfg["pivot_max"] and m["close"]<m["pivot"])
    near52=bool(0<=m["to52"]<=6)
    no_break=bool((d.close.iloc[max(0,idx-4):idx+1] < d.high.rolling(20).max().shift(1).iloc[max(0,idx-4):idx+1]).all())
    gates={"Trend":trend,"Contraction":contraction,"Compression":compression,"Pivot":pivot,"No breakout":no_break,
           "Near 52W high":near52,"Higher lows":m["higher_lows"],"No distribution":m["dist_ratio"]<=1.20,"Relative strength":m["rs"]>=0.5}
    score=0
    score += 12 if trend else 0
    score += 12 if contraction else 0
    score += 12 if compression else 0
    score += 10 if m["near"]>=3 else 7 if m["near"]==2 else 4 if m["near"]==1 else 0
    score += 10 if m["distance"]<=1.25 else 7 if m["distance"]<=2 else 4 if m["distance"]<=cfg["pivot_max"] else 0
    score += 8 if m["to52"]<=2 else 5 if m["to52"]<=4 else 2 if m["to52"]<=6 else 0
    score += 8 if m["ar"]<=0.65 else 5 if m["ar"]<=cfg["atr_ratio"] else 0
    score += 8 if m["vr"]<=0.60 else 5 if m["vr"]<=cfg["vol_ratio"] else 0
    score += 8 if m["higher_lows"] else 0
    score += 8 if m["rs"]>=4 else 5 if m["rs"]>=2 else 2 if m["rs"]>=0 else 0
    score += 6 if m["base_pos"]>=0.65 else 3 if m["base_pos"]>=0.55 else 0
    score += 3 if m["dist_ratio"]<=0.90 else 1 if m["dist_ratio"]<=1.20 else 0
    return {"score":int(score),"gates":gates,"m":m}


def row_from_eval(sym, ev, stage):
    m=ev["m"]
    return {"Stock":sym,"Stage":stage,"Score":ev["score"],"Close":round(m["close"],2),"Pivot":round(m["pivot"],2),
            "To Pivot %":round(m["distance"],2),"5D Range %":round(m["r5"],2),"7D Range %":round(m["r7"],2),"10D Range %":round(m["r10"],2),
            "20D Range %":round(m["r20"],2),"ATR5/ATR20":round(m["ar"],2),"Vol5/Vol20":round(m["vr"],2),
            "Resistance Tests":m["near"],"RS vs Nifty %":round(m["rs"],2),"To 52W High %":round(m["to52"],2),
            "Higher Lows":"YES" if m["higher_lows"] else "NO"}


def live_ohlc(keys):
    rows={}; errors=[]
    for i in range(0,len(keys),250):
        batch=keys[i:i+250]
        try:
            r=requests.get(OHLC_URL,headers=headers(),params={"instrument_key":",".join(batch),"interval":"1d"},timeout=15)
            if r.status_code!=200:
                errors.append(f"OHLC HTTP {r.status_code}"); continue
            data=(r.json() or {}).get("data") or {}
            for quote_name, item in data.items():
                if isinstance(item,dict):
                    item=dict(item)
                    item["_quote_name"]=str(quote_name)
                    # Keep multiple lookup forms. Upstox responses may expose instrument_token
                    # separately from the request instrument_key.
                    if item.get("instrument_token"):
                        rows[str(item["instrument_token"])]=item
                    rows[str(quote_name)]=item
        except Exception as e: errors.append(str(e)[:160])
    return rows,errors


def historical_backtest(d, nifty, cfg, min_sim_score, forward_days):
    horizon=10
    if len(d)<MIN_BARS+horizon+5: return {"Occurrences":0}
    events=[]; last_event=-999
    for i in range(MIN_BARS,len(d)-horizon):
        ev=evaluate(d,nifty,cfg,i)
        if not ev or ev["score"]<min_sim_score: continue
        m=ev["m"]
        if not (0.05<=m["distance"]<=max(5.0,cfg["pivot_max"]+1.5)): continue
        if i-last_event<8: continue
        future=d.iloc[i+1:i+horizon+1]; pivot=m["pivot"]; entry=m["close"]
        breakout_idx=next((j for j,(_,row) in enumerate(future.iterrows(),1) if float(row.close)>pivot),None)
        max_gain=((future.high.max()/entry)-1)*100
        max_dd=((future.low.min()/entry)-1)*100
        b5=breakout_idx is not None and breakout_idx<=5
        b10=breakout_idx is not None and breakout_idx<=10
        success=False
        if breakout_idx is not None and breakout_idx<=forward_days:
            after=future.iloc[breakout_idx-1:]
            follow=((after.high.max()/entry)-1)*100 if len(after) else np.nan
            success=bool(follow>=3.0 and (max_dd>-7.0 or breakout_idx<=2))
        events.append({"date":d.index[i],"breakout_5":b5,"breakout_10":b10,"max_gain":max_gain,"max_dd":max_dd,"success":success})
        last_event=i
    if not events: return {"Occurrences":0}
    e=pd.DataFrame(events)
    return {"Occurrences":len(e),"Breakout <=5D %":round(100*e.breakout_5.mean(),1),"Breakout <=10D %":round(100*e.breakout_10.mean(),1),"Validated success %":round(100*e.success.mean(),1),"Avg max gain %":round(float(e.max_gain.mean()),1),"Median max gain %":round(float(e.max_gain.median()),1),"Avg max drawdown %":round(float(e.max_dd.mean()),1),"Median max drawdown %":round(float(e.max_dd.median()),1),"Last historical setup":e.date.iloc[-1].strftime("%Y-%m-%d")}


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_candidate_history(symbol,key,start,end,token_fingerprint):
    return fetch_history_one(symbol,key,start,end)[1]


def backtest_candidates(candidates, mapping, nifty, cfg):
    key_map=dict(zip(mapping.Symbol,mapping.instrument_key))
    end=last_completed_weekday()
    start=end-timedelta(days=int(cfg.get("backtest_days",BACKTEST_CAL_DAYS)))
    try:
        bt_nifty=fetch_nifty_range(start,end,token_fp())
    except Exception:
        bt_nifty=nifty
    out=[]
    for sym in candidates:
        key=key_map.get(sym)
        if not key: continue
        df=fetch_candidate_history(sym,key,start,end,token_fp())
        if df.empty or len(df)<MIN_BARS+10+5: continue
        try:
            # Align NIFTY to the candidate history. We already have a recent NIFTY series; if it is short,
            # use the latest available relative-strength reference only for the event filter.
            n=bt_nifty.reindex(df.index).ffill()
            if n.dropna().empty: continue
            out.append((sym,historical_backtest(df,n,cfg,cfg["near_score"],cfg["forward_days"])))
        except Exception as e:
            out.append((sym,{"Occurrences":0,"Backtest error":str(e)[:100]}))
    return pd.DataFrame([dict(Stock=sym,**stats) for sym,stats in out]) if out else pd.DataFrame()


def live_enrich(rows, frames, live_map, mapping):
    if rows.empty: return rows
    key_to_sym=dict(zip(mapping.instrument_key,mapping.Symbol))
    lrows=[]; seen=set()
    for key,item in live_map.items():
        sym=key_to_sym.get(key)
        quote_name=str((item or {}).get("_quote_name", ""))
        if not sym and ":" in quote_name:
            sym=quote_name.split(":",1)[1].strip().upper()
        if not sym or sym in seen: continue
        lo=(item or {}).get("live_ohlc") or {}
        if sym and lo.get("close") is not None:
            try:
                lrows.append({"Stock":sym,"LTP":float(item.get("last_price",lo.get("close"))),"Live Open":float(lo.get("open")),
                              "Live High":float(lo.get("high")),"Live Low":float(lo.get("low")),"Live Volume":float(lo.get("volume",0)),
                              "Live TS":pd.to_datetime(lo.get("ts"),unit="ms",utc=True).tz_convert(IST)})
                seen.add(sym)
            except Exception: pass
    ld=pd.DataFrame(lrows)
    if ld.empty: return rows.copy()
    if "Stock" not in rows.columns:
        return rows.copy()
    out=rows.copy()
    out["Stock"]=out["Stock"].astype(str)
    ld["Stock"]=ld["Stock"].astype(str)
    out=out.merge(ld,on="Stock",how="left")
    out["Live To Pivot %"]=(out["Pivot"]-out["LTP"])/out["Pivot"]*100
    out["Live Range %"]=(out["Live High"]-out["Live Low"])/out["LTP"]*100
    out["Live Position %"]=np.where(out["Live High"]>out["Live Low"],100*(out["LTP"]-out["Live Low"])/(out["Live High"]-out["Live Low"]),np.nan)
    hist_avg=[]
    for sym in out["Stock"]:
        d=frames.get(sym)
        if d is None or len(d)<20: hist_avg.append(np.nan)
        else: hist_avg.append(float(((d.high-d.low)/d.low*100).iloc[-20:].mean()))
    out["20D Avg Daily Range %"]=hist_avg
    out["Live Range / 20D Avg"]=out["Live Range %"]/out["20D Avg Daily Range %"]
    out["Live Compression"]=np.where(out["Live Range / 20D Avg"]<=0.85,"YES","NO")
    out["Live Pivot Pressure"]=np.where((out["Live High"]>=out["Pivot"]*0.98)&(out["LTP"]<out["Pivot"]),"YES","NO")
    out["Live Status"]=np.where(out["LTP"]>=out["Pivot"],"⚡ INTRADAY TRIGGERED","🟢 PRE-BREAKOUT")
    return out


def main():
    st.title(f"🎯 Pre-Breakout Hunter {APP_VERSION}")
    st.caption("NIFTY 500 → minimum structure scan → LIVE Upstox OHLC → 7D/20D formation → candidate-only historical validation")
    if not token():
        st.error("UPSTOX_ACCESS_TOKEN is missing from Streamlit Secrets.")
        return
    try:
        universe=get_universe(); mapping,repaired=build_mapping()
    except Exception as e:
        st.error(f"Universe / Upstox mapping failed: {e}"); return
    mapped=mapping.dropna(subset=["instrument_key"]).copy()
    st.info(f"NIFTY 500: {len(universe)} • Upstox mapping: {len(mapped)}/{len(universe)}")
    if repaired: st.success(f"Repaired mappings: {', '.join(repaired)}")
    if len(mapped)<490:
        st.error("Mapping coverage below 490/500. Scan is blocked rather than returning an unreliable universe."); return

    with st.sidebar:
        st.header("Scanner settings")
        min_score=st.slider("Ready minimum score",80,100,DEFAULTS["min_score"])
        near_score=st.slider("Near-miss / backtest score",65,85,DEFAULTS["near_score"])
        range5=st.number_input("5D max range %",2.0,8.0,DEFAULTS["range5"],0.25)
        range10=st.number_input("10D max range %",4.0,15.0,DEFAULTS["range10"],0.25)
        range20=st.number_input("20D max range %",7.0,25.0,DEFAULTS["range20"],0.5)
        atr_ratio=st.number_input("Max ATR5/ATR20",0.50,1.0,DEFAULTS["atr_ratio"],0.01)
        vol_ratio=st.number_input("Max Vol5/Vol20",0.40,1.0,DEFAULTS["vol_ratio"],0.01)
        pivot_min=st.number_input("Min distance to pivot %",0.10,3.0,DEFAULTS["pivot_min"],0.05)
        pivot_max=st.number_input("Max distance to pivot %",1.0,6.0,DEFAULTS["pivot_max"],0.25)
        bt_days=st.selectbox("Historical validation",[730,1095,1825],index=2,format_func=lambda x:f"{x//365} years")
        forward_days=st.selectbox("Breakout window",[5,7,10],index=0,format_func=lambda x:f"Next {x} sessions")
        cfg=dict(min_score=min_score,near_score=near_score,range5=range5,range10=range10,range20=range20,atr_ratio=atr_ratio,vol_ratio=vol_ratio,pivot_min=pivot_min,pivot_max=pivot_max,backtest_days=bt_days,forward_days=forward_days)
        scan=st.button("🔎 RUN FULL SCANNER",type="primary",width="stretch")

    if "v15" not in st.session_state: st.session_state.v15=None
    if scan:
        asof=last_completed_weekday(); status=st.status("Running full scanner…",expanded=True)
        try:
            status.write("1/5 — Downloading completed daily history for the NIFTY 500…")
            frames,errors,start,end=fetch_scan_histories(mapping,asof,token_fp())
            if len(frames)<490:
                raise RuntimeError(f"Only {len(frames)}/{len(mapped)} mapped stocks returned at least {MIN_BARS} completed daily bars. Scan blocked for data integrity.")
            status.write("2/5 — Applying the minimum pre-breakout structure to all stocks…")
            nifty=fetch_nifty(start,end,token_fp())
            ready=[]; near=[]
            for sym,df in frames.items():
                ev=evaluate(df,nifty,cfg)
                if not ev: continue
                if all(ev["gates"].values()) and ev["score"]>=min_score:
                    ready.append(row_from_eval(sym,ev,"READY"))
                elif ev["score"]>=near_score:
                    near.append(row_from_eval(sym,ev,"NEAR-MISS"))
            # Always materialize the same schema, including when there are zero READY
            # or zero NEAR-MISS rows. This makes the zero-result case a normal result,
            # never a pandas KeyError.
            result_cols=["Stock","Stage","Score","Close","Pivot","To Pivot %","5D Range %","7D Range %","10D Range %","20D Range %","ATR5/ATR20","Vol5/Vol20","Resistance Tests","RS vs Nifty %","To 52W High %","Higher Lows"]
            ready_df=pd.DataFrame(ready,columns=result_cols)
            near_df=pd.DataFrame(near,columns=result_cols)
            for df in (ready_df,near_df):
                if not df.empty: df.sort_values(["Score","To Pivot %"],ascending=[False,True],inplace=True)
            status.write("3/5 — Reading current LIVE Upstox daily OHLC for the whole mapped universe…")
            live_map,live_errors=live_ohlc(mapped.instrument_key.astype(str).tolist())
            status.write("4/5 — Ranking candidates using today's evolving price/range formation…")
            ready_df=live_enrich(ready_df,frames,live_map,mapping)
            near_df=live_enrich(near_df,frames,live_map,mapping)
            triggered_parts=[]
            for df0 in (ready_df,near_df):
                if not df0.empty and "LTP" in df0.columns and "Pivot" in df0.columns:
                    mask=df0["LTP"]>=df0["Pivot"]
                    if mask.any():
                        t=df0.loc[mask].copy(); t["Stage"]="TRIGGERED"; triggered_parts.append(t)
            triggered_df=pd.concat(triggered_parts,ignore_index=True) if triggered_parts else pd.DataFrame(columns=ready_df.columns if not ready_df.empty else near_df.columns)
            if not ready_df.empty:
                ready_df=ready_df[ready_df["LTP"]<ready_df["Pivot"]].copy(); ready_df["Stage"]="READY"
            if not near_df.empty:
                near_df=near_df[near_df["LTP"]<near_df["Pivot"]].copy(); near_df["Stage"]="NEAR-MISS"
            status.write("5/5 — Backtesting strongest READY / NEAR-MISS / TRIGGERED candidates…")
            names=pd.concat([ready_df[["Stock"]] if not ready_df.empty else pd.DataFrame(),near_df[["Stock"]] if not near_df.empty else pd.DataFrame(),triggered_df[["Stock"]] if not triggered_df.empty else pd.DataFrame()],ignore_index=True)
            names=[str(x).strip().upper() for x in names.Stock.tolist() if pd.notna(x) and str(x).strip()]
            names=list(dict.fromkeys(names))[:30]
            bt=backtest_candidates(names,mapping,nifty,cfg)
            if not bt.empty and "Stock" in bt.columns:
                bt=bt.copy()
                bt["Stock"]=bt["Stock"].astype(str).str.strip().str.upper()
                ready_df["Stock"]=ready_df["Stock"].astype(str).str.strip().str.upper()
                near_df["Stock"]=near_df["Stock"].astype(str).str.strip().str.upper()
                ready_df=ready_df.merge(bt,on="Stock",how="left",suffixes=("","_BT"))
                near_df=near_df.merge(bt,on="Stock",how="left",suffixes=("","_BT"))
            st.session_state.v15=dict(ready=ready_df,near=near_df,triggered=triggered_df,frames=frames,errors=errors,live_errors=live_errors,
                                      nifty=nifty,mapping=mapping,start=start,end=end,asof=asof,cfg=cfg,run_time=now_ist(),live_map=live_map)
            status.update(label=f"Completed — {len(ready_df)} READY, {len(near_df)} NEAR-MISS, {len(triggered_df)} TRIGGERED, backtested {len(names)} candidates",state="complete")
        except Exception as e:
            status.update(label=f"Scanner stopped safely: {e}",state="error")
            st.exception(e)
            return

    state=st.session_state.v15
    if state is None:
        st.markdown("## How this scanner works")
        st.markdown("**500-stock minimum scan → live price/range → only candidates get historical event validation.**")
        st.info("The expensive backtest is deliberately NOT run on all 500 stocks. It runs only after the strategy filter produces READY/NEAR-MISS candidates.")
        return

    ready,near,triggered=state["ready"],state["near"],state.get("triggered",pd.DataFrame())
    st.subheader("🟢 LIVE MARKET + PRE-BREAKOUT RESULTS")
    a,b,c,d,e=st.columns(5)
    a.metric("READY",len(ready)); b.metric("NEAR-MISS",len(near)); c.metric("TRIGGERED",len(triggered)); d.metric("Backtested",min(30,len(pd.concat([ready,near,triggered])))); e.metric("Usable histories",f"{len(state['frames'])}/{len(state['mapping'])}")
    st.caption(f"Scan run: {state['run_time'].strftime('%Y-%m-%d %H:%M:%S %Z')} • Completed setup date: {state['asof']} • Current live OHLC: Upstox V3.")

    def candidate_cards(df,title,limit=10):
        st.subheader(title)
        if df.empty:
            st.info("None today. That is a valid scanner result when the market does not meet the required structure.")
            return
        for _,r in df.head(limit).iterrows():
            success=f"{float(r['Validated success %']):.1f}%" if pd.notna(r.get('Validated success %',np.nan)) else "—"
            occ=f"{int(r['Occurrences'])}" if pd.notna(r.get('Occurrences',np.nan)) else "0"
            b5=f"{float(r['Breakout <=5D %']):.1f}%" if pd.notna(r.get('Breakout <=5D %',np.nan)) else "—"
            b10=f"{float(r['Breakout <=10D %']):.1f}%" if pd.notna(r.get('Breakout <=10D %',np.nan)) else "—"
            with st.container(border=True):
                st.markdown(f"### {r['Stock']} • Score {int(r.get('Score',0))} • {r.get('Live Status','')}")
                x1,x2,x3,x4=st.columns(4)
                x1.metric("LTP",f"₹{float(r['LTP']):,.2f}" if pd.notna(r.get('LTP',np.nan)) else "—")
                x2.metric("To Pivot",f"{float(r['Live To Pivot %']):.2f}%" if pd.notna(r.get('Live To Pivot %',np.nan)) else "—")
                x3.metric("Live Range",f"{float(r['Live Range %']):.2f}%" if pd.notna(r.get('Live Range %',np.nan)) else "—")
                x4.metric("Historical success",success)
                st.caption(f"7D range {r.get('7D Range %','—')}% • 20D range {r.get('20D Range %','—')}% • ATR {r.get('ATR5/ATR20','—')} • Vol {r.get('Vol5/Vol20','—')} • Resistance tests {r.get('Resistance Tests','—')}")
                st.caption(f"Backtest events: {occ} • Breakout ≤5D: {b5} • Breakout ≤10D: {b10} • Avg max gain: {r.get('Avg max gain %','—')}% • Avg max DD: {r.get('Avg max drawdown %','—')}%")

    candidate_cards(ready,"🎯 READY — best pre-breakout candidates")
    candidate_cards(near,"🟡 NEAR-MISS — closest candidates")
    candidate_cards(triggered,"⚡ TRIGGERED — resistance reached/broken",5)

    def show_table(df,title):
        if df.empty: return
        st.subheader(title + " — compact view")
        cols=["Stock","Stage","Score","LTP","Live To Pivot %","Live Range %","7D Range %","20D Range %","Live Compression","Live Pivot Pressure","Occurrences","Validated success %"]
        show=[x for x in cols if x in df.columns]
        st.dataframe(df[show].reset_index(drop=True),width="stretch",hide_index=True)

    show_table(ready,"🎯 READY")
    show_table(near,"🟡 NEAR-MISS")
    show_table(triggered,"⚡ TRIGGERED")

    chart_df=ready if not ready.empty else near if not near.empty else triggered
    if not chart_df.empty:
        st.markdown("### 📊 Formation charts — completed candles + current live price")
        for sym in chart_df.Stock.head(5).tolist():
            d=state["frames"].get(sym)
            if d is None: continue
            r=chart_df[chart_df.Stock==sym].iloc[0]
            chart=d.iloc[-20:][["close"]].copy(); chart.columns=["Close"]; chart["Pivot"]=float(r["Pivot"])
            live_price=float(r["LTP"]) if pd.notna(r.get("LTP",np.nan)) else np.nan
            live_index=pd.Timestamp(r["Live TS"]).tz_convert(None) if pd.notna(r.get("Live TS",pd.NaT)) else chart.index[-1]+pd.Timedelta(days=1)
            chart.loc[live_index,"Current live price"]=live_price; chart=chart.sort_index()
            st.markdown(f"**{sym} — score {int(r.Score)} — {r.get('Live Status','')} — pivot ₹{float(r.Pivot):.2f}**")
            st.line_chart(chart,height=260)
            st.caption(f"Live H ₹{float(r['Live High']):.2f} • Live L ₹{float(r['Live Low']):.2f} • Live range {float(r['Live Range %']):.2f}% • Live/20D avg {float(r['Live Range / 20D Avg']):.2f}x • To pivot {float(r['Live To Pivot %']):.2f}%")

    with st.expander("🧪 Backtest definition",expanded=False):
        st.markdown(f"""
**Historical validation is candidate-only.** For each READY/NEAR-MISS/TRIGGERED stock, the engine walks its historical daily candles and finds prior dates with a similar pre-breakout score (≥ {near_score}) while price remained below the prior 20-session resistance. It then measures:

- **Breakout ≤5D:** a future daily close above that historical resistance within 5 sessions.
- **Breakout ≤10D:** a future daily close above that historical resistance within 10 sessions.
- **Validated success:** breakout within the selected success window plus at least +3% follow-through without a severe adverse move dominating the event.
- **Avg/median max gain:** highest future price relative to the setup close.
- **Avg/median max drawdown:** lowest future price relative to the setup close.

The selected success window is configurable (5/7/10 sessions). Breakout rates always use true 5-session and 10-session horizons. These are historical event statistics, **not guaranteed probabilities**.
""")

    with st.expander("🩺 Data health",expanded=False):
        st.write(f"NIFTY 500 universe: {len(get_universe())}")
        st.write(f"Exact Upstox mappings: {len(state['mapping'].dropna(subset=['instrument_key']))}/500")
        st.write(f"Completed daily histories usable: {len(state['frames'])}/500")
        st.write(f"History window: {state['start']} → {state['end']}")
        if state["errors"]:
            st.dataframe(pd.DataFrame([{"Stock":k,"Reason":v} for k,v in state["errors"].items()]),width="stretch",hide_index=True)
        if len(state["frames"]) == len(state["mapping"].dropna(subset=["instrument_key"])):
            st.success("All mapped stocks returned sufficient completed daily history.")
        else:
            st.warning(f"{len(state['frames'])} of {len(state['mapping'].dropna(subset=['instrument_key']))} mapped stocks returned sufficient completed history. Unusable symbols were excluded from scoring.")
        if state["live_errors"]: st.warning("Live OHLC partial errors: "+"; ".join(state["live_errors"][:3]))

    with st.expander("📐 Strategy rules",expanded=False):
        st.markdown("""
- Strong trend: Close > SMA50 > SMA150 > SMA200, rising SMA200, Close > EMA20.
- Progressive contraction: 5D range < 10D range < 20D range plus configurable ceilings.
- Volatility/volume compression: ATR5/ATR20, 5D/20D volume and candle-body contraction.
- Structure: higher lows + repeated tests of prior 20-session resistance.
- Position: price stays just below resistance and within 6% of the 52-week high.
- Breakout protection: recent closes remain below their prior 20-session highs.
- Relative strength: recent performance must outperform NIFTY 50.
- Live layer: current Upstox daily OHLC is compared with the completed setup, including live price, live range and distance to pivot.
""")
    st.caption("Pre-Breakout Hunter V15 • Upstox Analytics Token • no Yahoo • no WebSocket • no intraday-history polling • no order/trading API")

if __name__ == "__main__":
    main()
