import streamlit as st
import pandas as pd
import numpy as np
import requests
import gzip
import json
import time
import hashlib
from io import StringIO
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote
import threading

st.set_page_config(page_title="Pre-Breakout Hunter V10.1", page_icon="🎯", layout="wide")

APP_VERSION = "V10.1"
MIN_BARS = 230
HISTORY_DAYS = 420
HISTORY_CACHE_TTL = 1800
REQUEST_PACE_SECONDS = 0.035   # ~28 requests/sec, below Upstox 50/sec limit
HISTORY_WORKERS = 16

NIFTY500_URLS = [
    "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv",
    "https://raw.githubusercontent.com/pkjmesra/PKScreener/main/results/Indices/ind_nifty500list.csv",
]
UPSTOX_INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
UPSTOX_SEARCH_URL = "https://api.upstox.com/v2/instruments/search"
DEFAULTS = {
    "range5": 4.5,
    "range10": 7.5,
    "range20": 12.0,
    "atr_ratio": 0.72,
    "vol_ratio": 0.70,
    "pivot_min": 0.25,
    "pivot_max": 3.0,
    "min_score": 88,
}


def token():
    try:
        return str(st.secrets.get("UPSTOX_ACCESS_TOKEN", "")).strip()
    except Exception:
        return ""


def token_fingerprint():
    return hashlib.sha256(token().encode()).hexdigest()[:12]


def headers():
    return {"Accept": "application/json", "Authorization": f"Bearer {token()}"}


@st.cache_data(ttl=86400, show_spinner=False)
def get_nifty500_constituents():
    last_error = None
    for url in NIFTY500_URLS:
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 Pre-Breakout-Hunter-V10.1"}, timeout=20)
            r.raise_for_status()
            t = pd.read_csv(StringIO(r.text))
            cols = {str(c).strip().lower(): c for c in t.columns}
            sym_col = next((cols[k] for k in ("symbol", "ticker") if k in cols), None)
            if sym_col is None:
                raise RuntimeError("NIFTY 500 CSV has no Symbol column")
            sector_col = next((cols[k] for k in ("industry", "sector", "industry name", "industry_name") if k in cols), None)
            out = pd.DataFrame({"Symbol": t[sym_col].astype(str).str.strip().str.upper()})
            out["Sector"] = t[sector_col].astype(str).str.strip() if sector_col else "Other / Unclassified"
            out = out[(out.Symbol != "") & (out.Symbol.str.lower() != "nan")]
            out = out[~out.Symbol.str.startswith("DUMMY")]
            out = out.drop_duplicates("Symbol").reset_index(drop=True)
            if len(out) >= 480:
                return out.head(500)
            last_error = f"Only {len(out)} symbols returned"
        except Exception as e:
            last_error = str(e)
    raise RuntimeError(f"Could not load NIFTY 500 constituent list: {last_error}")


@st.cache_data(ttl=86400, show_spinner=False)
def get_upstox_equity_master():
    r = requests.get(UPSTOX_INSTRUMENTS_URL, headers={"User-Agent": "Mozilla/5.0 Pre-Breakout-Hunter-V10.1"}, timeout=30)
    r.raise_for_status()
    raw = gzip.decompress(r.content).decode("utf-8")
    data = json.loads(raw)
    rows = []
    for x in data:
        if not isinstance(x, dict):
            continue
        if str(x.get("segment", "")) != "NSE_EQ":
            continue
        if str(x.get("instrument_type", "")).upper() not in {"EQ", "A", "X"}:
            continue
        sym = str(x.get("trading_symbol", "")).strip().upper()
        key = str(x.get("instrument_key", "")).strip()
        if sym and key:
            rows.append({"Symbol": sym, "instrument_key": key})
    df = pd.DataFrame(rows).drop_duplicates("Symbol")
    if df.empty:
        raise RuntimeError("Upstox NSE_EQ instrument master returned no equities")
    return df


def repair_missing_symbols(mapping):
    """Repair only genuinely missing NIFTY500 symbols using Upstox Instrument Search."""
    out = mapping.copy()
    missing = out[out.instrument_key.isna()]["Symbol"].astype(str).tolist()
    if not missing:
        return out, []
    repaired = []
    for sym in missing:
        try:
            r = requests.get(
                UPSTOX_SEARCH_URL,
                headers=headers(),
                params={"query": sym, "exchanges": "NSE", "segments": "EQ", "records": 30, "page_number": 1},
                timeout=8,
            )
            if r.status_code != 200:
                continue
            items = (r.json() or {}).get("data", [])
            exact = None
            for item in items:
                if not isinstance(item, dict):
                    continue
                if str(item.get("segment", "")) == "NSE_EQ" and str(item.get("trading_symbol", "")).upper() == sym:
                    exact = str(item.get("instrument_key", "")).strip()
                    break
            if exact:
                out.loc[out.Symbol == sym, "instrument_key"] = exact
                repaired.append(sym)
        except Exception:
            continue
    return out, repaired


def build_mapping(constituents, master):
    c = constituents.copy()
    m = master.copy()
    c["Symbol"] = c["Symbol"].str.upper().str.strip()
    m["Symbol"] = m["Symbol"].str.upper().str.strip()
    mapping = c.merge(m, on="Symbol", how="left")
    mapping, repaired = repair_missing_symbols(mapping)
    return mapping, repaired


def parse_candles(payload):
    candles = (((payload or {}).get("data") or {}).get("candles"))
    if not isinstance(candles, list):
        return pd.DataFrame()
    rows = []
    for row in candles:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        try:
            ts = pd.to_datetime(row[0], utc=True).tz_convert("Asia/Kolkata").tz_localize(None).normalize()
            vals = [float(row[i]) for i in range(1, 6)]
            rows.append([ts, *vals])
        except Exception:
            continue
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
    return df.drop_duplicates("date").set_index("date").sort_index()


def strip_incomplete_today(df):
    if df.empty:
        return df
    today_ist = datetime.now(timezone.utc).astimezone().date()
    return df[df.index.date < today_ist].copy()


def fetch_history_network(symbol, instrument_key, start_date, end_date):
    url = f"https://api.upstox.com/v3/historical-candle/{quote(instrument_key, safe='')}/days/1/{end_date}/{start_date}"
    try:
        r = requests.get(url, headers=headers(), timeout=10)
        if r.status_code != 200:
            return symbol, pd.DataFrame(), f"HTTP {r.status_code}"
        df = strip_incomplete_today(parse_candles(r.json()))
        if len(df) < MIN_BARS:
            return symbol, df, f"Only {len(df)} completed daily bars"
        return symbol, df, ""
    except Exception as e:
        return symbol, pd.DataFrame(), str(e)[:160]


@st.cache_data(ttl=HISTORY_CACHE_TTL, show_spinner=False)
def fetch_histories(mapping, token_fp, _progress=None):
    end = datetime.now(timezone.utc).astimezone().date()
    start = end - timedelta(days=HISTORY_DAYS)
    valid = mapping.dropna(subset=["instrument_key"]).copy()
    jobs = list(valid[["Symbol", "instrument_key"]].itertuples(index=False, name=None))
    frames, errors = {}, {}
    total = len(jobs)
    completed = 0
    pace_lock = threading.Lock()
    last_request = [0.0]
    last_ui = [0.0]

    def paced_fetch(item):
        symbol, key = item
        with pace_lock:
            wait = REQUEST_PACE_SECONDS - (time.monotonic() - last_request[0])
            if wait > 0:
                time.sleep(wait)
            last_request[0] = time.monotonic()
        return fetch_history_network(symbol, key, start.isoformat(), end.isoformat())

    with ThreadPoolExecutor(max_workers=HISTORY_WORKERS) as ex:
        futures = [ex.submit(paced_fetch, item) for item in jobs]
        for fut in as_completed(futures):
            symbol, df, err = fut.result()
            if not df.empty:
                frames[symbol] = df
            if err:
                errors[symbol] = err
            completed += 1
            if _progress and (completed == total or completed % 10 == 0 or time.monotonic() - last_ui[0] >= 0.5):
                _progress(completed, total)
                last_ui[0] = time.monotonic()
    return frames, errors, start, end


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_nifty_history_cached(start_date, end_date, token_fp):
    url = f"https://api.upstox.com/v3/historical-candle/{quote('NSE_INDEX|Nifty 50', safe='')}/days/1/{end_date}/{start_date}"
    r = requests.get(url, headers=headers(), timeout=10)
    if r.status_code != 200:
        raise RuntimeError(f"NIFTY 50 history HTTP {r.status_code}")
    df = strip_incomplete_today(parse_candles(r.json()))
    if len(df) < MIN_BARS:
        raise RuntimeError(f"NIFTY 50 history returned only {len(df)} completed daily bars")
    return df["close"]


def fetch_live_ltp(keys):
    rows, errors = {}, []
    for i in range(0, len(keys), 200):
        chunk = keys[i:i + 200]
        try:
            r = requests.get(
                "https://api.upstox.com/v3/market-quote/ltp",
                headers=headers(),
                params={"instrument_key": ",".join(chunk)},
                timeout=10,
            )
            if r.status_code != 200:
                errors.append(f"LTP chunk {i//200 + 1}: HTTP {r.status_code}")
                continue
            data = (r.json() or {}).get("data", {})
            if isinstance(data, dict):
                for _, item in data.items():
                    if isinstance(item, dict) and item.get("instrument_token"):
                        rows[str(item["instrument_token"])] = item
        except Exception as e:
            errors.append(f"LTP chunk {i//200 + 1}: {str(e)[:120]}")
    return rows, errors


def atr(df, n):
    prev = df.close.shift(1)
    tr = pd.concat([(df.high-df.low), (df.high-prev).abs(), (df.low-prev).abs()], axis=1).max(axis=1)
    return tr.rolling(n, min_periods=n).mean()


def compute_metrics(d, nifty):
    c, h, l, v = d.close, d.high, d.low, d.volume
    sma50, sma150, sma200 = c.rolling(50).mean(), c.rolling(150).mean(), c.rolling(200).mean()
    ema20 = c.ewm(span=20, adjust=False).mean()
    a5, a20 = atr(d, 5), atr(d, 20)
    r5 = (h.iloc[-5:].max()-l.iloc[-5:].min()) / l.iloc[-5:].min() * 100
    r10 = (h.iloc[-10:].max()-l.iloc[-10:].min()) / l.iloc[-10:].min() * 100
    r20 = (h.iloc[-20:].max()-l.iloc[-20:].min()) / l.iloc[-20:].min() * 100
    pivot = h.iloc[-21:-1].max()
    close = float(c.iloc[-1])
    distance = (pivot-close) / pivot * 100 if pivot else np.nan
    v20 = v.iloc[-20:].mean()
    vr = float(v.iloc[-5:].mean() / v20) if v20 else np.inf
    ar = float(a5.iloc[-1] / a20.iloc[-1]) if a20.iloc[-1] else np.inf
    n = nifty.reindex(d.index).ffill()
    rs = ((close / c.iloc[-21] - 1) - (n.iloc[-1] / n.iloc[-21] - 1)) * 100 if len(n.dropna()) >= 22 else np.nan
    prev20 = h.rolling(20).max().shift(1)
    near = (c.iloc[-15:] >= prev20.iloc[-15:] * 0.98) & (c.iloc[-15:] < prev20.iloc[-15:])
    tests = int(near.sum())
    base_hi, base_lo = h.iloc[-20:].max(), l.iloc[-20:].min()
    pos = (close-base_lo)/(base_hi-base_lo) if base_hi > base_lo else 0
    bodies = (d.open-d.close).abs()/d.close*100
    body20 = bodies.iloc[-20:].mean()
    body_ratio = float(bodies.iloc[-5:].mean()/body20) if body20 else np.inf
    low_a, low_b, low_c = l.iloc[-5:].min(), l.iloc[-10:-5].min(), l.iloc[-15:-10].min()
    higher_lows = bool(low_a >= low_b*0.995 and low_b >= low_c*0.995)
    base = d.iloc[-20:]
    down = base.loc[base.close < base.open, "volume"].mean()
    up = base.loc[base.close >= base.open, "volume"].mean()
    dist_ratio = float(down/up) if np.isfinite(down) and np.isfinite(up) and up else 0
    high252, low252 = h.iloc[-252:].max(), l.iloc[-252:].min()
    pos52 = (close-low252)/(high252-low252)*100 if high252 > low252 else np.nan
    return locals()


def evaluate(d, nifty, cfg):
    if len(d) < MIN_BARS:
        return None, None
    d = d.sort_index().copy()
    m = compute_metrics(d, nifty)
    x, c, h = d.iloc[-1], d.close, d.high
    sma50, sma150, sma200 = m["sma50"].iloc[-1], m["sma150"].iloc[-1], m["sma200"].iloc[-1]
    sma200_20 = m["sma200"].iloc[-21]
    ar, vr, distance, rs = m["ar"], m["vr"], m["distance"], m["rs"]
    trend = bool(x.close > sma50 > sma150 > sma200 and sma200 > sma200_20 and x.close > m["ema20"].iloc[-1])
    ranges = bool(m["r5"] <= cfg["range5"] and m["r10"] <= cfg["range10"] and m["r20"] <= cfg["range20"] and m["r5"] < m["r10"] < m["r20"])
    compression = bool(ar <= cfg["atr_ratio"] and vr <= cfg["vol_ratio"] and m["body_ratio"] <= 0.80)
    pivot_ok = bool(cfg["pivot_min"] <= distance <= cfg["pivot_max"] and x.close < m["pivot"])
    prev20 = h.rolling(20).max().shift(1)
    no_break = bool((c.iloc[-5:] < prev20.iloc[-5:]).all())
    gates = {
        "Trend": trend,
        "Progressive contraction": ranges,
        "ATR + volume compression": compression,
        "Resistance still ahead": pivot_ok,
        "No recent breakout": no_break,
        "Higher lows": m["higher_lows"],
        "No distribution": m["dist_ratio"] <= 1.20,
        "Relative strength": rs >= 1.0,
    }
    score = 0
    score += 12 if m["r5"] <= 4.0 else 7
    score += 10 if m["r10"] <= 7.0 else 6
    score += 8 if m["r20"] <= 10.0 else 5
    score += 10 if ar <= 0.65 else 6
    score += 8 if vr <= 0.60 else 5
    score += 8 if m["body_ratio"] <= 0.65 else 4
    score += 12 if m["tests"] >= 3 else (8 if m["tests"] == 2 else 4)
    score += 10 if distance <= 1.25 else (7 if distance <= 2 else 4)
    score += 7 if m["pos"] >= 0.70 else (4 if m["pos"] >= 0.55 else 2)
    score += 7 if rs >= 4 else (4 if rs >= 2 else 2)
    score += 5 if m["higher_lows"] else 0
    score += 3 if m["dist_ratio"] <= 0.90 else 1
    result = {
        "Score": int(score), "Close": round(float(x.close), 2),
        "5D Range %": round(float(m["r5"]), 2), "10D Range %": round(float(m["r10"]), 2), "20D Range %": round(float(m["r20"]), 2),
        "ATR5/ATR20": round(float(ar), 2), "Vol5/Vol20": round(float(vr), 2), "Resistance Tests": m["tests"],
        "Pivot": round(float(m["pivot"]), 2), "To Pivot %": round(float(distance), 2),
        "Base Position %": round(float(m["pos"]*100), 1), "RS vs Nifty %": round(float(rs), 2),
        "Body Ratio": round(float(m["body_ratio"]), 2), "52W Position %": round(float(m["pos52"]), 1),
        "Trend": "PASS" if trend else "FAIL",
    }
    if all(gates.values()) and score >= cfg["min_score"]:
        result["Status"] = "🔥 A+ READY" if score >= 94 else "🟢 A WATCH"
        return result, gates
    return None, gates


def near_miss(d, nifty, cfg):
    if len(d) < MIN_BARS:
        return None
    m = compute_metrics(d.sort_index(), nifty)
    x = d.iloc[-1]
    sma50, sma150, sma200 = m["sma50"].iloc[-1], m["sma150"].iloc[-1], m["sma200"].iloc[-1]
    score = 0
    score += 12 if x.close > sma50 > sma150 > sma200 else 0
    score += 10 if m["r5"] < m["r10"] < m["r20"] else 0
    score += 10 if m["ar"] <= 0.85 else 0
    score += 10 if m["vr"] <= 0.85 else 0
    score += 10 if m["higher_lows"] else 0
    score += 10 if m["tests"] >= 2 else (5 if m["tests"] == 1 else 0)
    score += 10 if 0 < m["distance"] <= 5 else 0
    score += 10 if m["rs"] >= 0 else 0
    score += 8 if m["pos"] >= 0.60 else 0
    score += 5 if m["body_ratio"] <= 1 else 0
    if score < 68:
        return None
    return {
        "Score": int(score), "Close": round(float(x.close),2), "To Pivot %": round(float(m["distance"]),2),
        "5D Range %": round(float(m["r5"]),2), "10D Range %": round(float(m["r10"]),2),
        "20D Range %": round(float(m["r20"]),2), "ATR5/ATR20": round(float(m["ar"]),2),
        "Vol5/Vol20": round(float(m["vr"]),2), "Resistance Tests": m["tests"], "RS vs Nifty %": round(float(m["rs"]),2),
    }


def sector_strength(mapping, live_by_key):
    rows = []
    for _, r in mapping.dropna(subset=["instrument_key"]).iterrows():
        q = live_by_key.get(str(r.instrument_key))
        if not q:
            continue
        ltp, cp = q.get("last_price"), q.get("cp")
        if isinstance(ltp, (int,float)) and isinstance(cp, (int,float)) and cp:
            rows.append((r.Sector, (ltp/cp-1)*100))
    if not rows:
        return pd.DataFrame(columns=["Sector","Breadth %","Avg Change %","Strength"])
    df = pd.DataFrame(rows, columns=["Sector","Change %"])
    g = df.groupby("Sector")
    out = g.agg(Breadth_pct=("Change %", lambda s: float((s>0).mean()*100)), Avg_Change_pct=("Change %","mean"), Stocks=("Change %","size")).reset_index()
    out["Strength"] = out["Avg_Change_pct"]*0.6 + (out["Breadth_pct"]-50)*0.04
    return out.sort_values("Strength", ascending=False).reset_index(drop=True)


def run_scan(mapping, cfg, progress_box):
    if not token():
        raise RuntimeError("UPSTOX_ACCESS_TOKEN is missing from Streamlit Secrets")
    progress_bar = progress_box.progress(0, text="Downloading completed daily candles from Upstox…")
    def progress(done, total):
        progress_bar.progress(done/max(total,1), text=f"Downloading daily candles from Upstox… {done}/{total}")
    frames, errors, start, end = fetch_histories(mapping, token_fingerprint(), progress)
    progress_bar.progress(0.98, text="Downloading NIFTY 50 benchmark…")
    nifty = fetch_nifty_history_cached(start.isoformat(), end.isoformat(), token_fingerprint())
    progress_bar.progress(1.0, text="Daily history ready")
    return frames, errors, nifty, start, end


def main():
    st.title(f"🎯 Pre-Breakout Hunter {APP_VERSION}")
    st.caption("Upstox read-only data • completed daily candles for setup • fresh LTP only for current market position")
    if not token():
        st.error("UPSTOX_ACCESS_TOKEN is not available. Add it under Streamlit → Settings → Secrets.")
        return
    try:
        constituents = get_nifty500_constituents()
        master = get_upstox_equity_master()
        mapping, repaired = build_mapping(constituents, master)
    except Exception as e:
        st.error(f"Universe setup failed: {e}")
        return
    mapped = mapping.dropna(subset=["instrument_key"]).copy()
    st.info(f"Universe: {len(constituents)} NIFTY 500 constituents • Upstox mapping: {len(mapped)}/{len(constituents)}")
    if repaired:
        st.success(f"Repaired {len(repaired)} missing Upstox mappings: {', '.join(repaired)}")
    missing = mapping[mapping.instrument_key.isna()]["Symbol"].tolist()
    if missing:
        st.warning(f"Still unmapped: {', '.join(missing)}. These are excluded rather than guessed.")
    if len(mapped) < 480:
        st.error("Upstox instrument mapping is incomplete. Scan is blocked to prevent an inaccurate universe.")
        return
    with st.sidebar:
        st.header("Scanner settings")
        min_score = st.slider("Minimum score", 80, 100, DEFAULTS["min_score"])
        range5 = st.number_input("5D max range %", 2.0, 8.0, DEFAULTS["range5"], 0.25)
        range10 = st.number_input("10D max range %", 4.0, 15.0, DEFAULTS["range10"], 0.25)
        range20 = st.number_input("20D max range %", 7.0, 25.0, DEFAULTS["range20"], 0.5)
        atr_ratio = st.number_input("Max ATR5/ATR20", 0.50, 1.00, DEFAULTS["atr_ratio"], 0.01)
        vol_ratio = st.number_input("Max Vol5/Vol20", 0.40, 1.00, DEFAULTS["vol_ratio"], 0.01)
        pivot_min = st.number_input("Min distance to pivot %", 0.10, 3.0, DEFAULTS["pivot_min"], 0.05)
        pivot_max = st.number_input("Max distance to pivot %", 1.0, 6.0, DEFAULTS["pivot_max"], 0.25)
        cfg = {"min_score":min_score,"range5":range5,"range10":range10,"range20":range20,"atr_ratio":atr_ratio,"vol_ratio":vol_ratio,"pivot_min":pivot_min,"pivot_max":pivot_max}
        scan_button = st.button("🔎 SCAN NIFTY 500", type="primary", width="stretch")
    if "v10_result" not in st.session_state:
        st.session_state.v10_result = None
    if scan_button:
        progress_box = st.empty()
        try:
            frames, errors, nifty, start, end = run_scan(mapping, cfg, progress_box)
            progress_box.empty()
            results, near = [], []
            for sym, df in frames.items():
                try:
                    r, _ = evaluate(df, nifty, cfg)
                    if r:
                        r["Stock"] = sym
                        results.append(r)
                    else:
                        nm = near_miss(df, nifty, cfg)
                        if nm:
                            nm["Stock"] = sym
                            near.append(nm)
                except Exception:
                    continue
            result_df, near_df = pd.DataFrame(results), pd.DataFrame(near)
            if not result_df.empty:
                result_df = result_df.sort_values(["Score","To Pivot %"], ascending=[False,True]).reset_index(drop=True)
            if not near_df.empty:
                near_df = near_df.sort_values(["Score","To Pivot %"], ascending=[False,True]).head(20).reset_index(drop=True)
            st.session_state.v10_result = {"results":result_df,"near":near_df,"frames":frames,"errors":errors,"nifty":nifty,"mapping":mapping,"start":start,"end":end,"fetched_at":datetime.now(timezone.utc).astimezone(),"cfg":cfg}
        except Exception as e:
            progress_box.empty()
            st.error(f"Scan failed safely: {e}")
            return
    state = st.session_state.v10_result
    if state is None:
        st.markdown("### How V10.1 works")
        st.markdown("**Pattern:** strong trend → progressive contraction → quiet volume → higher lows → repeated resistance tests → improving relative strength → resistance still ahead.")
        st.warning("Run the scan. V10.1 uses completed daily candles only; it never manufactures intraday candles.")
        return
    frames, mapping = state["frames"], state["mapping"]
    results, near, errors = state["results"], state["near"], state["errors"]
    fetched_at = state["fetched_at"]
    live_by_key, live_errors = fetch_live_ltp(mapping.dropna(subset=["instrument_key"]).instrument_key.astype(str).tolist())
    rows = []
    for _, r in mapping.dropna(subset=["instrument_key"]).iterrows():
        q = live_by_key.get(str(r.instrument_key))
        if not q:
            continue
        ltp, cp, vol = q.get("last_price"), q.get("cp"), q.get("volume")
        if isinstance(ltp, (int,float)) and isinstance(cp, (int,float)):
            rows.append({"Stock":r.Symbol,"Sector":r.Sector,"LTP":round(float(ltp),2),"Prev Close":round(float(cp),2),"Live Change %":round((float(ltp)/float(cp)-1)*100,2),"Live Volume":int(vol) if isinstance(vol,(int,float)) else np.nan})
    live_df = pd.DataFrame(rows)
    st.subheader("🟢 Upstox live market snapshot")
    a,b,c,d = st.columns(4)
    a.metric("Live quotes", f"{len(live_df)}/{len(mapping.dropna(subset=['instrument_key']))}")
    b.metric("Setup candidates", str(len(results)))
    c.metric("Near-miss watchlist", str(len(near)))
    d.metric("Daily data fetched", f"{len(frames)}/{len(mapping.dropna(subset=['instrument_key']))}")
    st.caption(f"Completed daily history fetched: {fetched_at.strftime('%Y-%m-%d %H:%M:%S %Z')} • live LTP fetched after scan")
    if live_errors:
        st.warning("Some live LTP chunks failed: " + "; ".join(live_errors[:3]))
    sectors = sector_strength(mapping, live_by_key)
    if not sectors.empty:
        st.subheader("📊 Live sector strength")
        st.dataframe(sectors.head(15), width="stretch", hide_index=True)
    if not results.empty:
        st.subheader("🎯 Best pre-breakout candidates")
        merged = results.merge(live_df[["Stock","LTP","Live Change %"]], on="Stock", how="left") if not live_df.empty else results.copy()
        merged["Live vs Pivot %"] = (merged["Pivot"]-merged["LTP"])/merged["Pivot"]*100 if "LTP" in merged else np.nan
        cols = ["Stock","Score","Status","LTP","Live Change %","Close","Pivot","Live vs Pivot %","To Pivot %","5D Range %","10D Range %","20D Range %","ATR5/ATR20","Vol5/Vol20","Resistance Tests","RS vs Nifty %","52W Position %"]
        st.dataframe(merged[[c for c in cols if c in merged.columns]], width="stretch", hide_index=True)
        st.success("Watchlist candidates only. A daily closing breakout above the pivot is still required.")
    else:
        st.warning("No A/A+ pre-breakout candidates met every hard gate today. V10.1 does not loosen rules to force a trade.")
    if not near.empty:
        st.subheader("🟡 Near-miss watchlist")
        st.caption("Close structurally, but at least one hard gate is missing. Not buy signals.")
        st.dataframe(near, width="stretch", hide_index=True)
    with st.expander("🩺 Data health / exact failures"):
        st.write(f"Successful completed daily histories: {len(frames)} / {len(mapping.dropna(subset=['instrument_key']))}")
        st.write(f"History window requested: {state['start']} → {state['end']} (today's incomplete candle excluded)")
        st.write(f"Historical cache: {HISTORY_CACHE_TTL//60} minutes")
        if errors:
            err_df = pd.DataFrame([{"Stock":k,"Error":v} for k,v in list(errors.items())[:50]])
            st.dataframe(err_df, width="stretch", hide_index=True)
        else:
            st.success("No daily-history errors.")
    with st.expander("📐 Exact V10.1 setup rules"):
        st.markdown("""
- Strong trend: Close > SMA50 > SMA150 > SMA200, rising SMA200, and Close > EMA20.
- Progressive contraction: 5D range < 10D range < 20D range and each below configured maximum.
- Volatility contraction: ATR5/ATR20 and 5D/20D volume ratios below configured limits; candle-body contraction required.
- Structure: higher lows, repeated resistance tests, and price 0.25–3% below the prior 20-session pivot by default.
- No recent breakout: the last five completed closes remain below their prior 20-session highs.
- Relative strength: stock outperforms NIFTY 50 over the recent 20-session window.
- Distribution filter: average down-day volume does not materially dominate up-day volume.
- Final score must meet the minimum score, with all hard gates passing.
""")
    st.caption("V10.1 architecture: Upstox read-only Analytics Token → NIFTY500 mapping → completed daily history → deterministic scanner → fresh Upstox LTP snapshot. No trading API or WebSocket is used.")


if __name__ == "__main__":
    main()
