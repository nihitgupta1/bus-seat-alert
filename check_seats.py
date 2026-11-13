#!/usr/bin/env python3
"""Watch redBus Indore -> Pune buses and send an ntfy alert when single seats run low.

Runs once per invocation (GitHub Actions calls it every 30 minutes).
Config is in the block below. The ntfy topic comes from the NTFY_TOPIC env var
(a GitHub repo secret) and must never be written into this file.
"""
import json
import os
import re
import sys
import time
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

# ------------------------------ CONFIG ------------------------------------
DATES = ["17-Nov-2026", "18-Nov-2026"]
OPERATOR = "raj ratan"          # matched case-insensitively against the bus card text
DEPART_BEFORE = (19, 0)         # only buses departing before 19:00
ALERT_IF_SINGLES_BELOW = 3      # alert when available single seats is LESS than this (3 = alert at 2 or fewer)
COUNT_LADIES_SEATS = False      # pink "ladies only" seats: count them as available?
SEARCH_URL = (
    "https://www.redbus.in/bus-tickets/indore-to-pune"
    "?fromCityName=Indore&toCityName=Pune&fromCityId=313&toCityId=130"
    "&onward={date}&ref=modifyDate"
)
# --------------------------------------------------------------------------

STATE_FILE = Path("state.json")
DEBUG_DIR = Path("debug")
DEBUG = os.environ.get("DEBUG") == "1"
COL_TOL = 12  # px tolerance when grouping seats into columns
IST = timezone(timedelta(hours=5, minutes=30))
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

# ------------------------------ page scripts ------------------------------
COUNT_FN = r"""() => [...document.querySelectorAll('*')].filter(
  e => e.children.length === 0 && /^\s*(view seats?|select seats?)\s*$/i.test(e.textContent || '')
).length > 0"""

COUNT_JS = r"""() => [...document.querySelectorAll('*')].filter(
  e => e.children.length === 0 && /^\s*(view seats?|select seats?)\s*$/i.test(e.textContent || '')
).length"""

# Tags every "View seats" button and returns the text of its bus card.
TAG_CARDS_JS = r"""() => {
  const re = /^\s*(view seats?|select seats?)\s*$/i;
  const leaves = [...document.querySelectorAll('*')].filter(
    e => e.children.length === 0 && re.test(e.textContent || ''));
  return leaves.map((leaf, i) => {
    let card = leaf;
    while (card.parentElement) {
      const p = card.parentElement;
      const n = leaves.filter(l => p.contains(l)).length;
      if (n > 1) break;
      card = p;
    }
    leaf.setAttribute('data-sc-btn', String(i));
    return {i, text: card.innerText || ''};
  });
}"""

# Reads every seat on the open seat layout: position, state text, border colour, deck.
SEATS_JS = r"""() => {
  const seatRe = /^\s*(Sold|₹\s?[\d,]+(\.\d+)?)\s*$/i;
  const all = [...document.querySelectorAll('*')];
  const cand = all.filter(e => {
    const t = (e.innerText || '').trim();
    if (!seatRe.test(t)) return false;
    const r = e.getBoundingClientRect();
    return r.height >= 40 && r.height <= 120 && r.width >= 20 && r.width <= 100;
  });
  const seats = cand.filter(e => !cand.some(o => o !== e && e.contains(o)));
  const headings = all.filter(
    e => e.children.length === 0 && /^\s*(lower|upper)\s+deck\s*$/i.test(e.textContent || ''));
  const borderColor = (el) => {
    const nodes = [el, ...el.querySelectorAll('*')].slice(0, 12);
    for (const n of nodes) {
      const cs = getComputedStyle(n);
      const w = parseFloat(cs.borderTopWidth) || 0;
      const m = (cs.borderTopColor || '').match(/rgba?\(([^)]+)\)/);
      if (w >= 1 && m) {
        const parts = m[1].split(/[ ,\/]+/).filter(Boolean).map(Number);
        if (parts.length < 4 || parts[3] > 0.3) return parts.slice(0, 3);
      }
    }
    return null;
  };
  return seats.map(e => {
    const r = e.getBoundingClientRect();
    let deck = 'main';
    let a = e;
    while (a.parentElement) {
      a = a.parentElement;
      const hs = headings.filter(h => a.contains(h));
      if (hs.length === 1) { deck = hs[0].textContent.trim().toLowerCase(); break; }
      if (hs.length > 1) break;
    }
    return {
      x: r.left + window.scrollX, y: r.top + window.scrollY,
      w: r.width, h: r.height,
      text: (e.innerText || '').trim(), color: borderColor(e), deck: deck,
    };
  });
}"""


# ------------------------------ helpers -----------------------------------
def dump(page, name, force=False):
    """Save a screenshot + HTML for debugging (always on failure, else only if DEBUG)."""
    if not (DEBUG or force):
        return
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
        page.screenshot(path=str(DEBUG_DIR / f"{safe}.png"), full_page=False)
        (DEBUG_DIR / f"{safe}.html").write_text(page.content(), encoding="utf-8")
    except Exception as e:  # never let debugging break the run
        print(f"  (could not save debug files: {e})")


def notify(title, message, click=None, priority="high"):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        print("NTFY_TOPIC is not set. Would have sent:", title, "-", message)
        return False
    headers = {"Title": title, "Priority": priority, "Tags": "bus"}
    if click:
        headers["Click"] = click
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}", data=message.encode("utf-8"), headers=headers, method="POST"
    )
    try:
        urllib.request.urlopen(req, timeout=20).read()
        print("Notification sent.")
        return True
    except Exception as e:
        print(f"ntfy send failed: {e}")
        return False


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def evaluate_card(text):
    """Decide whether a bus card is one we want. Returns a dict."""
    t = text.replace("\u00a0", " ")
    low = t.lower()
    m = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", t)
    dep = f"{int(m.group(1)):02d}:{m.group(2)}" if m else None
    tm = re.search(r"(volvo[^\n]*|bharat[^\n]*)", t, re.I) or re.search(
        r"([^\n]*(?:sleeper|seater)[^\n]*)", t, re.I
    )
    btype = tm.group(1).strip()[:60] if tm else "unknown type"
    res = {"ok": False, "why": "", "dep": dep, "type": btype}
    if OPERATOR not in low:
        res["why"] = "other operator"
        return res
    if dep is None:
        res["why"] = "no departure time found"
        return res
    if (int(dep[:2]), int(dep[3:])) >= DEPART_BEFORE:
        res["why"] = f"departs {dep}, not before {DEPART_BEFORE[0]:02d}:{DEPART_BEFORE[1]:02d}"
        return res
    non_ac = re.search(r"non[\s\-]*a/?c", low)
    bharat_ac_sleeper = "bharat" in low and "benz" in low and "sleeper" in low and not non_ac
    if not ("volvo" in low or bharat_ac_sleeper):
        res["why"] = f"bus type not matched ({btype})"
        return res
    res["ok"] = True
    return res


def load_all(page):
    """Scroll until the list of buses stops growing."""
    last = -1
    page.mouse.move(600, 500)
    for _ in range(25):
        n = page.evaluate(COUNT_JS)
        if n == last:
            break
        last = n
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.mouse.wheel(0, 5000)
        page.wait_for_timeout(1500)


def open_results(page, url):
    last_err = None
    for attempt in range(2):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            break
        except Exception as e:
            last_err = e
            print(f"    page load attempt {attempt + 1} failed: {str(e).splitlines()[0]}")
            page.wait_for_timeout(3000)
    else:
        raise last_err
    page.wait_for_function(COUNT_FN, timeout=45000)
    load_all(page)


def classify(seat):
    if "sold" in seat["text"].lower():
        return "sold"
    col = seat.get("color")
    if not col:
        return "other"
    r, g, b = col
    if g > r + 25 and g > b + 25:
        return "green"
    if r > g + 60 and b > g + 20:
        return "pink"
    if b > r + 40 and b > g + 20:
        return "blue"
    return "other"


def available_states():
    states = {"green", "other"}
    if COUNT_LADIES_SEATS:
        states.add("pink")
    return states


def single_column(deck_seats):
    """x position of the 1-seat column in a 2+1 deck, else None."""
    centers = []
    cols = []
    for x in sorted(s["x"] for s in deck_seats):
        if cols and abs(x - sum(cols[-1]) / len(cols[-1])) < COL_TOL:
            cols[-1].append(x)
        else:
            cols.append([x])
    centers = [sum(c) / len(c) for c in cols]
    if len(centers) < 3:
        return None
    gaps = [centers[i + 1] - centers[i] for i in range(len(centers) - 1)]
    k = gaps.index(max(gaps))
    left, right = centers[: k + 1], centers[k + 1 :]
    if len(left) == 1 and len(right) == 2:
        return left[0]
    if len(right) == 1 and len(left) == 2:
        return right[0]
    return None


def analyse(seats):
    """Count available single seats across decks. None if no 2+1 layout was found."""
    decks = {}
    for s in seats:
        s["state"] = classify(s)
        decks.setdefault(s["deck"], []).append(s)
    total, found = 0, False
    for deck, ds in sorted(decks.items()):
        sx = single_column(ds)
        if sx is None:
            print(f"    {deck}: {len(ds)} seats, no 2+1 layout detected, so no single side")
            continue
        found = True
        singles = [s for s in ds if abs(s["x"] - sx) < COL_TOL]
        sc = Counter(s["state"] for s in singles)
        avail = sum(sc[k] for k in available_states())
        total += avail
        print(
            f"    {deck}: {len(singles)} single berths, available={avail} "
            f"(states: {dict(sc)}); all seats on deck: {dict(Counter(s['state'] for s in ds))}"
        )
    return total if found else None


def read_singles(ctx, url, date, target):
    page = ctx.new_page()
    try:
        open_results(page, url)
        cards = page.evaluate(TAG_CARDS_JS)
        match = None
        for c in cards:
            ev = evaluate_card(c["text"])
            if ev["ok"] and ev["dep"] == target["dep"] and ev["type"] == target["type"]:
                match = c
                break
        if match is None:
            print("    bus no longer listed on reload")
            return None
        btn = page.locator(f'[data-sc-btn="{match["i"]}"]').first
        btn.scroll_into_view_if_needed(timeout=10000)
        btn.click(timeout=10000)
        try:
            page.wait_for_function(f"({SEATS_JS})().length > 0", timeout=20000)
        except PWTimeout:
            print("    seat layout did not appear in time")
        page.wait_for_timeout(1500)
        seats = page.evaluate(SEATS_JS)
        print(f"    read {len(seats)} seats")
        dump(page, f"seats-{date}-{target['dep']}")
        if not seats:
            dump(page, f"noseats-{date}-{target['dep']}", force=True)
            return None
        result = analyse(seats)
        if result is None:
            dump(page, f"nolayout-{date}-{target['dep']}", force=True)
        return result
    except Exception as e:
        print(f"    error reading seats: {e}")
        dump(page, f"error-{date}-{target['dep']}", force=True)
        return None
    finally:
        page.close()


# ------------------------------ main --------------------------------------
def main():
    if "--test-notify" in sys.argv:
        ok = notify("Test alert", "ntfy is working. Seat alerts will look like this.")
        sys.exit(0 if ok else 1)

    today = datetime.now(IST).date()
    dates = [d for d in DATES if datetime.strptime(d, "%d-%b-%Y").date() >= today]
    if not dates:
        print("All travel dates are in the past. Disable the workflow.")
        return

    state = load_state()
    failures = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled", "--disable-http2"],
        )
        ctx = browser.new_context(
            viewport={"width": 1366, "height": 900},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            user_agent=UA,
        )
        for date in dates:
            url = SEARCH_URL.format(date=date)
            page = ctx.new_page()
            try:
                open_results(page, url)
            except Exception as e:
                print(f"[{date}] could not load results: {e}")
                dump(page, f"load-fail-{date}", force=True)
                failures += 1
                page.close()
                continue
            cards = page.evaluate(TAG_CARDS_JS)
            dump(page, f"results-{date}")
            page.close()
            print(f"[{date}] {len(cards)} buses listed")

            targets = []
            for c in cards:
                ev = evaluate_card(c["text"])
                if OPERATOR in c["text"].lower():
                    verdict = "WATCH" if ev["ok"] else f"skip ({ev['why']})"
                    print(f"  {verdict}: dep={ev['dep']} type={ev['type']!r}")
                if ev["ok"] and not any(
                    t["dep"] == ev["dep"] and t["type"] == ev["type"] for t in targets
                ):
                    targets.append(ev)
            if not targets:
                print(f"[{date}] no matching buses right now")

            for target in targets:
                key = f"{date}|{target['dep']}|{target['type']}"
                print(f"  checking {key}")
                singles = read_singles(ctx, url, date, target)
                time.sleep(3)
                if singles is None:
                    print("    could not determine single seats; no alert decision made")
                    failures += 1
                    continue
                prev = state.get(key, {})
                low = singles < ALERT_IF_SINGLES_BELOW
                print(f"    available single seats: {singles} (alert threshold: below {ALERT_IF_SINGLES_BELOW})")
                if low and not prev.get("low"):
                    notify(
                        "Single seats running low",
                        f"{date} {target['dep']} {target['type']}: "
                        f"{singles} single seat{'s' if singles != 1 else ''} left.",
                        click=url,
                    )
                state[key] = {
                    "low": low,
                    "singles": singles,
                    "checked": datetime.now(IST).isoformat(timespec="minutes"),
                }
        browser.close()

    save_state(state)
    if failures and failures >= len(dates):
        sys.exit(1)


if __name__ == "__main__":
    main()
