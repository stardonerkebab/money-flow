#!/usr/bin/env python3
"""
Money Flow Daily Digest
=======================
Scans the market each day, scores which economic sectors are attracting (or
losing) big money, and emails you a clean HTML digest.

Data source : Yahoo Finance via the free `yfinance` library (no API key needed).
Email        : Any SMTP provider (Gmail app-password setup documented in README).

Run a real scan + email:   python3 money_flow_digest.py
Preview without emailing:   python3 money_flow_digest.py --no-email
Use fake data (offline):    python3 money_flow_digest.py --test --no-email
"""

import argparse
import datetime as dt
import os
import smtplib
import ssl
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

# ----------------------------------------------------------------------------
# TIMEZONE  — all "today" references use this so dates match Spain local time.
# ----------------------------------------------------------------------------
LOCAL_TZ = ZoneInfo("Europe/Madrid")

def local_today():
    """Return today's date in Spain local time (not UTC)."""
    return dt.datetime.now(LOCAL_TZ).date()

# ----------------------------------------------------------------------------
# CONFIG  — edit these, or set the matching environment variables.
# ----------------------------------------------------------------------------
SMTP_HOST = os.getenv("MF_SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("MF_SMTP_PORT", "587"))
SMTP_USER = os.getenv("MF_SMTP_USER", "youremail@gmail.com")      # your email
SMTP_PASS = os.getenv("MF_SMTP_PASS", "your-app-password")        # app password
EMAIL_TO  = os.getenv("MF_EMAIL_TO",  SMTP_USER)                  # where to send

# ---- Phone notifications (no password needed) ----
# Install the free "ntfy" app on your phone, subscribe to a topic name you make
# up, and put that EXACT name here. Anything sent to it pops up on your phone.
# Pick something long and hard to guess (topics are public).
NTFY_TOPIC = os.getenv("MF_NTFY_TOPIC", "moneyflowjaswindersinghkaur")

# If a pick's share price is above this, the digest also surfaces a cheaper
# alternative (lower-priced name in the same sector with strong flow).
PRICE_THRESHOLD = float(os.getenv("MF_PRICE_THRESHOLD", "50"))

# ----------------------------------------------------------------------------
# WHAT WE TRACK
# ----------------------------------------------------------------------------
# The 11 S&P sector SPDR ETFs — the cleanest proxy for sector capital flows.
SECTORS = {
    "XLK":  "Technology",
    "XLF":  "Financials",
    "XLE":  "Energy",
    "XLV":  "Healthcare",
    "XLY":  "Consumer Discretionary",
    "XLP":  "Consumer Staples",
    "XLI":  "Industrials",
    "XLB":  "Materials",
    "XLRE": "Real Estate",
    "XLU":  "Utilities",
    "XLC":  "Communication Services",
}

# For each sector, a handful of its largest / most liquid constituent stocks.
# When a sector is hot, we score these and surface the leaders so you know
# which specific names (not just the broad ETF) are pulling capital.
STOCK_UNIVERSE = {
    "XLK":  ["AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "CRM", "AMD", "ADBE"],
    "XLF":  ["BRK-B", "JPM", "V", "MA", "BAC", "WFC", "GS", "AXP"],
    "XLE":  ["XOM", "CVX", "COP", "SLB", "EOG", "MPC", "PSX"],
    "XLV":  ["LLY", "UNH", "JNJ", "ABBV", "MRK", "TMO", "ABT"],
    "XLY":  ["AMZN", "TSLA", "HD", "MCD", "NKE", "LOW", "SBUX"],
    "XLP":  ["PG", "COST", "WMT", "KO", "PEP", "PM", "MDLZ"],
    "XLI":  ["GE", "CAT", "RTX", "UBER", "BA", "HON", "UNP", "DE"],
    "XLB":  ["LIN", "SHW", "FCX", "ECL", "NEM", "APD"],
    "XLRE": ["PLD", "AMT", "EQIX", "WELL", "SPG", "O", "DLR"],
    "XLU":  ["NEE", "SO", "DUK", "CEG", "AEP", "D", "EXC"],
    "XLC":  ["META", "GOOGL", "NFLX", "DIS", "TMUS", "GOOG", "EA"],
}

# Macro reference instruments shown at the top of the digest.
MACRO = {
    "^GSPC":    "S&P 500",
    "^TNX":     "US 10Y Yield",
    "GC=F":     "Gold",
    "CL=F":     "Crude Oil",
    "DX-Y.NYB": "US Dollar (DXY)",
    "BTC-USD":  "Bitcoin",
}


# ----------------------------------------------------------------------------
# DATA
# ----------------------------------------------------------------------------
def fetch_history(tickers, period="1y"):
    """Return {ticker: pandas.DataFrame} of recent daily OHLCV bars."""
    import yfinance as yf
    out = {}
    data = yf.download(
        list(tickers), period=period, interval="1d",
        group_by="ticker", auto_adjust=False, progress=False, threads=True,
    )
    for t in tickers:
        try:
            df = data[t].dropna()
            if len(df) >= 6:
                out[t] = df
        except Exception:
            pass
    return out


def make_fake_history(tickers):
    """Synthetic data so the script can be demoed with no network access."""
    import numpy as np
    import pandas as pd
    rng = np.random.default_rng(7)
    out = {}
    days = pd.date_range(end=local_today(), periods=260, freq="B")
    for i, t in enumerate(tickers):
        # separate short-term and long-term drift so the two scores diverge
        long_drift = rng.normal(0.0006 * (1 if i % 2 else -1), 0.0004)
        rets = rng.normal(long_drift, 0.012, len(days))
        # inject a recent burst (last ~10 sessions) for some names -> ST > LT
        if i % 4 == 0:
            rets[-10:] += 0.004
        elif i % 4 == 1:
            rets[-10:] -= 0.004
        base = float(rng.choice([18, 35, 47, 88, 140, 260]))
        close = base * (1 + pd.Series(rets, index=days)).cumprod()
        vol = pd.Series(rng.integers(8_000_000, 30_000_000, len(days)), index=days)
        if i % 3 == 0:
            vol.iloc[-1] = int(vol.iloc[-1] * 2.1)
        out[t] = pd.DataFrame({
            "Open": close * 0.998, "High": close * 1.01,
            "Low": close * 0.99, "Close": close, "Volume": vol,
        })
    return out


def fetch_fundamentals(tickers):
    """Pull valuation + quality metrics per ticker from Yahoo Finance."""
    import yfinance as yf
    out = {}
    for t in tickers:
        try:
            info = yf.Ticker(t).info
            out[t] = {
                "pe": info.get("trailingPE"),
                "fpe": info.get("forwardPE"),
                "pb": info.get("priceToBook"),
                "roe": info.get("returnOnEquity"),
                "margin": info.get("profitMargins"),
                "d2e": info.get("debtToEquity"),
                "rev_growth": info.get("revenueGrowth"),
            }
        except Exception:
            pass
    return out


def make_fake_fundamentals(tickers):
    """Synthetic fundamentals so the watchlist can be demoed offline."""
    import numpy as np
    rng = np.random.default_rng(11)
    out = {}
    for t in tickers:
        out[t] = {
            "pe": round(float(rng.uniform(8, 40)), 1),
            "fpe": round(float(rng.uniform(7, 32)), 1),
            "pb": round(float(rng.uniform(0.8, 9)), 1),
            "roe": round(float(rng.uniform(-0.05, 0.40)), 3),
            "margin": round(float(rng.uniform(-0.05, 0.35)), 3),
            "d2e": round(float(rng.uniform(10, 220)), 1),
            "rev_growth": round(float(rng.uniform(-0.10, 0.35)), 3),
        }
    return out


# ----------------------------------------------------------------------------
# SCORING  — turn price + volume into a "money flow" signal
# ----------------------------------------------------------------------------
def pct(series, lookback):
    """Percent change over the last `lookback` sessions."""
    if len(series) <= lookback:
        return 0.0
    return (series.iloc[-1] / series.iloc[-1 - lookback] - 1) * 100


def _classify(score):
    return "inflow" if score >= 58 else "outflow" if score <= 42 else "neutral"


def score_sector(df):
    """
    Produce TWO money-flow scores (each 0-100) for one instrument:

    short  — days-to-weeks view. Recent momentum (1/5/20-day) + volume surge
             + price vs its 20-day average. Catches what is hot *right now*.
    long   — months view. Slower momentum (~3 & 6-month) + price vs its 50- and
             200-day averages. Catches sustained, durable strength.

    Returns both, plus a 'horizon' tag summarising how they line up.
    """
    close, vol = df["Close"], df["Volume"]

    # ---- short term ----
    r1, r5, r20 = pct(close, 1), pct(close, 5), pct(close, 20)
    avg_vol = vol.iloc[-20:].mean() if len(vol) >= 20 else vol.mean()
    vol_ratio = (vol.iloc[-1] / avg_vol) if avg_vol else 1.0
    sma20 = close.iloc[-20:].mean() if len(close) >= 20 else close.mean()
    above20 = (close.iloc[-1] / sma20 - 1) * 100
    st_mom = 0.5 * r1 + 0.3 * r5 + 0.2 * r20
    st_raw = st_mom * 6 + (vol_ratio - 1) * 18 + above20 * 1.5
    score_short = round(max(0, min(100, 50 + st_raw)))

    # ---- long term ----
    r60, r120 = pct(close, 60), pct(close, 120)
    sma50 = close.iloc[-50:].mean() if len(close) >= 50 else close.mean()
    sma200 = close.iloc[-200:].mean() if len(close) >= 200 else close.mean()
    above50 = (close.iloc[-1] / sma50 - 1) * 100
    above200 = (close.iloc[-1] / sma200 - 1) * 100
    lt_mom = 0.45 * r60 + 0.35 * r120
    lt_raw = lt_mom * 1.6 + above50 * 1.2 + above200 * 1.4
    score_long = round(max(0, min(100, 50 + lt_raw)))

    fs, fl = _classify(score_short), _classify(score_long)
    if fs == "inflow" and fl == "inflow":
        horizon = "Strong both"
    elif fs == "inflow" and fl != "inflow":
        horizon = "Short-term trade"
    elif fl == "inflow" and fs != "inflow":
        horizon = "Long-term hold"
    elif fs == "outflow" and fl == "outflow":
        horizon = "Weak both"
    else:
        horizon = "Mixed"

    return {
        # default 'score'/'flow' stay = short term, for backward compatibility
        "score": score_short, "flow": fs,
        "score_short": score_short, "flow_short": fs,
        "score_long": score_long, "flow_long": fl,
        "horizon": horizon,
        "r1": r1, "r5": r5, "r20": r20, "r60": r60, "r120": r120,
        "vol_ratio": vol_ratio, "above50": above50, "above200": above200,
    }


# ----------------------------------------------------------------------------
# VALUE WATCHLIST  — quality companies currently on sale
# ----------------------------------------------------------------------------
def _clamp(v, lo=0, hi=100):
    return max(lo, min(hi, v))


def rsi(close, period=14):
    """Relative Strength Index — <30 oversold (cheap), >70 overbought."""
    delta = close.diff().dropna()
    if len(delta) < period:
        return 50.0
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, 1e-9)
    return float((100 - 100 / (1 + rs)).iloc[-1])


def technicals(df):
    close = df["Close"]
    price = float(close.iloc[-1])
    win = close.iloc[-252:] if len(close) >= 252 else close
    hi52, lo52 = float(win.max()), float(win.min())
    discount = (hi52 - price) / hi52 * 100 if hi52 else 0.0          # % below 52w high
    off_low = (price - lo52) / lo52 * 100 if lo52 else 0.0           # % above 52w low
    sma200 = close.iloc[-200:].mean() if len(close) >= 200 else close.mean()
    above200 = (price / sma200 - 1) * 100
    return {"price": price, "discount": discount, "off_low": off_low,
            "above200": above200, "rsi": rsi(close), "r5": pct(close, 5)}


def score_quality(f):
    """How good the *company* is: returns, margins, debt, growth (0-100)."""
    roe = (f.get("roe") or 0) * 100
    margin = (f.get("margin") or 0) * 100
    rev = (f.get("rev_growth") or 0) * 100
    d2e = f.get("d2e")
    q_roe = _clamp(roe, 0, 30) / 30 * 30
    q_margin = _clamp(margin, 0, 25) / 25 * 25
    q_rev = _clamp(rev, 0, 30) / 30 * 25
    q_debt = 20 if d2e is None else _clamp((150 - _clamp(d2e, 0, 300)) / 150, 0, 1) * 20
    return round(_clamp(q_roe + q_margin + q_rev + q_debt))


def score_value(f):
    """How cheap the *valuation* is: P/E and P/B (0-100, higher = cheaper)."""
    fpe = f.get("fpe") or f.get("pe")
    pb = f.get("pb")
    v_pe = _clamp(100 - max(0, fpe - 12) * 4) if (fpe and fpe > 0) else 20
    v_pb = _clamp(100 - max(0, pb - 1) * 15) if (pb and pb > 0) else 50
    return round(0.6 * v_pe + 0.4 * v_pb)


def score_entry(t):
    """How attractive the *price entry* is right now (0-100)."""
    d = t["discount"]
    if d < 8:        ds = 25       # barely off its high — not on sale
    elif d < 15:     ds = 60
    elif d <= 40:    ds = 100      # meaningfully discounted
    elif d <= 55:    ds = 70
    else:            ds = 35       # very deep — could be broken
    rs = t["rsi"]
    if rs < 25:      rsc = 55      # very oversold (risky knife-catch)
    elif rs < 45:    rsc = 100     # oversold-ish, room to recover
    elif rs < 60:    rsc = 70
    else:            rsc = 35      # already hot
    turning = 100 if t["r5"] > 0 else 50
    return round(0.45 * ds + 0.30 * rsc + 0.25 * turning)


def build_watchlist(stock_hist, fundamentals, min_quality=50, min_discount=8, top=8):
    """
    The 'special list': quality companies trading at a good price.
    Requires solid fundamentals (so we don't flag cheap junk) AND a real
    discount from recent highs, then ranks by a blended opportunity score.
    """
    if not fundamentals:
        return []
    stock_to_sector = {s: SECTORS.get(sec, sec)
                       for sec, lst in STOCK_UNIVERSE.items() for s in lst}
    rows = []
    for tkr, df in stock_hist.items():
        f = fundamentals.get(tkr)
        if not f:
            continue
        q = score_quality(f)
        v = score_value(f)
        t = technicals(df)
        e = score_entry(t)
        opp = round(0.35 * q + 0.30 * v + 0.35 * e)
        if q >= min_quality and t["discount"] >= min_discount:
            rows.append({
                "ticker": tkr, "sector": stock_to_sector.get(tkr, ""),
                "price": t["price"], "discount": t["discount"],
                "rsi": t["rsi"], "above200": t["above200"],
                "quality": q, "value": v, "entry": e, "opp": opp,
                "pe": f.get("fpe") or f.get("pe"),
                "roe": (f.get("roe") or 0) * 100,
                "margin": (f.get("margin") or 0) * 100,
            })
    rows.sort(key=lambda x: x["opp"], reverse=True)
    return rows[:top]


def find_leaders(top_sectors, stock_hist, sector_hist, max_per_sector=3):
    """
    For each hot sector, rank its constituent stocks by money-flow score and
    return the strongest names — each with its latest share price. When a pick
    (or the sector ETF) costs more than PRICE_THRESHOLD, also attach the best
    lower-priced alternative from the same sector so you have a cheaper option.
    """
    leaders = {}
    for sec in top_sectors:
        tkr = sec["ticker"]

        # Score every constituent and record its price.
        scored = []
        for stk in STOCK_UNIVERSE.get(tkr, []):
            if stk in stock_hist:
                s = score_sector(stock_hist[stk])
                s["ticker"] = stk
                s["price"] = float(stock_hist[stk]["Close"].iloc[-1])
                scored.append(s)
        scored.sort(key=lambda x: x["score"], reverse=True)

        # Pool of affordable, decent-flow names to draw alternatives from.
        cheap_pool = sorted(
            [s for s in scored if s["price"] <= PRICE_THRESHOLD and s["score"] >= 50],
            key=lambda x: x["score"], reverse=True,
        )

        def cheaper_alt(exclude_ticker):
            for c in cheap_pool:
                if c["ticker"] != exclude_ticker:
                    return {"ticker": c["ticker"], "price": c["price"], "score": c["score"]}
            return None

        picks = []
        for s in scored[:max_per_sector]:
            s = dict(s)
            s["alt"] = cheaper_alt(s["ticker"]) if s["price"] > PRICE_THRESHOLD else None
            picks.append(s)

        # ETF price + its own cheaper alternative.
        etf_price = float(sector_hist[tkr]["Close"].iloc[-1]) if tkr in sector_hist else None
        etf_alt = (cheaper_alt(None) if etf_price and etf_price > PRICE_THRESHOLD else None)

        leaders[tkr] = {"picks": picks, "etf_price": etf_price, "etf_alt": etf_alt}
    return leaders


def build_report(sector_hist, macro_hist, stock_hist=None, fundamentals=None):
    sectors = []
    for tkr, name in SECTORS.items():
        if tkr in sector_hist:
            s = score_sector(sector_hist[tkr])
            s.update(ticker=tkr, name=name,
                     price=float(sector_hist[tkr]["Close"].iloc[-1]))
            sectors.append(s)

    sectors_short = sorted(sectors, key=lambda x: x["score_short"], reverse=True)
    sectors_long = sorted(sectors, key=lambda x: x["score_long"], reverse=True)

    macros = []
    for tkr, name in MACRO.items():
        if tkr in macro_hist:
            close = macro_hist[tkr]["Close"]
            macros.append({
                "name": name,
                "value": close.iloc[-1],
                "change": pct(close, 1),
            })

    def mood_of(key):
        infl = [s for s in sectors if s[key] == "inflow"]
        outf = [s for s in sectors if s[key] == "outflow"]
        return ("Risk-On" if len(infl) > len(outf) + 1
                else "Risk-Off" if len(outf) > len(infl) + 1 else "Mixed")

    mood_short = mood_of("flow_short")
    mood_long = mood_of("flow_long")

    # Pick stocks for the union of the top short-term and top long-term hot
    # sectors (so the "what to buy" list covers both horizons), de-duplicated.
    hot_short = [s for s in sectors_short if s["flow_short"] == "inflow"][:3]
    hot_long = [s for s in sectors_long if s["flow_long"] == "inflow"][:3]
    seen, hot_union = set(), []
    for s in hot_short + hot_long:
        if s["ticker"] not in seen:
            seen.add(s["ticker"])
            hot_union.append(s)

    leaders = {}
    if stock_hist is not None:
        leaders = find_leaders(hot_union, stock_hist, sector_hist)

    watchlist = []
    if stock_hist is not None and fundamentals:
        watchlist = build_watchlist(stock_hist, fundamentals)

    return {"sectors_short": sectors_short, "sectors_long": sectors_long,
            "macros": macros, "mood_short": mood_short, "mood_long": mood_long,
            "hot_short": hot_short, "hot_long": hot_long, "hot_union": hot_union,
            "leaders": leaders, "watchlist": watchlist}


# ----------------------------------------------------------------------------
# HTML EMAIL
# ----------------------------------------------------------------------------
def fmt(v, money=False, pct_=False):
    if pct_:
        return f"{'+' if v >= 0 else ''}{v:.2f}%"
    if money:
        return f"{v:,.2f}"
    return f"{v:,.2f}"


def render_html(report, demo=False):
    today = local_today().strftime("%A, %B %d, %Y")
    mc = {"Risk-On": "#1D9E75", "Risk-Off": "#E24B4A", "Mixed": "#888780"}
    ms, ml = report["mood_short"], report["mood_long"]

    macro_cells = ""
    for m in report["macros"]:
        up = m["change"] >= 0
        arrow = "&#9650;" if up else "&#9660;"
        c = "#1D9E75" if up else "#E24B4A"
        macro_cells += f"""
        <td style="padding:10px 14px;border:1px solid #e6e4dc;border-radius:8px;">
          <div style="font-size:12px;color:#6b6a64;">{m['name']}</div>
          <div style="font-size:18px;font-weight:600;color:#1a1a18;">{fmt(m['value'])}</div>
          <div style="font-size:12px;color:{c};">{arrow} {fmt(m['change'], pct_=True)}</div>
        </td>"""

    def sector_rows(items, score_key, flow_key, detail):
        rows = ""
        for s in items:
            flow = s[flow_key]
            score = s[score_key]
            bar_c = "#1D9E75" if flow == "inflow" else "#E24B4A" if flow == "outflow" else "#b4b2a9"
            badge_bg, badge_c, badge_t = (
                ("#EAF3DE", "#3B6D11", "&#8593; Inflow") if flow == "inflow" else
                ("#FCEBEB", "#A32D2D", "&#8595; Outflow") if flow == "outflow" else
                ("#F1EFE8", "#5F5E5A", "&#8594; Neutral"))
            rows += f"""
            <tr>
              <td style="padding:10px 8px;border-bottom:1px solid #efeee8;">
                <div style="font-weight:600;color:#1a1a18;">{s['name']}</div>
                <div style="font-size:11px;color:#9b9a94;">{s['ticker']} &middot; &#36;{s['price']:.2f}</div>
              </td>
              <td style="padding:10px 8px;border-bottom:1px solid #efeee8;">
                <span style="background:{badge_bg};color:{badge_c};font-size:11px;padding:2px 8px;border-radius:6px;">{badge_t}</span>
              </td>
              <td style="padding:10px 8px;border-bottom:1px solid #efeee8;width:140px;">
                <div style="background:#f1efe8;border-radius:4px;height:7px;width:120px;">
                  <div style="background:{bar_c};height:7px;border-radius:4px;width:{score}%;"></div>
                </div>
                <div style="font-size:11px;color:#9b9a94;margin-top:3px;">{score}/100</div>
              </td>
              <td style="padding:10px 8px;border-bottom:1px solid #efeee8;font-size:12px;color:#6b6a64;text-align:right;">{detail(s)}</td>
            </tr>"""
        return rows

    short_detail = lambda s: f"1d {fmt(s['r1'], pct_=True)}<br>5d {fmt(s['r5'], pct_=True)}<br>vol &times;{s['vol_ratio']:.1f}"
    long_detail = lambda s: f"3mo {fmt(s['r60'], pct_=True)}<br>6mo {fmt(s['r120'], pct_=True)}<br>vs 200d {fmt(s['above200'], pct_=True)}"

    top_s = report["sectors_short"][0] if report["sectors_short"] else None
    top_l = report["sectors_long"][0] if report["sectors_long"] else None
    headline = ""
    if top_s:
        headline += f"Short term, money is rotating into <b>{top_s['name']}</b>. "
    if top_l:
        headline += f"Longer term, <b>{top_l['name']}</b> leads."

    # horizon tag colors
    HTAG = {
        "Strong both": ("#EAF3DE", "#3B6D11"),
        "Short-term trade": ("#FAEEDA", "#854F0B"),
        "Long-term hold": ("#E6F1FB", "#0C447C"),
        "Weak both": ("#FCEBEB", "#A32D2D"),
        "Mixed": ("#F1EFE8", "#5F5E5A"),
    }

    def alt_line(alt):
        if not alt:
            return ""
        return (f"""<div style="font-size:11px;color:#0C447C;margin-top:3px;">
                &#8627; cheaper alt: <b>{alt['ticker']}</b> &#36;{alt['price']:.0f}</div>""")

    def score_pair(st):
        sb, lb = HTAG.get(st['horizon'], HTAG['Mixed'])
        return (f"""<span style="font-size:11px;color:#6b6a64;">ST {st['score_short']}</span>
            &nbsp;<span style="font-size:11px;color:#6b6a64;">LT {st['score_long']}</span><br>
            <span style="font-size:10px;background:{sb};color:{lb};padding:1px 6px;border-radius:5px;">{st['horizon']}</span>""")

    picks_html = ""
    leaders = report.get("leaders", {})
    for sec in report["hot_union"]:
        tkr = sec["ticker"]
        data = leaders.get(tkr, {})
        names = data.get("picks", [])
        etf_price = data.get("etf_price")
        etf_alt = data.get("etf_alt")
        # which horizon(s) made this sector hot
        tags = []
        if sec in report["hot_short"]:
            tags.append('<span style="font-size:10px;background:#FAEEDA;color:#854F0B;padding:1px 7px;border-radius:5px;margin-left:4px;">hot short-term</span>')
        if sec in report["hot_long"]:
            tags.append('<span style="font-size:10px;background:#E6F1FB;color:#0C447C;padding:1px 7px;border-radius:5px;margin-left:4px;">hot long-term</span>')
        stock_chips = ""
        for st in names:
            over = st["price"] > PRICE_THRESHOLD
            price_c = "#A32D2D" if over else "#3B6D11"
            stock_chips += f"""
            <tr>
              <td style="padding:7px 8px;border-bottom:1px solid #f3f2ec;font-weight:600;color:#1a1a18;vertical-align:top;">{st['ticker']}
                <span style="font-size:10px;font-weight:400;background:#E6F1FB;color:#0C447C;padding:1px 6px;border-radius:5px;margin-left:4px;">Stock</span>
                {alt_line(st.get('alt'))}
              </td>
              <td style="padding:7px 8px;border-bottom:1px solid #f3f2ec;text-align:right;font-size:13px;font-weight:600;color:{price_c};vertical-align:top;">&#36;{st['price']:.2f}</td>
              <td style="padding:7px 8px;border-bottom:1px solid #f3f2ec;text-align:right;vertical-align:top;">{score_pair(st)}</td>
            </tr>"""
        etf_price_txt = f"&#36;{etf_price:.2f}" if etf_price else "&mdash;"
        etf_over = etf_price and etf_price > PRICE_THRESHOLD
        etf_price_c = "#A32D2D" if etf_over else "#3B6D11"
        picks_html += f"""
        <div style="background:#fff;border:1px solid #e6e4dc;border-radius:10px;padding:14px 16px;margin-bottom:12px;">
          <div style="font-size:14px;font-weight:700;color:#1a1a18;margin-bottom:8px;">{sec['name']}{''.join(tags)}</div>
          <table style="width:100%;border-collapse:collapse;">
            <tr>
              <td style="padding:7px 8px;border-bottom:2px solid #efeee8;font-weight:600;color:#1a1a18;vertical-align:top;">{tkr}
                <span style="font-size:10px;font-weight:400;background:#F1EFE8;color:#5F5E5A;padding:1px 6px;border-radius:5px;margin-left:4px;">ETF &mdash; whole sector</span>
                {alt_line(etf_alt)}
              </td>
              <td style="padding:7px 8px;border-bottom:2px solid #efeee8;text-align:right;font-size:13px;font-weight:600;color:{etf_price_c};vertical-align:top;">{etf_price_txt}</td>
              <td style="padding:7px 8px;border-bottom:2px solid #efeee8;text-align:right;vertical-align:top;">{score_pair(sec)}</td>
            </tr>
            {stock_chips}
          </table>
        </div>"""

    picks_section = ""
    if picks_html:
        picks_section = f"""
  <div style="font-size:12px;font-weight:700;letter-spacing:.04em;color:#6b6a64;text-transform:uppercase;margin:24px 0 8px;">What you could buy</div>
  <div style="font-size:12px;color:#9b9a94;margin-bottom:12px;line-height:1.5;">Each name shows <b>ST</b> (short-term, days&ndash;weeks) and <b>LT</b> (long-term, months) score out of 100, plus a tag: <b>Short-term trade</b>, <b>Long-term hold</b>, or <b>Strong both</b>. Prices over &#36;{PRICE_THRESHOLD:.0f} show a cheaper same-sector alternative.</div>
  {picks_html}"""

    section_title = lambda t: f'<div style="font-size:12px;font-weight:700;letter-spacing:.04em;color:#6b6a64;text-transform:uppercase;margin:24px 0 8px;">{t}</div>'

    # Special list — quality names on sale
    wl = report.get("watchlist", [])
    watchlist_section = ""
    if wl:
        wl_rows = ""
        for w in wl:
            pe_txt = f"{w['pe']:.1f}" if w["pe"] else "&mdash;"
            over = w["price"] > PRICE_THRESHOLD
            price_c = "#A32D2D" if over else "#3B6D11"
            wl_rows += f"""
            <tr>
              <td style="padding:9px 8px;border-bottom:1px solid #efeee8;vertical-align:top;">
                <span style="font-weight:600;color:#1a1a18;">{w['ticker']}</span>
                <div style="font-size:11px;color:#9b9a94;">{w['sector']}</div>
              </td>
              <td style="padding:9px 8px;border-bottom:1px solid #efeee8;text-align:right;font-size:13px;font-weight:600;color:{price_c};vertical-align:top;">&#36;{w['price']:.2f}</td>
              <td style="padding:9px 8px;border-bottom:1px solid #efeee8;text-align:right;font-size:12px;color:#A32D2D;vertical-align:top;">&#8595; {w['discount']:.0f}%<br><span style="color:#9b9a94;font-size:10px;">off 52w high</span></td>
              <td style="padding:9px 8px;border-bottom:1px solid #efeee8;text-align:right;font-size:11px;color:#6b6a64;vertical-align:top;">Q {w['quality']}<br>V {w['value']}<br>Entry {w['entry']}</td>
              <td style="padding:9px 8px;border-bottom:1px solid #efeee8;text-align:right;vertical-align:top;">
                <span style="font-size:15px;font-weight:700;color:#0C447C;">{w['opp']}</span>
                <div style="font-size:10px;color:#9b9a94;">opp / 100</div>
                <div style="font-size:10px;color:#6b6a64;margin-top:2px;">PE {pe_txt} &middot; ROE {w['roe']:.0f}%</div>
              </td>
            </tr>"""
        watchlist_section = f"""
  {section_title('&#11088; Special list &mdash; quality names on sale')}
  <div style="font-size:12px;color:#9b9a94;margin-bottom:12px;line-height:1.5;">Companies that look <b>fundamentally solid</b> (Q = quality: returns, margins, debt, growth) <b>and</b> <b>reasonably valued</b> (V = value: P/E, P/B) that are also trading at a <b>discount</b> from their 52-week high with a constructive technical setup (Entry). Ranked by overall opportunity score. A discount alone isn't a green light &mdash; check why it's down.</div>
  <table style="border-collapse:collapse;width:100%;background:#fff;border:1px solid #e6e4dc;border-radius:12px;overflow:hidden;">
    {wl_rows}
  </table>"""

    return f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Money Flow">
<title>Money Flow Daily</title>
<style>
  @media (max-width:640px) {{
    .mf-wrap {{ padding:14px !important; }}
    table {{ font-size:13px; }}
  }}
  body {{ -webkit-text-size-adjust:100%; }}
</style>
</head><body style="margin:0;padding:0;background:#faf9f5;font-family:Arial,Helvetica,sans-serif;">
<div class="mf-wrap" style="max-width:640px;width:100%;box-sizing:border-box;margin:0 auto;padding:24px;">
  <div style="margin-bottom:4px;font-size:22px;font-weight:700;color:#1a1a18;">&#129517; Money Flow Daily</div>
  <div style="font-size:13px;color:#6b6a64;margin-bottom:20px;">{today}</div>
  {'<div style="background:#FCEBEB;border:1px solid #F09595;border-radius:10px;padding:12px 16px;margin-bottom:20px;color:#791F1F;font-size:13px;font-weight:600;">&#9888; DEMO DATA &mdash; these are randomly generated test numbers, NOT real prices. Run the script without --test for live market data.</div>' if demo else ''}

  <div style="background:#fff;border:1px solid #e6e4dc;border-radius:12px;padding:16px 18px;margin-bottom:22px;">
    <div style="font-size:13px;margin-bottom:6px;">
      <span style="font-weight:700;color:{mc[ms]};">Short-term mood: {ms}</span>
      &nbsp;&nbsp;|&nbsp;&nbsp;
      <span style="font-weight:700;color:{mc[ml]};">Long-term mood: {ml}</span>
    </div>
    <div style="font-size:14px;color:#1a1a18;line-height:1.55;">{headline}</div>
  </div>

  <div style="font-size:12px;font-weight:700;letter-spacing:.04em;color:#6b6a64;text-transform:uppercase;margin-bottom:8px;">Macro snapshot</div>
  <table style="border-collapse:separate;border-spacing:6px;width:100%;margin-bottom:24px;"><tr>{macro_cells}</tr></table>

  {section_title('Short-term money flow &mdash; days to weeks')}
  <table style="border-collapse:collapse;width:100%;background:#fff;border:1px solid #e6e4dc;border-radius:12px;overflow:hidden;">
    {sector_rows(report['sectors_short'], 'score_short', 'flow_short', short_detail)}
  </table>

  {section_title('Long-term money flow &mdash; months')}
  <table style="border-collapse:collapse;width:100%;background:#fff;border:1px solid #e6e4dc;border-radius:12px;overflow:hidden;">
    {sector_rows(report['sectors_long'], 'score_long', 'flow_long', long_detail)}
  </table>
{picks_section}
{watchlist_section}
  <div style="font-size:11px;color:#9b9a94;margin-top:20px;line-height:1.5;">
    Short-term score = 1/5/20-day momentum + volume + 20-day trend.
    Long-term score = 3/6-month momentum + position vs 50- and 200-day averages.
    Special list = fundamentals (quality + value) combined with a discounted, stabilising price.
    Data: Yahoo Finance. Informational only &mdash; not financial advice. Cheap can stay cheap; always do your own research before buying.
  </div>
</div>
</body></html>"""


# ----------------------------------------------------------------------------
# SEND
# ----------------------------------------------------------------------------
def send_push(report):
    """Send a short summary to your phone via ntfy.sh (no login/password)."""
    import urllib.request
    if not NTFY_TOPIC or NTFY_TOPIC == "PUT-YOUR-TOPIC-HERE":
        print("No ntfy topic set — skipping phone push. "
              "Set NTFY_TOPIC in the script to enable it.")
        return False

    hot_s = [s["name"] for s in report["hot_short"][:3]] or ["none"]
    hot_l = [s["name"] for s in report["hot_long"][:3]] or ["none"]
    lines = [
        f"Short-term: {report['mood_short']} | Long-term: {report['mood_long']}",
        f"Hot now: {', '.join(hot_s)}",
        f"Long-term leaders: {', '.join(hot_l)}",
    ]
    wl = report.get("watchlist", [])
    if wl:
        w = wl[0]
        lines.append(f"Top value pick: {w['ticker']} ${w['price']:.0f} "
                     f"(-{w['discount']:.0f}% off high)")
    body = "\n".join(lines).encode("utf-8")

    # Use Spain local date in the notification title
    today_local = local_today()

    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=body,
        headers={
            "Title": f"Money Flow - {today_local:%b %d}",
            "Tags": "chart_with_upwards_trend",
            "Priority": "default",
        },
    )
    try:
        urllib.request.urlopen(req, timeout=15)
        print(f"Phone notification sent to ntfy topic '{NTFY_TOPIC}'.")
        return True
    except Exception as e:
        print(f"Push failed: {e}")
        return False


def send_email(html, subject):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO
    msg.attach(MIMEText("Your money flow digest is in HTML. View in an HTML-capable client.", "plain"))
    msg.attach(MIMEText(html, "html"))
    ctx = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls(context=ctx)
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, [EMAIL_TO], msg.as_string())


# ----------------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Daily sector money-flow email digest")
    ap.add_argument("--no-email", action="store_true", help="build report but don't send")
    ap.add_argument("--test", action="store_true", help="use synthetic data (offline)")
    ap.add_argument("--save", metavar="PATH", help="also write the HTML to a file")
    ap.add_argument("--no-fundamentals", action="store_true",
                    help="skip the value watchlist (faster; no fundamentals fetch)")
    ap.add_argument("--push", action="store_true",
                    help="send a short summary to your phone via ntfy.sh")
    args = ap.parse_args()

    all_stocks = sorted({s for lst in STOCK_UNIVERSE.values() for s in lst})
    all_tickers = list(SECTORS) + list(MACRO) + all_stocks
    if args.test:
        print("Using synthetic test data...")
        hist = make_fake_history(all_tickers)
        fundamentals = {} if args.no_fundamentals else make_fake_fundamentals(all_stocks)
    else:
        print("Fetching live market data from Yahoo Finance...")
        hist = fetch_history(all_tickers)
        fundamentals = {}
        if not args.no_fundamentals:
            print("Fetching fundamentals for the value watchlist (this takes a moment)...")
            fundamentals = fetch_fundamentals(all_stocks)

    if not hist:
        print("ERROR: no data returned. Check your network connection.")
        sys.exit(1)

    sector_hist = {t: hist[t] for t in SECTORS if t in hist}
    macro_hist = {t: hist[t] for t in MACRO if t in hist}
    stock_hist = {t: hist[t] for t in all_stocks if t in hist}
    report = build_report(sector_hist, macro_hist, stock_hist, fundamentals)
    html = render_html(report, demo=args.test)

    today_local = local_today()
    print(f"\nShort-term mood: {report['mood_short']}   Long-term mood: {report['mood_long']}")
    print("\nShort-term top sectors:")
    for s in report["sectors_short"][:5]:
        print(f"  {s['score_short']:>3}/100  {s['flow_short']:<8}  {s['name']:<24} {s['ticker']:<5} ${s['price']:.2f}")
    print("\nLong-term top sectors:")
    for s in report["sectors_long"][:5]:
        print(f"  {s['score_long']:>3}/100  {s['flow_long']:<8}  {s['name']:<24} {s['ticker']:<5} ${s['price']:.2f}")
    if report.get("leaders"):
        print("\nWhat you could buy (ST=short-term, LT=long-term score):")
        for sec in report["hot_union"]:
            data = report["leaders"].get(sec["ticker"], {})
            etf_p = data.get("etf_price")
            etf_alt = data.get("etf_alt")
            alt_txt = f"  [cheaper alt: {etf_alt['ticker']} ${etf_alt['price']:.0f}]" if etf_alt else ""
            head = f"  {sec['name']} — ETF {sec['ticker']} ${etf_p:.2f}  [{sec['horizon']}]{alt_txt}" if etf_p else f"  {sec['name']}"
            print(head)
            for p in data.get("picks", []):
                a = f"  -> alt {p['alt']['ticker']} ${p['alt']['price']:.0f}" if p.get("alt") else ""
                print(f"      {p['ticker']:<6} ${p['price']:>8.2f}  ST {p['score_short']:>3}  LT {p['score_long']:>3}  {p['horizon']}{a}")

    if report.get("watchlist"):
        print("\nSpecial list — quality names on sale (opp score):")
        for w in report["watchlist"]:
            print(f"  {w['opp']:>3}  {w['ticker']:<6} ${w['price']:>8.2f}  -{w['discount']:.0f}% off high  "
                  f"Q{w['quality']} V{w['value']} E{w['entry']}  ({w['sector']})")

    if args.save:
        with open(args.save, "w") as f:
            f.write(html)
        print(f"\nHTML saved to {args.save}")

    if args.push:
        send_push(report)

    if args.no_email:
        print("\n(--no-email set; skipping send)")
    else:
        subject = f"Money Flow Daily — ST {report['mood_short']} / LT {report['mood_long']} — {today_local:%b %d}"
        send_email(html, subject)
        print(f"\nEmail sent to {EMAIL_TO}")


if __name__ == "__main__":
    main()
