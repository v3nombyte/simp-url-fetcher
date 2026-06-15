"""
SimpCity thread crawler — fetches HTML pages, saves loose in input folder.
"""

import json
import logging
import os
import re
import time
from typing import Callable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)


def editthis_to_cookies(cookies_json: str) -> dict[str, str]:
    """Convert EditThisCookie JSON string to {name: value} dict."""
    if not cookies_json or not cookies_json.strip():
        return {}
    try:
        data = json.loads(cookies_json)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, list):
        return {}
    result = {}
    for entry in data:
        if isinstance(entry, dict) and "name" in entry and "value" in entry:
            result[entry["name"]] = entry["value"]
    return result


def validate_cookie_json(raw: str) -> list | str:
    """Validate a JSON string as EditThisCookie format. Returns parsed list or error string."""
    if not raw or not raw.strip():
        return "Cookie JSON is empty"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return f"Invalid JSON: {e}"
    if not isinstance(data, list):
        return "Root must be a JSON array"
    if len(data) == 0:
        return "Cookie array is empty (at least one cookie required)"
    for i, entry in enumerate(data):
        if not isinstance(entry, dict):
            return f"Entry {i} is not an object"
        if "name" not in entry or "value" not in entry:
            return f"Entry {i} missing 'name' or 'value' fields"
    return data


def parse_thread_id(url: str) -> str | None:
    """Extract thread ID from a SimpCity URL."""
    m = re.search(r'threads/[^.]+\.(\d+)/?', url)
    if m:
        return m.group(1)
    m = re.search(r'threads/[^/]+-(\d+)/', url)
    if m:
        return m.group(1)
    return None


def parse_thread_title(html: str) -> str:
    """Extract thread title from HTML."""
    m = re.search(r'<title>(.+?)</title>', html, re.IGNORECASE | re.DOTALL)
    if m:
        title = m.group(1).strip()
        # Strip site suffix: " | SimpCity Forums", " - SimpCity"
        title = re.sub(r'\s*[|–-]\s*SimpCity(?:\s+Forums)?\s*$', '', title, flags=re.IGNORECASE)
        # Sanitize for folder name
        title = re.sub(r'[\\/*?:"<>|]', '_', title)
        title = re.sub(r'\s+', ' ', title).strip()
        if title:
            return title[:120]
    return "unknown_thread"


def parse_last_page(html: str) -> int:
    """Parse the last page number from pagination."""
    m = re.search(
        r'page-nav[^>]*>.*?<a[^>]*href="[^"]*page-(\d+)[^>]*class="[^"]*last[^"]*"',
        html, re.DOTALL
    )
    if m:
        return int(m.group(1))
    page_nums = re.findall(r'page-(\d+)', html)
    if page_nums:
        return max(int(p) for p in page_nums)
    return 1


def crawl_thread(
    url: str,
    cookies_json: str,
    input_dir: str,
    request_delay: float = 3.0,
    user_agent: str | None = None,
    max_pages: int = 50,
    cancel_check: Callable[[], bool] | None = None,
    log_callback: Callable[[str], None] | None = None,
) -> dict:
    """
    Crawl a SimpCity thread and save HTML pages loose into input_dir.
    Returns dict with results info.
    """
    log = log_callback or (lambda msg: None)
    cancel_check = cancel_check or (lambda: False)

    from urllib.parse import urlparse
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    cookies = editthis_to_cookies(cookies_json)
    if not cookies:
        return {"error": "No valid cookies found. Check Settings → Cookies."}

    user_agent = user_agent or (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    log(f"Thread URL: {url}")
    log(f"Base domain: {base_url}")

    # Build session
    session = requests.Session()
    retry_strategy = Retry(
        total=2, backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    session.mount("https://", HTTPAdapter(max_retries=retry_strategy))
    session.mount("http://", HTTPAdapter(max_retries=retry_strategy))

    thread_id = parse_thread_id(url)
    if not thread_id:
        return {"error": f"Could not parse thread ID from: {url}"}
    log(f"Thread ID: {thread_id}")

    last_request_time = 0.0
    pages_fetched = 0
    errors = []
    os.makedirs(input_dir, exist_ok=True)

    def _rate_limit():
        nonlocal last_request_time
        elapsed = time.time() - last_request_time
        if elapsed < request_delay:
            wait = request_delay - elapsed
            log(f"Rate limit: waiting {wait:.1f}s...")
            time.sleep(wait)
        last_request_time = time.time()

    def _do_request(page_url: str) -> requests.Response | None:
        if cancel_check():
            log("Cancel requested.")
            return None
        _rate_limit()
        headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Referer": base_url,
        }
        try:
            log(f"GET {page_url}")
            resp = session.get(page_url, headers=headers, cookies=cookies, timeout=30)
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as e:
            log(f"Request failed: {e}")
            errors.append(f"Failed {page_url}: {e}")
            return None

    # ─── Page 1 ─────────────────────────────────────────────────────────
    page1_url = url.rstrip("/") + "/"
    resp = _do_request(page1_url)
    if resp is None:
        return {"error": "Failed to fetch first page", "pages": 0, "errors": errors}

    html = resp.text
    title = parse_thread_title(html)
    last_page = parse_last_page(html)
    pages_total = min(last_page, max_pages)
    log(f"Thread: {title}")
    log(f"Pages: {pages_total} total")

    # Build filename prefix from title
    fname_prefix = title[:60]
    fname_prefix = re.sub(r'[\\/*?:"<>|]', '_', fname_prefix)
    fname_prefix = re.sub(r'\s+', ' ', fname_prefix).strip()

    # Save page 1
    fname = f"{fname_prefix} - Page 1.html"
    with open(os.path.join(input_dir, fname), "w", encoding="utf-8") as f:
        f.write(html)
    pages_fetched += 1
    log(f"Saved: {fname} ({len(html)} bytes)")

    if cancel_check():
        return {"title": title, "pages": pages_fetched, "total": pages_total,
                "output_dir": input_dir, "errors": errors}

    # ─── Pages 2+ ───────────────────────────────────────────────────────
    for page_num in range(2, pages_total + 1):
        if cancel_check():
            break

        page_url = f"{url.rstrip('/')}/page-{page_num}"
        resp = _do_request(page_url)
        if resp is None:
            errors.append(f"Failed to fetch page {page_num}")
            continue

        fname = f"{fname_prefix} - Page {page_num}.html"
        with open(os.path.join(input_dir, fname), "w", encoding="utf-8") as f:
            f.write(resp.text)
        pages_fetched += 1
        log(f"Saved: {fname} ({len(resp.text)} bytes)")

    log(f"Done. Fetched {pages_fetched}/{pages_total} pages.")
    return {
        "title": title,
        "thread_id": thread_id,
        "pages": pages_fetched,
        "total": pages_total,
        "output_dir": input_dir,
        "errors": errors,
    }
