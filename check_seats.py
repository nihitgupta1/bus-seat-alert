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
DEPART_BEFORE = (19, 30)        # only buses departing before 19:30
ALERT_IF_SINGLES_BELOW = 3      # alert when available singles is LESS than this (3 = at 2 or fewer)
MIN_SINGLES_TO_ALERT = 1        # no alert at 0 singles (nothing to book); set 0 to hear about sell-outs
TRAVELLER = "male"              # "male", "female" or "any": which gender-restricted seats count
FAIL_ALERT_AFTER = 4            # warn on phone after this many failed runs in a row (~2 hours)

# redBus sometimes swaps the date in the URL for today's date, so the same search is
# written three ways. Each attempt uses the next form; the one that works is tried first.
_BASE = "https://www.redbus.in/bus-tickets/indore-to-pune"
URL_FORMS = [
    _BASE + "?fromCityId=313&fromCityName=Indore&toCityId=130&toCityName=Pune&onward={date}&ref=search",
    _BASE + "?fromCityName=Indore&toCityName=Pune&fromCityId=313&toCityId=130&onward={date}&ref=modifyDate",
    _BASE + "?fromCityId=313&fromCityName=Indore&toCityId=130&toCityName=Pune&onward={date}",
]
_url_order = [0, 1, 2]
# --------------------------------------------------------------------------

STATE_FILE = Path("state.json")
DEBUG_DIR = Path("debug")
COL_TOL = 12  # px tolerance when grouping seats into columns
IST = timezone(timedelta(hours=5, minutes=30))
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

# ------------------------------ page scripts ------------------------------
# Every "View seats" button on the page (one per bus).
_BUTTONS = r"""[...document.querySelectorAll('*')].filter(
  e => e.children.length === 0 && /^\s*(view seats?|select seats?)\s*$/i.test(e.textContent || ''))"""

COUNT_JS = "() => " + _BUTTONS + ".length"

# How many buses the page says it has (e.g. "25 buses"); 0 if not found.
EXPECTED_JS = r"""() => {
  const m = (document.body.innerText || '').match(/(\d+)\s+buses/i);
  return m ? parseInt(m[1], 10) : 0;
}"""

# Centres the last bus card on screen and returns where it is.
SCROLL_STEP_JS = """() => {
  const leaves = BUTTONS;
  if (!leaves.length) { window.scrollBy(0, window.innerHeight * 0.9); return null; }
  const last = leaves[leaves.length - 1];
  last.scrollIntoView({block: 'center'});
  const r = last.getBoundingClientRect();
  return {x: r.left + r.width / 2, y: r.top + r.height / 2};
}""".replace("BUTTONS", _BUTTONS)

# Tags each "View seats" button and returns the text of its bus card.
TAG_CARDS_JS = """() => {
  const leaves = BUTTONS;
  return leaves.map((leaf, i) => {
    let card = leaf;
    while (card.parentElement) {
      const p = card.parentElement;
      if (leaves.filter(l => p.contains(l)).length > 1) break;
      card = p;
    }
    leaf.setAttribute('data-sc-btn', String(i));
    return {i, text: card.innerText || ''};
  });
}""".replace("BUTTONS", _BUTTONS)

# Reads every seat on the open seat layout: position, "Sold"/price text, border colour, deck.
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
      text: (e.innerText || '').trim(), color: borderColor(e), deck: deck,
    };
  });
}"""


# ------------------------------ helpers -----------------------------------
def dump(page, name):
    """Save a screenshot and the page HTML to debug/ (uploaded by the workflow)."""
    try:
        DEBUG_DIR.mkdir(exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
        page.screenshot(path=str(DEBUG_DIR / f"{safe}.png"))
        (DEBUG_DIR / f"{safe}.html").write_text(page.content(), encoding="utf-8")
    except Exception as e:
        print(f"    (could not save debug files: {e})")


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
    elif dep is None:
        res["why"] = "no departure time found"
    elif (int(dep[:2]), int(dep[3:])) >= DEPART_BEFORE:
        res["why"] = f"departs {dep}, not before {DEPART_BEFORE[0]:02d}:{DEPART_BEFORE[1]:02d}"
    else:
        bharat_ac_sleeper = (
            "bharat" in low and "benz" in low and "sleeper" in low
            and not re.search(r"non[\s\-]*a/?c", low)
        )
        if "volvo" in low or bharat_ac_sleeper:
            res["ok"] = True
        else:
            res["why"] = f"bus type not matched ({btype})"
    return res


def load_all(page):
    """Scroll like a person until every bus the page announces is loaded."""
    last, stable = -1, 0
    for _ in range(60):
        n = page.evaluate(COUNT_JS)
        expected = page.evaluate(EXPECTED_JS)
        if expected and n >= expected:
            break
        if n == last:
            stable += 1
            if stable >= 4:  # nothing new after 4 scroll rounds in a row
                break
        else:
            last, stable = n, 0
        # Centre the last bus, hover over it, then scroll in small wheel steps. This is
        # what triggers the page's "load more as you scroll" logic.
        pos = page.evaluate(SCROLL_STEP_JS)
        if pos:
            page.mouse.move(pos["x"], pos["y"])
        for _ in range(4):
            page.mouse.wheel(0, 500)
            page.wait_for_timeout(400)
        page.wait_for_timeout(1500)


def open_results(browser, date):
    """Open the results page for `date` in a fresh browser profile and load every bus.

    Up to 3 attempts, each with a different URL form. Fails if redBus keeps showing a
    different date than the one requested. Returns (context, page); caller closes context.
    """
    last_err = None
    for attempt in range(3):
        idx = _url_order[attempt]
        ctx = browser.new_context(
            viewport={"width": 1366, "height": 900},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            user_agent=UA,
        )
        page = ctx.new_page()
        try:
            page.goto(URL_FORMS[idx].format(date=date), wait_until="domcontentloaded", timeout=40000)
            page.wait_for_function(f"({COUNT_JS})() > 0", timeout=30000)
            shown = re.search(r"onward=([^&]+)", page.url)
            if shown and shown.group(1).lower() != date.lower():
                raise RuntimeError(f"redBus is showing date {shown.group(1)} instead of {date}")
            load_all(page)
            if idx != _url_order[0]:
                _url_order.remove(idx)
                _url_order.insert(0, idx)
            return ctx, page
        except Exception as e:
            last_err = e
            print(f"    attempt {attempt + 1}/3 (URL form #{idx + 1}) failed: {str(e).splitlines()[0]}")
            if attempt == 2:
                dump(page, f"load-fail-{date}")
            ctx.close()
            if attempt < 2:
                time.sleep(45)
    raise last_err


def classify(seat):
    """sold / green (free for anyone) / pink (ladies only) / blue (men only) / other."""
    if "sold" in seat["text"].lower():
        return "sold"
    if not seat.get("color"):
        return "other"
    r, g, b = seat["color"]
    if g > r + 25 and g > b + 25:
        return "green"
    if r > g + 60 and b > g + 20:
        return "pink"
    if b > r + 40 and b > g + 20:
        return "blue"
    return "other"


def available_states():
    # A seat showing a price (not "Sold") is free; its border colour says who may book it.
    states = {"green", "other"}
    if TRAVELLER in ("male", "any"):
        states.add("blue")
    if TRAVELLER in ("female", "any"):
        states.add("pink")
    return states


def single_column(deck_seats):
    """x position of the 1-seat column in a 2+1 deck, else None."""
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
    k = gaps.index(max(gaps))  # the aisle is the widest gap
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
            print(f"    {deck}: {len(ds)} seats, no 2+1 layout detected")
            continue
        found = True
        singles = [s for s in ds if abs(s["x"] - sx) < COL_TOL]
        counts = Counter(s["state"] for s in singles)
        avail = sum(counts[k] for k in available_states())
        total += avail
        print(f"    {deck}: {len(singles)} single berths, {avail} available {dict(counts)}")
    return total if found else None


def read_singles(page, target):
    """Open one bus's seat map on an already-loaded results page and count its available
    single seats (None on failure)."""
    card = next(
        (
            c for c in page.evaluate(TAG_CARDS_JS)
            if (ev := evaluate_card(c["text"]))["ok"]
            and ev["dep"] == target["dep"] and ev["type"] == target["type"]
        ),
        None,
    )
    if card is None:
        print("    bus no longer listed")
        return None
    btn = page.locator(f'[data-sc-btn="{card["i"]}"]').first
    result = None
    try:
        btn.scroll_into_view_if_needed(timeout=10000)
        btn.click(timeout=10000)
        try:
            page.wait_for_function(f"({SEATS_JS})().length > 0", timeout=20000)
        except PWTimeout:
            print("    seat layout did not appear in time")
        page.wait_for_timeout(1500)
        seats = page.evaluate(SEATS_JS)
        print(f"    read {len(seats)} seats")
        result = analyse(seats) if seats else None
        if result is None:
            dump(page, f"seats-fail-{target['dep']}")
    except Exception as e:
        print(f"    error reading seats: {e}")
        dump(page, f"seats-error-{target['dep']}")
    try:  # close the seat map again so the next bus starts from a clean page
        btn.click(timeout=3000)
        page.wait_for_timeout(1000)
    except Exception:
        pass
    return result


def clean_page(browser, date, ctx, page):
    """Make sure no seat map is left open from the previous bus; reload if one is."""
    if not page.evaluate(SEATS_JS):
        return ctx, page
    print("    previous seat map is still open; reloading results")
    ctx.close()
    try:
        return open_results(browser, date)
    except Exception as e:
        print(f"    could not reload results: {str(e).splitlines()[0]}")
        return None, None


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
    attempted = succeeded = 0
    sessions = []  # (date, context, page, targets) for every date that loaded
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            channel="chrome",
            args=["--disable-blink-features=AutomationControlled", "--disable-http2"],
        )

        # Step 1: load each date's results ONCE and keep the pages open. redBus starts
        # showing today's date if it is asked for the same search several times in a row,
        # so every bus is read from these already-loaded pages instead of reloading.
        for n, date in enumerate(dates):
            if n:
                time.sleep(10)
            try:
                ctx, page = open_results(browser, date)
            except Exception as e:
                print(f"[{date}] could not load results: {str(e).splitlines()[0]}")
                continue
            cards = page.evaluate(TAG_CARDS_JS)
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
            sessions.append((date, ctx, page, targets))

        # Step 2: read the seat map of each watched bus and decide on alerts.
        for date, ctx, page, targets in sessions:
            for target in targets:
                key = f"{date}|{target['dep']}|{target['type']}"
                print(f"  checking {key}")
                attempted += 1
                if ctx is not None:
                    ctx, page = clean_page(browser, date, ctx, page)
                singles = read_singles(page, target) if ctx is not None else None
                time.sleep(3)
                if singles is None:
                    print("    could not determine single seats; no alert decision made")
                    continue
                succeeded += 1
                alerted = state.get(key, {}).get("alerted", False)
                eligible = MIN_SINGLES_TO_ALERT <= singles < ALERT_IF_SINGLES_BELOW
                print(f"    available single seats: {singles}")
                if eligible and not alerted:
                    if notify(
                        "Single seats running low",
                        f"{date} {target['dep']} {target['type']}: "
                        f"{singles} single seat{'s' if singles != 1 else ''} left.",
                        click=URL_FORMS[0].format(date=date),
                    ):
                        alerted = True
                elif singles >= ALERT_IF_SINGLES_BELOW:
                    alerted = False  # plenty of seats again; re-arm the alert
                state[key] = {
                    "alerted": alerted,
                    "singles": singles,
                    "checked": datetime.now(IST).isoformat(timespec="minutes"),
                }
            if ctx is not None:
                ctx.close()
        browser.close()

    # A run counts as failed if no date loaded, or buses were found but none could be read.
    run_failed = not sessions or (attempted > 0 and succeeded == 0)
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
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))
    if run_failed:
        sys.exit(1)


if __name__ == "__main__":
    main()