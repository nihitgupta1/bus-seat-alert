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
MIN_SINGLES_TO_ALERT = 1        # no alert when 0 singles are left (nothing to book); 0 = tell me about sell-outs too
TRAVELLER = "male"              # "male", "female" or "any": which gender-restricted seats count as bookable
FAIL_ALERT_AFTER = 4            # warn on phone after this many failed runs in a row (~2 hours)
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
    last, stable = -1, 0
    page.mouse.move(600, 500)
    for _ in range(40):
        n = page.evaluate(COUNT_JS)
        if DEBUG:
            print(f"    scrolling: {n} buses in the page so far")
        if n == last:
            stable += 1
            if stable >= 3:  # list hasn't grown for 3 checks in a row
                break
        else:
            last, stable = n, 0
        # bring the last bus on screen (triggers lazy loading), then scroll further
        page.evaluate(
            r"""() => {
              const b = [...document.querySelectorAll('*')].filter(e => e.children.length === 0
                && /^\s*(view seats?|select seats?)\s*$/i.test(e.textContent || ''));
              if (b.length) b[b.length - 1].scrollIntoView({block: 'end'});
              window.scrollTo(0, document.body.scrollHeight);
            }"""
        )
        page.mouse.wheel(0, 5000)
        page.wait_for_timeout(2000)


def make_context(browser):
    """A brand-new browser profile (no cookies or storage carried over from earlier pages)."""
    return browser.new_context(
        viewport={"width": 1366, "height": 900},
        locale="en-IN",
        timezone_id="Asia/Kolkata",
        user_agent=UA,
    )


def open_results(browser, url, date):
    """Open the results page for `date` in a fresh profile; retry up to 3 times.

    Returns (context, page); the caller must close the context. Raises if the page
    never loads or keeps showing a different date than requested.
    """
    last_err = None
    for attempt in range(3):
        ctx = make_context(browser)
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=40000)
            page.wait_for_function(COUNT_FN, timeout=30000)
            shown = re.search(r"onward=([^&]+)", page.url)
            if shown and shown.group(1).lower() != date.lower():
                raise RuntimeError(
                    f"redBus is showing date {shown.group(1)} instead of {date}"
                )
            if not shown:
                print("    warning: could not read the date from the page URL")
            load_all(page)
            return ctx, page
        except Exception as e:
            last_err = e
            print(f"    page load attempt {attempt + 1}/3 failed: {str(e).splitlines()[0]}")
            if attempt == 2:
                describe_page(page)
                dump(page, f"load-fail-{date}", force=True)
            ctx.close()
            if attempt < 2:
                time.sleep(10)
    raise last_err


def describe_page(page):
    """Print what the browser is actually looking at (for diagnosing load failures)."""
    try:
        print(f"    final URL: {page.url}")
        print(f"    title: {page.title()!r}")
        body = page.evaluate(
            r"() => (document.body.innerText || '').replace(/\s+/g, ' ').slice(0, 600)"
        )
        print(f"    page text starts: {body!r}")
        btns = page.evaluate(
            r"""() => [...new Set([...document.querySelectorAll(
                'button, a[role=button], [role=button]')]
                .map(e => (e.innerText || '').trim())
                .filter(t => t && t.length < 30))].slice(0, 25)"""
        )
        print(f"    button texts: {btns}")
    except Exception as e:
        print(f"    (could not describe page: {e})")


def report_listing(page, found):
    """Debug: compare the bus count the page announces with the cards we found."""
    try:
        txt = page.evaluate("() => document.body.innerText || ''")
        m = re.search(r"(\d+)\s+buses", txt, re.I)
        says = m.group(0) if m else "no bus count found in page text"
        print(f"    page says: {says!r}; 'View seats' buttons found: {found}")
        print(f"    page URL: {page.url}")
        print(f"    page text starts: {' '.join(txt.split())[:300]!r}")
        btns = page.evaluate(
            r"""() => [...new Set([...document.querySelectorAll('button, [role=button], a')]
                .map(e => (e.innerText || '').trim())
                .filter(t => /more|show|load|next|page/i.test(t) && t.length < 40))].slice(0, 15)"""
        )
        print(f"    buttons/links mentioning more/show/load/next/page: {btns}")
    except Exception as e:
        print(f"    (could not report listing: {e})")


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
    # green = free for anyone, blue = free but men only, pink = free but ladies only.
    # A seat showing a price (not "Sold") is free; its border colour says who may book it.
    states = {"green", "other"}
    if TRAVELLER in ("male", "any"):
        states.add("blue")
    if TRAVELLER in ("female", "any"):
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
    if DEBUG:
        print("    seat table (deck, x, y, text, border colour, state):")
        for s in sorted(seats, key=lambda s: (s["deck"], round(s["x"]), round(s["y"]))):
            print(
                f"      {s['deck']:<11} x={s['x']:.0f} y={s['y']:.0f} "
                f"{s['text']:<8} colour={s['color']} -> {s['state']}"
            )
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


def read_singles(browser, url, date, target):
    try:
        ctx, page = open_results(browser, url, date)
    except Exception as e:
        print(f"    could not reopen results: {str(e).splitlines()[0]}")
        return None
    try:
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
        ctx.close()


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
        for date in dates:
            url = SEARCH_URL.format(date=date)
            try:
                ctx, page = open_results(browser, url, date)
            except Exception as e:
                print(f"[{date}] could not load results: {str(e).splitlines()[0]}")
                failures += 1
                continue
            cards = page.evaluate(TAG_CARDS_JS)
            if DEBUG:
                report_listing(page, len(cards))
            dump(page, f"results-{date}")
            ctx.close()
            print(f"[{date}] {len(cards)} buses listed")

            targets = []
            for c in cards:
                ev = evaluate_card(c["text"])
                if OPERATOR in c["text"].lower():
                    verdict = "WATCH" if ev["ok"] else f"skip ({ev['why']})"
                    print(f"  {verdict}: dep={ev['dep']} type={ev['type']!r}")
                    if DEBUG:
                        print(f"      card text: {c['text'].replace(chr(10), ' | ')[:350]!r}")
                if ev["ok"] and not any(
                    t["dep"] == ev["dep"] and t["type"] == ev["type"] for t in targets
                ):
                    targets.append(ev)
            if not targets:
                print(f"[{date}] no matching buses right now")

            for target in targets:
                key = f"{date}|{target['dep']}|{target['type']}"
                print(f"  checking {key}")
                singles = read_singles(browser, url, date, target)
                time.sleep(3)
                if singles is None:
                    print("    could not determine single seats; no alert decision made")
                    failures += 1
                    continue
                alerted = state.get(key, {}).get("alerted", False)
                eligible = MIN_SINGLES_TO_ALERT <= singles < ALERT_IF_SINGLES_BELOW
                print(
                    f"    available single seats: {singles} "
                    f"(alert when {MIN_SINGLES_TO_ALERT} to {ALERT_IF_SINGLES_BELOW - 1})"
                )
                if eligible and not alerted:
                    if notify(
                        "Single seats running low",
                        f"{date} {target['dep']} {target['type']}: "
                        f"{singles} single seat{'s' if singles != 1 else ''} left.",
                        click=url,
                    ):
                        alerted = True
                elif singles >= ALERT_IF_SINGLES_BELOW:
                    alerted = False  # plenty of seats again; re-arm the alert
                state[key] = {
                    "alerted": alerted,
                    "singles": singles,
                    "checked": datetime.now(IST).isoformat(timespec="minutes"),
                }
        browser.close()

    run_failed = bool(failures) and failures >= len(dates)
    streak = state.get("_meta", {}).get("fail_streak", 0)
    streak = streak + 1 if run_failed else 0
    if run_failed and streak == FAIL_ALERT_AFTER:
        notify(
            "Seat checker is failing",
            f"{streak} runs in a row failed (about {streak * 30} min). "
            "You are not being watched until it recovers. Check the Actions tab.",
            priority="default",
        )
    state["_meta"] = {"fail_streak": streak}
    save_state(state)
    if run_failed:
        sys.exit(1)


if __name__ == "__main__":
    main()