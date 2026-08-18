#!/usr/bin/env python3
"""
weekly_pick.py
--------------
Monday DCA helper. Ranks a fixed universe of quality names on
cross-sectional value / quality / trend / under-allocation, and pushes
the top candidate for this week's $100 contribution via ntfy.

This is a RANKING TOOL based on rules you chose. It is not investment
advice and it does not know your tax, currency or broker situation.

Env vars:
  NTFY_TOPIC      required, e.g. jaswinder-invest
  NTFY_SERVER     default https://ntfy.sh
  WEEKLY_AMOUNT   default 100
  UNIVERSE_FILE   default universe.txt   (one ticker per line, # = comment)
  LEDGER_FILE     default picks_log.csv
  DRY_RUN         set to 1 to skip the push
"""

import os
import sys
import time
import datetime as dt

import numpy as np
import pandas as pd
import requests
import yfinance as yf

# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")
WEEKLY_AMOUNT = float(os.getenv("WEEKLY_AMOUNT", "100"))
UNIVERSE_FILE = os.getenv("UNIVERSE_FILE", "universe.txt")
LEDGER_FILE = os.getenv("LEDGER_FILE", "picks_log.csv")
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"

# Score weights. Positive weight = higher metric is better.
WEIGHTS = {
    "fcf_yield": 1.0,    # cash generation per euro of market cap
    "roe": 0.8,          # quality
    "rev_growth": 0.6,   # is the business still growing
    "gross_margin": 0.5, # pricing power / moat proxy
    "dd_52w": 0.9,       # how far below 52w high -> buy weakness, not strength
    "fwd_pe": -1.0,      # cheaper is better
    "debt_equity": -0.5, # leverage penalty
    "under_alloc": 0.25, # tiebreaker only - repeats are allowed and expected
}

# Hard filters. A name failing any of these is skipped this week.
MIN_MCAP = 5e9          # no micro caps
MAX_BELOW_200DMA = 0.80 # skip if price < 80% of 200-day MA (broken trend)
MAX_FWD_PE = 60         # skip obvious froth

# Max share price, in USD, for a name to be eligible. Set high (e.g. 999999)
# to disable. NOTE: share price level says nothing about whether a stock is
# cheap - a $40 share can be far more expensive than a $400 one. This filter
# only exists for brokers without fractional shares. Prices in other
# currencies are converted to USD before the comparison.
MAX_PRICE = float(os.getenv("MAX_PRICE", "100"))

# The same name CAN win week after week - that is intended. The only brake
# is this: once a ticker exceeds this share of everything invested so far,
# it steps aside and the runner-up gets the money. Set to 1.0 to disable.
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "0.30"))

DEFAULT_UNIVERSE = """
# Core long-term universe. Edit freely - this is YOUR list, the script
# only decides which of YOUR names looks best this week.
# Broad market ballast
VOO
VWCE.DE
# Quality compounders
MSFT
GOOGL
AAPL
AMZN
BRK-B
V
MA
UNH
JNJ
COST
LIN
ASML
NVO
"""


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def load_universe(path: str) -> list:
    if not os.path.exists(path):
        with open(path, "w") as fh:
            fh.write(DEFAULT_UNIVERSE.lstrip())
        print(f"[i] created {path} with default universe")
    out = []
    for line in open(path):
        line = line.split("#")[0].strip()
        if line:
            out.append(line.upper())
    return sorted(set(out))


def load_ledger(path: str) -> pd.DataFrame:
    cols = ["date", "ticker", "amount", "price", "shares", "score"]
    if os.path.exists(path):
        df = pd.read_csv(path)
        for c in cols:
            if c not in df.columns:
                df[c] = np.nan
        return df[cols]
    return pd.DataFrame(columns=cols)


def safe_info(ticker: str, tries: int = 3) -> dict:
    for i in range(tries):
        try:
            info = yf.Ticker(ticker).info
            if info and info.get("regularMarketPrice") is not None:
                return info
            if info:
                return info
        except Exception as exc:
            print(f"[!] info {ticker} attempt {i+1}: {exc}")
        time.sleep(1.5 * (i + 1))
    return {}


def num(info: dict, key: str):
    v = info.get(key)
    if v is None:
        return np.nan
    try:
        v = float(v)
    except (TypeError, ValueError):
        return np.nan
    return np.nan if not np.isfinite(v) else v


_FX_CACHE = {}


def fx_rate(frm: str, to: str = "USD") -> float:
    """
    FX rate to multiply an amount in `frm` by, to get `to`.
    Returns np.nan if it can't be resolved, so callers can fail loudly
    rather than silently comparing DKK against USD.
    """
    if not frm or not to:
        return np.nan
    frm, to = frm.upper(), to.upper()
    if frm == to:
        return 1.0
    key = (frm, to)
    if key in _FX_CACHE:
        return _FX_CACHE[key]

    rate = np.nan
    for pair, invert in ((f"{frm}{to}=X", False), (f"{to}{frm}=X", True)):
        try:
            px = yf.Ticker(pair).history(period="5d")["Close"].dropna()
            if len(px):
                v = float(px.iloc[-1])
                if v > 0:
                    rate = (1.0 / v) if invert else v
                    break
        except Exception as exc:
            print(f"[!] fx {pair}: {exc}")
        time.sleep(0.3)

    if np.isnan(rate):
        print(f"[!] could not resolve FX {frm}->{to}")
    _FX_CACHE[key] = rate
    return rate


def zscore(s: pd.Series) -> pd.Series:
    """Winsorised z-score. NaN -> 0 so a missing field is neutral, not fatal."""
    s = s.astype(float)
    if s.notna().sum() < 3:
        return pd.Series(0.0, index=s.index)
    lo, hi = s.quantile(0.05), s.quantile(0.95)
    s = s.clip(lo, hi)
    sd = s.std(ddof=0)
    if not sd or not np.isfinite(sd):
        return pd.Series(0.0, index=s.index)
    return ((s - s.mean()) / sd).fillna(0.0)


# ----------------------------------------------------------------------
# Data collection
# ----------------------------------------------------------------------
def build_frame(tickers: list, ledger: pd.DataFrame) -> pd.DataFrame:
    hist = yf.download(
        tickers, period="1y", interval="1d",
        auto_adjust=True, progress=False, group_by="ticker", threads=True,
    )

    invested = ledger.groupby("ticker")["amount"].sum() if len(ledger) else pd.Series(dtype=float)
    total_invested = float(invested.sum()) if len(invested) else 0.0

    rows = []
    for t in tickers:
        try:
            px = hist[t]["Close"].dropna() if len(tickers) > 1 else hist["Close"].dropna()
        except Exception:
            px = pd.Series(dtype=float)
        if len(px) < 60:
            print(f"[skip] {t}: not enough price history")
            continue

        price = float(px.iloc[-1])
        sma200 = float(px.tail(200).mean())
        high52 = float(px.max())

        info = safe_info(t)
        mcap = num(info, "marketCap")
        fcf = num(info, "freeCashflow")

        # --- currency handling -------------------------------------------
        # marketCap and price come back in the LISTING currency, but
        # freeCashflow comes back in the REPORTING currency. For ADRs and
        # foreign listings these differ (NVO: USD listing, DKK books), and
        # dividing one by the other inflates FCF yield by the FX rate.
        quote_cur = (info.get("currency") or "USD").upper()
        fin_cur = (info.get("financialCurrency") or quote_cur).upper()

        if fcf is not np.nan and not np.isnan(fcf) and fin_cur != quote_cur:
            r = fx_rate(fin_cur, quote_cur)
            if np.isnan(r):
                fcf = np.nan  # unknown FX -> drop the metric, never guess
                print(f"[!] {t}: dropping FCF, no {fin_cur}->{quote_cur} rate")
            else:
                fcf = fcf * r
                print(f"[i] {t}: FCF converted {fin_cur}->{quote_cur} @ {r:.5f}")

        usd_r = fx_rate(quote_cur, "USD")
        price_usd = price * usd_r if not np.isnan(usd_r) else np.nan

        # forwardPE fallback: NaN is truthy in Python, so `a or b` never
        # falls through. Test explicitly.
        fwd = num(info, "forwardPE")
        if np.isnan(fwd):
            fwd = num(info, "trailingPE")

        rows.append({
            "ticker": t,
            "price": price,
            "price_usd": price_usd,
            "currency": quote_cur,
            "mcap": mcap,
            "sma200_ratio": price / sma200 if sma200 else np.nan,
            "dd_52w": -(price / high52 - 1) * 100 if high52 else np.nan,  # 12 = 12% below high
            "fwd_pe": fwd,
            "roe": num(info, "returnOnEquity") * 100 if not np.isnan(num(info, "returnOnEquity")) else np.nan,
            "rev_growth": num(info, "revenueGrowth") * 100 if not np.isnan(num(info, "revenueGrowth")) else np.nan,
            "gross_margin": num(info, "grossMargins") * 100 if not np.isnan(num(info, "grossMargins")) else np.nan,
            "debt_equity": num(info, "debtToEquity"),
            "fcf_yield": (fcf / mcap * 100) if (mcap and fcf and mcap > 0) else np.nan,
            "invested": float(invested.get(t, 0.0)),
        })
        time.sleep(0.4)  # be polite to yahoo

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    # Under-allocation: how far below equal-weight this name currently sits.
    target = total_invested / len(df) if total_invested else 0.0
    df["under_alloc"] = target - df["invested"]
    return df


# ----------------------------------------------------------------------
# Scoring
# ----------------------------------------------------------------------
def apply_filters(df: pd.DataFrame) -> pd.DataFrame:
    before = set(df["ticker"])
    ok = df.copy()
    ok = ok[(ok["mcap"].isna()) | (ok["mcap"] >= MIN_MCAP)]
    ok = ok[(ok["sma200_ratio"].isna()) | (ok["sma200_ratio"] >= MAX_BELOW_200DMA)]
    ok = ok[(ok["fwd_pe"].isna()) | (ok["fwd_pe"] <= MAX_FWD_PE)]

    # Share price ceiling. Unlike the others, a missing price is NOT given
    # the benefit of the doubt - if we can't price it in USD we can't know
    # it clears the ceiling.
    if MAX_PRICE < 999999:
        too_dear = ok["price_usd"].isna() | (ok["price_usd"] > MAX_PRICE)
        for _, r in ok[too_dear].iterrows():
            shown = "unknown" if np.isnan(r["price_usd"]) else f"${r['price_usd']:.0f}"
            print(f"[i] {r['ticker']} price {shown} > ${MAX_PRICE:.0f} cap - skipped")
        ok = ok[~too_dear]

    total = float(df["invested"].sum())
    if total > 0 and MAX_POSITION_PCT < 1.0 and len(ok) > 1:
        over = ok["invested"] / total > MAX_POSITION_PCT
        if over.any() and (~over).any():
            for t in ok.loc[over, "ticker"]:
                print(f"[i] {t} over {MAX_POSITION_PCT:.0%} cap - stepping aside")
            ok = ok[~over]

    dropped = before - set(ok["ticker"])
    if dropped:
        print(f"[i] filtered out: {', '.join(sorted(dropped))}")
    return ok.reset_index(drop=True)


def score(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["score"] = 0.0
    for col, w in WEIGHTS.items():
        if col not in df.columns:
            continue
        df[f"z_{col}"] = zscore(df[col])
        df["score"] += w * df[f"z_{col}"]
    return df.sort_values("score", ascending=False).reset_index(drop=True)


def reasons(row: pd.Series) -> str:
    bits = []
    if not np.isnan(row.get("dd_52w", np.nan)):
        bits.append(f"{row['dd_52w']:.0f}% off 52w high")
    if not np.isnan(row.get("fwd_pe", np.nan)):
        bits.append(f"fwd P/E {row['fwd_pe']:.1f}")
    if not np.isnan(row.get("fcf_yield", np.nan)):
        bits.append(f"FCF yld {row['fcf_yield']:.1f}%")
    if not np.isnan(row.get("roe", np.nan)):
        bits.append(f"ROE {row['roe']:.0f}%")
    if row.get("invested", 0) == 0:
        bits.append("not owned yet")
    return " | ".join(bits)


# ----------------------------------------------------------------------
# Output
# ----------------------------------------------------------------------
def ntfy_url() -> str:
    """Accept either a bare topic name or a full https://server/topic URL."""
    t = NTFY_TOPIC.strip().strip("/")
    if t.startswith("http://") or t.startswith("https://"):
        return t
    return f"{NTFY_SERVER}/{t}"


def ascii_safe(s: str) -> str:
    """ntfy headers must be latin-1. Strip anything that isn't."""
    subs = {"\u2014": "-", "\u2013": "-", "\u2018": "'", "\u2019": "'",
            "\u201c": '"', "\u201d": '"', "\u2026": "...", "\u2192": "->"}
    for bad, good in subs.items():
        s = s.replace(bad, good)
    return s.encode("latin-1", "replace").decode("latin-1")


def notify(title: str, body: str, tags: str = "chart_with_upwards_trend"):
    if DRY_RUN or not NTFY_TOPIC:
        print("--- ntfy (not sent, topic empty) ---")
        print(title)
        print(body)
        return
    url = ntfy_url()
    print(f"[i] posting to {url}")
    try:
        r = requests.post(
            url,
            data=body.encode("utf-8"),
            headers={"Title": ascii_safe(title), "Tags": tags, "Priority": "default"},
            timeout=20,
        )
        print(f"[i] ntfy status {r.status_code}: {r.text[:200]}")
        r.raise_for_status()
        print("[ok] ntfy sent")
    except Exception as exc:
        print(f"[!] ntfy failed: {exc}")


def main():
    today = dt.date.today()
    tickers = load_universe(UNIVERSE_FILE)
    ledger = load_ledger(LEDGER_FILE)
    print(f"[i] universe {len(tickers)} names, ledger {len(ledger)} rows")

    df = build_frame(tickers, ledger)
    if df.empty:
        notify("Weekly pick FAILED", "No usable data this week.", "warning")
        sys.exit(1)

    df = apply_filters(df)
    if df.empty:
        notify("Weekly pick: no candidate",
               "Every name failed the trend/valuation filters. "
               "Consider holding the $100 in cash this week.", "warning")
        return

    ranked = score(df)
    top = ranked.iloc[0]
    # size off the USD price - the $100 is dollars, the quote may not be
    px_usd = top.get("price_usd", np.nan)
    px_for_sizing = px_usd if not np.isnan(px_usd) else top["price"]
    shares = WEEKLY_AMOUNT / px_for_sizing if px_for_sizing else 0

    # how many weeks in a row this name has won
    streak = 0
    for tk in reversed(ledger["ticker"].tolist()):
        if tk == top["ticker"]:
            streak += 1
        else:
            break

    lines = [
        f"BUY: {top['ticker']}  ~${WEEKLY_AMOUNT:.0f}",
        f"Price ${px_for_sizing:.2f}  ->  {shares:.4f} sh"
        + (f"  ({top['currency']} {top['price']:.2f})"
           if top.get("currency", "USD") != "USD" else ""),
        f"Why: {reasons(top)}",
    ]
    if streak:
        lines.append(f"Repeat: week {streak + 1} in a row")
    lines += ["", "Runners-up:"]
    for _, r in ranked.iloc[1:4].iterrows():
        lines.append(f"  {r['ticker']:<7} {r['score']:+.2f}  {reasons(r)}")
    lines += ["", f"Total invested so far: ${ledger['amount'].sum():,.0f}" if len(ledger) else "",
              "Ranking tool, not advice. Check it yourself."]

    body = "\n".join([l for l in lines if l is not None])
    notify(f"Weekly DCA {today.isoformat()} - {top['ticker']}", body)

    # append to ledger
    new = pd.DataFrame([{
        "date": today.isoformat(),
        "ticker": top["ticker"],
        "amount": WEEKLY_AMOUNT,
        "price": round(float(top["price"]), 4),
        "shares": round(float(shares), 6),
        "score": round(float(top["score"]), 3),
    }])
    pd.concat([ledger, new], ignore_index=True).to_csv(LEDGER_FILE, index=False)

    ranked.to_csv("weekly_rank_latest.csv", index=False)
    print(body)


if __name__ == "__main__":
    main()
