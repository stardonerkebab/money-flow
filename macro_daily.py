#!/usr/bin/env python3
"""
Macro Intelligence Daily Report
================================
Pulls macro + market data every day, interprets the economic regime,
and generates a full PDF report with short-term and long-term trade ideas.

Run:  python3 macro_daily.py --push
PDF is saved as macro_report_YYYY-MM-DD.pdf and emailed if configured.
"""

import argparse
import datetime as dt
import io
import os
import sys
import urllib.request
import urllib.parse
from zoneinfo import ZoneInfo

# ── Timezone ────────────────────────────────────────────────────────────────
LOCAL_TZ = ZoneInfo("Europe/Madrid")

def local_today():
    return dt.datetime.now(LOCAL_TZ).date()

def local_now():
    return dt.datetime.now(LOCAL_TZ)

# ── Config ───────────────────────────────────────────────────────────────────
NTFY_TOPIC  = os.getenv("MF_NTFY_TOPIC", "moneyflowjaswindersinghkaur")
SMTP_HOST   = os.getenv("MF_SMTP_HOST", "smtp.gmail.com")
SMTP_PORT   = int(os.getenv("MF_SMTP_PORT", "587"))
SMTP_USER   = os.getenv("MF_SMTP_USER", "youremail@gmail.com")
SMTP_PASS   = os.getenv("MF_SMTP_PASS", "your-app-password")
EMAIL_TO    = os.getenv("MF_EMAIL_TO", SMTP_USER)

# ── Tickers ──────────────────────────────────────────────────────────────────
MACRO_TICKERS = {
    "^GSPC":    "S&P 500",
    "^IXIC":    "NASDAQ",
    "^DJI":     "Dow Jones",
    "^VIX":     "VIX",
    "^TNX":     "10Y Yield",
    "^TYX":     "30Y Yield",
    "^IRX":     "3M Yield",
    "DX-Y.NYB": "US Dollar (DXY)",
    "GC=F":     "Gold",
    "CL=F":     "Crude Oil",
    "BTC-USD":  "Bitcoin",
    "TLT":      "20Y Bond ETF",
    "HYG":      "High Yield Bond",
    "UUP":      "Dollar ETF",
}

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

# Stocks per sector — short + long candidates
STOCK_UNIVERSE = {
    "XLK":  ["AAPL", "MSFT", "NVDA", "AMD", "AVGO", "CRM", "ORCL"],
    "XLF":  ["JPM", "BAC", "WFC", "GS", "MS", "V", "MA"],
    "XLE":  ["XOM", "CVX", "COP", "MPC", "PSX", "SLB", "EOG"],
    "XLV":  ["LLY", "UNH", "JNJ", "ABBV", "MRK", "TMO", "ABT"],
    "XLY":  ["AMZN", "TSLA", "HD", "MCD", "NKE", "LOW", "SBUX"],
    "XLP":  ["PG", "COST", "WMT", "KO", "PEP", "PM", "MDLZ"],
    "XLI":  ["GE", "CAT", "RTX", "HON", "UNP", "DE", "BA"],
    "XLB":  ["LIN", "FCX", "NEM", "ECL", "SHW", "APD"],
    "XLRE": ["PLD", "AMT", "EQIX", "WELL", "SPG", "O"],
    "XLU":  ["NEE", "SO", "DUK", "AEP", "D", "CEG"],
    "XLC":  ["META", "GOOGL", "NFLX", "DIS", "TMUS"],
}

# Best ETF/stock plays per macro regime
REGIME_PLAYBOOK = {
    "Early Cycle": {
        "description": "Recovery phase. GDP rebounds, rates low, credit expanding.",
        "best_sectors": ["XLY", "XLF", "XLI", "XLK"],
        "best_etfs": ["SPY", "QQQ", "XLY", "XLF", "IWM"],
        "avoid": ["XLU", "XLP"],
        "bond_play": "Avoid long bonds — rates will rise",
        "notes": "Risk-on. Cyclicals and growth lead. Small caps outperform.",
    },
    "Mid Cycle": {
        "description": "Expansion. Earnings strong, rates rising gradually.",
        "best_sectors": ["XLK", "XLI", "XLE", "XLB"],
        "best_etfs": ["QQQ", "XLK", "XLE", "XLI", "DIA"],
        "avoid": ["XLRE", "XLU"],
        "bond_play": "Short duration bonds better",
        "notes": "Broadening rally. Tech and energy shine. Stay invested.",
    },
    "Late Cycle": {
        "description": "Slowdown. Inflation high, rates peaking, margins squeezed.",
        "best_sectors": ["XLE", "XLB", "XLV", "XLP"],
        "best_etfs": ["XLE", "GLD", "XLV", "XLP", "TIP"],
        "avoid": ["XLY", "XLK", "XLRE"],
        "bond_play": "TIPS and short duration",
        "notes": "Value over growth. Commodities and defensives. Be selective.",
    },
    "Recession": {
        "description": "Contraction. GDP falling, unemployment rising, credit tight.",
        "best_sectors": ["XLP", "XLV", "XLU", "XLC"],
        "best_etfs": ["TLT", "GLD", "XLP", "XLV", "BIL"],
        "avoid": ["XLY", "XLF", "XLI", "XLE"],
        "bond_play": "Long bonds (TLT) — rates will fall",
        "notes": "Cash and defensives. Gold and Treasuries. Capital preservation.",
    },
}

# ── Data Fetching ─────────────────────────────────────────────────────────────
def fetch_all_data():
    import yfinance as yf
    print("Fetching market data...")
    all_tickers = (list(MACRO_TICKERS) + list(SECTORS) +
                   [s for lst in STOCK_UNIVERSE.values() for s in lst])
    # deduplicate
    all_tickers = list(dict.fromkeys(all_tickers))
    raw = yf.download(all_tickers, period="1y", interval="1d",
                      group_by="ticker", auto_adjust=False,
                      progress=False, threads=True)
    hist = {}
    for t in all_tickers:
        try:
            df = raw[t].dropna()
            if len(df) >= 5:
                hist[t] = df
        except Exception:
            pass
    print(f"  Got data for {len(hist)}/{len(all_tickers)} tickers")
    return hist


def fetch_earnings(tickers):
    """Get next earnings date per ticker via yfinance."""
    import yfinance as yf
    earnings = {}
    for t in tickers:
        try:
            cal = yf.Ticker(t).calendar
            if cal is not None and not cal.empty:
                # calendar is a DataFrame with dates as columns
                dates = cal.columns.tolist()
                if dates:
                    earnings[t] = str(dates[0].date()) if hasattr(dates[0], 'date') else str(dates[0])
        except Exception:
            pass
    return earnings


def fetch_fear_greed():
    """Fetch Fear & Greed index from alternative.me (crypto-based proxy)."""
    try:
        req = urllib.request.Request(
            "https://api.alternative.me/fng/?limit=1",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            import json
            data = json.loads(r.read())
            val = int(data["data"][0]["value"])
            label = data["data"][0]["value_classification"]
            return val, label
    except Exception:
        return None, None


def fetch_insider_trades():
    """Fetch recent insider purchases from openinsider.com."""
    try:
        url = ("https://openinsider.com/screener?s=&o=&pl=&ph=&ll=&lh="
               "&fd=7&fdr=&td=0&tdr=&fdlyl=&fdlyh=&daysago=&xp=1&xs=1"
               "&vl=100&vh=&ocl=&och=&sic1=-1&sicl=100&sich=9999"
               "&grp=0&nfl=&nfh=&nil=&nih=&nol=&noh=&v2l=&v2h="
               "&oc2l=&oc2h=&sortcol=0&cnt=10&page=1&action=1")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            from html.parser import HTMLParser
            html = r.read().decode("utf-8", errors="ignore")
            # parse table rows
            trades = []
            import re
            rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL)
            for row in rows[1:11]:  # skip header
                cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
                cells = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]
                if len(cells) >= 11:
                    trades.append({
                        "date": cells[1],
                        "ticker": cells[3],
                        "insider": cells[5],
                        "title": cells[6],
                        "type": cells[7],
                        "value": cells[10],
                    })
            return trades[:6]
    except Exception as e:
        print(f"  Insider data unavailable: {e}")
        return []


# ── Scoring ───────────────────────────────────────────────────────────────────
def pct_change(series, n):
    if len(series) <= n:
        return 0.0
    return float((series.iloc[-1] / series.iloc[-1 - n] - 1) * 100)


def score_momentum(df):
    """Score 0-100 for both short and long term."""
    close = df["Close"]
    vol   = df["Volume"]

    # Short term
    r1  = pct_change(close, 1)
    r5  = pct_change(close, 5)
    r20 = pct_change(close, 20)
    avg_vol = float(vol.iloc[-20:].mean()) if len(vol) >= 20 else float(vol.mean())
    vol_ratio = float(vol.iloc[-1]) / avg_vol if avg_vol else 1.0
    sma20 = float(close.iloc[-20:].mean()) if len(close) >= 20 else float(close.mean())
    above20 = (float(close.iloc[-1]) / sma20 - 1) * 100
    st_raw = (0.5*r1 + 0.3*r5 + 0.2*r20)*6 + (vol_ratio-1)*18 + above20*1.5
    score_st = round(max(0, min(100, 50 + st_raw)))

    # Long term
    r60  = pct_change(close, 60)
    r120 = pct_change(close, 120)
    sma50  = float(close.iloc[-50:].mean())  if len(close) >= 50  else float(close.mean())
    sma200 = float(close.iloc[-200:].mean()) if len(close) >= 200 else float(close.mean())
    above50  = (float(close.iloc[-1]) / sma50  - 1) * 100
    above200 = (float(close.iloc[-1]) / sma200 - 1) * 100
    lt_raw = (0.45*r60 + 0.35*r120)*1.6 + above50*1.2 + above200*1.4
    score_lt = round(max(0, min(100, 50 + lt_raw)))

    price = float(close.iloc[-1])
    hi52 = float(close.iloc[-252:].max()) if len(close) >= 252 else float(close.max())
    discount = (hi52 - price) / hi52 * 100 if hi52 else 0.0

    return {
        "score_st": score_st, "score_lt": score_lt,
        "r1": r1, "r5": r5, "r20": r20, "r60": r60, "r120": r120,
        "vol_ratio": vol_ratio, "above200": above200,
        "price": price, "discount": discount,
        "flow_st": "Inflow" if score_st >= 58 else "Outflow" if score_st <= 42 else "Neutral",
        "flow_lt": "Inflow" if score_lt >= 58 else "Outflow" if score_lt <= 42 else "Neutral",
    }


def detect_regime(hist):
    """
    Determine economic cycle regime from yield curve, VIX, momentum, bond/stock ratio.
    Returns one of: Early Cycle, Mid Cycle, Late Cycle, Recession
    """
    signals = {}

    # Yield curve: 10Y minus 3M
    y10 = pct_change(hist["^TNX"]["Close"], 0) if "^TNX" in hist else None
    y3m = pct_change(hist["^IRX"]["Close"], 0) if "^IRX" in hist else None
    if "^TNX" in hist:
        signals["10y_yield"] = float(hist["^TNX"]["Close"].iloc[-1])
    if "^IRX" in hist:
        signals["3m_yield"] = float(hist["^IRX"]["Close"].iloc[-1])
    if "^TNX" in hist and "^IRX" in hist:
        signals["yield_curve"] = signals["10y_yield"] - signals["3m_yield"]
    else:
        signals["yield_curve"] = 0.5

    # VIX
    signals["vix"] = float(hist["^VIX"]["Close"].iloc[-1]) if "^VIX" in hist else 20

    # S&P trend
    if "^GSPC" in hist:
        sp = hist["^GSPC"]["Close"]
        signals["sp500_r60"]  = pct_change(sp, 60)
        signals["sp500_r120"] = pct_change(sp, 120)
        sma200 = float(sp.iloc[-200:].mean()) if len(sp) >= 200 else float(sp.mean())
        signals["sp500_vs_200d"] = (float(sp.iloc[-1]) / sma200 - 1) * 100

    # Bond vs Stock momentum (TLT rising = risk-off)
    signals["tlt_r20"] = pct_change(hist["TLT"]["Close"], 20) if "TLT" in hist else 0
    signals["hyg_r20"] = pct_change(hist["HYG"]["Close"], 20) if "HYG" in hist else 0

    # Score each regime
    yc  = signals.get("yield_curve", 0.5)
    vix = signals.get("vix", 20)
    sp_trend = signals.get("sp500_vs_200d", 0)
    sp_mom   = signals.get("sp500_r60", 0)
    tlt      = signals.get("tlt_r20", 0)
    hyg      = signals.get("hyg_r20", 0)

    # Simple heuristic scoring
    recession_score  = 0
    late_score       = 0
    mid_score        = 0
    early_score      = 0

    # Yield curve inverted = recession/late signal
    if yc < -0.3:
        recession_score += 3
        late_score += 1
    elif yc < 0:
        late_score += 2
        recession_score += 1
    elif yc < 1.0:
        mid_score += 2
    else:
        early_score += 2

    # VIX
    if vix > 35:
        recession_score += 3
    elif vix > 25:
        late_score += 2
        recession_score += 1
    elif vix > 18:
        mid_score += 1
    else:
        early_score += 1
        mid_score += 1

    # S&P trend
    if sp_trend < -10:
        recession_score += 3
    elif sp_trend < -5:
        late_score += 2
    elif sp_trend < 0:
        late_score += 1
        mid_score += 1
    elif sp_trend > 10:
        mid_score += 2
        early_score += 1
    else:
        mid_score += 1

    # S&P momentum
    if sp_mom < -10:
        recession_score += 2
    elif sp_mom < 0:
        late_score += 1
    elif sp_mom > 10:
        mid_score += 2
        early_score += 1

    # Bond flows
    if tlt > 3:
        recession_score += 2  # risk-off, buying safety
    if hyg < -2:
        late_score += 1
        recession_score += 1

    scores = {
        "Recession":   recession_score,
        "Late Cycle":  late_score,
        "Mid Cycle":   mid_score,
        "Early Cycle": early_score,
    }
    regime = max(scores, key=scores.get)
    return regime, signals, scores


def build_report_data(hist):
    """Assemble all data needed for the PDF report."""
    today = local_today()

    # Macro snapshot
    macro = {}
    for t, name in MACRO_TICKERS.items():
        if t in hist:
            close = hist[t]["Close"]
            macro[name] = {
                "ticker": t,
                "price": float(close.iloc[-1]),
                "r1":  pct_change(close, 1),
                "r5":  pct_change(close, 5),
                "r20": pct_change(close, 20),
            }

    # Regime
    regime, signals, regime_scores = detect_regime(hist)
    playbook = REGIME_PLAYBOOK[regime]

    # Sector scores
    sectors = []
    for tkr, name in SECTORS.items():
        if tkr in hist:
            s = score_momentum(hist[tkr])
            s["ticker"] = tkr
            s["name"]   = name
            sectors.append(s)
    sectors_st = sorted(sectors, key=lambda x: x["score_st"], reverse=True)
    sectors_lt = sorted(sectors, key=lambda x: x["score_lt"], reverse=True)

    # Stock picks — top 3 sectors each horizon, score their stocks
    def pick_stocks(top_sectors, horizon_key, n=3):
        picks = []
        seen = set()
        for sec in top_sectors[:3]:
            tkr = sec["ticker"]
            for stk in STOCK_UNIVERSE.get(tkr, []):
                if stk in hist and stk not in seen:
                    s = score_momentum(hist[stk])
                    s["ticker"]  = stk
                    s["sector"]  = SECTORS[tkr]
                    s["sec_etf"] = tkr
                    seen.add(stk)
                    if s[horizon_key] >= 55:
                        picks.append(s)
        picks.sort(key=lambda x: x[horizon_key], reverse=True)
        return picks[:n*2]  # return more, we'll filter in PDF

    hot_st = [s for s in sectors_st if s["score_st"] >= 55]
    hot_lt = [s for s in sectors_lt if s["score_lt"] >= 55]
    picks_st = pick_stocks(hot_st or sectors_st, "score_st")
    picks_lt = pick_stocks(hot_lt or sectors_lt, "score_lt")

    # Earnings for pick tickers
    all_pick_tickers = list({p["ticker"] for p in picks_st + picks_lt})
    print("Fetching earnings dates...")
    earnings = fetch_earnings(all_pick_tickers)

    # Fear & Greed
    print("Fetching Fear & Greed index...")
    fg_val, fg_label = fetch_fear_greed()

    # Insider trades
    print("Fetching insider trades...")
    insider = fetch_insider_trades()

    # Overall market mood
    inflow_st = sum(1 for s in sectors if s["flow_st"] == "Inflow")
    inflow_lt = sum(1 for s in sectors if s["flow_lt"] == "Inflow")
    mood_st = "Risk-On" if inflow_st > 6 else "Risk-Off" if inflow_st < 4 else "Mixed"
    mood_lt = "Risk-On" if inflow_lt > 6 else "Risk-Off" if inflow_lt < 4 else "Mixed"

    return {
        "today": today,
        "macro": macro,
        "regime": regime,
        "regime_scores": regime_scores,
        "playbook": playbook,
        "signals": signals,
        "sectors_st": sectors_st,
        "sectors_lt": sectors_lt,
        "picks_st": picks_st,
        "picks_lt": picks_lt,
        "earnings": earnings,
        "fear_greed": (fg_val, fg_label),
        "insider": insider,
        "mood_st": mood_st,
        "mood_lt": mood_lt,
        "vix": signals.get("vix", 20),
    }


# ── PDF Generation ────────────────────────────────────────────────────────────
def build_pdf(data, path):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                     Table, TableStyle, HRFlowable, PageBreak)
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches

    # ── Colors ────────────────────────────────────────────────────────────────
    C_BG      = colors.HexColor("#0D1117")
    C_CARD    = colors.HexColor("#161B22")
    C_GREEN   = colors.HexColor("#1D9E75")
    C_RED     = colors.HexColor("#E24B4A")
    C_YELLOW  = colors.HexColor("#F0A500")
    C_BLUE    = colors.HexColor("#1A7FBF")
    C_PURPLE  = colors.HexColor("#7C3AED")
    C_TEXT    = colors.HexColor("#E6EDF3")
    C_MUTED   = colors.HexColor("#8B949E")
    C_BORDER  = colors.HexColor("#30363D")
    C_ORANGE  = colors.HexColor("#F97316")
    WHITE     = colors.white

    doc = SimpleDocTemplate(
        path, pagesize=A4,
        leftMargin=14*mm, rightMargin=14*mm,
        topMargin=14*mm, bottomMargin=14*mm,
    )
    W = A4[0] - 28*mm
    story = []

    # ── Styles ────────────────────────────────────────────────────────────────
    def style(name, **kw):
        s = ParagraphStyle(name, **kw)
        return s

    S_TITLE  = style("Title",  fontSize=22, textColor=WHITE,      fontName="Helvetica-Bold", spaceAfter=2)
    S_SUB    = style("Sub",    fontSize=10, textColor=C_MUTED,    fontName="Helvetica",      spaceAfter=8)
    S_H1     = style("H1",     fontSize=13, textColor=C_BLUE,     fontName="Helvetica-Bold", spaceBefore=10, spaceAfter=4)
    S_H2     = style("H2",     fontSize=11, textColor=C_TEXT,     fontName="Helvetica-Bold", spaceBefore=6,  spaceAfter=3)
    S_BODY   = style("Body",   fontSize=9,  textColor=C_TEXT,     fontName="Helvetica",      spaceAfter=3, leading=13)
    S_MUTED  = style("Muted",  fontSize=8,  textColor=C_MUTED,    fontName="Helvetica",      spaceAfter=2)
    S_GREEN  = style("Green",  fontSize=10, textColor=C_GREEN,    fontName="Helvetica-Bold")
    S_RED    = style("Red",    fontSize=10, textColor=C_RED,      fontName="Helvetica-Bold")
    S_CENTER = style("Ctr",    fontSize=9,  textColor=C_TEXT,     fontName="Helvetica",      alignment=TA_CENTER)

    def hr():
        return HRFlowable(width="100%", thickness=0.5, color=C_BORDER, spaceAfter=6, spaceBefore=6)

    def color_val(v):
        return C_GREEN if v >= 0 else C_RED

    def signed(v, dec=2):
        return f"+{v:.{dec}f}%" if v >= 0 else f"{v:.{dec}f}%"

    def regime_color(r):
        return {
            "Early Cycle": C_GREEN,
            "Mid Cycle":   C_BLUE,
            "Late Cycle":  C_YELLOW,
            "Recession":   C_RED,
        }.get(r, C_MUTED)

    # ── Helper: chart to image bytes ──────────────────────────────────────────
    def sector_bar_chart(sectors, score_key, title, top_n=11):
        fig, ax = plt.subplots(figsize=(7, 3.2))
        fig.patch.set_facecolor("#161B22")
        ax.set_facecolor("#161B22")
        names  = [s["name"][:18] for s in sectors[:top_n]]
        scores = [s[score_key] for s in sectors[:top_n]]
        bar_colors = ["#1D9E75" if v >= 58 else "#E24B4A" if v <= 42 else "#F0A500" for v in scores]
        bars = ax.barh(names[::-1], scores[::-1], color=bar_colors[::-1], height=0.6)
        ax.axvline(50, color="#30363D", linewidth=1, linestyle="--")
        ax.set_xlim(0, 100)
        ax.set_xlabel("Score", color="#8B949E", fontsize=8)
        ax.set_title(title, color="#E6EDF3", fontsize=9, pad=8)
        ax.tick_params(colors="#8B949E", labelsize=7)
        for spine in ax.spines.values():
            spine.set_edgecolor("#30363D")
        # Score labels
        for bar, score in zip(bars[::-1], scores[::-1]):
            ax.text(score + 1, bar.get_y() + bar.get_height()/2,
                    str(score), va="center", color="#E6EDF3", fontsize=7)
        plt.tight_layout(pad=0.5)
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=130, facecolor="#161B22")
        plt.close()
        buf.seek(0)
        return buf

    def price_chart(ticker, hist_data, label):
        if ticker not in hist_data:
            return None
        close = hist_data[ticker]["Close"].iloc[-60:]
        fig, ax = plt.subplots(figsize=(3.2, 1.6))
        fig.patch.set_facecolor("#161B22")
        ax.set_facecolor("#161B22")
        color = "#1D9E75" if float(close.iloc[-1]) >= float(close.iloc[0]) else "#E24B4A"
        ax.plot(close.values, color=color, linewidth=1.2)
        ax.fill_between(range(len(close)), close.values, alpha=0.15, color=color)
        ax.set_title(label, color="#E6EDF3", fontsize=7, pad=3)
        ax.tick_params(left=False, bottom=False, labelleft=False, labelbottom=False)
        for spine in ax.spines.values():
            spine.set_edgecolor("#30363D")
        plt.tight_layout(pad=0.3)
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=120, facecolor="#161B22")
        plt.close()
        buf.seek(0)
        return buf

    # ── PAGE 1: Header + Regime + Macro ──────────────────────────────────────
    today = data["today"]
    regime = data["regime"]

    story.append(Paragraph("📊 Macro Intelligence Daily", S_TITLE))
    story.append(Paragraph(
        f"Generated {today.strftime('%A, %B %d, %Y')} · Spain Time · Data: Yahoo Finance",
        S_SUB))
    story.append(hr())

    # Regime banner
    rc = regime_color(regime)
    pb = data["playbook"]
    regime_tbl = Table(
        [[Paragraph(f"<b>MARKET REGIME: {regime.upper()}</b>", style("RH", fontSize=13,
            textColor=rc, fontName="Helvetica-Bold")),
          Paragraph(pb["description"], style("RB", fontSize=9, textColor=C_TEXT,
            fontName="Helvetica", leading=13))]],
        colWidths=[W*0.32, W*0.68]
    )
    regime_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), C_CARD),
        ("ROWBACKGROUNDS", (0,0), (-1,-1), [C_CARD]),
        ("BOX", (0,0), (-1,-1), 1, C_BORDER),
        ("LINEAFTER", (0,0), (0,-1), 1, C_BORDER),
        ("TOPPADDING", (0,0), (-1,-1), 10),
        ("BOTTOMPADDING", (0,0), (-1,-1), 10),
        ("LEFTPADDING", (0,0), (-1,-1), 10),
        ("RIGHTPADDING", (0,0), (-1,-1), 10),
    ]))
    story.append(regime_tbl)
    story.append(Spacer(1, 6))

    # Regime notes + best plays
    notes_data = [
        [Paragraph("<b>Regime Notes</b>", style("rn", fontSize=9, textColor=C_MUTED, fontName="Helvetica-Bold")),
         Paragraph("<b>Best ETFs Now</b>", style("re", fontSize=9, textColor=C_MUTED, fontName="Helvetica-Bold")),
         Paragraph("<b>Best Sectors</b>", style("rs", fontSize=9, textColor=C_MUTED, fontName="Helvetica-Bold")),
         Paragraph("<b>Bond Play</b>", style("rb", fontSize=9, textColor=C_MUTED, fontName="Helvetica-Bold"))],
        [Paragraph(pb["notes"], style("rv", fontSize=9, textColor=C_TEXT, fontName="Helvetica", leading=13)),
         Paragraph("  ".join(pb["best_etfs"]), style("rv2", fontSize=9, textColor=C_GREEN, fontName="Helvetica-Bold")),
         Paragraph("  ".join(pb["best_sectors"]), style("rv3", fontSize=9, textColor=C_BLUE, fontName="Helvetica-Bold")),
         Paragraph(pb["bond_play"], style("rv4", fontSize=9, textColor=C_YELLOW, fontName="Helvetica", leading=13))],
    ]
    notes_tbl = Table(notes_data, colWidths=[W*0.3, W*0.22, W*0.22, W*0.26])
    notes_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), C_CARD),
        ("BOX", (0,0), (-1,-1), 1, C_BORDER),
        ("INNERGRID", (0,0), (-1,-1), 0.3, C_BORDER),
        ("TOPPADDING", (0,0), (-1,-1), 8),
        ("BOTTOMPADDING", (0,0), (-1,-1), 8),
        ("LEFTPADDING", (0,0), (-1,-1), 8),
        ("RIGHTPADDING", (0,0), (-1,-1), 8),
        ("VALIGN", (0,0), (-1,-1), "TOP"),
    ]))
    story.append(notes_tbl)
    story.append(Spacer(1, 8))

    # Market mood + VIX + Fear Greed row
    vix = data["vix"]
    fg_val, fg_label = data["fear_greed"]
    vix_color = C_RED if vix > 30 else C_YELLOW if vix > 20 else C_GREEN
    fg_color  = (C_RED if fg_val and fg_val < 30 else
                 C_GREEN if fg_val and fg_val > 60 else C_YELLOW) if fg_val else C_MUTED

    mood_row = [
        [Paragraph("<b>ST Mood</b>", S_MUTED),
         Paragraph("<b>LT Mood</b>", S_MUTED),
         Paragraph("<b>VIX</b>", S_MUTED),
         Paragraph("<b>Fear & Greed</b>", S_MUTED),
         Paragraph("<b>Yield Curve</b>", S_MUTED)],
        [Paragraph(f"<b>{data['mood_st']}</b>",
            style("ms", fontSize=12, textColor=C_GREEN if data['mood_st']=="Risk-On" else C_RED, fontName="Helvetica-Bold")),
         Paragraph(f"<b>{data['mood_lt']}</b>",
            style("ml", fontSize=12, textColor=C_GREEN if data['mood_lt']=="Risk-On" else C_RED, fontName="Helvetica-Bold")),
         Paragraph(f"<b>{vix:.1f}</b>",
            style("vx", fontSize=12, textColor=vix_color, fontName="Helvetica-Bold")),
         Paragraph(f"<b>{fg_val} — {fg_label}</b>" if fg_val else "<b>N/A</b>",
            style("fg", fontSize=10, textColor=fg_color, fontName="Helvetica-Bold")),
         Paragraph(f"<b>{data['signals'].get('yield_curve', 0):.2f}%</b>",
            style("yc", fontSize=12,
                  textColor=C_RED if data['signals'].get('yield_curve',0)<0 else C_GREEN,
                  fontName="Helvetica-Bold"))],
    ]
    mood_tbl = Table(mood_row, colWidths=[W/5]*5)
    mood_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), C_CARD),
        ("BOX", (0,0), (-1,-1), 1, C_BORDER),
        ("INNERGRID", (0,0), (-1,-1), 0.3, C_BORDER),
        ("TOPPADDING", (0,0), (-1,-1), 8),
        ("BOTTOMPADDING", (0,0), (-1,-1), 8),
        ("LEFTPADDING", (0,0), (-1,-1), 10),
        ("ALIGN", (0,0), (-1,-1), "CENTER"),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
    ]))
    story.append(mood_tbl)
    story.append(Spacer(1, 10))

    # Macro table
    story.append(Paragraph("MACRO SNAPSHOT", S_H1))
    macro_header = [
        Paragraph("<b>Instrument</b>", S_MUTED),
        Paragraph("<b>Price</b>", S_MUTED),
        Paragraph("<b>1D</b>", S_MUTED),
        Paragraph("<b>5D</b>", S_MUTED),
        Paragraph("<b>1M</b>", S_MUTED),
    ]
    macro_rows = [macro_header]
    for name, m in data["macro"].items():
        r1c  = C_GREEN if m["r1"]  >= 0 else C_RED
        r5c  = C_GREEN if m["r5"]  >= 0 else C_RED
        r20c = C_GREEN if m["r20"] >= 0 else C_RED
        macro_rows.append([
            Paragraph(name, style("mn", fontSize=8, textColor=C_TEXT, fontName="Helvetica")),
            Paragraph(f"<b>{m['price']:,.2f}</b>", style("mp", fontSize=8, textColor=WHITE, fontName="Helvetica-Bold")),
            Paragraph(signed(m["r1"]),  style("m1", fontSize=8, textColor=r1c,  fontName="Helvetica-Bold")),
            Paragraph(signed(m["r5"]),  style("m5", fontSize=8, textColor=r5c,  fontName="Helvetica-Bold")),
            Paragraph(signed(m["r20"]), style("m20",fontSize=8, textColor=r20c, fontName="Helvetica-Bold")),
        ])
    macro_tbl = Table(macro_rows, colWidths=[W*0.36, W*0.2, W*0.15, W*0.15, W*0.14])
    macro_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#1C2128")),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [C_CARD, colors.HexColor("#0D1117")]),
        ("BOX", (0,0), (-1,-1), 1, C_BORDER),
        ("INNERGRID", (0,0), (-1,-1), 0.3, C_BORDER),
        ("TOPPADDING", (0,0), (-1,-1), 5),
        ("BOTTOMPADDING", (0,0), (-1,-1), 5),
        ("LEFTPADDING", (0,0), (-1,-1), 7),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
    ]))
    story.append(macro_tbl)

    # ── PAGE 2: Sector Charts + Rankings ─────────────────────────────────────
    story.append(PageBreak())
    story.append(Paragraph("SECTOR ANALYSIS", S_H1))

    # Sector bar charts side by side
    from reportlab.platypus import Image as RLImage
    st_chart_buf = sector_bar_chart(data["sectors_st"], "score_st", "Short-Term Flow Score (Days–Weeks)")
    lt_chart_buf = sector_bar_chart(data["sectors_lt"], "score_lt", "Long-Term Flow Score (Months)")

    chart_tbl = Table(
        [[RLImage(st_chart_buf, width=W*0.49, height=95),
          RLImage(lt_chart_buf, width=W*0.49, height=95)]],
        colWidths=[W*0.5, W*0.5]
    )
    chart_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), C_CARD),
        ("BOX", (0,0), (-1,-1), 1, C_BORDER),
        ("TOPPADDING", (0,0), (-1,-1), 4),
        ("BOTTOMPADDING", (0,0), (-1,-1), 4),
    ]))
    story.append(chart_tbl)
    story.append(Spacer(1, 8))

    # Sector ranking table
    sec_header = [
        Paragraph("<b>Sector</b>", S_MUTED),
        Paragraph("<b>ETF</b>", S_MUTED),
        Paragraph("<b>Price</b>", S_MUTED),
        Paragraph("<b>ST Score</b>", S_MUTED),
        Paragraph("<b>ST Flow</b>", S_MUTED),
        Paragraph("<b>LT Score</b>", S_MUTED),
        Paragraph("<b>LT Flow</b>", S_MUTED),
        Paragraph("<b>1D</b>", S_MUTED),
        Paragraph("<b>1M</b>", S_MUTED),
    ]
    sec_rows = [sec_header]
    for s in data["sectors_st"]:
        stc = C_GREEN if s["flow_st"] == "Inflow" else C_RED if s["flow_st"] == "Outflow" else C_YELLOW
        ltc = C_GREEN if s["flow_lt"] == "Inflow" else C_RED if s["flow_lt"] == "Outflow" else C_YELLOW
        sec_rows.append([
            Paragraph(s["name"], style("sn", fontSize=8, textColor=C_TEXT, fontName="Helvetica")),
            Paragraph(s["ticker"], style("st", fontSize=8, textColor=C_MUTED, fontName="Helvetica")),
            Paragraph(f"${s['price']:.2f}", style("sp", fontSize=8, textColor=WHITE, fontName="Helvetica-Bold")),
            Paragraph(f"<b>{s['score_st']}</b>", style("ss", fontSize=8, textColor=C_BLUE, fontName="Helvetica-Bold")),
            Paragraph(s["flow_st"], style("sf", fontSize=8, textColor=stc, fontName="Helvetica-Bold")),
            Paragraph(f"<b>{s['score_lt']}</b>", style("sl", fontSize=8, textColor=C_PURPLE, fontName="Helvetica-Bold")),
            Paragraph(s["flow_lt"], style("lf", fontSize=8, textColor=ltc, fontName="Helvetica-Bold")),
            Paragraph(signed(s["r1"]),  style("d1", fontSize=8, textColor=color_val(s["r1"]),  fontName="Helvetica-Bold")),
            Paragraph(signed(s["r20"]), style("d20",fontSize=8, textColor=color_val(s["r20"]), fontName="Helvetica-Bold")),
        ])
    sec_tbl = Table(sec_rows, colWidths=[W*0.21, W*0.07, W*0.09, W*0.09, W*0.1, W*0.09, W*0.1, W*0.1, W*0.1])  # noqa
    sec_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#1C2128")),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [C_CARD, colors.HexColor("#0D1117")]),
        ("BOX", (0,0), (-1,-1), 1, C_BORDER),
        ("INNERGRID", (0,0), (-1,-1), 0.3, C_BORDER),
        ("TOPPADDING", (0,0), (-1,-1), 5),
        ("BOTTOMPADDING", (0,0), (-1,-1), 5),
        ("LEFTPADDING", (0,0), (-1,-1), 5),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
    ]))
    story.append(sec_tbl)

    # ── PAGE 3: Stock Picks ───────────────────────────────────────────────────
    story.append(PageBreak())

    def picks_table(picks, title, score_key, color, earnings):
        story.append(Paragraph(title, S_H1))
        if not picks:
            story.append(Paragraph("No strong picks identified today.", S_MUTED))
            return
        hdr = [
            Paragraph("<b>Stock</b>", S_MUTED),
            Paragraph("<b>Sector</b>", S_MUTED),
            Paragraph("<b>Price</b>", S_MUTED),
            Paragraph(f"<b>Score</b>", S_MUTED),
            Paragraph("<b>ST</b>", S_MUTED),
            Paragraph("<b>LT</b>", S_MUTED),
            Paragraph("<b>1D</b>", S_MUTED),
            Paragraph("<b>1M</b>", S_MUTED),
            Paragraph("<b>vs200d</b>", S_MUTED),
            Paragraph("<b>Disc%</b>", S_MUTED),
            Paragraph("<b>Earnings</b>", S_MUTED),
            Paragraph("<b>Signal</b>", S_MUTED),
        ]
        rows = [hdr]
        for p in picks:
            sc  = p[score_key]
            stc = color_val(p["r1"])
            disc_c = C_GREEN if p["discount"] > 15 else C_YELLOW if p["discount"] > 8 else C_MUTED
            # Determine signal
            if p["score_st"] >= 60 and p["score_lt"] >= 60:
                sig, sigc = "Strong Both", C_GREEN
            elif p["score_st"] >= 60:
                sig, sigc = "ST Trade", C_YELLOW
            elif p["score_lt"] >= 60:
                sig, sigc = "LT Hold", C_BLUE
            else:
                sig, sigc = "Watch", C_MUTED
            earn = earnings.get(p["ticker"], "—")
            rows.append([
                Paragraph(f"<b>{p['ticker']}</b>", style("pk", fontSize=8, textColor=WHITE, fontName="Helvetica-Bold")),
                Paragraph(p["sector"][:14], style("ps", fontSize=7, textColor=C_MUTED, fontName="Helvetica")),
                Paragraph(f"${p['price']:.2f}", style("pp", fontSize=8, textColor=WHITE, fontName="Helvetica-Bold")),
                Paragraph(f"<b>{sc}</b>", style("psc", fontSize=8, textColor=color, fontName="Helvetica-Bold")),
                Paragraph(str(p["score_st"]), style("pst", fontSize=8, textColor=C_BLUE,   fontName="Helvetica-Bold")),
                Paragraph(str(p["score_lt"]), style("plt", fontSize=8, textColor=C_PURPLE, fontName="Helvetica-Bold")),
                Paragraph(signed(p["r1"]),  style("p1", fontSize=8, textColor=color_val(p["r1"]),  fontName="Helvetica-Bold")),
                Paragraph(signed(p["r20"]), style("p20",fontSize=8, textColor=color_val(p["r20"]), fontName="Helvetica-Bold")),
                Paragraph(signed(p["above200"]), style("p200", fontSize=8, textColor=color_val(p["above200"]), fontName="Helvetica-Bold")),
                Paragraph(f"-{p['discount']:.0f}%", style("pd", fontSize=8, textColor=disc_c, fontName="Helvetica-Bold")),
                Paragraph(earn, style("pe", fontSize=7, textColor=C_YELLOW, fontName="Helvetica")),
                Paragraph(f"<b>{sig}</b>", style("psi", fontSize=7, textColor=sigc, fontName="Helvetica-Bold")),
            ])
        t = Table(rows, colWidths=[W*0.09, W*0.12, W*0.08, W*0.07,
                                    W*0.06, W*0.06, W*0.08, W*0.08,
                                    W*0.09, W*0.07, W*0.1,  W*0.1])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#1C2128")),
            ("ROWBACKGROUNDS", (0,1), (-1,-1), [C_CARD, colors.HexColor("#0D1117")]),
            ("BOX", (0,0), (-1,-1), 1, C_BORDER),
            ("INNERGRID", (0,0), (-1,-1), 0.3, C_BORDER),
            ("TOPPADDING", (0,0), (-1,-1), 5),
            ("BOTTOMPADDING", (0,0), (-1,-1), 5),
            ("LEFTPADDING", (0,0), (-1,-1), 5),
            ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ]))
        story.append(t)
        story.append(Spacer(1, 6))

        # Signal explanations
        story.append(Paragraph("<b>Why these picks?</b>", S_H2))
        for p in picks[:4]:
            if p["score_st"] >= 60 and p["score_lt"] >= 60:
                why = (f"<b>{p['ticker']}</b> — Strong inflow both short and long term "
                       f"(ST:{p['score_st']} LT:{p['score_lt']}). "
                       f"Price {signed(p['above200'])} vs 200-day average. "
                       f"1-day {signed(p['r1'])}, volume x{p['vol_ratio']:.1f} average. "
                       f"In {p['sector']} which is hot in current {data['regime']} regime.")
            elif p["score_st"] >= 60:
                why = (f"<b>{p['ticker']}</b> — Short-term momentum play (ST:{p['score_st']}). "
                       f"Recent 1D {signed(p['r1'])}, 1M {signed(p['r20'])} with volume surge x{p['vol_ratio']:.1f}. "
                       f"Take profit quickly — long-term score lower (LT:{p['score_lt']}).")
            else:
                why = (f"<b>{p['ticker']}</b> — Long-term accumulation candidate (LT:{p['score_lt']}). "
                       f"Price {signed(p['above200'])} vs 200d. "
                       f"In {p['sector']}. Short-term weak — consider scaling in.")
            earn = data["earnings"].get(p["ticker"])
            if earn:
                why += f" ⚠️ Earnings: {earn} — be aware of event risk."
            story.append(Paragraph(why, S_BODY))

    picks_table(data["picks_st"], "SHORT-TERM PICKS — Days to Weeks", "score_st", C_YELLOW, data["earnings"])
    picks_table(data["picks_lt"], "LONG-TERM PICKS — Weeks to Months", "score_lt", C_PURPLE, data["earnings"])

    # ── PAGE 4: Insider Trades + Risk Factors ─────────────────────────────────
    story.append(PageBreak())

    # Insider trades
    story.append(Paragraph("RECENT INSIDER BUYING (Last 7 Days, $100k+)", S_H1))
    if data["insider"]:
        ins_hdr = [
            Paragraph("<b>Date</b>", S_MUTED),
            Paragraph("<b>Ticker</b>", S_MUTED),
            Paragraph("<b>Insider</b>", S_MUTED),
            Paragraph("<b>Title</b>", S_MUTED),
            Paragraph("<b>Type</b>", S_MUTED),
            Paragraph("<b>Value</b>", S_MUTED),
        ]
        ins_rows = [ins_hdr]
        for t in data["insider"]:
            ins_rows.append([
                Paragraph(t.get("date","")[:10], style("id", fontSize=8, textColor=C_MUTED, fontName="Helvetica")),
                Paragraph(f"<b>{t.get('ticker','')}</b>", style("it", fontSize=8, textColor=C_GREEN, fontName="Helvetica-Bold")),
                Paragraph(t.get("insider","")[:20], style("ii", fontSize=7, textColor=C_TEXT, fontName="Helvetica")),
                Paragraph(t.get("title","")[:18], style("itr", fontSize=7, textColor=C_MUTED, fontName="Helvetica")),
                Paragraph(t.get("type",""), style("ity", fontSize=8,
                    textColor=C_GREEN if "P" in t.get("type","") else C_RED, fontName="Helvetica-Bold")),
                Paragraph(t.get("value",""), style("iv", fontSize=8, textColor=C_YELLOW, fontName="Helvetica-Bold")),
            ])
        ins_tbl = Table(ins_rows, colWidths=[W*0.13, W*0.1, W*0.25, W*0.22, W*0.12, W*0.18])
        ins_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#1C2128")),
            ("ROWBACKGROUNDS", (0,1), (-1,-1), [C_CARD, colors.HexColor("#0D1117")]),
            ("BOX", (0,0), (-1,-1), 1, C_BORDER),
            ("INNERGRID", (0,0), (-1,-1), 0.3, C_BORDER),
            ("TOPPADDING", (0,0), (-1,-1), 5),
            ("BOTTOMPADDING", (0,0), (-1,-1), 5),
            ("LEFTPADDING", (0,0), (-1,-1), 6),
            ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ]))
        story.append(ins_tbl)
        story.append(Paragraph(
            "Insider buying is a strong signal — insiders know their company best. "
            "Focus on purchase transactions (P) not sales (S).",
            S_MUTED))
    else:
        story.append(Paragraph("Insider data unavailable today (openinsider.com may be rate-limited).", S_MUTED))

    story.append(Spacer(1, 10))

    # Risk factors
    story.append(Paragraph("RISK FACTORS & WHAT TO WATCH", S_H1))
    vix = data["vix"]
    yc  = data["signals"].get("yield_curve", 0)
    risks = []
    if vix > 30:
        risks.append(("🔴 HIGH VIX", f"VIX at {vix:.1f} — extreme fear, consider reducing exposure or hedging with puts."))
    elif vix > 20:
        risks.append(("🟡 ELEVATED VIX", f"VIX at {vix:.1f} — caution warranted. Markets nervous."))
    else:
        risks.append(("🟢 LOW VIX", f"VIX at {vix:.1f} — complacency? Good for momentum but watch for spikes."))

    if yc < -0.5:
        risks.append(("🔴 INVERTED YIELD CURVE", f"Curve at {yc:.2f}% — strong recession signal. Historically leads recession by 12-18 months."))
    elif yc < 0:
        risks.append(("🟡 FLAT/INVERTED CURVE", f"Curve at {yc:.2f}% — mild inversion. Monitor credit markets."))
    else:
        risks.append(("🟢 NORMAL YIELD CURVE", f"Curve at {yc:.2f}% — healthy sign. Economy expanding."))

    fg_val, fg_label = data["fear_greed"]
    if fg_val:
        if fg_val < 25:
            risks.append(("🔴 EXTREME FEAR", f"Fear & Greed at {fg_val} ({fg_label}) — contrarian BUY signal historically."))
        elif fg_val > 75:
            risks.append(("🟡 EXTREME GREED", f"Fear & Greed at {fg_val} ({fg_label}) — market euphoric, be careful chasing."))
        else:
            risks.append(("🟡 SENTIMENT", f"Fear & Greed at {fg_val} ({fg_label}) — neutral sentiment."))

    # Upcoming earnings risk
    earn_tickers = [t for t, d in data["earnings"].items() if d != "—"]
    if earn_tickers:
        risks.append(("⚠️ EARNINGS RISK", f"Upcoming earnings for: {', '.join(earn_tickers[:8])}. Avoid holding through earnings unless intended."))

    for label, text in risks:
        risk_tbl = Table(
            [[Paragraph(f"<b>{label}</b>", style("rl", fontSize=9, textColor=C_TEXT, fontName="Helvetica-Bold")),
              Paragraph(text, style("rt", fontSize=9, textColor=C_TEXT, fontName="Helvetica", leading=13))]],
            colWidths=[W*0.28, W*0.72]
        )
        risk_tbl.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,-1), C_CARD),
            ("BOX", (0,0), (-1,-1), 1, C_BORDER),
            ("LINEAFTER", (0,0), (0,-1), 1, C_BORDER),
            ("TOPPADDING", (0,0), (-1,-1), 7),
            ("BOTTOMPADDING", (0,0), (-1,-1), 7),
            ("LEFTPADDING", (0,0), (-1,-1), 8),
        ]))
        story.append(risk_tbl)
        story.append(Spacer(1, 3))

    story.append(Spacer(1, 10))

    # Regime score breakdown
    story.append(Paragraph("REGIME SCORING BREAKDOWN", S_H1))
    rs = data["regime_scores"]
    reg_rows = [[
        Paragraph("<b>Early Cycle</b>", style("r0", fontSize=9, textColor=C_GREEN,  fontName="Helvetica-Bold")),
        Paragraph("<b>Mid Cycle</b>",   style("r1", fontSize=9, textColor=C_BLUE,   fontName="Helvetica-Bold")),
        Paragraph("<b>Late Cycle</b>",  style("r2", fontSize=9, textColor=C_YELLOW, fontName="Helvetica-Bold")),
        Paragraph("<b>Recession</b>",   style("r3", fontSize=9, textColor=C_RED,    fontName="Helvetica-Bold")),
    ], [
        Paragraph(str(rs.get("Early Cycle", 0)), style("r0v", fontSize=14, textColor=C_GREEN,  fontName="Helvetica-Bold")),
        Paragraph(str(rs.get("Mid Cycle",   0)), style("r1v", fontSize=14, textColor=C_BLUE,   fontName="Helvetica-Bold")),
        Paragraph(str(rs.get("Late Cycle",  0)), style("r2v", fontSize=14, textColor=C_YELLOW, fontName="Helvetica-Bold")),
        Paragraph(str(rs.get("Recession",   0)), style("r3v", fontSize=14, textColor=C_RED,    fontName="Helvetica-Bold")),
    ]]
    reg_tbl = Table(reg_rows, colWidths=[W/4]*4)
    reg_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), C_CARD),
        ("BOX", (0,0), (-1,-1), 1, C_BORDER),
        ("INNERGRID", (0,0), (-1,-1), 0.3, C_BORDER),
        ("ALIGN", (0,0), (-1,-1), "CENTER"),
        ("TOPPADDING", (0,0), (-1,-1), 10),
        ("BOTTOMPADDING", (0,0), (-1,-1), 10),
    ]))
    story.append(reg_tbl)
    story.append(Spacer(1, 6))
    story.append(Paragraph(
        "Inputs: Yield curve (10Y-3M), VIX level, S&P 500 trend vs 200-day, "
        "3 & 6-month momentum, TLT bond flows, HYG credit flows. "
        "Higher score = more signals pointing to that regime.",
        S_MUTED))
    story.append(Spacer(1, 8))
    story.append(hr())
    story.append(Paragraph(
        f"Generated {local_now().strftime('%Y-%m-%d %H:%M')} Spain time. "
        "Data via Yahoo Finance & openinsider.com. "
        "For informational purposes only — not financial advice. "
        "Always do your own research.",
        S_MUTED))

    # ── Background color ──────────────────────────────────────────────────────
    def dark_background(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(C_BG)
        canvas.rect(0, 0, A4[0], A4[1], fill=True, stroke=False)
        canvas.restoreState()

    doc.build(story, onFirstPage=dark_background, onLaterPages=dark_background)
    print(f"PDF saved: {path}")


# ── ntfy Push ─────────────────────────────────────────────────────────────────
def send_push(data, pdf_path):
    if not NTFY_TOPIC or NTFY_TOPIC == "PUT-YOUR-TOPIC-HERE":
        return
    regime = data["regime"]
    mood_st = data["mood_st"]
    mood_lt = data["mood_lt"]
    vix = data["vix"]
    fg_val, fg_label = data["fear_greed"]
    top3_st = [p["ticker"] for p in data["picks_st"][:3]]
    top3_lt = [p["ticker"] for p in data["picks_lt"][:3]]

    lines = [
        f"Regime: {regime}",
        f"ST: {mood_st} | LT: {mood_lt}",
        f"VIX: {vix:.1f}" + (f" | F&G: {fg_val} {fg_label}" if fg_val else ""),
        f"ST picks: {', '.join(top3_st) or 'none'}",
        f"LT picks: {', '.join(top3_lt) or 'none'}",
    ]
    if data["insider"]:
        ins = data["insider"][0]
        lines.append(f"Insider buy: {ins.get('ticker','')} {ins.get('value','')}")

    body = "\n".join(lines).encode("utf-8")
    today = local_today()
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=body,
        headers={
            "Title": f"Macro Report - {today:%b %d} | {regime}",
            "Tags": "chart_with_upwards_trend,page_facing_up",
            "Priority": "default",
        },
    )
    try:
        urllib.request.urlopen(req, timeout=15)
        print(f"ntfy push sent to '{NTFY_TOPIC}'")
    except Exception as e:
        print(f"ntfy push failed: {e}")


# ── Email ─────────────────────────────────────────────────────────────────────
def send_email(pdf_path, data):
    import smtplib, ssl
    from email.mime.multipart import MIMEMultipart
    from email.mime.base import MIMEBase
    from email.mime.text import MIMEText
    from email import encoders

    today = local_today()
    subject = (f"Macro Report {today:%b %d} | {data['regime']} | "
               f"ST:{data['mood_st']} LT:{data['mood_lt']}")
    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"]    = SMTP_USER
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(
        f"Your daily macro intelligence report is attached.\n\n"
        f"Regime: {data['regime']}\n"
        f"ST Mood: {data['mood_st']} | LT Mood: {data['mood_lt']}\n"
        f"VIX: {data['vix']:.1f}\n"
        f"Top ST picks: {', '.join(p['ticker'] for p in data['picks_st'][:3])}\n"
        f"Top LT picks: {', '.join(p['ticker'] for p in data['picks_lt'][:3])}\n",
        "plain"
    ))
    with open(pdf_path, "rb") as f:
        part = MIMEBase("application", "octet-stream")
        part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f'attachment; filename="{os.path.basename(pdf_path)}"')
        msg.attach(part)
    ctx = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls(context=ctx)
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, [EMAIL_TO], msg.as_string())
    print(f"Email sent to {EMAIL_TO}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Macro Intelligence Daily Report")
    ap.add_argument("--no-email", action="store_true", help="skip email")
    ap.add_argument("--push",     action="store_true", help="send ntfy summary")
    ap.add_argument("--out",      default="", help="output PDF path (default: auto)")
    args = ap.parse_args()

    today = local_today()
    pdf_path = args.out or f"macro_report_{today}.pdf"

    hist = fetch_all_data()
    if not hist:
        print("ERROR: no data. Check network.")
        sys.exit(1)

    data = build_report_data(hist)

    print(f"\nRegime: {data['regime']}")
    print(f"ST Mood: {data['mood_st']}  |  LT Mood: {data['mood_lt']}")
    print(f"VIX: {data['vix']:.1f}  |  Yield Curve: {data['signals'].get('yield_curve',0):.2f}%")
    if data["fear_greed"][0]:
        print(f"Fear & Greed: {data['fear_greed'][0]} — {data['fear_greed'][1]}")
    print(f"Top ST picks: {[p['ticker'] for p in data['picks_st'][:5]]}")
    print(f"Top LT picks: {[p['ticker'] for p in data['picks_lt'][:5]]}")
    if data["insider"]:
        print(f"Insider trades: {[t['ticker'] for t in data['insider']]}")

    build_pdf(data, pdf_path)

    if args.push:
        send_push(data, pdf_path)

    if not args.no_email:
        send_email(pdf_path, data)
    else:
        print("(--no-email set; skipping email)")


if __name__ == "__main__":
    main()
