import json
import os
import re
import sys
import time
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
ERROR_ALERT_INTERVAL = timedelta(hours=1)
DEBUG_HTML_FILE = "page.html"
DEBUG_SCREENSHOT_FILE = "debug.png"

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

BLOCK_PATTERNS = {
    "captcha": r"\b(captcha|recaptcha|hcaptcha|verify you are human)\b",
    "access denied": r"\b(access denied|request denied|403 forbidden)\b",
    "bot detected": r"\b(bot detected|automated traffic|unusual traffic|robot)\b",
    "cloudflare": r"\b(cloudflare|cf-ray|cloudflare ray id)\b",
    "akamai": r"\b(akamai|reference #[0-9a-f.]+|akamai bot manager)\b",
    "blocked": r"\b(sorry, you have been blocked|you are unable to access)\b",
}


def send(msg):
    response = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": msg
        },
        timeout=30
    )
    try:
        data = response.json()
    except ValueError:
        data = {"description": response.text}

    if response.status_code >= 400:
        description = data.get("description", response.text)
        raise Exception(
            f"Telegram send failed with HTTP {response.status_code}: {description}"
        )

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


def parse_state_time(value):
    if not value:
        return None

    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)

    return parsed


def log_debug(message):
    print(f"[monitor] {message}", flush=True)


def save_text_debug(html):
    with open(DEBUG_HTML_FILE, "w", encoding="utf-8") as f:
        f.write(html or "")


def detect_block_page(text, html=""):
    visible_text = (text or "").lower()
    raw_html = (html or "").lower()

    if len((text or "").strip()) < 50 and len((html or "").strip()) < 200:
        return "empty content"

    for label, pattern in BLOCK_PATTERNS.items():
        if re.search(pattern, visible_text, re.I):
            return label

    html_block_markers = {
        "cloudflare": (
            "cloudflare ray id",
            "cf-error-code",
            "cf-error-details",
            "attention required! | cloudflare",
        ),
        "akamai": (
            "akamai bot manager",
            "access denied | akamai",
        ),
    }
    for label, markers in html_block_markers.items():
        if any(marker in raw_html for marker in markers):
            return label

    return None


def log_page_snapshot(source, title, final_url, text, html):
    log_debug(f"{source} title: {title or '<empty>'}")
    log_debug(f"{source} final URL: {final_url or '<unknown>'}")
    log_debug(f"{source} text first 1000 chars:\n{(text or '')[:1000]}")

    block_reason = detect_block_page(text, html)
    if block_reason:
        log_debug(f"{source} block detector matched: {block_reason}")

    return block_reason


def get_page_text_with_playwright(wait_until):
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )

        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1365, "height": 900},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
            geolocation={"latitude": 11.0168, "longitude": 76.9558},
            permissions=["geolocation"],
            extra_http_headers=REQUEST_HEADERS,
        )

        page = context.new_page()
        page.add_init_script(
            """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-IN', 'en']});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            window.chrome = window.chrome || { runtime: {} };
            """
        )

        try:
            response = page.goto(URL, wait_until=wait_until, timeout=60000)
            status = response.status if response else "no response"
            log_debug(f"Playwright wait_until={wait_until} status: {status}")

            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeoutError:
                log_debug("Playwright networkidle wait timed out; continuing")

            page.wait_for_timeout(5000)

            title = page.title()
            final_url = page.url
            html = page.content()
            text = page.locator("body").inner_text(timeout=30000)

            save_text_debug(html)
            page.screenshot(path=DEBUG_SCREENSHOT_FILE, full_page=True)

            block_reason = log_page_snapshot("Playwright", title, final_url, text, html)
            if block_reason:
                raise Exception(f"BookMyShow blocked the browser request: {block_reason}")

            return text
        finally:
            browser.close()


def get_page_text_with_requests():
    response = requests.get(URL, headers=REQUEST_HEADERS, timeout=60)
    response.raise_for_status()

    html = response.text
    text = re.sub(r"<(script|style).*?</\1>", " ", html, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)

    save_text_debug(html)

    block_reason = log_page_snapshot(
        "requests",
        "<not available>",
        response.url,
        text,
        html,
    )
    if block_reason:
        raise Exception(f"BookMyShow blocked the direct request: {block_reason}")

    return text


def get_page_text():
    last_error = None
    wait_strategies = ("domcontentloaded", "load", "networkidle")

    for attempt in range(1, MAX_SCRAPE_ATTEMPTS + 1):
        wait_until = wait_strategies[(attempt - 1) % len(wait_strategies)]
        try:
            log_debug(f"Playwright attempt {attempt}/{MAX_SCRAPE_ATTEMPTS}")
            return get_page_text_with_playwright(wait_until)
        except Exception as error:
            last_error = error
            log_debug(f"Playwright attempt {attempt} failed: {error}")
            time.sleep(2 ** (attempt - 1))

    try:
        log_debug("Trying direct requests fallback")
        return get_page_text_with_requests()
    except Exception as error:
        last_error = error
        log_debug(f"Direct requests fallback failed: {error}")

    raise last_error


def find_shows(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    if "The Odyssey" not in text:
        raise Exception("BookMyShow ticket page did not load correctly")

    venue_index = next(
        (
            i for i, line in enumerate(lines)
            if line.casefold() == VENUE.casefold()
        ),
        None,
    )
    if venue_index is None:
        log_debug(f"{VENUE} section not found in page text")
        return []

    section_lines = []
    stop_markers = {
        "Unable to find what you are looking for?",
        "Change Location",
        "HomeMovies in CoimbatoreHindi MoviesThe Odyssey",
    }

    for line in lines[venue_index + 1:]:
        if line in stop_markers:
            break
        section_lines.append(line)

    section = "\n".join(section_lines)
    shows = re.findall(r"\b\d{2}:\d{2}\s(?:AM|PM)\b", section)

    return sort_shows(set(shows))


def get_shows():
    text = get_page_text()
    shows = find_shows(text)
    log_debug(f"Parsed Broadway show(s): {', '.join(shows) or 'None'}")
    return shows


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

    last = parse_state_time(state.get("last_heartbeat"))

    if force or not last:
        send(
            "✅ Odyssey monitor alive\n\n"
            f"Current available show(s): {shows_text}"
        )
        state["last_heartbeat"] = now.isoformat()
        return

    if now - last > timedelta(hours=1):
        send(
            "✅ Odyssey monitor alive\n\n"
            f"Current available show(s): {shows_text}"
        )
        state["last_heartbeat"] = now.isoformat()


def report_scrape_error(state, error, force=False):
    now = datetime.now(UTC)
    last = parse_state_time(state.get("last_scrape_error_alert"))

    if force or not last or now - last > ERROR_ALERT_INTERVAL:
        send(
            "⚠️ Odyssey monitor could not check shows\n\n"
            f"Reason: {error}\n\n"
            "GitHub Actions will try again on the next scheduled run."
        )
        state["last_scrape_error_alert"] = now.isoformat()


def main():
    state = load_state()
    force_status = sys.stdout.isatty()

    try:
        current = set(get_shows())
    except Exception as error:
        report_scrape_error(state, error, force=force_status)
        save_state(state)
        return

    heartbeat(state, current, force=force_status)
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
