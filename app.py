import streamlit as st
import pandas as pd
import numpy as np
import requests, gzip, json, hashlib, time
from io import StringIO
from datetime import datetime, timedelta, timezone, time as dtime
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote
from zoneinfo import ZoneInfo

st.set_page_config(page_title="Pre-Breakout Hunter V23", page_icon="🎯", layout="wide")

APP_VERSION = "V24.0 FINAL"
IST = ZoneInfo("Asia/Kolkata")
MIN_BARS = 230
HISTORY_CAL_DAYS = 390
BACKTEST_CAL_DAYS = 1825
HISTORY_CACHE_TTL = 1800
WORKERS = 8
REQUESTS_PER_SEC = 35
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
def fetch_history_cached(symbol, key, start_iso, end_iso, token_fingerprint):
    return fetch_history_one(symbol, key, start_iso, end_iso)


def fetch_history_batch(jobs, start, end, token_fingerprint):
    frames, errors = {}, {}
    last_req = [0.0]
    lock = __import__("threading").Lock()

    def job(item):
        with lock:
            wait = REQUEST_INTERVAL - (time.monotonic() - last_req[0])
            if wait > 0:
                time.sleep(wait)
            last_req[0] = time.monotonic()
        return fetch_history_cached(item[0], item[1], start.isoformat(), end.isoformat(), token_fingerprint)

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = [ex.submit(job, x) for x in jobs]
        for fut in as_completed(futures):
            sym, df, err = fut.result()
            if not df.empty and len(df) >= MIN_BARS:
                # Keep only what the live structural scan actually needs.
                frames[sym] = df.iloc[-260:].copy()
            if err:
                errors[sym] = err
    return frames, errors


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


def add_trade_plan(df, frames):
    """Add a conservative, rule-based breakout trade plan.

    Entry is NOT the current LTP. The strategy waits for a daily close above
    prior 20-session resistance, then plans the next-session entry using a
    small confirmation buffer. Stop is below the 20-session base and volatility
    adjusted. Targets are 2R/3R/4R. These are planning levels, not guarantees.
    """
    if df.empty or "Stock" not in df.columns:
        return df
    out=df.copy()
    planned_entry=[]; stop=[]; risk=[]; t1=[]; t2=[]; t3=[]; base_lows=[]; atr20s=[]
    for _,r in out.iterrows():
        sym=str(r.get("Stock","")).strip().upper()
        d=frames.get(sym)
        if d is None or d.empty or len(d)<20 or pd.isna(r.get("Pivot",np.nan)):
            planned_entry.append(np.nan); stop.append(np.nan); risk.append(np.nan); t1.append(np.nan); t2.append(np.nan); t3.append(np.nan); base_lows.append(np.nan); atr20s.append(np.nan); continue
        x=d.sort_index().copy()
        close=x.close.astype(float); high=x.high.astype(float); low=x.low.astype(float); prev=close.shift(1)
        tr=pd.concat([(high-low),(high-prev).abs(),(low-prev).abs()],axis=1).max(axis=1)
        atr20=float(tr.rolling(20,min_periods=20).mean().iloc[-1]) if len(x)>=20 else np.nan
        base_low=float(low.iloc[-20:].min())
        pivot=float(r["Pivot"])
        entry=pivot*1.0025
        # Use the tighter of structure/volatility stops, but never above the base.
        vol_stop=entry-1.25*atr20 if np.isfinite(atr20) else np.nan
        struct_stop=base_low*0.995
        if np.isfinite(vol_stop):
            sl=min(struct_stop,vol_stop)
        else:
            sl=struct_stop
        rr=entry-sl
        # If the calculated risk is invalid, leave levels blank rather than inventing them.
        if not np.isfinite(rr) or rr<=0 or entry<=sl:
            pe=slv=rv=a=b=c=np.nan
        else:
            pe=entry; slv=sl; rv=rr
            a=entry+2*rr; b=entry+3*rr; c=entry+4*rr
        planned_entry.append(pe); stop.append(slv); risk.append(rv); t1.append(a); t2.append(b); t3.append(c); base_lows.append(base_low); atr20s.append(atr20)
    out["Planned Entry"] = planned_entry
    out["Stop Loss"] = stop
    out["Risk/Share"] = risk
    out["Target 1 (2R)"] = t1
    out["Target 2 (3R)"] = t2
    out["Target 3 (4R)"] = t3
    out["20D Base Low"] = base_lows
    out["ATR20 ₹"] = atr20s
    out["Risk %"] = np.where(out["Planned Entry"]>0, out["Risk/Share"]/out["Planned Entry"]*100, np.nan)
    out["Trade Plan"] = np.where(out["Planned Entry"].notna(), "WAIT FOR CLOSE > PIVOT → NEXT OPEN", "NO PLAN")
    return out


def live_ohlc(keys, interval="1d"):
    """Fresh Upstox V3 OHLC snapshot for the entire mapped universe.

    V3 returns last_price plus live_ohlc. For I1 it also returns the current
    one-minute candle and previous minute candle. No Streamlit cache is used:
    every scanner run makes a fresh network request.
    """
    rows={}; errors=[]
    for i in range(0,len(keys),250):
        batch=keys[i:i+250]
        try:
            r=requests.get(OHLC_URL,headers=headers(),params={"instrument_key":",".join(batch),"interval":interval},timeout=15)
            if r.status_code!=200:
                errors.append(f"OHLC {interval} HTTP {r.status_code}"); continue
            data=(r.json() or {}).get("data") or {}
            for quote_name,item in data.items():
                if isinstance(item,dict):
                    item=dict(item); item["_quote_name"]=str(quote_name)
                    rows[str(item.get("instrument_token") or quote_name)]=item
                    rows[str(quote_name)]=item
        except Exception as e:
            errors.append(f"OHLC {interval}: {str(e)[:160]}")
    return rows,errors


def merge_live_maps(day_map, minute_map):
    """Merge fresh 1D and I1 snapshots by Upstox instrument token/name."""
    merged={}
    keys=set(day_map) | set(minute_map)
    for k in keys:
        d=day_map.get(k,{}) or {}; m=minute_map.get(k,{}) or {}
        if not d and m: d=dict(m)
        else: d=dict(d)
        if m:
            d["minute_ohlc"]=m.get("live_ohlc") or {}
            d["prev_minute_ohlc"]=m.get("prev_ohlc") or {}
        merged[k]=d
    return merged


def live_regrade(structure_df, frames, live_map, mapping, min_score=84, near_score=72, now=None, near_limit=10):
    """Apply the live breakout layer to the WHOLE mapped universe.

    Completed daily candles establish the resistance/base context, but they no
    longer act as a pre-filter. Every mapped stock with usable history and a
    fresh Upstox quote is re-evaluated at the current LTP/current-session OHLC.
    This is the key LIVE-FIRST change in V21.
    """
    if structure_df.empty: return structure_df.copy()
    key_to_sym=dict(zip(mapping.instrument_key,mapping.Symbol))
    lrows=[]; seen=set()
    now = now or now_ist()
    for key,item in live_map.items():
        item=item or {}; sym=key_to_sym.get(key)
        q=str(item.get("_quote_name",""))
        if not sym and ":" in q: sym=q.split(":",1)[1].strip().upper()
        if not sym or sym in seen: continue
        lo=item.get("live_ohlc") or {}
        try:
            ts=pd.to_datetime(lo.get("ts"),unit="ms",utc=True).tz_convert(IST) if lo.get("ts") is not None else pd.NaT
            if pd.isna(ts): continue
            ltp=float(item.get("last_price",lo.get("close")))
            op=float(lo.get("open")); hi=float(lo.get("high")); low=float(lo.get("low")); vol=float(lo.get("volume",0))
            age=(now-ts.to_pydatetime()).total_seconds()
            # During the NSE session, a current 1D candle should not be hours old.
            # Outside the session we still display the quote but mark it stale.
            market_open = now.weekday()<5 and dtime(9,15) <= now.time() <= dtime(15,35)
            fresh = (age <= 180) if market_open else True
            minute= item.get("minute_ohlc") or {}
            pminute=item.get("prev_minute_ohlc") or {}
            lrows.append({"Stock":sym,"LTP":ltp,"Live Open":op,"Live High":hi,"Live Low":low,
                          "Live Volume":vol,"Live TS":ts,"Live Age Sec":age,"Live Fresh":fresh,
                          "Minute Open":float(minute.get("open")) if minute.get("open") is not None else np.nan,
                          "Minute High":float(minute.get("high")) if minute.get("high") is not None else np.nan,
                          "Minute Low":float(minute.get("low")) if minute.get("low") is not None else np.nan,
                          "Minute Close":float(minute.get("close")) if minute.get("close") is not None else np.nan,
                          "Minute Volume":float(minute.get("volume",0)) if minute.get("volume") is not None else np.nan,
                          "Prev Minute Close":float(pminute.get("close")) if pminute.get("close") is not None else np.nan})
            seen.add(sym)
        except Exception: continue
    ld=pd.DataFrame(lrows)
    if ld.empty: return structure_df.copy()
    out=structure_df.copy(); out["Stock"]=out["Stock"].astype(str).str.strip().str.upper(); ld["Stock"]=ld.Stock.str.upper()
    out=out.merge(ld,on="Stock",how="left")

    out["Live To Pivot %"]=(out["Pivot"]-out["LTP"])/out["Pivot"]*100
    out["Live Range %"]=(out["Live High"]-out["Live Low"])/out["LTP"]*100
    out["Live Position %"]=np.where(out["Live High"]>out["Live Low"],100*(out["LTP"]-out["Live Low"])/(out["Live High"]-out["Live Low"]),np.nan)
    out["Live Range / 20D Avg"]=np.nan; out["Live Volume / 20D Avg"]=np.nan; out["Live To 52W High %"]=np.nan
    for i,r in out.iterrows():
        d=frames.get(r["Stock"])
        if d is None or d.empty: continue
        avg_rng=float(((d.high-d.low)/d.low*100).iloc[-20:].mean())
        avg_vol=float(d.volume.iloc[-20:].mean())
        hi52=float(d.high.iloc[-252:].max())
        out.at[i,"20D Avg Daily Range %"]=avg_rng
        out.at[i,"Live Range / 20D Avg"]=float(r["Live Range %"])/avg_rng if avg_rng else np.nan
        out.at[i,"Live Volume / 20D Avg"]=float(r["Live Volume"])/avg_vol if avg_vol else np.nan
        out.at[i,"Live To 52W High %"]=(hi52-float(r["LTP"]))/hi52*100 if hi52 else np.nan

    out["Live Compression"]=np.where(out["Live Range / 20D Avg"]<=1.0,"YES","NO")
    out["Live Pivot Pressure"]=np.where((out["Live High"]>=out["Pivot"]*0.99)&(out["LTP"]<out["Pivot"]),"YES","NO")
    out["Live Minute Pressure"]=np.where((out["Minute High"]>=out["Pivot"]*0.995)&(out["LTP"]<out["Pivot"]),"YES","NO")

    # LIVE-FIRST score: combine established structure with what price is doing RIGHT NOW.
    base=out["Score"].fillna(0).astype(float)
    dist=out["Live To Pivot %"]
    pressure=pd.Series(0.0,index=out.index)
    pressure += np.where((dist>=0)&(dist<=5), np.clip(30-dist*6,0,30), 0)
    pressure += np.where((dist>=0)&(dist<=1.0),20,np.where((dist>=0)&(dist<=2.0),12,0))
    pressure += np.where(out["Live High"]>=out["Pivot"],25,0)
    pressure += np.where(out["Minute High"]>=out["Pivot"]*0.997,10,0)
    pressure += np.where(out["Live Position %"]>=80,8,np.where(out["Live Position %"]>=65,4,0))
    pressure += np.where(out["Live Range / 20D Avg"]>=1.20,4,np.where(out["Live Range / 20D Avg"]>=0.80,2,0))
    pressure += np.where(out["Live Fresh"]==False,-30,0)
    out["Live Breakout Pressure"]=pressure.clip(0,100).round().astype(int)
    out["Live Score"]=(0.70*base+0.30*out["Live Breakout Pressure"]).round().clip(0,100).astype(int)
    reasons=[]
    for _,r in out.iterrows():
        rr=[]; d=float(r.get("Live To Pivot %",np.nan)) if pd.notna(r.get("Live To Pivot %",np.nan)) else np.nan
        if pd.notna(d): rr.append("price at/above resistance" if d<=0 else "within 1% of resistance" if d<=1 else "within 2% of resistance" if d<=2 else "approaching resistance" if d<=5 else "too far from resistance")
        if str(r.get("Live Pivot Pressure","NO"))=="YES": rr.append("session high testing pivot")
        if str(r.get("Live Minute Pressure","NO"))=="YES": rr.append("latest 1m pressure")
        if float(r.get("Live Position %",0) or 0)>=80: rr.append("strong intraday position")
        if float(r.get("Live Range / 20D Avg",0) or 0)<0.8: rr.append("quiet live range")
        if float(r.get("Score",0) or 0)<min_score: rr.append("structure score below READY")
        reasons.append(" • ".join(rr[:3]) if rr else "insufficient live pressure")
    out["Live Signal Reason"]=reasons
    strict_structure=(out["Score"]>=min_score)
    breakout=(out["LTP"]>=out["Pivot"]) & strict_structure & (out["Live Fresh"]==True) & (out["Live Breakout Pressure"]>=55)
    ready=(out["LTP"]<out["Pivot"]) & (out["Live To Pivot %"]>=0) & (out["Live To Pivot %"]<=3.5) & (out["Score"]>=min_score) & (out["Live Breakout Pressure"]>=45) & (out["Live Fresh"]==True)
    near=((out["LTP"]<out["Pivot"]) & (out["Live To Pivot %"]>=0) & (out["Live To Pivot %"]<=5.0) & (out["Score"]>=near_score))
    out["Live Status"]=np.select([breakout,ready,near],["⚡ LIVE BREAKOUT NOW","🟢 READY NOW","🟡 NEAR-MISS NOW"],default="WATCH")
    out["Stage"]=np.select([breakout,ready,near],["BREAKOUT NOW","READY NOW","NEAR-MISS NOW"],default="WATCH")
    # Near-miss is still a real structural candidate: never promote a stock below the configured near-miss score.
    # Do not let structural score alone determine the live ranking.
    # Immediate pressure/proximity now carry the largest weight.
    out=add_live_priority(out)
    stage_order=pd.Categorical(out["Stage"],categories=["BREAKOUT NOW","READY NOW","NEAR-MISS NOW","WATCH"],ordered=True)
    out["_stage_order"]=stage_order
    out=out.sort_values(["_stage_order","Live Priority Score","Live Breakout Pressure","Live To Pivot %"],ascending=[True,False,False,True]).drop(columns=["_stage_order"])
    return out


def evidence_grade(n):
    n=int(n or 0)
    if n < 3: return "🔴 INSUFFICIENT"
    if n < 5: return "🟠 VERY LIMITED"
    if n < 10: return "🟡 LIMITED"
    if n < 20: return "🟢 DEVELOPING"
    if n < 30: return "🟢 VALIDATED"
    return "🟢🟢 STRONG"


def adjusted_success(raw_success, occurrences):
    """Shrink tiny samples toward a neutral 50% baseline.

    The raw historical rate remains visible. This adjusted rate is used only
    for ranking so that 100% from one event cannot outrank a robust sample.
    """
    if raw_success is None or not np.isfinite(raw_success):
        return np.nan
    n=int(occurrences or 0)
    if n >= 30: w=1.00
    elif n >= 20: w=0.85
    elif n >= 10: w=0.70
    elif n >= 5: w=0.50
    elif n >= 3: w=0.35
    else: w=0.00
    return round(50.0 + w*(float(raw_success)-50.0),1)


def add_live_priority(df):
    """Rank live candidates by immediate breakout pressure, not structure alone."""
    if df.empty:
        return df.copy()
    out=df.copy()
    structure=out.get("Score",pd.Series(0,index=out.index)).fillna(0).astype(float)
    pressure=out.get("Live Breakout Pressure",pd.Series(0,index=out.index)).fillna(0).astype(float)
    dist=out.get("Live To Pivot %",pd.Series(np.nan,index=out.index)).astype(float)
    pos=out.get("Live Position %",pd.Series(0,index=out.index)).fillna(0).astype(float)
    comp=out.get("Live Range / 20D Avg",pd.Series(np.nan,index=out.index)).astype(float)
    raw=out.get("Validated success %",pd.Series(np.nan,index=out.index)).astype(float)
    occ=out.get("Occurrences",pd.Series(0,index=out.index)).fillna(0).astype(int)
    adj=pd.Series([adjusted_success(a,n) for a,n in zip(raw,occ)],index=out.index,dtype=float)
    proximity=np.where((dist>=0)&(dist<=5),100-np.clip(dist,0,5)*20,0)
    proximity=np.clip(proximity,0,100)
    position=np.clip(pos,0,100)
    compression=np.where(np.isfinite(comp),np.clip(100-(comp-0.5)*100,0,100),50)
    evidence=np.where(occ>=30,100,np.where(occ>=20,85,np.where(occ>=10,70,np.where(occ>=5,50,np.where(occ>=3,35,0)))))
    out["Historical Adjusted Success %"]=adj
    out["Evidence Grade"]=[evidence_grade(n) for n in occ]
    out["Live Priority Score"]=(
        0.35*pressure + 0.25*structure + 0.15*proximity +
        0.10*position + 0.10*compression + 0.05*evidence
    ).round().clip(0,100).astype(int)
    # A transparent label that prevents a raw 100% / 1-event sample from looking validated.
    out["Historical Evidence"]=[f"{(raw.iloc[i] if pd.notna(raw.iloc[i]) else np.nan):.1f}% raw • {evidence_grade(occ.iloc[i])}" if pd.notna(raw.iloc[i]) else "No qualifying events" for i in range(len(out))]
    return out


def historical_backtest(d, nifty, cfg, min_sim_score, forward_days):
    """Fast historical event validation.

    V16 recalculated all rolling indicators inside evaluate() for every historical
    date, making a 5-year validation effectively O(n^2).  This version computes
    the complete feature table once, then scans the rows.  Historical validation
    therefore stays close to O(n), with only a small loop over qualifying events.
    """
    horizon = 10
    if len(d) < MIN_BARS + horizon + 5:
        return {"Occurrences": 0}

    x = d.sort_index().copy()
    c, h, l, o, v = x.close.astype(float), x.high.astype(float), x.low.astype(float), x.open.astype(float), x.volume.astype(float)

    # Core rolling features — computed once for the entire history.
    sma50 = c.rolling(50, min_periods=50).mean()
    sma150 = c.rolling(150, min_periods=150).mean()
    sma200 = c.rolling(200, min_periods=200).mean()
    sma200_prev = sma200.shift(21)
    ema20 = c.ewm(span=20, adjust=False).mean()

    prev_close = c.shift(1)
    tr = pd.concat([(h-l), (h-prev_close).abs(), (l-prev_close).abs()], axis=1).max(axis=1)
    a5 = tr.rolling(5, min_periods=5).mean()
    a20 = tr.rolling(20, min_periods=20).mean()
    ar = a5 / a20.replace(0, np.nan)

    def rng(n):
        lo = l.rolling(n, min_periods=n).min()
        hi = h.rolling(n, min_periods=n).max()
        return (hi-lo) / lo.replace(0, np.nan) * 100
    r5, r7, r10, r20 = rng(5), rng(7), rng(10), rng(20)

    pivot = h.rolling(20, min_periods=20).max().shift(1)
    distance = (pivot-c) / pivot.replace(0, np.nan) * 100
    vol5 = v.rolling(5, min_periods=5).mean()
    vol20 = v.rolling(20, min_periods=20).mean()
    vr = vol5 / vol20.replace(0, np.nan)

    prev20 = h.rolling(20, min_periods=20).max().shift(1)
    near_flag = ((c >= prev20 * 0.985) & (c < prev20)).astype(float)
    near = near_flag.rolling(20, min_periods=20).sum()

    low5 = l.rolling(5, min_periods=5).min()
    higher_lows = (low5 >= low5.shift(5) * 0.995) & (low5.shift(5) >= low5.shift(10) * 0.995)

    bodies = (o-c).abs() / c.replace(0, np.nan) * 100
    body_ratio = bodies.rolling(5, min_periods=5).mean() / bodies.rolling(20, min_periods=20).mean().replace(0, np.nan)

    down_v = v.where(c < o)
    up_v = v.where(c >= o)
    dist_ratio = down_v.rolling(20, min_periods=1).mean() / up_v.rolling(20, min_periods=1).mean().replace(0, np.nan)

    high252 = h.rolling(252, min_periods=1).max()
    low252 = l.rolling(252, min_periods=1).min()
    to52 = (high252-c) / high252.replace(0, np.nan) * 100
    base_high = h.rolling(20, min_periods=20).max()
    base_low = l.rolling(20, min_periods=20).min()
    base_pos = (c-base_low) / (base_high-base_low).replace(0, np.nan)

    # Relative strength vs NIFTY, aligned to candidate dates.
    if isinstance(nifty, pd.DataFrame):
        nn = nifty["close"] if "close" in nifty.columns else nifty.iloc[:, 0]
    else:
        nn = nifty
    nn = pd.Series(nn).reindex(x.index).ffill()
    rs = ((c / c.shift(21) - 1) - (nn / nn.shift(21) - 1)) * 100

    trend = (c > sma50) & (sma50 > sma150) & (sma150 > sma200) & (sma200 > sma200_prev) & (c > ema20)
    contraction = (r5 < r10) & (r10 < r20) & (r5 <= cfg["range5"]) & (r10 <= cfg["range10"]) & (r20 <= cfg["range20"])
    compression = (ar <= cfg["atr_ratio"]) & (vr <= cfg["vol_ratio"]) & (body_ratio <= 0.90)
    pivot_gate = (distance >= cfg["pivot_min"]) & (distance <= cfg["pivot_max"]) & (c < pivot)
    near52 = (to52 >= 0) & (to52 <= 6)
    no_break = (c < prev20).rolling(5, min_periods=5).sum().eq(5)
    no_distribution = dist_ratio <= 1.20
    rel_strength = rs >= 0.5

    score = (
        trend.astype(int)*12 + contraction.astype(int)*12 + compression.astype(int)*12 +
        np.select([near>=3, near==2, near==1], [10,7,4], default=0) +
        np.select([distance<=1.25, distance<=2, distance<=cfg["pivot_max"]], [10,7,4], default=0) +
        np.select([to52<=2, to52<=4, to52<=6], [8,5,2], default=0) +
        np.select([ar<=0.65, ar<=cfg["atr_ratio"]], [8,5], default=0) +
        np.select([vr<=0.60, vr<=cfg["vol_ratio"]], [8,5], default=0) +
        higher_lows.astype(int)*8 +
        np.select([rs>=4, rs>=2, rs>=0], [8,5,2], default=0) +
        np.select([base_pos>=0.65, base_pos>=0.55], [6,3], default=0) +
        np.select([dist_ratio<=0.90, dist_ratio<=1.20], [3,1], default=0)
    )

    eligible = (
        trend & contraction & compression & pivot_gate & no_break & near52 &
        higher_lows & no_distribution & rel_strength & (score >= min_sim_score)
    )
    # Keep the same minimum historical pivot distance used by the old engine.
    eligible &= (distance >= 0.05) & (distance <= max(5.0, cfg["pivot_max"] + 1.5))

    candidate_idx = np.flatnonzero(eligible.to_numpy())
    candidate_idx = candidate_idx[candidate_idx >= MIN_BARS]
    candidate_idx = candidate_idx[candidate_idx < len(x)-horizon]
    if len(candidate_idx) == 0:
        return {"Occurrences": 0}

    events=[]
    last_event=-999
    for i in candidate_idx.tolist():
        if i-last_event < 8:
            continue
        pivot_i=float(pivot.iloc[i]); entry=float(c.iloc[i])
        future=x.iloc[i+1:i+horizon+1]
        breakout_idx=None
        for j, (_, row) in enumerate(future.iterrows(), 1):
            if float(row.close) > pivot_i:
                breakout_idx=j; break
        max_gain=(float(future.high.max())/entry-1)*100
        max_dd=(float(future.low.min())/entry-1)*100
        b5=breakout_idx is not None and breakout_idx<=5
        b10=breakout_idx is not None and breakout_idx<=10
        success=False
        if breakout_idx is not None and breakout_idx<=forward_days:
            after=future.iloc[breakout_idx-1:]
            follow=(float(after.high.max())/entry-1)*100 if len(after) else np.nan
            success=bool(follow>=3.0 and (max_dd>-7.0 or breakout_idx<=2))
        events.append({"date":x.index[i],"breakout_5":b5,"breakout_10":b10,"max_gain":max_gain,"max_dd":max_dd,"success":success})
        last_event=i

    if not events:
        return {"Occurrences":0}
    e=pd.DataFrame(events)
    return {
        "Occurrences":len(e),
        "Breakout <=5D %":round(100*e.breakout_5.mean(),1),
        "Breakout <=10D %":round(100*e.breakout_10.mean(),1),
        "Validated success %":round(100*e.success.mean(),1),
        "Avg max gain %":round(float(e.max_gain.mean()),1),
        "Median max gain %":round(float(e.max_gain.median()),1),
        "Avg max drawdown %":round(float(e.max_dd.mean()),1),
        "Median max drawdown %":round(float(e.max_dd.median()),1),
        "Last historical setup":e.date.iloc[-1].strftime("%Y-%m-%d")
    }


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


def main():
    st.title(f"🎯 Pre-Breakout Hunter {APP_VERSION}")
    st.caption("NIFTY 500 → fresh full-universe live snapshot → daily structure context → 1-minute pressure → breakout/ready/near-miss")
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

    # Resumable scanner state: each history batch is a separate Streamlit run.
    # This prevents one long 500-request execution from losing the whole session.
    if "v21_job" not in st.session_state: st.session_state.v21_job=None
    if "v21" not in st.session_state: st.session_state.v21=None

    if scan and st.session_state.v21_job is None:
        asof=last_completed_weekday()
        jobs=list(mapped[["Symbol","instrument_key"]].itertuples(index=False,name=None))
        st.session_state.v21_job=dict(
            asof=asof, start=asof-timedelta(days=HISTORY_CAL_DAYS), end=asof,
            jobs=jobs, idx=0, batch_size=25, frames={}, errors={}, cfg=cfg,
            mapping=mapping, repaired=repaired, created=now_ist()
        )
        st.session_state.v21=None
        st.rerun()

    job=st.session_state.v21_job
    if job is not None:
        total=len(job["jobs"]); idx=job["idx"]
        if idx < total:
            batch=job["jobs"][idx:min(idx+job["batch_size"],total)]
            st.subheader("🔄 Building scanner history")
            st.progress(min(idx/total,1.0),text=f"Daily history: {idx}/{total} stocks completed")
            st.caption("Each batch is committed before the next one starts. If the browser reconnects, completed batches remain cached and the scanner continues safely.")
            try:
                bf,be=fetch_history_batch(batch,job["start"],job["end"],token_fp())
                job["frames"].update(bf); job["errors"].update(be); job["idx"]+=len(batch)
                st.session_state.v21_job=job
                st.success(f"Batch complete: {job['idx']}/{total} • usable histories {len(job['frames'])}")
                st.rerun()
            except Exception as e:
                st.error(f"This batch failed safely: {e}")
                st.info("Press RUN FULL SCANNER again to continue from cached completed history batches.")
                return
        else:
            # History phase is complete; continue into the short analysis phase.
            frames=job["frames"]; errors=job["errors"]; mapping=job["mapping"]; asof=job["asof"]; start=job["start"]; end=job["end"]; cfg=job["cfg"]
            if len(frames)<490:
                st.error(f"Only {len(frames)}/{total} mapped stocks returned at least {MIN_BARS} completed daily bars. Scan blocked for data integrity.")
                st.session_state.v21_job=None
                return
            status=st.status("History complete — finishing live scan…",expanded=True)
            try:
                status.write("2/5 — Building the established pre-breakout structure from completed daily candles…")
                nifty=fetch_nifty(start,end,token_fp())
                # Build the stable historical context for EVERY usable stock.
                # It is not a gate: the live layer below sees the whole mapped universe.
                pool=[]
                for sym,df in frames.items():
                    ev=evaluate(df,nifty,cfg)
                    if ev: pool.append(row_from_eval(sym,ev,"WATCH"))
                result_cols=["Stock","Stage","Score","Close","Pivot","To Pivot %","5D Range %","7D Range %","10D Range %","20D Range %","ATR5/ATR20","Vol5/Vol20","Resistance Tests","RS vs Nifty %","To 52W High %","Higher Lows"]
                pool_df=pd.DataFrame(pool,columns=result_cols)
                status.write("3/5 — Reading a FRESH LIVE Upstox price + current-session OHLC for ALL mapped stocks…")
                live_day,day_errors=live_ohlc(mapped.instrument_key.astype(str).tolist(),interval="1d")
                live_min,minute_errors=live_ohlc(mapped.instrument_key.astype(str).tolist(),interval="I1")
                live_map=merge_live_maps(live_day,live_min)
                live_errors=day_errors+minute_errors
                status.write("4/5 — Recalculating the breakout pattern from the LIVE price + latest 1-minute pressure…")
                live_df=live_regrade(pool_df,frames,live_map,mapping,min_score=cfg["min_score"],near_score=cfg["near_score"],now=now_ist())
                if live_df.empty:
                    ready_df=pd.DataFrame(columns=result_cols); near_df=pd.DataFrame(columns=result_cols); triggered_df=pd.DataFrame(columns=result_cols)
                else:
                    # Keep only stocks for which current live data is actually available.
                    live_df=live_df[live_df["LTP"].notna()].copy()
                    triggered_df=live_df[live_df["Stage"]=="BREAKOUT NOW"].copy().sort_values(["Live Score","Live To Pivot %"],ascending=[False,True])
                    ready_df=live_df[live_df["Stage"]=="READY NOW"].copy().sort_values(["Live Score","Live To Pivot %"],ascending=[False,True])
                    near_df=live_df[live_df["Stage"]=="NEAR-MISS NOW"].copy().sort_values(["Live Score","Live To Pivot %"],ascending=[False,True])
                    # Do not manufacture near-misses from WATCH stocks.
                    # A strict scanner is allowed to return fewer than five names
                    # (or zero) when the market does not meet the defined setup.
                status.write("5/5 — Validating only the strongest LIVE candidates historically…")
                names_df=pd.concat([x[["Stock"]] for x in (triggered_df,ready_df,near_df) if not x.empty],ignore_index=True) if any(not x.empty for x in (triggered_df,ready_df,near_df)) else pd.DataFrame(columns=["Stock"])
                names=list(dict.fromkeys([str(x).strip().upper() for x in names_df.Stock.tolist() if pd.notna(x) and str(x).strip()]))[:30]
                bt=backtest_candidates(names,mapping,nifty,cfg)
                for df in (ready_df,near_df,triggered_df):
                    if not df.empty and "Stock" in df.columns: df["Stock"]=df["Stock"].astype(str).str.strip().str.upper()
                if not bt.empty and "Stock" in bt.columns:
                    bt["Stock"]=bt["Stock"].astype(str).str.strip().str.upper()
                    if not ready_df.empty: ready_df=ready_df.merge(bt,on="Stock",how="left")
                    if not near_df.empty: near_df=near_df.merge(bt,on="Stock",how="left")
                    if not triggered_df.empty: triggered_df=triggered_df.merge(bt,on="Stock",how="left")
                # Historical evidence is now part of the final live ranking.
                ready_df=add_live_priority(ready_df)
                near_df=add_live_priority(near_df)
                triggered_df=add_live_priority(triggered_df)
                for _df_name in ("ready_df","near_df","triggered_df"):
                    _df=locals()[_df_name]
                    if not _df.empty:
                        _df.sort_values(["Live Priority Score","Live Breakout Pressure","Live To Pivot %"],ascending=[False,False,True],inplace=True)
                ready_df=add_trade_plan(ready_df,frames)
                near_df=add_trade_plan(near_df,frames)
                triggered_df=add_trade_plan(triggered_df,frames)
                for _df in (ready_df, triggered_df, near_df):
                    if not _df.empty:
                        _df["Risk Status"]=np.where(_df["Risk %"].fillna(999)<=10.0,"OK","HIGH RISK")
                        _df["Action"]=np.where(_df["Risk %"].fillna(999)<=10.0,"ACTIONABLE PLAN","WATCH ONLY — RISK > 10%")
                st.session_state.v21=dict(ready=ready_df,near=near_df,triggered=triggered_df,frames=frames,errors=errors,live_errors=live_errors,nifty=nifty,mapping=mapping,start=start,end=end,asof=asof,cfg=cfg,run_time=now_ist(),live_map=live_map)
                st.session_state.v21_job=None

            except Exception as e:
                status.update(label=f"Final analysis failed: {e}",state="error")
                st.exception(e)
                return

    state=st.session_state.v21
    if state is None:
        st.markdown("## How this scanner works")
        st.markdown("**500-stock structure → LIVE price at scan time → LIVE resistance test → breakout-now / ready-now / near-miss-now.**")
        st.info("Daily candles establish resistance/base/trend context. Every mapped stock is then re-evaluated from a fresh Upstox LTP + current-session OHLC + latest 1-minute candle on every RUN.")
        return

    ready,near,triggered=state["ready"],state["near"],state.get("triggered",pd.DataFrame())
    st.subheader("🟢 LIVE MARKET + PRE-BREAKOUT RESULTS")
    all_candidates=pd.concat([ready,near,triggered],ignore_index=True) if any(not x.empty for x in (ready,near,triggered)) else pd.DataFrame()
    backtested_count=all_candidates["Stock"].nunique() if not all_candidates.empty and "Stock" in all_candidates.columns else 0
    a,b,c,d=st.columns(4); a.metric("LIVE BREAKOUT",len(triggered)); b.metric("READY NOW",len(ready)); c.metric("NEAR-MISS NOW",len(near)); d.metric("BACKTESTED",min(30,backtested_count))
    mapped_count=len(state['mapping'].dropna(subset=['instrument_key']))
    if ready.empty and triggered.empty:
        st.warning("No LIVE BREAKOUT/READY NOW stock at this exact scan time. The NEAR-MISS NOW list is ranked by LIVE PRIORITY — immediate pressure + resistance proximity + structure + historical evidence.")
    st.caption(f"Data: {len(state['frames'])}/{mapped_count} mapped histories usable • LIVE scan time: {state['run_time'].strftime('%Y-%m-%d %H:%M:%S %Z')} • Live OHLC: Upstox V3 current session.")
    st.info("STRICT MODE: only genuine READY/NEAR-MISS/TRIGGERED setups are shown. We no longer fill the list with weak WATCH stocks. Historical success shown on the card is sample-adjusted; raw success and sample size remain visible below it.")

    def candidate_cards(df,title,limit=10):
        st.subheader(title)
        if df.empty:
            st.info("None today. That is a valid scanner result when the market does not meet the required structure."); return
        for _,r in df.head(limit).iterrows():
            adj_success=f"{float(r['Historical Adjusted Success %']):.1f}%" if pd.notna(r.get('Historical Adjusted Success %',np.nan)) else "—"
            raw_success=f"{float(r['Validated success %']):.1f}%" if pd.notna(r.get('Validated success %',np.nan)) else "—"
            success=adj_success
            occ=f"{int(r['Occurrences'])}" if pd.notna(r.get('Occurrences',np.nan)) else "0"
            b5=f"{float(r['Breakout <=5D %']):.1f}%" if pd.notna(r.get('Breakout <=5D %',np.nan)) else "—"
            b10=f"{float(r['Breakout <=10D %']):.1f}%" if pd.notna(r.get('Breakout <=10D %',np.nan)) else "—"
            with st.container(border=True):
                st.markdown(f"### {r['Stock']} • {r.get('Live Status','')}")
                x1,x2=st.columns(2); x1.metric("LIVE PRIORITY",f"{int(r.get('Live Priority Score',0))}/100"); x2.metric("Structure",f"{int(r.get('Score',0))}/100")
                x3,x4=st.columns(2); x3.metric("LTP",f"₹{float(r['LTP']):,.2f}" if pd.notna(r.get('LTP',np.nan)) else "—"); x4.metric("Historical success",success)
                x5,x6=st.columns(2); x5.metric("To Pivot",f"{float(r['Live To Pivot %']):.2f}%" if pd.notna(r.get('Live To Pivot %',np.nan)) else "—"); x6.metric("Live Pressure",f"{int(r.get('Live Breakout Pressure',0))}/100")
                y1,y2=st.columns(2); y1.metric("Live Position",f"{float(r.get('Live Position %',0)):.0f}%"); y2.metric("Evidence",str(r.get('Evidence Grade','—')))
                st.caption(f"Why: {r.get('Live Signal Reason','—')}")
                st.caption(f"7D range {r.get('7D Range %','—')}% • 20D range {r.get('20D Range %','—')}% • ATR {r.get('ATR5/ATR20','—')} • Vol {r.get('Vol5/Vol20','—')} • Resistance tests {r.get('Resistance Tests','—')}")
                adj=r.get('Historical Adjusted Success %',np.nan)
                adj_txt=f"{float(adj):.1f}%" if pd.notna(adj) else "—"
                st.caption(f"Backtest events: {occ} • Raw success: {raw_success} • Adjusted success: {adj_txt} • {r.get('Evidence Grade','—')}")
                st.caption(f"Breakout ≤5D: {b5} • ≤10D: {b10} • Avg max gain: {r.get('Avg max gain %','—')}% • Avg max DD: {r.get('Avg max drawdown %','—')}%")
                if pd.notna(r.get("Planned Entry",np.nan)):
                    p1,p2=st.columns(2); p1.metric("Planned Entry",f"₹{float(r['Planned Entry']):,.2f}"); p2.metric("Stop Loss",f"₹{float(r['Stop Loss']):,.2f}")
                    q1,q2,q3=st.columns(3); q1.metric("Target 1",f"₹{float(r['Target 1 (2R)']):,.2f}"); q2.metric("Target 2",f"₹{float(r['Target 2 (3R)']):,.2f}"); q3.metric("Target 3",f"₹{float(r['Target 3 (4R)']):,.2f}")
                    risk=float(r['Risk %'])
                    if np.isfinite(risk) and risk<=10: st.success(f"Risk {risk:.2f}% • ACTIONABLE PLAN • {r.get('Trade Plan','')}")
                    elif np.isfinite(risk): st.warning(f"Risk {risk:.2f}% • WATCH ONLY — RISK > 10%")

    candidate_cards(ready,"🎯 READY NOW — live price meets the pattern",5)
    candidate_cards(near,"🟡 NEAR-MISS NOW — closest live candidates",5)
    candidate_cards(triggered,"⚡ LIVE BREAKOUT NOW — resistance reached/broken",5)

    def show_table(df,title):
        if df.empty: return
        st.subheader(title + " — compact view")
        cols=["Stock","Stage","Live Priority Score","Score","Live Breakout Pressure","LTP","Live To Pivot %","Live Range %","Historical Adjusted Success %","Validated success %","Occurrences","Evidence Grade","Risk %","Action"]
        show=[x for x in cols if x in df.columns]
        st.dataframe(df[show].reset_index(drop=True),width="stretch",hide_index=True)
    show_table(triggered,"⚡ LIVE BREAKOUT NOW"); show_table(ready,"🎯 READY NOW"); show_table(near,"🟡 NEAR-MISS NOW")

    chart_df=ready if not ready.empty else near if not near.empty else triggered
    if not chart_df.empty:
        st.markdown("### 📊 Formation charts — daily candles + current live candle")
        st.caption("Completed daily structure + TODAY'S LIVE candle. The classification is recalculated from the LTP that existed when you pressed RUN FULL SCANNER.")
        try: import plotly.graph_objects as go
        except Exception: go=None
        for sym in chart_df.Stock.head(5).tolist():
            d=state["frames"].get(sym)
            if d is None or d.empty: continue
            r=chart_df[chart_df.Stock==sym].iloc[0]; hist=d.iloc[-20:].copy()
            if go is not None:
                fig=go.Figure(); fig.add_trace(go.Candlestick(x=hist.index,open=hist.open,high=hist.high,low=hist.low,close=hist.close,name="Completed"))
                live_ts=pd.Timestamp(r.get("Live TS")) if pd.notna(r.get("Live TS",pd.NaT)) else hist.index[-1]+pd.Timedelta(days=1)
                if live_ts.tzinfo is not None: live_ts=live_ts.tz_convert(IST).tz_localize(None)
                live_open=float(r["Live Open"]); live_high=float(r["Live High"]); live_low=float(r["Live Low"]); live_close=float(r["LTP"]); pivot=float(r["Pivot"])
                fig.add_trace(go.Candlestick(x=[live_ts],open=[live_open],high=[live_high],low=[live_low],close=[live_close],name="LIVE today"))
                fig.add_hline(y=pivot,line_dash="dash",line_width=2,annotation_text=f"Pivot ₹{pivot:,.2f}")
                fig.add_hline(y=live_close,line_dash="dot",line_width=1,annotation_text=f"LTP ₹{live_close:,.2f}")
                vals=list(hist.low.astype(float))+list(hist.high.astype(float))+[live_low,live_high,pivot,live_close]; lo=min(vals); hi=max(vals); pad=max((hi-lo)*0.08,max(abs(hi),1)*0.003)
                fig.update_layout(height=360,margin=dict(l=10,r=10,t=35,b=10),xaxis_rangeslider_visible=False,showlegend=True,title=f"{sym} • Score {int(r.Score)} • {r.get('Live Status','')}")
                fig.update_yaxes(range=[lo-pad,hi+pad],tickformat=",.0f")
                st.plotly_chart(fig,use_container_width=True,config={"displayModeBar":False})
                st.caption(f"Live OHLC: O ₹{live_open:,.2f} • H ₹{live_high:,.2f} • L ₹{live_low:,.2f} • LTP ₹{live_close:,.2f} • Live range {float(r['Live Range %']):.2f}% • Live/20D avg {float(r['Live Range / 20D Avg']):.2f}x • To pivot {float(r['Live To Pivot %']):.2f}%")

    with st.expander("🧪 Backtest definition",expanded=False):
        st.markdown(f"""
**Historical validation is candidate-only.** For each READY/NEAR-MISS/TRIGGERED stock, the engine walks historical daily candles and finds prior dates with a similar pre-breakout score (≥ {near_score}) while price remained below prior 20-session resistance.

- **Breakout ≤5D:** future daily close above historical resistance within 5 sessions.
- **Breakout ≤10D:** future daily close above historical resistance within 10 sessions.
- **Validated success:** breakout within the selected {forward_days}-session success window plus at least +3% follow-through without a severe adverse move dominating the event.

The 5D and 10D breakout rates are always calculated on their true horizons. These are historical event statistics, not guaranteed probabilities.
""")

    with st.expander("🩺 Data health",expanded=False):
        st.write(f"NIFTY 500 universe: {len(get_universe())}")
        st.write(f"Exact Upstox mappings: {mapped_count}/500")
        st.write(f"Completed daily histories usable: {len(state['frames'])}/500")
        st.write(f"History window: {state['start']} → {state['end']}")
        if state["errors"]: st.dataframe(pd.DataFrame([{"Stock":k,"Reason":v} for k,v in state["errors"].items()]),width="stretch",hide_index=True)
        usable_count=len(state["frames"])
        if usable_count == mapped_count: st.success(f"All {mapped_count} mapped stocks returned sufficient completed daily history.")
        else: st.warning(f"{usable_count} of {mapped_count} mapped stocks returned sufficient completed history. {mapped_count-usable_count} mapped stocks were excluded from scoring.")
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
- **Live decision layer:** the current Upstox LTP/current-session OHLC is used at the exact scan time; a stock can become LIVE BREAKOUT NOW without waiting for today's daily candle to close.
- **LIVE PRIORITY:** 35% live breakout pressure, 25% structure, 15% resistance proximity, 10% live position, 10% live compression, 5% historical evidence.
- **Evidence control:** raw historical success remains visible, but ranking uses a sample-adjusted rate and an evidence grade.
- Re-run the scanner later to recalculate the setup from the new live price.
""")
    st.caption("Pre-Breakout Hunter V24 • FINAL STRICT LIVE-PRIORITY engine • Upstox Analytics Token • no Yahoo • no order/trading API")

if __name__ == "__main__":
    main()
