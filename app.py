import streamlit as st
import pandas as pd
import numpy as np
import requests, gzip, json, hashlib, time
from io import StringIO
from datetime import datetime, timedelta, timezone, time as dtime
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote
from zoneinfo import ZoneInfo

st.set_page_config(page_title="Pre-Breakout Hunter V29", page_icon="🎯", layout="wide")

APP_VERSION = "V29.0 FINAL — STRICT + SMART LIVE RADAR + ROBUST LIVE DATA + FAST REFRESH"
IST = ZoneInfo("Asia/Kolkata")
MIN_BARS = 230
HISTORY_CAL_DAYS = 390
BACKTEST_CAL_DAYS = 1825
HISTORY_CACHE_TTL = 1800
WORKERS = 24
REQUESTS_PER_SEC = 42
REQUEST_INTERVAL = 1.0 / REQUESTS_PER_SEC
HISTORY_BATCH_SIZE = 100
HISTORY_TIMEOUT = 8

NIFTY500_URLS = [
    "https://raw.githubusercontent.com/pkjmesra/PKScreener/main/results/Indices/ind_nifty500list.csv",
    "https://raw.githubusercontent.com/sswapnil2/tradingview-mcp-india/main/src/tradingview_mcp/coinlist/nse.txt",
]
INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
SEARCH_URL = "https://api.upstox.com/v2/instruments/search"
HISTORY_URL = "https://api.upstox.com/v3/historical-candle"
OHLC_URL = "https://api.upstox.com/v3/market-quote/ohlc"
FULL_QUOTES_URL = "https://api.upstox.com/v3/market-quote/quotes"

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
    last_err = ""
    for attempt in range(3):
        try:
            r = requests.get(url, headers=headers(), timeout=HISTORY_TIMEOUT)
            if r.status_code == 200:
                df = parse_candles(r.json())
                end_ts = pd.Timestamp(end)
                df = df[df.index <= end_ts]
                if len(df) < MIN_BARS:
                    return symbol, df, f"Only {len(df)} completed daily bars (need {MIN_BARS})"
                return symbol, df, ""
            last_err = f"HTTP {r.status_code}"
            if r.status_code not in (429, 500, 502, 503, 504):
                return symbol, pd.DataFrame(), last_err
            time.sleep(0.75 * (attempt + 1))
        except Exception as e:
            last_err = str(e)[:180]
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
    return symbol, pd.DataFrame(), last_err


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


def full_market_quotes(keys):
    """Fresh V3 full-market snapshot. Upstox accepts up to 500 instruments/request."""
    rows, errors = {}, []
    for i in range(0, len(keys), 500):
        batch = keys[i:i+500]
        try:
            r = requests.get(FULL_QUOTES_URL, headers=headers(),
                             params={"instrument_key": ",".join(batch)}, timeout=15)
            if r.status_code != 200:
                errors.append(f"FULL QUOTES HTTP {r.status_code}")
                continue
            data = (r.json() or {}).get("data") or {}
            response_ts = (r.json() or {}).get("timestamp")
            for quote_name, item in data.items():
                if not isinstance(item, dict):
                    continue
                o = item.get("ohlc") or {}
                if not o:
                    continue
                token_key = str(item.get("instrument_token") or "")
                normalized = {
                    "last_price": item.get("last_price", o.get("close")),
                    "instrument_token": token_key,
                    "live_ohlc": {
                        "open": o.get("open"), "high": o.get("high"),
                        "low": o.get("low"), "close": o.get("close"),
                        "volume": o.get("volume", item.get("volume", 0)),
                        "ts": o.get("ts"),
                    },
                    "_quote_name": str(quote_name),
                    "_fresh_ts": item.get("last_trade_time") or response_ts,
                    "_full_quote": True,
                }
                rows[str(quote_name)] = normalized
                if token_key:
                    rows[token_key] = normalized
        except Exception as e:
            errors.append(f"FULL QUOTES: {str(e)[:160]}")
    return rows, errors


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
    """Re-grade the complete mapped universe from fresh Upstox live data.

    STRICT stages are deliberately unchanged: READY/NEAR-MISS/BREAKOUT NOW still
    require the established daily structure score.  A separate EVENT RADAR is
    calculated independently from live resistance testing, live range/volume,
    intraday position and 1-minute pressure.  Event Radar never promotes a
    stock into the strict lists.
    """
    if structure_df.empty:
        return structure_df.copy(), pd.DataFrame()
    key_to_sym=dict(zip(mapping.instrument_key,mapping.Symbol))
    lrows=[]; seen=set(); now=now or now_ist()
    for key,item in live_map.items():
        item=item or {}; sym=key_to_sym.get(key); q=str(item.get("_quote_name",""))
        if not sym and ":" in q: sym=q.split(":",1)[1].strip().upper()
        if not sym or sym in seen: continue
        lo=item.get("live_ohlc") or {}
        try:
            raw_ts=item.get("_fresh_ts") or lo.get("ts")
            if isinstance(raw_ts, str):
                raw_clean=raw_ts.strip()
                if raw_clean.isdigit() and len(raw_clean)>=12:
                    ts=pd.to_datetime(int(raw_clean),unit="ms",utc=True).tz_convert(IST)
                else:
                    ts=pd.to_datetime(raw_clean,utc=True).tz_convert(IST)
            elif raw_ts is not None:
                ts=pd.to_datetime(raw_ts,unit="ms",utc=True).tz_convert(IST)
            else:
                ts=pd.NaT
            if pd.isna(ts): continue
            ltp=float(item.get("last_price",lo.get("close"))); op=float(lo.get("open")); hi=float(lo.get("high")); low=float(lo.get("low")); vol=float(lo.get("volume",0))
            age=max(0.0,(now-ts.to_pydatetime()).total_seconds())
            market_open=now.weekday()<5 and dtime(9,15)<=now.time()<=dtime(15,35)
            fresh=(age<=180) if market_open else False
            minute=item.get("minute_ohlc") or {}; pminute=item.get("prev_minute_ohlc") or {}
            lrows.append({"Stock":sym,"LTP":ltp,"Live Open":op,"Live High":hi,"Live Low":low,"Live Volume":vol,
                          "Live TS":ts,"Live Age Sec":age,"Live Fresh":fresh,
                          "Minute Open":float(minute.get("open")) if minute.get("open") is not None else np.nan,
                          "Minute High":float(minute.get("high")) if minute.get("high") is not None else np.nan,
                          "Minute Low":float(minute.get("low")) if minute.get("low") is not None else np.nan,
                          "Minute Close":float(minute.get("close")) if minute.get("close") is not None else np.nan,
                          "Minute Volume":float(minute.get("volume",0)) if minute.get("volume") is not None else np.nan,
                          "Prev Minute Close":float(pminute.get("close")) if pminute.get("close") is not None else np.nan})
            seen.add(sym)
        except Exception: continue
    ld=pd.DataFrame(lrows)
    if ld.empty:
        empty = structure_df.copy()
        for c, default in [("LTP",np.nan),("Live High",np.nan),("Live Low",np.nan),("Live Volume",np.nan),("Live Fresh",False),("Live Position %",np.nan),("Live Breakout Pressure",0)]:
            empty[c]=default
        return empty, pd.DataFrame()
    out=structure_df.copy(); out["Stock"]=out["Stock"].astype(str).str.strip().str.upper(); ld["Stock"]=ld.Stock.str.upper()
    out=out.merge(ld,on="Stock",how="left")
    out["Live To Pivot %"]=(out["Pivot"]-out["LTP"])/out["Pivot"]*100
    out["Live Range %"]=(out["Live High"]-out["Live Low"])/out["LTP"]*100
    out["Live Position %"]=np.where(out["Live High"]>out["Live Low"],100*(out["LTP"]-out["Live Low"])/(out["Live High"]-out["Live Low"]),np.nan)
    out["Live Range / 20D Avg"]=np.nan; out["Live Volume / 20D Avg"]=np.nan; out["Live To 52W High %"]=np.nan; out["20D Avg Daily Range %"]=np.nan
    for i,r in out.iterrows():
        d=frames.get(r["Stock"])
        if d is None or d.empty: continue
        avg_rng=float(((d.high-d.low)/d.low*100).iloc[-20:].mean()); avg_vol=float(d.volume.iloc[-20:].mean()); hi52=float(d.high.iloc[-252:].max())
        out.at[i,"20D Avg Daily Range %"]=avg_rng
        out.at[i,"Live Range / 20D Avg"]=float(r["Live Range %"])/avg_rng if avg_rng else np.nan
        out.at[i,"Live Volume / 20D Avg"]=float(r["Live Volume"])/avg_vol if avg_vol else np.nan
        out.at[i,"Live To 52W High %"]=(hi52-float(r["LTP"]))/hi52*100 if hi52 else np.nan
    out["Live Compression"]=np.where(out["Live Range / 20D Avg"]<=1.0,"YES","NO")
    out["Live Pivot Pressure"]=np.where((out["Live High"]>=out["Pivot"]*0.99)&(out["LTP"]<out["Pivot"]),"YES","NO")
    out["Live Minute Pressure"]=np.where((out["Minute High"]>=out["Pivot"]*0.995)&(out["LTP"]<out["Pivot"]),"YES","NO")

    base=out["Score"].fillna(0).astype(float); dist=out["Live To Pivot %"]
    pressure=pd.Series(0.0,index=out.index)
    pressure += np.where((dist>=0)&(dist<=5),np.clip(30-dist*6,0,30),0)
    pressure += np.where((dist>=0)&(dist<=1),20,np.where((dist>=0)&(dist<=2),12,0))
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
        if pd.notna(d): rr.append("at/above resistance" if d<=0 else "within 1% of resistance" if d<=1 else "within 2% of resistance" if d<=2 else "within 5% of resistance" if d<=5 else "too far from resistance")
        if str(r.get("Live Pivot Pressure","NO"))=="YES": rr.append("session high tested pivot")
        if str(r.get("Live Minute Pressure","NO"))=="YES": rr.append("latest 1m pressure")
        if float(r.get("Live Position %",0) or 0)>=80: rr.append("strong intraday position")
        if float(r.get("Live Range / 20D Avg",0) or 0)>=1.20: rr.append("range expansion")
        if float(r.get("Live Volume / 20D Avg",0) or 0)>=1.50: rr.append("volume expansion")
        if float(r.get("Score",0) or 0)<min_score: rr.append("structure below READY")
        reasons.append(" • ".join(rr[:4]) if rr else "insufficient live pressure")
    out["Live Signal Reason"]=reasons

    # ---------------- STRICT PATH: unchanged philosophy ----------------
    strict_structure=out["Score"]>=min_score
    breakout=(out["LTP"]>=out["Pivot"])&strict_structure&(out["Live Fresh"]==True)&(out["Live Breakout Pressure"]>=55)
    ready=(out["LTP"]<out["Pivot"])&(out["Live To Pivot %"]>=0)&(out["Live To Pivot %"]<=3.5)&(out["Score"]>=min_score)&(out["Live Breakout Pressure"]>=45)&(out["Live Fresh"]==True)
    near=(out["LTP"]<out["Pivot"])&(out["Live To Pivot %"]>=0)&(out["Live To Pivot %"]<=5.0)&(out["Score"]>=near_score)
    out["Live Status"]=np.select([breakout,ready,near],["⚡ LIVE BREAKOUT NOW","🟢 READY NOW","🟡 NEAR-MISS NOW"],default="WATCH")
    out["Stage"]=np.select([breakout,ready,near],["BREAKOUT NOW","READY NOW","NEAR-MISS NOW"],default="WATCH")
    strict=add_live_priority(out)
    stage_order=pd.Categorical(strict["Stage"],categories=["BREAKOUT NOW","READY NOW","NEAR-MISS NOW","WATCH"],ordered=True)
    strict["_stage_order"]=stage_order
    strict=strict.sort_values(["_stage_order","Live Priority Score","Live Breakout Pressure","Live To Pivot %"],ascending=[True,False,False,True]).drop(columns=["_stage_order"])

    # ---------------- SMART LIVE EVENT RADAR ----------------
    # Completely independent from READY/NEAR-MISS.  The strict scanner is not
    # loosened.  The event radar distinguishes a fresh resistance cross from
    # an already-extended move so that "BREAKOUT NOW" remains actionable.
    fresh=out["Live Fresh"]==True
    dist_above=(out["LTP"]-out["Pivot"])/out["Pivot"]*100
    below_dist=(out["Pivot"]-out["LTP"])/out["Pivot"]*100
    close_to_resistance=(dist_above>=-2.50)&(dist_above<=3.00)
    resistance_test=out["Live High"]>=out["Pivot"]*0.997
    position_ok=out["Live Position %"]>=60
    range_ok=out["Live Range / 20D Avg"]>=0.80
    volume_ok=out["Live Volume / 20D Avg"]>=1.25
    strong_range=out["Live Range / 20D Avg"]>=1.20
    strong_volume=out["Live Volume / 20D Avg"]>=1.50
    exceptional_live=strong_volume&(out["Live Range / 20D Avg"]>=1.50)&(out["Live Position %"]>=80)
    minute_test=(out["Minute High"]>=out["Pivot"]*0.997)|(out["Minute Close"]>=out["Pivot"]*0.997)
    # Minimum quality floor: normal events need at least moderate daily
    # structure; only an exceptional live surge can bypass that floor.
    structure_ok=(out["Score"]>=40)|exceptional_live
    participation=volume_ok|range_ok
    quality_floor=fresh&resistance_test&position_ok&participation&structure_ok

    # A) Fresh cross: keep the "NOW" bucket tight.  Do not call a stock that
    # is already >2% above resistance a breakout-at-resistance signal.
    breakout_now=(quality_floor & (dist_above>=0) & (dist_above<=2.0) &
                  (out["Live Position %"]>=70) &
                  (strong_volume | (strong_range & minute_test)))

    # B) Still breaking, but already 2-3% above resistance.  Requires stronger
    # participation so this does not become a duplicate of BREAKOUT NOW.
    confirming=(quality_floor & (dist_above>2.0) & (dist_above<=3.0) &
                (out["Live Position %"]>=70) & strong_volume & strong_range)

    # C) Resistance was crossed intraday but price is back below it.
    tested=(quality_floor & (below_dist>=0) & (below_dist<=2.50) &
            (out["LTP"]<out["Pivot"]) & (out["Live High"]>=out["Pivot"]) &
            (out["Live Position %"]>=60))

    # D) A tested level is being reclaimed right now.  This gets priority over
    # a generic TESTED label and needs fresh 1-minute pressure + volume.
    reclaim=(fresh & (out["LTP"]<out["Pivot"]) & (below_dist<=0.75) &
             (out["Live High"]>=out["Pivot"]) & (out["Live Position %"]>=65) &
             strong_volume & minute_test & structure_ok)

    # E) The move has already travelled >3% above resistance.  Keep it visible
    # for context, but explicitly label it EXTENDED rather than BREAKOUT NOW.
    extended=(fresh & resistance_test & (dist_above>3.0) &
               (out["Live Position %"]>=65) & (strong_volume|strong_range) &
               (out["Score"]>=40))

    # F) Failed test: resistance was crossed but price has rejected materially.
    failed=(fresh & (out["LTP"]<out["Pivot"]) & (below_dist>2.50) &
            (out["Live High"]>=out["Pivot"]) & (out["Live Position %"]<60) &
            participation & structure_ok)

    event_quality=breakout_now|confirming|reclaim|tested|extended|failed
    ev=out[event_quality].copy()
    if not ev.empty:
        idx=ev.index
        ev["Event Status"]=np.select(
            [breakout_now.loc[idx],confirming.loc[idx],reclaim.loc[idx],tested.loc[idx],extended.loc[idx],failed.loc[idx]],
            ["⚡ BREAKOUT NOW","🟢 BREAKOUT CONFIRMING","🔄 BREAKOUT RECLAIMING","🚨 BREAKOUT TESTED","🔵 EXTENDED BREAKOUT","🔴 FAILED BREAKOUT"],
            default="🚨 BREAKOUT TESTED")

        # Event quality is a ranking score only; it never changes the strict
        # scanner's score or thresholds.
        resistance_score=np.where((dist_above>=-1)&(dist_above<=2),100,
                           np.where((dist_above>2)&(dist_above<=3),80,
                           np.where(dist_above>3,45,70)))
        range_score=np.clip(ev["Live Range / 20D Avg"].fillna(0)*45,0,100)
        volume_score=np.clip(ev["Live Volume / 20D Avg"].fillna(0)*30,0,100)
        position_score=np.clip(ev["Live Position %"].fillna(0),0,100)
        structure_score=np.clip(ev["Score"].fillna(0),0,100)
        ev["Event Quality Score"]=(
            0.30*resistance_score[idx] + 0.20*range_score.loc[idx] +
            0.25*volume_score.loc[idx] + 0.15*position_score.loc[idx] +
            0.10*structure_score.loc[idx]
        ).round().clip(0,100).astype(int)

        reasons=[]
        for _,r in ev.iterrows():
            d=float(r.get("Live To Pivot %",np.nan)) if pd.notna(r.get("Live To Pivot %",np.nan)) else np.nan
            rr=[]
            if pd.notna(d):
                if d>=0 and d<=2: rr.append("fresh resistance cross within 2%")
                elif d>2 and d<=3: rr.append("breakout 2–3% above resistance")
                elif d>3: rr.append("already >3% above resistance — extended")
                elif d>=-0.75: rr.append("testing/reclaiming resistance")
                else: rr.append("rejected below resistance")
            if float(r.get("Live Volume / 20D Avg",0) or 0)>=1.50: rr.append(f"volume {float(r['Live Volume / 20D Avg']):.2f}×")
            elif float(r.get("Live Volume / 20D Avg",0) or 0)>=1.25: rr.append("volume participation")
            if float(r.get("Live Range / 20D Avg",0) or 0)>=1.20: rr.append(f"range {float(r['Live Range / 20D Avg']):.2f}×")
            elif float(r.get("Live Range / 20D Avg",0) or 0)>=0.80: rr.append("range expansion")
            if float(r.get("Live Position %",0) or 0)>=80: rr.append("strong intraday position")
            elif float(r.get("Live Position %",0) or 0)>=65: rr.append("good intraday position")
            if pd.notna(r.get("Score")) and float(r.get("Score",0))<72: rr.append(f"structure {int(r['Score'])}/100")
            reasons.append(" • ".join(rr[:5]) if rr else "live resistance event")
        ev["Event Reason"]=reasons
        # Priority: status first, then event quality and proximity.  A stock
        # 7% above resistance therefore cannot outrank a clean fresh cross.
        status_order={"⚡ BREAKOUT NOW":0,"🟢 BREAKOUT CONFIRMING":1,"🔄 BREAKOUT RECLAIMING":2,"🚨 BREAKOUT TESTED":3,"🔵 EXTENDED BREAKOUT":4,"🔴 FAILED BREAKOUT":5}
        ev["_event_order"]=ev["Event Status"].map(status_order).fillna(9)
        ev=ev.sort_values(["_event_order","Event Quality Score","Live Breakout Pressure","Live Position %"],ascending=[True,False,False,False]).drop(columns=["_event_order"])
    return strict,ev

    return strict,ev

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


def perform_live_refresh(state, mapping, cfg):
    """Fast path: reuse completed daily history and refresh only live data."""
    frames=state["frames"]; start=state["start"]; end=state["end"]; nifty=state["nifty"]
    live_day,day_errors=full_market_quotes(mapping.instrument_key.astype(str).tolist())
    live_min,minute_errors=live_ohlc(mapping.instrument_key.astype(str).tolist(),interval="I1")
    live_map=merge_live_maps(live_day,live_min)
    pool=[]
    result_cols=["Stock","Stage","Score","Close","Pivot","To Pivot %","5D Range %","7D Range %","10D Range %","20D Range %","ATR5/ATR20","Vol5/Vol20","Resistance Tests","RS vs Nifty %","To 52W High %","Higher Lows"]
    for sym,df in frames.items():
        ev=evaluate(df,nifty,cfg)
        if ev: pool.append(row_from_eval(sym,ev,"WATCH"))
    pool_df=pd.DataFrame(pool,columns=result_cols)
    strict_df,event_df=live_regrade(pool_df,frames,live_map,mapping,min_score=cfg["min_score"],near_score=cfg["near_score"],now=now_ist())
    if strict_df.empty or "LTP" not in strict_df.columns:
        ready=pd.DataFrame(columns=result_cols); near=pd.DataFrame(columns=result_cols); triggered=pd.DataFrame(columns=result_cols)
    else:
        strict_df=strict_df[strict_df["LTP"].notna()].copy()
        triggered=strict_df[strict_df.Stage=="BREAKOUT NOW"].copy()
        ready=strict_df[strict_df.Stage=="READY NOW"].copy()
        near=strict_df[strict_df.Stage=="NEAR-MISS NOW"].copy()
    names=[]
    for df in (triggered,ready,near):
        if not df.empty: names.extend(df.Stock.astype(str).str.upper().tolist())
    names=list(dict.fromkeys(names))[:30]
    bt=backtest_candidates(names,mapping,nifty,cfg)
    for df in (triggered,ready,near):
        if not df.empty: df["Stock"]=df["Stock"].astype(str).str.upper()
    if not bt.empty:
        bt["Stock"]=bt["Stock"].astype(str).str.upper()
        if not ready.empty: ready=ready.merge(bt,on="Stock",how="left")
        if not near.empty: near=near.merge(bt,on="Stock",how="left")
        if not triggered.empty: triggered=triggered.merge(bt,on="Stock",how="left")
    ready=add_live_priority(ready); near=add_live_priority(near); triggered=add_live_priority(triggered)
    ready=add_trade_plan(ready,frames); near=add_trade_plan(near,frames); triggered=add_trade_plan(triggered,frames)
    for df in (ready,near,triggered):
        if not df.empty:
            df["Risk Status"]=np.where(df["Risk %"].fillna(999)<=10,"OK","HIGH RISK")
            df["Action"]=np.where(df["Risk %"].fillna(999)<=10,"ACTIONABLE PLAN","WATCH ONLY — RISK > 10%")
    out=dict(state)
    out.update(ready=ready,near=near,triggered=triggered,event=event_df,live_errors=day_errors+minute_errors,live_map=live_map,run_time=now_ist())
    return out


def render_candidate_cards(df,title,limit=5):
    st.subheader(title)
    if df.empty:
        st.info("None at this scan time. Zero candidates is a valid strict result.")
        return
    for _,r in df.head(limit).iterrows():
        adj=f"{float(r['Historical Adjusted Success %']):.1f}%" if pd.notna(r.get('Historical Adjusted Success %',np.nan)) else "—"
        raw=f"{float(r['Validated success %']):.1f}%" if pd.notna(r.get('Validated success %',np.nan)) else "—"
        with st.container(border=True):
            st.markdown(f"### {r['Stock']} • {r.get('Live Status','')}")
            a,b=st.columns(2); a.metric("LIVE PRIORITY",f"{int(r.get('Live Priority Score',0))}/100"); b.metric("STRUCTURE",f"{int(r.get('Score',0))}/100")
            a,b=st.columns(2); a.metric("LTP",f"₹{float(r['LTP']):,.2f}"); b.metric("HIST. SUCCESS",adj)
            a,b=st.columns(2); a.metric("TO PIVOT",f"{float(r['Live To Pivot %']):.2f}%"); b.metric("LIVE PRESSURE",f"{int(r.get('Live Breakout Pressure',0))}/100")
            a,b=st.columns(2); a.metric("LIVE POSITION",f"{float(r.get('Live Position %',0)):.0f}%"); b.metric("EVIDENCE",str(r.get('Evidence Grade','—')))
            st.caption(f"Why: {r.get('Live Signal Reason','—')}")
            st.caption(f"7D range {r.get('7D Range %','—')}% • 20D range {r.get('20D Range %','—')}% • ATR {r.get('ATR5/ATR20','—')} • Vol {r.get('Vol5/Vol20','—')} • Resistance tests {r.get('Resistance Tests','—')}")
            occ=int(r.get('Occurrences',0) or 0); b5=r.get('Breakout <=5D %','—'); b10=r.get('Breakout <=10D %','—')
            st.caption(f"Backtest events: {occ} • Raw success: {raw} • Breakout ≤5D: {b5}% • ≤10D: {b10}%")
            if pd.notna(r.get('Planned Entry',np.nan)):
                a,b=st.columns(2); a.metric("PLANNED ENTRY",f"₹{float(r['Planned Entry']):,.2f}"); b.metric("STOP LOSS",f"₹{float(r['Stop Loss']):,.2f}")


def render_event_radar(event):
    st.subheader("🚨 LIVE EVENT RADAR — separate from the strict scanner")
    st.caption("This tab NEVER promotes a stock into READY/NEAR-MISS. It separates fresh crosses, confirming moves, tested/reclaiming levels, extended breakouts, and failed tests using fresh Upstox data.")
    if event.empty:
        st.success("No qualifying live breakout event at this scan time.")
        return
    for _,r in event.head(10).iterrows():
        with st.container(border=True):
            st.markdown(f"### {r['Stock']} • {r.get('Event Status','🚨 BREAKOUT TESTED')}")
            a,b=st.columns(2); a.metric("EVENT QUALITY",f"{int(r.get('Event Quality Score',0))}/100"); b.metric("LTP",f"₹{float(r['LTP']):,.2f}")
            a,b=st.columns(2); a.metric("RESISTANCE",f"₹{float(r['Pivot']):,.2f}"); b.metric("TO PIVOT",f"{float(r['Live To Pivot %']):.2f}%")
            a,b=st.columns(2); a.metric("SESSION HIGH",f"₹{float(r['Live High']):,.2f}"); b.metric("LIVE POSITION",f"{float(r['Live Position %']):.0f}%")
            a,b=st.columns(2); a.metric("RANGE / 20D",f"{float(r['Live Range / 20D Avg']):.2f}x"); b.metric("VOL / 20D",f"{float(r['Live Volume / 20D Avg']):.2f}x")
            st.caption(f"Why: {r.get('Event Reason','—')}")
            st.caption(f"Market state: {'OPEN / LIVE' if (9*60+15) <= (now_ist().hour*60+now_ist().minute) <= (15*60+35) and now_ist().weekday()<5 else 'CLOSED / FINAL SESSION DATA'}")
            st.caption(f"Structure score: {int(r.get('Score',0))}/100 • Live pressure: {int(r.get('Live Breakout Pressure',0))}/100 • 1m high: ₹{float(r.get('Minute High',np.nan)):.2f} if available")
    cols=[c for c in ["Stock","Event Status","Event Quality Score","LTP","Pivot","Live To Pivot %","Live High","Live Position %","Live Range / 20D Avg","Live Volume / 20D Avg","Score","Live Breakout Pressure"] if c in event.columns]
    st.dataframe(event[cols].head(20).reset_index(drop=True),width="stretch",hide_index=True)


def render_chart(state, dfs):
    chart_df=next((x for x in dfs if not x.empty),pd.DataFrame())
    if chart_df.empty: return
    st.markdown("### 📊 Formation chart")
    try: import plotly.graph_objects as go
    except Exception: return
    for sym in chart_df.Stock.head(3).tolist():
        d=state["frames"].get(sym)
        if d is None or d.empty: continue
        r=chart_df[chart_df.Stock==sym].iloc[0]; hist=d.iloc[-20:].copy()
        fig=go.Figure()
        fig.add_trace(go.Candlestick(x=hist.index,open=hist.open,high=hist.high,low=hist.low,close=hist.close,name="Completed"))
        if pd.notna(r.get("Live TS",pd.NaT)):
            ts=pd.Timestamp(r["Live TS"])
            if ts.tzinfo is not None: ts=ts.tz_convert(IST).tz_localize(None)
            fig.add_trace(go.Candlestick(x=[ts],open=[float(r["Live Open"])],high=[float(r["Live High"])],low=[float(r["Live Low"])],close=[float(r["LTP"])],name="LIVE today"))
        pivot=float(r["Pivot"]); ltp=float(r["LTP"])
        fig.add_hline(y=pivot,line_dash="dash",line_width=2,annotation_text=f"Pivot ₹{pivot:,.2f}")
        fig.add_hline(y=ltp,line_dash="dot",line_width=1,annotation_text=f"LTP ₹{ltp:,.2f}")
        vals=list(hist.low.astype(float))+list(hist.high.astype(float))+[float(r["Live Low"]),float(r["Live High"]),pivot,ltp]
        lo,hi=min(vals),max(vals); pad=max((hi-lo)*0.08,max(abs(hi),1)*0.003)
        fig.update_layout(height=360,margin=dict(l=8,r=8,t=30,b=8),xaxis_rangeslider_visible=False,title=f"{sym} • {r.get('Live Status',r.get('Event Status',''))}")
        fig.update_yaxes(range=[lo-pad,hi+pad],tickformat=",.0f")
        st.plotly_chart(fig,use_container_width=True,config={"displayModeBar":False})


def main():
    st.title(f"🎯 Pre-Breakout Hunter {APP_VERSION}")
    st.caption("Strict pre-breakout scanner + independent live breakout-event radar + fast live refresh")
    if not token():
        st.error("UPSTOX_ACCESS_TOKEN is missing from Streamlit Secrets."); return
    try: universe=get_universe(); mapping,repaired=build_mapping()
    except Exception as e:
        st.error(f"Universe / Upstox mapping failed: {e}"); return
    mapped=mapping.dropna(subset=["instrument_key"]).copy()
    st.info(f"NIFTY 500: {len(universe)} • Upstox mapping: {len(mapped)}/{len(universe)}")
    if repaired: st.success(f"Repaired mappings: {', '.join(repaired)}")
    if len(mapped)<490:
        st.error("Mapping coverage below 490/500. Scan is blocked."); return

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
        live_refresh=st.button("⚡ LIVE REFRESH — no history download",width="stretch")

    if "v27_job" not in st.session_state: st.session_state.v27_job=None
    if "v27" not in st.session_state: st.session_state.v27=None

    # Fast path: today's completed history already exists in state; only live data is refreshed.
    if live_refresh and st.session_state.v27 is not None:
        state=st.session_state.v27
        if state.get("asof")==last_completed_weekday():
            try:
                with st.spinner("Refreshing live Upstox 1D + 1-minute data…"):
                    new_state=perform_live_refresh(state,mapping,cfg)
                st.session_state.v27=new_state
                st.success("Live refresh complete — no 500-stock historical download was performed.")
            except Exception as e:
                st.error(f"Live refresh failed: {e}")
        else:
            st.warning("Cached history is from an older trading session. Run the full scanner once to rebuild today's structure.")

    if scan and st.session_state.v27_job is None:
        asof=last_completed_weekday(); jobs=list(mapped[["Symbol","instrument_key"]].itertuples(index=False,name=None))
        st.session_state.v27_job=dict(asof=asof,start=asof-timedelta(days=HISTORY_CAL_DAYS),end=asof,jobs=jobs,idx=0,batch_size=HISTORY_BATCH_SIZE,frames={},errors={},cfg=cfg,mapping=mapping,repaired=repaired,created=now_ist())
        st.session_state.v27=None
        st.rerun()

    job=st.session_state.v27_job
    if job is not None:
        total=len(job["jobs"]); idx=job["idx"]
        if idx<total:
            batch=job["jobs"][idx:min(idx+job["batch_size"],total)]
            st.subheader("🔄 Building scanner history")
            st.progress(min(idx/total,1.0),text=f"Daily history: {idx}/{total} stocks completed")
            st.caption("First scan of a new trading session downloads completed daily history. Batches are committed separately and cached.")
            try:
                bf,be=fetch_history_batch(batch,job["start"],job["end"],token_fp()); job["frames"].update(bf); job["errors"].update(be); job["idx"]+=len(batch); st.session_state.v27_job=job
                st.success(f"Batch complete: {job['idx']}/{total} • usable histories {len(job['frames'])}"); st.rerun()
            except Exception as e:
                st.error(f"History batch failed safely: {e}"); return
        else:
            frames=job["frames"]; errors=job["errors"]; mapping=job["mapping"]; asof=job["asof"]; start=job["start"]; end=job["end"]; cfg=job["cfg"]
            if len(frames)<450:
                st.error(f"Only {len(frames)}/{total} mapped stocks returned sufficient history. Scan blocked to protect data quality. Retry RUN FULL SCANNER after a short wait."); st.session_state.v27_job=None; return
            if len(frames)<490:
                st.warning(f"History coverage {len(frames)}/{total}. Scanner will continue with the usable mapped stocks; missing histories are excluded rather than inventing data.")
            status=st.status("Finishing full scan…",expanded=True)
            try:
                status.write("1/4 — Loading NIFTY 50 reference…")
                nifty=fetch_nifty(start,end,token_fp())
                status.write("2/4 — Building strict daily structure for the mapped universe…")
                pool=[]; result_cols=["Stock","Stage","Score","Close","Pivot","To Pivot %","5D Range %","7D Range %","10D Range %","20D Range %","ATR5/ATR20","Vol5/Vol20","Resistance Tests","RS vs Nifty %","To 52W High %","Higher Lows"]
                for sym,df in frames.items():
                    ev=evaluate(df,nifty,cfg)
                    if ev: pool.append(row_from_eval(sym,ev,"WATCH"))
                pool_df=pd.DataFrame(pool,columns=result_cols)
                status.write("3/4 — Fetching FRESH Upstox live 1D + 1-minute data…")
                live_day,day_errors=full_market_quotes(mapped.instrument_key.astype(str).tolist()); live_min,minute_errors=live_ohlc(mapped.instrument_key.astype(str).tolist(),"I1")
                live_map=merge_live_maps(live_day,live_min)
                status.write("4/4 — Applying strict stages and separate LIVE EVENT RADAR…")
                strict_df,event_df=live_regrade(pool_df,frames,live_map,mapping,min_score=cfg["min_score"],near_score=cfg["near_score"],now=now_ist())
                if strict_df.empty or "LTP" not in strict_df.columns:
                    triggered=pd.DataFrame(columns=result_cols); ready=pd.DataFrame(columns=result_cols); near=pd.DataFrame(columns=result_cols)
                else:
                    triggered=strict_df[strict_df.Stage=="BREAKOUT NOW"].copy(); ready=strict_df[strict_df.Stage=="READY NOW"].copy(); near=strict_df[strict_df.Stage=="NEAR-MISS NOW"].copy()
                names=[]
                for df in (triggered,ready,near):
                    if not df.empty: names.extend(df.Stock.astype(str).str.upper().tolist())
                names=list(dict.fromkeys(names))[:30]
                bt=backtest_candidates(names,mapping,nifty,cfg)
                for df in (triggered,ready,near):
                    if not df.empty: df["Stock"]=df["Stock"].astype(str).str.upper()
                if not bt.empty:
                    bt["Stock"]=bt["Stock"].astype(str).str.upper()
                    if not ready.empty: ready=ready.merge(bt,on="Stock",how="left")
                    if not near.empty: near=near.merge(bt,on="Stock",how="left")
                    if not triggered.empty: triggered=triggered.merge(bt,on="Stock",how="left")
                ready=add_live_priority(ready); near=add_live_priority(near); triggered=add_live_priority(triggered)
                ready=add_trade_plan(ready,frames); near=add_trade_plan(near,frames); triggered=add_trade_plan(triggered,frames)
                for df in (ready,near,triggered):
                    if not df.empty:
                        df["Risk Status"]=np.where(df["Risk %"].fillna(999)<=10,"OK","HIGH RISK"); df["Action"]=np.where(df["Risk %"].fillna(999)<=10,"ACTIONABLE PLAN","WATCH ONLY — RISK > 10%")
                st.session_state.v27=dict(ready=ready,near=near,triggered=triggered,event=event_df,frames=frames,errors=errors,live_errors=day_errors+minute_errors,nifty=nifty,mapping=mapping,start=start,end=end,asof=asof,cfg=cfg,run_time=now_ist(),live_map=live_map)
                st.session_state.v27_job=None
                status.update(label="Full scan complete",state="complete")
                st.rerun()
            except Exception as e:
                status.update(label=f"Full scan failed: {e}",state="error"); st.exception(e); return

    state=st.session_state.v27
    if state is None:
        st.markdown("## How this scanner works")
        st.markdown("**500-stock completed-history structure → fresh live Upstox 1D/I1 → strict scanner + separate event radar.**")
        st.info("RUN FULL SCANNER builds/caches daily structure. After that, use ⚡ LIVE REFRESH for fresh live conditions without downloading 500 histories again.")
        return

    ready,near,triggered=state["ready"],state["near"],state["triggered"]; event=state.get("event",pd.DataFrame())
    tabs=st.tabs(["🎯 STRICT SCANNER","🚨 LIVE EVENT RADAR","📊 FORMATION CHART"])
    with tabs[0]:
        st.subheader("🎯 STRICT PRE-BREAKOUT SCANNER")
        a,b,c,d=st.columns(4); a.metric("LIVE BREAKOUT",len(triggered)); b.metric("READY NOW",len(ready)); c.metric("NEAR-MISS",len(near)); d.metric("BACKTESTED",min(30,len(pd.concat([ready,near,triggered],ignore_index=True))))
        st.caption(f"History: {len(state['frames'])}/{len(state['mapping'])} mapped usable • scan: {state['run_time'].strftime('%Y-%m-%d %H:%M:%S %Z')}")
        st.info("Strict rules were NOT loosened. Event Radar never promotes a stock into these lists.")
        render_candidate_cards(ready,"🎯 READY NOW",5); render_candidate_cards(near,"🟡 NEAR-MISS NOW",5); render_candidate_cards(triggered,"⚡ LIVE BREAKOUT NOW",5)
        cols=["Stock","Stage","Live Priority Score","Score","Live Breakout Pressure","LTP","Live To Pivot %","Live Range %","Historical Adjusted Success %","Validated success %","Occurrences","Evidence Grade","Risk %","Action"]
        all_strict=pd.concat([triggered,ready,near],ignore_index=True) if any(not x.empty for x in (triggered,ready,near)) else pd.DataFrame()
        if not all_strict.empty:
            st.dataframe(all_strict[[x for x in cols if x in all_strict.columns]].head(30),width="stretch",hide_index=True)
        with st.expander("🧪 Backtest definition"):
            st.markdown(f"Historical validation is candidate-only. Breakout ≤5D and ≤10D are calculated on their true horizons. Validated success uses the selected {state['cfg']['forward_days']}-session window plus follow-through; these are historical statistics, not guarantees.")

    with tabs[1]:
        render_event_radar(event)
        if not event.empty:
            render_chart(state,[event])

    with tabs[2]:
        chart_source=ready if not ready.empty else near if not near.empty else triggered
        render_chart(state,[chart_source])

    with st.expander("🩺 Data health",expanded=False):
        mapped_count=len(state["mapping"].dropna(subset=["instrument_key"]))
        st.write(f"NIFTY 500 universe: {len(universe)}")
        st.write(f"Exact Upstox mappings: {mapped_count}/{len(universe)}")
        st.write(f"Completed daily histories usable: {len(state['frames'])}/{mapped_count}")
        st.write(f"History window: {state['start']} → {state['end']}")
        st.write(f"Last live refresh: {state['run_time'].strftime('%Y-%m-%d %H:%M:%S %Z')}")
        if state.get("errors"): st.dataframe(pd.DataFrame([{"Stock":k,"Reason":v} for k,v in state["errors"].items()]),width="stretch",hide_index=True)
        if state.get("live_errors"): st.warning("Live OHLC partial errors: "+"; ".join(state["live_errors"][:3]))

    with st.expander("📐 Strategy rules",expanded=False):
        st.markdown("""
**STRICT:** Strong trend, progressive 5D/10D/20D contraction, ATR/volume compression, higher lows, repeated resistance tests, near 20-session resistance, 52-week positioning, no recent breakout/distribution and relative strength. These rules are unchanged.

**LIVE EVENT RADAR:** independent. It requires fresh data plus a genuine resistance test and meaningful intraday participation. It distinguishes BREAKOUT NOW (0–2% above resistance), BREAKOUT CONFIRMING (2–3%), BREAKOUT TESTED/RECLAIMING, EXTENDED BREAKOUT (>3%), and FAILED BREAKOUT. It never changes the strict scanner.

**FAST REFRESH:** once today's completed history is cached, ⚡ LIVE REFRESH makes only fresh live-market requests and recalculates the strict/event tabs. It does not re-download 500 daily histories.
""")
    st.caption("Pre-Breakout Hunter V27 • Strict scanner + separate Live Event Radar + Fast Live Refresh • Upstox V3 • no Yahoo • no order API")


if __name__=="__main__":
    main()
