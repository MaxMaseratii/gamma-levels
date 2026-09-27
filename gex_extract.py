#!/usr/bin/env python3
"""
MMM Gamma Levels Extractor  (@MaxMaserati)
------------------------------------------
Every run it:
  1. Downloads the free, 15-min delayed SPX and NDX option chains from CBOE.
  2. Calculates gamma exposure (GEX) per strike itself (naive open-interest model).
  3. Finds the 90-day Call Wall, Put Wall, Zero Gamma, and today's 0DTE levels.
  4. Saves a ready-to-paste block for the TradingView indicator:
        ~/GammaLevels/latest_NQ.txt   (paste into the NQ / MNQ chart, overwritten every run)
        ~/GammaLevels/latest_ES.txt   (paste into the ES / MES chart, overwritten every run)
        ~/GammaLevels/latest_BOTH.txt + latest_NQ/ES_90D_strike_detail.csv
        ~/GammaLevels/gex_history.csv (one row per index per run - the only file that grows)

Only Python 3 standard library is used. Nothing to install.
Run by hand:   python3 ~/GammaLevels/gex_extract.py
"""

import csv
import html
import datetime as dt
import json
import math
import os
import sys
import urllib.request
from zoneinfo import ZoneInfo

# ---------------- SETTINGS ----------------
OUT_DIR        = os.environ.get("GAMMA_OUT_DIR") or os.path.expanduser("~/GammaLevels")   # GitHub uses GAMMA_OUT_DIR=docs
INDEXES        = [("SPX", "ES / MES"), ("NDX", "NQ / MNQ")]
STRUCT_DAYS    = 90      # "90-day" structural window (same as GEX-Metrix Custom Range today -> +3 months)
TOP_STRIKES    = 16      # how many 0DTE strikes go in the paste block
NEAR_PCT       = 0.03    # 0DTE strikes kept within +/- 3% of spot
WALL_PCT       = 0.05    # walls searched within +/- 5% of spot (ceiling above, floor below)
SWEEP_PCT      = 0.15    # zero-gamma search range: +/- 15% of spot
SWEEP_STEPS    = 240     # zero-gamma search resolution
RATE           = 0.04    # risk-free rate used in the Black-Scholes gamma
NY             = ZoneInfo("America/New_York")
URL            = "https://cdn.cboe.com/api/global/delayed_quotes/options/_{sym}.json"
# ------------------------------------------


def fetch_chain(sym):
    req = urllib.request.Request(
        URL.format(sym=sym),
        headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
                 "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def parse_symbol(s):
    """SPXW261002C07800000 -> (date 2026-10-02, 'C', 7800.0). Root length varies, so read from the end."""
    s = s.strip()
    yymmdd, cp, strike = s[-15:-9], s[-9], s[-8:]
    exp = dt.date(2000 + int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6]))
    return exp, cp, int(strike) / 1000.0


def bs_gamma(S, K, T, iv):
    if S <= 0 or K <= 0 or T <= 0 or iv <= 0:
        return 0.0
    vt = iv * math.sqrt(T)
    d1 = (math.log(S / K) + (RATE + 0.5 * iv * iv) * T) / vt
    return math.exp(-0.5 * d1 * d1) / (math.sqrt(2 * math.pi) * S * vt)


def load_options(raw, now):
    data = raw.get("data", raw)
    spot = float(data.get("current_price") or data.get("close") or 0)
    rows = []
    for o in data.get("options", []):
        try:
            exp, cp, k = parse_symbol(o["option"])
        except Exception:
            continue
        oi = float(o.get("open_interest") or 0)
        if oi <= 0:
            continue
        expiry_dt = dt.datetime(exp.year, exp.month, exp.day, 16, 0, tzinfo=NY)
        T = max((expiry_dt - now).total_seconds() / (365.0 * 86400), 1.0 / (365 * 24))
        iv = float(o.get("iv") or 0)
        g = float(o.get("gamma") or 0)
        src = "cboe"
        if g <= 0:
            g = bs_gamma(spot, k, T, iv)
            src = "calc" if g > 0 else "none"
        rows.append({"exp": exp, "cp": cp, "k": k, "oi": oi, "iv": iv, "T": T, "g": g, "src": src})
    return spot, rows


def gex_by_strike(rows, spot):
    """Dollar gamma per 1% move, in $ millions. Calls +, puts - (dealer long calls / short puts)."""
    out = {}
    for r in rows:
        v = r["g"] * r["oi"] * 100 * spot * spot * 0.01
        if r["cp"] == "P":
            v = -v
        out[r["k"]] = out.get(r["k"], 0.0) + v
    return {k: v / 1e6 for k, v in out.items()}


def zero_gamma(rows, spot):
    """Moves spot up/down, re-prices every option's gamma, finds where total GEX flips sign (nearest to spot)."""
    usable = [r for r in rows if r["iv"] > 0]
    if not usable:
        return None
    lo, hi = spot * (1 - SWEEP_PCT), spot * (1 + SWEEP_PCT)
    levels = [lo + (hi - lo) * i / SWEEP_STEPS for i in range(SWEEP_STEPS + 1)]
    totals = []
    for S in levels:
        t = 0.0
        for r in usable:
            v = bs_gamma(S, r["k"], r["T"], r["iv"]) * r["oi"] * 100 * S * S * 0.01
            t += -v if r["cp"] == "P" else v
        totals.append(t)
    best = None
    for i in range(1, len(levels)):
        a, b = totals[i - 1], totals[i]
        if (a < 0 <= b) or (a > 0 >= b):
            x = levels[i - 1] + (levels[i] - levels[i - 1]) * (-a / (b - a) if b != a else 0)
            if best is None or abs(x - spot) < abs(best - spot):
                best = x
    return best


def walls(gex, spot):
    """Call Wall = biggest positive strike AT/ABOVE price (the ceiling).
       Put Wall  = biggest negative strike AT/BELOW price (the floor).
       Both searched within +/- WALL_PCT of price so far-away strikes don't win."""
    lo, hi, tol = spot * (1 - WALL_PCT), spot * (1 + WALL_PCT), spot * 0.002
    pos = {k: v for k, v in gex.items() if v > 0 and spot - tol <= k <= hi}
    neg = {k: v for k, v in gex.items() if v < 0 and lo <= k <= spot + tol}
    cw = max(pos, key=pos.get) if pos else None
    pw = min(neg, key=neg.get) if neg else None
    return cw, pw


def biggest(gex, spot):
    """Biggest bars anywhere within +/- 10% (info only)."""
    near = {k: v for k, v in gex.items() if abs(k - spot) <= spot * 0.10}
    pos = {k: v for k, v in near.items() if v > 0}
    neg = {k: v for k, v in near.items() if v < 0}
    return (max(pos, key=pos.get) if pos else None), (min(neg, key=neg.get) if neg else None)


def write_profile(sym, rows, spot, today, path):
    """Strike-by-strike 90-day detail, to compare with the GEX-Metrix bars."""
    agg = {}
    for r in rows:
        if not (0 <= (r["exp"] - today).days <= STRUCT_DAYS) or abs(r["k"] - spot) > spot * 0.10:
            continue
        a = agg.setdefault(r["k"], {"call": 0.0, "put": 0.0, "call_oi": 0.0, "put_oi": 0.0, "no_gamma_oi": 0.0})
        v = r["g"] * r["oi"] * 100 * spot * spot * 0.01 / 1e6
        if r["cp"] == "C":
            a["call"] += v
            a["call_oi"] += r["oi"]
        else:
            a["put"] -= v
            a["put_oi"] += r["oi"]
        if r["src"] == "none":
            a["no_gamma_oi"] += r["oi"]
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Strike", "Net Gamma ($M)", "Call Gamma ($M)", "Put Gamma ($M)",
                    "Call Open Interest", "Put Open Interest", "Open Interest With No Gamma Data"])
        for k in sorted(agg, reverse=True):
            a = agg[k]
            w.writerow([fmt(k), f"{a['call'] + a['put']:.1f}", f"{a['call']:.1f}", f"{a['put']:.1f}",
                        int(a["call_oi"]), int(a["put_oi"]), int(a["no_gamma_oi"])])


def fmt(x):
    return "0" if x is None else (f"{x:.2f}".rstrip("0").rstrip("."))


def analyse(sym, now):
    spot, rows = load_options(fetch_chain(sym), now)
    today = now.date()
    if spot <= 0 or not rows:
        raise RuntimeError(f"{sym}: no usable data")

    # 90-day structural set
    s90 = [r for r in rows if 0 <= (r["exp"] - today).days <= STRUCT_DAYS]
    g90 = gex_by_strike(s90, spot)
    cw90, pw90 = walls(g90, spot)
    big_c, big_p = biggest(g90, spot)
    zg90 = zero_gamma(s90, spot)

    # 0DTE set = nearest expiry that is today or later (weekend -> next trading day)
    future = sorted({r["exp"] for r in rows if r["exp"] >= today})
    exp0 = future[0] if future else today
    s0 = [r for r in rows if r["exp"] == exp0]
    g0 = gex_by_strike(s0, spot)
    near = {k: v for k, v in g0.items() if abs(k - spot) <= spot * NEAR_PCT and abs(v) > 0}
    top = sorted(near.items(), key=lambda kv: abs(kv[1]), reverse=True)[:TOP_STRIKES]
    top.sort(key=lambda kv: kv[0], reverse=True)
    cw0, pw0 = walls(near, spot) if near else (None, None)
    flip0 = zero_gamma(s0, spot)

    return {"sym": sym, "spot": spot, "cw90": cw90, "pw90": pw90, "zg90": zg90,
            "exp0": exp0, "cw0": cw0, "pw0": pw0, "flip0": flip0, "top0": top,
            "n90": len(s90), "n0": len(s0), "big_c": big_c, "big_p": big_p,
            "_rows": rows, "_today": today}


def block(res, fut, stamp, day_label):
    """Names and order match the indicator settings exactly."""
    line = "=" * 52
    out = [line,
           f"MMM GAMMA LEVELS - {fut} CHART",
           line,
           day_label,
           f"Index = {res['sym']}",
           f"Updated {stamp} (CBOE delayed data)",
           f"Spot Price = {fmt(res['spot'])}",
           "",
           "1. 90-Day Walls (Big Picture)",
           f"90D Call Wall = {fmt(res['cw90'])}",
           f"90D Put Wall = {fmt(res['pw90'])}",
           f"90D Zero Gamma = {fmt(res['zg90'])}",
           f"Biggest 90D Call Bar Within 10 Percent = {fmt(res['big_c'])}",
           f"Biggest 90D Put Bar Within 10 Percent = {fmt(res['big_p'])}",
           "",
           f"2. Today's Levels (0DTE, expiry {res['exp0']})",
           f"Today Call Wall = {fmt(res['cw0'])}",
           f"Today Put Wall = {fmt(res['pw0'])}",
           f"Today GEX Flip = {fmt(res['flip0'])}",
           "Today Strikes (Strike : Gamma In Millions Of Dollars)"]
    out += [f"{fmt(k)} : {v:.1f}" for k, v in res["top0"]]
    return "\n".join(out) + "\n"


PAGE_CSS = """
:root{--bg:#f7f7f5;--card:#ffffff;--ink:#1c1c1a;--quiet:#6b6b66;--line:#e2e1dc;--accent:#0f766e;--call:#15803d;--put:#b91c1c;--zero:#a16207}
@media (prefers-color-scheme: dark){:root{--bg:#141413;--card:#1e1e1c;--ink:#ecebe6;--quiet:#9c9b95;--line:#33322f;--accent:#2dd4bf;--call:#4ade80;--put:#f87171;--zero:#facc15}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{max-width:980px;margin:0 auto;padding:28px 16px 48px}h1{font-size:24px;margin:0 0 4px}.sub{color:var(--quiet);margin:0 0 24px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px}
.card h2{font-size:18px;margin:0 0 2px}.card .meta{color:var(--quiet);font-size:13px;margin:0 0 14px}
table{width:100%;border-collapse:collapse;margin:0 0 14px;font-variant-numeric:tabular-nums}
td{padding:6px 0;border-bottom:1px solid var(--line)}td:last-child{text-align:right;font-weight:600}
.call{color:var(--call)}.put{color:var(--put)}.zero{color:var(--zero)}
button{width:100%;padding:10px;border:0;border-radius:8px;background:var(--accent);color:#fff;font-weight:600;font-size:15px;cursor:pointer}
@media (prefers-color-scheme: dark){button{color:#0b0b0a}}
details{margin-top:12px}summary{cursor:pointer;color:var(--quiet);font-size:13px}
pre{white-space:pre-wrap;word-break:break-word;font-size:12px;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px;max-height:320px;overflow:auto}
.steps{margin-top:28px;color:var(--quiet);font-size:14px}.steps li{margin:4px 0}.err{color:var(--put)}
footer{margin-top:28px;color:var(--quiet);font-size:12px}.warn{border:1px solid var(--put);border-radius:8px;padding:10px 12px;margin:0 0 20px;font-size:13px}.terms{margin-top:28px;font-size:13px;color:var(--quiet)}.terms h3{font-size:15px;color:var(--ink);margin:14px 0 4px}.terms p{margin:0 0 8px}a{color:var(--accent)}
"""


def write_page(results, texts, errors, stamp, day_label):
    """index.html: one card per chart with the key levels and a Copy button for the indicator."""
    cards = []
    for res, fut in results:
        tag = "ES" if res["sym"] == "SPX" else "NQ"
        rows = [("90D Call Wall", res["cw90"], "call"), ("90D Put Wall", res["pw90"], "put"),
                ("90D Zero Gamma", res["zg90"], "zero"), ("Today Call Wall", res["cw0"], "call"),
                ("Today Put Wall", res["pw0"], "put"), ("Today GEX Flip", res["flip0"], "zero")]
        trs = "".join(f'<tr><td>{n}</td><td class="{c}">{fmt(v)}</td></tr>' for n, v, c in rows)
        body = html.escape(texts[tag])
        cards.append(f"""<section class="card"><h2>{tag} / M{tag} chart</h2>
<p class="meta">Index {res['sym']} &middot; spot {fmt(res['spot'])} &middot; index prices, the indicator converts to futures</p>
<table>{trs}</table>
<button onclick="copyBlock('{tag}', this)">Copy {tag} block for the indicator</button>
<details><summary>Show the block</summary><pre id="{tag}">{body}</pre></details>
<p class="meta" style="margin-top:10px"><a href="latest_{tag}.txt">latest_{tag}.txt</a></p></section>""")
    errs = "".join(f'<p class="err">{html.escape(e)}</p>' for e in errors)
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Gamma Levels</title>
<style>{PAGE_CSS}</style></head><body><main>
<h1>Gamma Levels for ES and NQ</h1>
<p class="sub">{html.escape(day_label)} &middot; updated {html.escape(stamp)} &middot; CBOE delayed data, naive open-interest model</p>
<p class="warn"><b>Not financial advice.</b> Educational information only. Futures trading carries a high risk of loss. You alone are responsible for your trades. By using this page you accept the Terms below.</p>
{errs}<div class="grid">{''.join(cards)}</div>
<ol class="steps"><li>Click <b>Copy</b> for your chart (NQ block for NQ / MNQ, ES block for ES / MES).</li>
<li>In TradingView open <b>Gamma Walls and Institutional Bias @MaxMaserati</b> settings.</li>
<li>Paste into <b>Paste Daily Block</b>, leave the three 90D boxes at 0, click OK.</li></ol>
<details class="terms"><summary><b>Disclaimer and Terms of Use</b> (please read)</summary>
<h3>1. Education only, not advice</h3><p>Everything on this page, in the files and in the related TradingView indicator is for educational and informational purposes only. It is not financial, investment, trading, tax or legal advice, and it is not a recommendation or solicitation to buy or sell any security, future or option. The publisher is not a registered investment adviser, broker or financial adviser.</p>
<h3>2. Risk warning</h3><p>Trading futures and options involves substantial risk of loss and is not suitable for everyone. You can lose more than your initial deposit. Only trade money you can afford to lose. Past performance, including past signals or levels, does not guarantee future results. Hypothetical or simulated results have inherent limits: they are prepared with hindsight and do not reflect real trading, slippage, fees or liquidity.</p>
<h3>3. No warranty on the data</h3><p>Levels are calculated automatically from delayed third-party data with a simplified model. They may be late, incomplete, inaccurate or unavailable at any time, and may differ from other providers. Everything is provided "as is" and "as available", without warranty of any kind, express or implied, including accuracy, completeness, fitness for a particular purpose or uninterrupted availability.</p>
<h3>4. Your responsibility</h3><p>You are solely responsible for your own trading decisions, risk management, position sizing and results, and for complying with the rules of your broker, prop firm and local laws. Always verify levels yourself before acting on them.</p>
<h3>5. Limitation of liability</h3><p>To the fullest extent permitted by law, the publisher, Max Maserati Trading University and their affiliates are not liable for any direct, indirect, incidental, consequential or special loss or damage, including trading losses, lost profits or lost data, arising from the use of, or inability to use, this page, its files or the related indicator, even if advised of the possibility of such loss. Nothing in these terms excludes liability that cannot be excluded by law.</p>
<h3>6. No affiliation</h3><p>This page is independent and is not affiliated with, endorsed or sponsored by Cboe, CME Group, the CFTC, TradingView, GitHub or any exchange or data provider. All trademarks belong to their owners.</p>
<h3>7. Changes and access</h3><p>The page, the files and these terms may change, pause or stop at any time without notice.</p>
<h3>8. Acceptance and law</h3><p>By accessing or using this page, its files or the related indicator you agree to these terms. If you do not agree, do not use them. These terms are governed by the laws of England and Wales.</p>
</details>
<footer>Educational levels only, not financial advice. Levels are modelled from open interest and can differ from other providers.
History: <a href="gex_history.csv">gex_history.csv</a></footer>
</main><script>
function copyBlock(id, btn) {{
  const t = document.getElementById(id).textContent;
  navigator.clipboard.writeText(t).then(() => {{ const o = btn.textContent; btn.textContent = 'Copied'; setTimeout(() => btn.textContent = o, 1500); }});
}}
</script></body></html>"""
    with open(os.path.join(OUT_DIR, "index.html"), "w") as f:
        f.write(page)


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    now = dt.datetime.now(NY)
    stamp = now.strftime("%Y-%m-%d %H:%M ET")

    results, errors = [], []
    for sym, fut in INDEXES:
        try:
            results.append((analyse(sym, now), fut))
        except Exception as e:
            errors.append(f"{sym} FAILED: {e}")

    # The trading day these levels are FOR = the nearest option expiry (weekend run -> Monday)
    trade_date = results[0][0]["exp0"] if results else now.date()
    day_label = f"Trading Date {trade_date:%Y-%m-%d (%A)}"

    blocks, texts = [], {}
    for res, fut in results:
        tag = "ES" if res["sym"] == "SPX" else "NQ"
        txt = block(res, fut, stamp, day_label)
        blocks.append(txt)
        texts[tag] = txt
        # Overwritten every run: open it, select all, copy, paste into the indicator
        with open(os.path.join(OUT_DIR, f"latest_{tag}.txt"), "w") as f:
            f.write(txt)
        write_profile(res["sym"], res["_rows"], res["spot"], res["_today"],
                      os.path.join(OUT_DIR, f"latest_{tag}_90D_strike_detail.csv"))

    text = "\n".join(blocks + errors) + "\n"
    with open(os.path.join(OUT_DIR, "latest_BOTH.txt"), "w") as f:
        f.write(text)

    # Clean up files from older versions of this script (the history file is kept)
    for old in os.listdir(OUT_DIR):
        if old == "latest.txt" or old.startswith("levels_") or old.startswith("profile_90D_"):
            os.remove(os.path.join(OUT_DIR, old))

    # The only file that grows: one row per index per run, for future review / backtests
    hist = os.path.join(OUT_DIR, "gex_history.csv")
    new = not os.path.exists(hist)
    with open(hist, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["Trading Date", "Updated (ET)", "Index", "Spot Price",
                        "90D Call Wall", "90D Put Wall", "90D Zero Gamma",
                        "Today Call Wall", "Today Put Wall", "Today GEX Flip"])
        for res, fut in results:
            w.writerow([f"{trade_date:%Y-%m-%d}", stamp, res["sym"], fmt(res["spot"]),
                        fmt(res["cw90"]), fmt(res["pw90"]), fmt(res["zg90"]),
                        fmt(res["cw0"]), fmt(res["pw0"]), fmt(res["flip0"])])

    write_page(results, texts, errors, stamp, day_label)

    print(text)
    print(f"Saved in: {OUT_DIR}")
    return 1 if errors and not results else 0


if __name__ == "__main__":
    sys.exit(main())
