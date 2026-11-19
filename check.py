"""Track direct CLJ->BCN Tue->Sat (4 night) fares via the LetsFG developer API.

Budget rule: LetsFG gives a fixed pool of free searches. Never search below RESERVE
remaining, and report the remaining count on Telegram every run.
Run plan: full sweep of every trip weekly, otherwise only the cheapest few.
"""
import json, os, sys, time, traceback, urllib.parse, urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

API = "https://letsfg.co/developers/api/v1"
DATA = Path(__file__).parent / "data" / "prices.json"
RESERVE = 5          # free searches never touched
SWEEP_DAYS = 7       # full sweep interval
TOP_N = 3            # trips re-checked between sweeps
MIN_DROP = 1.0       # EUR change that counts as a drop
ORIGIN, DEST = "CLJ", "BCN"


def trips():
    d, out = date(2027, 2, 2), []   # first Tuesday of Feb 2027
    while d <= date(2027, 4, 27):
        if d.weekday() == 1:
            out.append((d.isoformat(), (d + timedelta(days=4)).isoformat()))
        d += timedelta(days=1)
    return out


def call(path, key, body=None, timeout=180):
    req = urllib.request.Request(
        f"{API}{path}", data=json.dumps(body).encode() if body else None, method="POST" if body else "GET",
        headers={"Content-Type": "application/json", "X-API-Key": key, "User-Agent": "flight-tracker/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def remaining(key):
    return call("/agents/me", key)["developer_api"]["flight_search"]["searches_remaining"]


def telegram(text):
    token, chat = os.environ["TG_BOT_TOKEN"], os.environ["TG_CHAT_ID"]
    data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=30)


def search(key, out, back):
    r = call("/flights/search", key, {
        "origin": ORIGIN, "destination": DEST, "date_from": out, "return_from": back, "adults": 1,
        "currency": "EUR", "limit": 3, "sort": "price", "max_stopovers": 0})
    offers = r.get("offers") or []
    return float(offers[0]["price"]) if offers else None


def main():
    key = os.environ["LETSFG_API_KEY"]
    state = json.loads(DATA.read_text()) if DATA.exists() else {"last_sweep": None, "trips": {}}
    now = datetime.now(timezone.utc)
    left = remaining(key)
    allowed = left - RESERVE
    if allowed <= 0:
        telegram(f"Flight tracker paused: {left} free searches left (reserve {RESERVE}). No search was run.")
        return

    all_trips = trips()
    last = state["last_sweep"] and datetime.fromisoformat(state["last_sweep"])
    sweep = not last or now - last >= timedelta(days=SWEEP_DAYS)
    if sweep:
        todo = all_trips
    else:
        priced = [(state["trips"].get(o, {}).get("last"), o, b) for o, b in all_trips]
        priced = sorted((p for p in priced if p[0] is not None), key=lambda p: p[0])
        todo = [(o, b) for _, o, b in priced[:TOP_N]]
    todo = todo[:min(allowed, int(os.environ.get("MAX_TRIPS") or 999))]
    sweep = sweep and len(todo) == len(all_trips)
    print(f"left={left} sweep={sweep} trips={len(todo)}", flush=True)

    drops, errors, done = [], [], 0
    for out, back in todo:
        try:
            price = search(key, out, back)
        except Exception as e:
            traceback.print_exc()
            errors.append(f"{out}: {type(e).__name__}")
            continue
        print(out, price, flush=True)
        done += 1
        t = state["trips"].setdefault(out, {"return": back, "low": None, "last": None, "history": []})
        t["history"].append({"t": now.isoformat(timespec="minutes"), "price": price})
        if price is not None:
            if t["low"] is not None and price <= t["low"] - MIN_DROP:
                drops.append(f"{out} -> {back}: EUR {price:.0f} (was low {t['low']:.0f})")
            t["low"] = price if t["low"] is None else min(t["low"], price)
        t["last"] = price
        time.sleep(1)
    if sweep:
        state["last_sweep"] = now.isoformat(timespec="minutes")
    DATA.parent.mkdir(exist_ok=True)
    DATA.write_text(json.dumps(state, indent=1))

    cheapest = sorted((v["last"], k, v["return"]) for k, v in state["trips"].items() if v["last"] is not None)[:3]
    lines = ["PRICE DROP!" if drops else "Flight tracker", *drops,
             f"Checked {done}/{len(todo)} trips ({'full sweep' if sweep else 'cheapest only'}).",
             "Cheapest now: " + "; ".join(f"{o}->{b} EUR {p:.0f}" for p, o, b in cheapest)]
    if errors:
        lines.append("Errors: " + ", ".join(errors))
    try:
        lines.append(f"Free searches left: {remaining(key)}")
    except Exception:
        lines.append(f"Free searches left: ~{left - done}")
    telegram("\n".join(lines))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # tell the phone instead of failing silently
        traceback.print_exc()
        try:
            telegram(f"Flight tracker error: {type(e).__name__}")
        except Exception:
            traceback.print_exc()
        sys.exit(1)
