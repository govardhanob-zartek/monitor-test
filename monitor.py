import json
import os
import re
import sys
from datetime import UTC, datetime, timedelta

import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

URL = "https://in.bookmyshow.com/movies/coimbatore/the-odyssey/buytickets/ET00480917/20260717"
VENUE = "Broadway Cinemas: Coimbatore"

TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

if not TOKEN or not CHAT_ID:
    raise RuntimeError(
        "Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID. "
        "Set them as environment variables or GitHub Actions secrets."
    )

STATE_FILE = "state.json"
MAX_SCRAPE_ATTEMPTS = 3


def send(msg):
    response = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": msg
        },
        timeout=30
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise Exception(f"Telegram send failed: {data}")


def load_state():
    with open(STATE_FILE) as f:
        return json.load(f)


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def sort_shows(shows):
    return sorted(shows, key=lambda show: datetime.strptime(show, "%I:%M %p").time())


def get_page_text():

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )

        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1365, "height": 900},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            extra_http_headers={"Accept-Language": "en-IN,en;q=0.9"},
        )

        page = context.new_page()
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=60000)

            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeoutError:
                pass

            page.wait_for_timeout(5000)

            text = page.locator("body").inner_text(timeout=30000)
        finally:
            browser.close()

    if "Sorry, you have been blocked" in text or "Cloudflare Ray ID" in text:
        raise Exception("BookMyShow blocked the browser request")

    return text


def find_shows(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    if "The Odyssey" not in text:
        raise Exception("BookMyShow ticket page did not load correctly")

    try:
        venue_index = next(
            i for i, line in enumerate(lines)
            if line.casefold() == VENUE.casefold()
        )
    except StopIteration:
        return []

    section_lines = []
    for line in lines[venue_index + 1:]:
        if line in {"Non-cancellable", "Unable to find what you are looking for?"}:
            break
        section_lines.append(line)

    shows = re.findall(r"\b\d{2}:\d{2}\s(?:AM|PM)\b", "\n".join(section_lines))

    return sort_shows(set(shows))


def get_shows():
    last_error = None

    for _ in range(MAX_SCRAPE_ATTEMPTS):
        try:
            shows = find_shows(get_page_text())
            if shows:
                return shows
        except Exception as error:
            last_error = error

    if last_error:
        raise last_error

    return []


def check_ack():

    r = requests.get(
        f"https://api.telegram.org/bot{TOKEN}/getUpdates",
        timeout=30
    ).json()

    for update in r.get("result", []):

        msg = update.get("message", {})

        text = msg.get("text", "").strip().upper()

        if text == "ACK":
            return True

    return False


def heartbeat(state, current_shows, force=False):

    now = datetime.now(UTC)
    shows_text = ", ".join(sort_shows(current_shows)) or "None"

    last = state.get("last_heartbeat")

    if force or not last:
        send(
            "✅ Odyssey monitor alive\n\n"
            f"Current available show(s): {shows_text}"
        )
        state["last_heartbeat"] = now.isoformat()
        return

    last = datetime.fromisoformat(last)
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)

    if now - last > timedelta(hours=1):
        send(
            "✅ Odyssey monitor alive\n\n"
            f"Current available show(s): {shows_text}"
        )
        state["last_heartbeat"] = now.isoformat()


def main():
    state = load_state()

    current = set(get_shows())

    heartbeat(state, current, force=sys.stdout.isatty())
    known = set(state["known_shows"])

    new = current - known

    if new:

        state["pending_alert"] = True
        state["new_shows"] = sort_shows(new)

        send(
            "🚨 NEW ODYSSEY SHOW OPENED\n\n"
            f"New show(s): {', '.join(state['new_shows'])}\n\n"
            f"{URL}"
        )

        state["known_shows"] = sort_shows(current)

    if state["pending_alert"]:

        if check_ack():

            state["pending_alert"] = False
            state["new_shows"] = []

            send("✅ Alert acknowledged")

        else:

            send(
                "⚠️ Reminder\n\n"
                f"New show(s): {', '.join(state['new_shows'])}\n\n"
                "Reply ACK to stop reminders."
            )

    save_state(state)


if __name__ == "__main__":
    main()
