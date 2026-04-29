#!/usr/bin/env python3
"""Free market intelligence system — $0/month. No Claude API required.

Three commands:
  python brief.py daily          → daily prompt+data → GitHub Issue (you paste into Claude.ai)
  python brief.py ideas          → weekly idea prompt+data → GitHub Issue
  python brief.py research TICK  → produces research_TICK_DATE.md you upload to Claude.ai

Default mode: gathers all data, builds a clean prompt, delivers via GitHub Issue
(GitHub emails you the notification — that IS the push). You paste into Claude.ai
free tier and get the briefing in 30 seconds.

Optional auto mode: set GEMINI_API_KEY to use Google's free Gemini 2.5 Flash
(1500 req/day free, no card required). Fully automated. Lower quality than
Claude but hands-free.
"""

import os
import re
import sys
import json
import time
import datetime as dt
from pathlib import Path

import feedparser
import requests


# ---------- Config ----------

FRED_KEY = os.environ.get("FRED_API_KEY", "")
SEC_UA = os.environ.get("SEC_USER_AGENT", "Evan Cogan evancogan21@gmail.com")

# Set automatically by GitHub Actions when running in CI
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPOSITORY", "")  # e.g. "evanc/market-intel-free"

# Optional Telegram phone-push
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

# Optional fully-automated mode using Google Gemini free tier
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
USE_GEMINI = bool(GEMINI_KEY)

# RSS feeds — primary news sources
FEEDS = {
    "FT Markets": "https://www.ft.com/markets?format=rss",
    "WSJ Markets": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
    "Bloomberg Markets": "https://feeds.bloomberg.com/markets/news.rss",
    "Reuters Business": "https://www.reutersagency.com/feed/?best-topics=business-finance",
    "Net Interest": "https://www.netinterest.co/feed",
    "Apricitas Economics": "https://www.apricitas.io/feed",
    "Marginal Revolution": "https://marginalrevolution.com/feed",
}

# FRED series for the macro snapshot
FRED_SERIES = {
    "10Y Yield": "DGS10",
    "2Y Yield": "DGS2",
    "HY OAS (BAML)": "BAMLH0A0HYM2",
    "Fed Funds": "DFF",
    "VIX": "VIXCLS",
    "DXY": "DTWEXBGS",
}

WATCHLIST_FILE = Path(__file__).parent / "watchlist.txt"


# ---------- Sources ----------

def _sec_get(url: str) -> requests.Response:
    return requests.get(url, headers={"User-Agent": SEC_UA}, timeout=30)


def fetch_rss_today(hours: int = 30) -> list[dict]:
    """Pull recent items from RSS feeds within the last N hours."""
    cutoff = dt.datetime.utcnow() - dt.timedelta(hours=hours)
    items = []
    for source, url in FEEDS.items():
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:25]:
                pub = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
                if pub:
                    pub_dt = dt.datetime.fromtimestamp(time.mktime(pub))
                    if pub_dt < cutoff:
                        continue
                items.append({
                    "source": source,
                    "title": e.get("title", "").strip(),
                    "summary": re.sub(r"<[^>]+>", " ", e.get("summary", ""))[:500].strip(),
                    "link": e.get("link", ""),
                })
        except Exception as ex:
            print(f"[warn] {source}: {ex}", file=sys.stderr)
    return items


def fetch_fred() -> dict:
    """Latest macro values for each series + previous reading."""
    if not FRED_KEY:
        return {}
    out = {}
    for label, sid in FRED_SERIES.items():
        try:
            url = (
                f"https://api.stlouisfed.org/fred/series/observations"
                f"?series_id={sid}&api_key={FRED_KEY}&file_type=json"
                f"&sort_order=desc&limit=2"
            )
            r = requests.get(url, timeout=10)
            obs = [o for o in r.json().get("observations", []) if o["value"] != "."]
            if obs:
                latest = obs[0]
                prev = obs[1] if len(obs) > 1 else obs[0]
                out[label] = {"value": latest["value"], "date": latest["date"], "prev": prev["value"]}
        except Exception as ex:
            print(f"[warn] FRED {sid}: {ex}", file=sys.stderr)
    return out


def fetch_company_filings(ticker: str, limit: int = 6) -> list[dict]:
    """Fetch the latest filings for a ticker from SEC EDGAR (no key required)."""
    r = _sec_get("https://www.sec.gov/files/company_tickers.json")
    r.raise_for_status()
    cik = None
    for entry in r.json().values():
        if entry["ticker"].upper() == ticker.upper():
            cik = str(entry["cik_str"]).zfill(10)
            break
    if not cik:
        raise ValueError(f"Ticker {ticker} not found in SEC tickers")

    r = _sec_get(f"https://data.sec.gov/submissions/CIK{cik}.json")
    r.raise_for_status()
    recent = r.json()["filings"]["recent"]
    out = []
    for i, form in enumerate(recent["form"]):
        if len(out) >= limit:
            break
        out.append({
            "form": form,
            "date": recent["filingDate"][i],
            "accession": recent["accessionNumber"][i],
            "primaryDoc": recent["primaryDocument"][i],
            "url": (
                f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                f"{recent['accessionNumber'][i].replace('-','')}/"
                f"{recent['primaryDocument'][i]}"
            ),
        })
    return out


def fetch_filing_text(url: str, max_chars: int = 50_000) -> str:
    """Pull raw filing text, lightly cleaned. Capped to fit issue/upload limits."""
    r = _sec_get(url)
    r.raise_for_status()
    text = re.sub(r"<[^>]+>", " ", r.text)
    text = re.sub(r"&nbsp;|&amp;|&#160;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text[:max_chars]


def fetch_recent_8ks(days: int = 7, limit: int = 200) -> list[dict]:
    """Pull recent 8-Ks via EDGAR full-text search (no key)."""
    end = dt.date.today()
    start = end - dt.timedelta(days=days)
    url = (
        "https://efts.sec.gov/LATEST/search-index"
        f"?q=&dateRange=custom&startdt={start}&enddt={end}&forms=8-K&hits={limit}"
    )
    r = _sec_get(url)
    r.raise_for_status()
    hits = r.json().get("hits", {}).get("hits", [])
    out = []
    for h in hits:
        s = h.get("_source", {})
        out.append({
            "name": (s.get("display_names") or ["unknown"])[0],
            "form": (s.get("forms") or ["8-K"])[0],
            "date": s.get("file_date", ""),
            "accession": h.get("_id", ""),
            "items": s.get("items", []),
        })
    return out


def load_watchlist() -> list[str]:
    if not WATCHLIST_FILE.exists():
        return []
    return [
        line.strip().upper()
        for line in WATCHLIST_FILE.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]


# ---------- Prompts ----------

DAILY_PROMPT = """You are an experienced markets analyst writing the morning briefing for a UIUC Finance student who is recruiting for IB / private credit / hedge fund roles and pitches stocks in the Investment Management Academy. He reads Money Stuff and the FT every day. Do NOT define terms. Do NOT hand-hold. Sound like a senior analyst messaging a junior at 6 AM.

Tight, opinionated, actionable. No fluff. No "here's a roundup."

Format (use exactly):

# Market Briefing — {date}

## 1. Macro snapshot
[3 bullets max. One-line takes on yields, spreads, FX, equities. Note what changed overnight that matters. Skip the line if nothing meaningful.]

## 2. The 3 stories that matter
[The 3 most important stories from the news inputs. Each: 2-sentence summary + your one-line editorial on why it matters or what's the second-order effect. Cite source. Order by importance, not time.]

## 3. Watchlist updates
[Any filings, earnings, or news on the watchlist tickers. Skip section if nothing.]

## 4. Idea of the day
[ONE US-listed ticker (>$1B market cap) you'd flag for him today, with a 3-sentence variant view: consensus says X, but Y is being missed, here's the catalyst window. Pick something interesting from today's news flow — not a household name unless the angle is genuinely sharp.]

## 5. One-line read
[A single sentence that captures the smart-money consensus right now, in your view.]

---

INPUTS

MACRO DATA:
{macro}

NEWS (last 24h):
{news}

WATCHLIST: {watchlist}

WATCHLIST FILINGS (last 24h):
{watchlist_filings}

Generate the briefing now. Markdown only. No emoji. Under 600 words total.
"""

IDEAS_PROMPT = """You are a sharp hedge fund analyst surfacing weekly stock idea candidates for a UIUC IMA student who pitches stocks regularly. He needs DIFFERENTIATED ideas with VARIANT VIEWS — not consensus longs, not "Apple has a moat." He wants situations: spinoffs, recap candidates, broken IPOs, hidden compounders, hated names with a re-rating catalyst, balance-sheet inflections.

You'll be given a list of recent 8-K filings. Pick the THREE most interesting situations and write a sharp pitch teaser for each. Bias toward under-followed names ($300M-$5B market cap when possible). Skip mega-caps.

Format (use exactly):

# Idea Engine — Week of {date}

## {{N}}. {{TICKER}} — {{one-sentence hook}}

**Setup:** [What just happened — the 8-K event, in 2 sentences.]

**Why it's interesting:** [The variant view in 3 sentences. What does consensus think? What might consensus be missing? Why now?]

**Catalyst window:** [When does this get re-rated, and by what mechanism? Be specific — earnings, a vote, a divestiture close, a debt maturity, a refi.]

**Key risks:** [Two risks that could blow up the thesis. Be honest.]

**Next step:** [What he should do in 30 minutes to validate or kill the idea — pull a specific filing, build a specific model, check a specific data point.]

---

RECENT 8-Ks:
{filings}

Pick the 3 best. Be opinionated. No more than 250 words per idea.
"""

RESEARCH_PROMPT = """You are a senior hedge fund analyst writing a tear-out on {ticker} for a UIUC IMA student preparing a pitch. He has read the basics. He needs SHARP, NON-OBVIOUS analysis — what consensus misses, where the asymmetric setup is, what the variant view could be.

You'll be given the latest 10-K and 10-Q text. Output exactly:

# {ticker} — Tear-out

## 1. The business in 3 sentences
[What it actually does. Not the marketing language — the real economic engine.]

## 2. Unit economics
[3-5 bullets on the core economics: gross margin, customer acquisition, retention, capital intensity, cash conversion. Pull actual numbers from the filings.]

## 3. KPIs to track
[The 3-5 KPIs that explain the stock. What's trending well, what's trending poorly.]

## 4. Capital structure
[Debt, maturity wall, leverage, liquidity. One paragraph.]

## 5. Bear case (the one consensus underweights)
[The strongest bear argument, in 4-5 sentences. Be specific about the mechanism.]

## 6. Bull case (the variant view)
[Where the asymmetry is. What does the market price wrong? What would need to be true for this to work?]

## 7. What to do next
[3 concrete next steps to validate or kill the thesis: a specific data point, a specific competitor to compare to, a specific customer/channel check.]

## 8. Pitch one-liner
[The 15-word version of why this is interesting right now.]

---

10-K excerpt:
{ten_k}

10-Q excerpt:
{ten_q}

Be sharp, specific, opinionated. No filler. No "it depends." Pick a side.
"""


# ---------- Optional Gemini auto mode ----------

def call_gemini(prompt: str, max_tokens: int = 4000) -> str:
    """Use Google Gemini 2.5 Flash free tier (1500 req/day, no card required)."""
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.5-flash:generateContent?key={GEMINI_KEY}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.7},
    }
    r = requests.post(url, json=payload, timeout=120)
    r.raise_for_status()
    data = r.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


# ---------- Delivery ----------

def post_github_issue(title: str, body: str) -> str | None:
    """Create a GitHub Issue. GitHub auto-emails you the notification — that's the push.
    Issue history is your archive. Free."""
    if not (GITHUB_TOKEN and GITHUB_REPO):
        print("[skip] GITHUB_TOKEN / GITHUB_REPOSITORY not set — issue not created")
        return None
    url = f"https://api.github.com/repos/{GITHUB_REPO}/issues"
    r = requests.post(
        url,
        headers={"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"},
        json={"title": title, "body": body, "labels": ["briefing"]},
        timeout=30,
    )
    if r.status_code >= 300:
        print(f"[err] github issue: {r.status_code} {r.text[:300]}", file=sys.stderr)
        return None
    issue_url = r.json()["html_url"]
    print(f"[ok] github issue: {issue_url}")
    return issue_url


def post_telegram(text: str, link: str | None = None):
    """Optional phone push via Telegram bot. Skip if not configured."""
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT):
        return
    msg = f"{text}\n\n→ {link}" if link else text
    for chunk in [msg[i:i + 3500] for i in range(0, len(msg), 3500)]:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT, "text": chunk},
                timeout=10,
            )
            print("[ok] telegram delivered")
        except Exception as e:
            print(f"[err] telegram: {e}", file=sys.stderr)


# ---------- Build the prompt+data payload ----------

def build_daily() -> str:
    macro = fetch_fred()
    news = fetch_rss_today()
    watchlist = load_watchlist()

    watchlist_filings = []
    for t in watchlist[:25]:
        try:
            for f in fetch_company_filings(t, limit=2):
                fdate = dt.date.fromisoformat(f["date"])
                if (dt.date.today() - fdate).days <= 1:
                    watchlist_filings.append({"ticker": t, **f})
        except Exception:
            pass

    macro_str = "\n".join(
        f"- {k}: {v['value']} ({v['date']}, prev {v['prev']})" for k, v in macro.items()
    ) or "(no macro — set FRED_API_KEY)"
    news_str = "\n\n".join(
        f"[{n['source']}] {n['title']}\n{n['summary']}\n{n['link']}" for n in news[:40]
    ) or "(no news pulled)"
    wl_str = ", ".join(watchlist) or "(none)"
    wlf_str = "\n".join(
        f"- {f['ticker']} {f['form']} {f['date']}: {f['url']}" for f in watchlist_filings
    ) or "(none)"

    return DAILY_PROMPT.format(
        date=dt.date.today().isoformat(),
        macro=macro_str, news=news_str,
        watchlist=wl_str, watchlist_filings=wlf_str,
    )


def build_ideas() -> str:
    eights = fetch_recent_8ks(days=7, limit=200)
    fs = "\n".join(
        f"- {h['name']} {h['form']} {h['date']} items={h['items']} (acc: {h['accession']})"
        for h in eights[:80]
    ) or "(no 8-Ks pulled)"
    return IDEAS_PROMPT.format(date=dt.date.today().isoformat(), filings=fs)


def build_research(ticker: str) -> str:
    filings = fetch_company_filings(ticker, limit=10)
    ten_k = next((f for f in filings if f["form"] == "10-K"), None)
    ten_q = next((f for f in filings if f["form"] == "10-Q"), None)
    if not ten_k:
        raise ValueError(f"No 10-K found for {ticker}")
    ten_k_text = fetch_filing_text(ten_k["url"], max_chars=50_000)
    ten_q_text = fetch_filing_text(ten_q["url"], max_chars=30_000) if ten_q else "(no 10-Q)"
    return RESEARCH_PROMPT.format(ticker=ticker.upper(), ten_k=ten_k_text, ten_q=ten_q_text)


# ---------- Wrap prompt for paste-to-Claude UX ----------

def wrap_paste(prompt: str, kind: str) -> str:
    return f"""## How to use

1. Open [Claude.ai](https://claude.ai/new) (free tier works).
2. Triple-click inside the prompt block below, select all, copy.
3. Paste into Claude. Done.

You'll get the {kind} written by Claude itself in ~30 seconds. Free.

---

````
{prompt}
````

---

*Data above was gathered automatically. Just paste, get the briefing, move on.*
"""


# ---------- Commands ----------

def _run(prompt_builder, kind: str, title_prefix: str, telegram_emoji: str):
    """Common flow: build prompt, send via Gemini OR wrap for paste, deliver."""
    date = dt.date.today().isoformat()
    prompt = prompt_builder()

    if USE_GEMINI:
        try:
            generated = call_gemini(prompt)
            title = f"{title_prefix} — {date}"
            body = generated
        except Exception as e:
            print(f"[warn] Gemini failed ({e}), using manual mode")
            title = f"[paste-to-claude] {title_prefix} — {date}"
            body = wrap_paste(prompt, kind)
    else:
        title = f"[paste-to-claude] {title_prefix} — {date}"
        body = wrap_paste(prompt, kind)

    issue_url = post_github_issue(title, body)
    post_telegram(f"{telegram_emoji} {title_prefix} ready — {date}", issue_url)


def cmd_daily():
    _run(build_daily, "daily briefing", "Market Briefing", "📊")


def cmd_ideas():
    _run(build_ideas, "weekly stock idea digest", "Idea Engine", "💡")


def cmd_research(ticker: str):
    """On-demand. Always saves a file (which you upload to Claude.ai), and
    creates an issue with a link."""
    ticker = ticker.upper()
    date = dt.date.today().isoformat()
    prompt = build_research(ticker)
    fname = f"research_{ticker}_{date}.md"

    if USE_GEMINI:
        try:
            tearsheet = call_gemini(prompt)
            Path(fname).write_text(tearsheet)
            print(tearsheet)
            print(f"[ok] saved {fname}")
            issue_url = post_github_issue(f"Research — {ticker} — {date}", tearsheet)
            post_telegram(f"📑 Research ready — {ticker}", issue_url)
            return
        except Exception as e:
            print(f"[warn] Gemini failed ({e}), using manual mode")

    # Manual mode: save the full prompt+filings to a file you upload to Claude.ai
    Path(fname).write_text(prompt)
    print(f"[ok] saved {fname}")
    print("[next] Upload this file to Claude.ai (paperclip icon) and type 'go'.")

    body = f"""## How to use

This research request is too big for a paste — Claude.ai's file upload handles it cleanly.

1. Download `{fname}` from this run's [Actions artifacts](https://github.com/{GITHUB_REPO}/actions) (top-right of the run page) **OR** run `python brief.py research {ticker}` locally.
2. Open [Claude.ai](https://claude.ai/new), click the paperclip, upload the file.
3. Type "go" — Claude will produce the tear-out.

Built from the latest 10-K and 10-Q for {ticker}.
"""
    issue_url = post_github_issue(f"Research — {ticker} — {date}", body)
    post_telegram(f"📑 Research ready — {ticker}", issue_url)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "daily":
        cmd_daily()
    elif cmd == "ideas":
        cmd_ideas()
    elif cmd == "research":
        if len(sys.argv) < 3:
            print("usage: python brief.py research TICKER")
            sys.exit(1)
        cmd_research(sys.argv[2])
    else:
        print(f"unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
