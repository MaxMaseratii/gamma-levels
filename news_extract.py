#!/usr/bin/env python3
"""
MMM News Calendar Extractor  (@MaxMaserati)
-------------------------------------------
Every run it:
  1. Downloads this week's economic calendar (ForexFactory weekly feed, all currencies).
  2. Converts every event to New York time and to the MMM indicator line format:
        YYYY-MM-DD HH:MM CUR I Title        I = H (red) · M (orange) · L (yellow) · N (grey / holiday)
  3. Merges it into docs/news/news_history.csv (it only grows, so past weeks stay for backtests).
  4. Writes docs/news/news.json (read by docs/news/index.html) and ready-to-paste text files:
        latest_USD_high.txt      this week, USD, red only
        latest_USD_all.txt       this week, USD, every impact
        history_USD_high.txt     everything saved, USD, red only

Only the Python 3 standard library is used. Nothing to install.
Run by hand:   python3 news_extract.py
"""

import csv
import datetime as dt
import json
import os
import sys
import urllib.request
from zoneinfo import ZoneInfo

OUT_DIR = os.path.join(os.environ.get("GAMMA_OUT_DIR") or "docs", "news")
FEED    = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
NY      = ZoneInfo("America/New_York")
IMPACT  = {"High": "H", "Medium": "M", "Low": "L", "Holiday": "N", "Non-Economic": "N"}
FIELDS  = ["date", "time", "cur", "imp", "title", "forecast", "previous"]


def fetch_week():
    req = urllib.request.Request(FEED, headers={
        "User-Agent": "Mozilla/5.0 (MMM News Calendar; +https://github.com/MaxMaseratii/gamma-levels)",
        "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def clean(s):
    # one line, single spaces: the indicator splits on spaces and new lines
    return " ".join(str(s or "").replace(";", ",").split())


def to_row(ev):
    t = dt.datetime.fromisoformat(ev["date"]).astimezone(NY)
    return {
        "date": t.strftime("%Y-%m-%d"),
        "time": t.strftime("%H:%M"),
        "cur": clean(ev.get("country")).upper() or "ALL",
        "imp": IMPACT.get(ev.get("impact", ""), "N"),
        "title": clean(ev.get("title")) or "News",
        "forecast": clean(ev.get("forecast")),
        "previous": clean(ev.get("previous")),
    }


def key(r):
    return (r["date"], r["time"], r["cur"], r["title"])


def load_history(path):
    rows = {}
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows[key(r)] = {k: r.get(k, "") for k in FIELDS}
    return rows


def lines(rows, cur=None, imps=None):
    out = []
    for r in rows:
        if cur and r["cur"] != cur:
            continue
        if imps and r["imp"] not in imps:
            continue
        out.append(f'{r["date"]} {r["time"]} {r["cur"]} {r["imp"]} {r["title"]}')
    return "\n".join(out) + ("\n" if out else "")


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    hist_path = os.path.join(OUT_DIR, "news_history.csv")
    rows = load_history(hist_path)

    week = [to_row(ev) for ev in fetch_week()]
    if not week:
        print("Feed returned no events - nothing changed.")
        return 1
    # this week's feed is the truth for this week: a moved or cancelled event replaces the old one
    week_dates = {r["date"] for r in week}
    rows = {k: v for k, v in rows.items() if v["date"] not in week_dates}
    for r in week:
        rows[key(r)] = r

    allrows = sorted(rows.values(), key=lambda r: (r["date"], r["time"], r["cur"], r["title"]))
    with open(hist_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(allrows)

    now = dt.datetime.now(NY)
    week_sorted = sorted(week, key=lambda r: (r["date"], r["time"], r["cur"], r["title"]))
    with open(os.path.join(OUT_DIR, "news.json"), "w", encoding="utf-8") as f:
        json.dump({
            "updated": now.strftime("%Y-%m-%d %H:%M ET"),
            "week_from": week_sorted[0]["date"],
            "week_to": week_sorted[-1]["date"],
            "events": allrows,
        }, f, ensure_ascii=False, separators=(",", ":"))

    with open(os.path.join(OUT_DIR, "latest_USD_high.txt"), "w", encoding="utf-8") as f:
        f.write(lines(week_sorted, "USD", {"H"}))
    with open(os.path.join(OUT_DIR, "latest_USD_all.txt"), "w", encoding="utf-8") as f:
        f.write(lines(week_sorted, "USD"))
    with open(os.path.join(OUT_DIR, "history_USD_high.txt"), "w", encoding="utf-8") as f:
        f.write(lines(allrows, "USD", {"H"}))

    print(f"{len(week)} events this week ({week_sorted[0]['date']} -> {week_sorted[-1]['date']}), "
          f"{len(allrows)} saved in total.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
