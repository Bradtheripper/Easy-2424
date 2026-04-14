#!/usr/bin/env python3
"""
FOHCigars 24:24 Forum Monitor.

Monitors the "Water Hole" sub-forum during Beijing-time windows and sends an
email per product whenever a matching 24:24 thread appears:

  Tuesday   08:27-08:32 Beijing time  -> titles containing "24:24" + "Tuesday"
  Wednesday 08:27-08:32 Beijing time  -> titles containing "24:24" + "Wednesday"
  Thursday  08:27-08:32 Beijing time  -> titles containing "24:24" + "Thursday"
                                         (or "Today" / "Weekend" variants)
  Friday    08:27-08:32 Beijing time  -> titles containing "24:24" + "Friday"
                                         (or "Weekend" / "Today" variants)

Usage:
    cp config.example.yaml config.yaml   # fill in SMTP + recipients
    python monitor.py                    # long-running loop
    python monitor.py --now              # do one poll immediately (for testing)
    python monitor.py --test-email       # send a dummy email to verify SMTP
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import smtplib
import ssl
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import List, Optional, Set

import requests
import yaml
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FORUM_URL = (
    "https://www.fohcigars.com/forum/"
    "forum/1-cigars-discussion-forum-quotthe-water-holequot/"
)
BEIJING_TZ = timezone(timedelta(hours=8))

# Python weekday(): Monday=0, Tuesday=1, ..., Sunday=6
MONITOR_WEEKDAYS = {1, 2, 3, 4}  # Tue, Wed, Thu, Fri
WINDOW_START = (8, 27, 0)
WINDOW_END = (8, 32, 0)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

LOGGER = logging.getLogger("fohc24")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class Thread:
    title: str
    url: str


@dataclass
class Product:
    name: str
    price: str
    raw_line: str


@dataclass
class AppConfig:
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_use_ssl: bool
    sender_name: str
    recipients: List[str]
    poll_workers: int = 3
    poll_interval_seconds: float = 2.0
    http_timeout_seconds: float = 10.0
    dedup_state_path: Path = field(default_factory=lambda: Path(".state/seen.txt"))


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def beijing_now() -> datetime:
    return datetime.now(tz=BEIJING_TZ)


def today_keyword(now: Optional[datetime] = None) -> Optional[str]:
    """Return the weekday keyword (lowercase) we look for in titles."""
    now = now or beijing_now()
    return {1: "tuesday", 2: "wednesday", 3: "thursday", 4: "friday"}.get(now.weekday())


def in_window(now: Optional[datetime] = None) -> bool:
    now = now or beijing_now()
    if now.weekday() not in MONITOR_WEEKDAYS:
        return False
    sh, sm, ss = WINDOW_START
    eh, em, es = WINDOW_END
    start = now.replace(hour=sh, minute=sm, second=ss, microsecond=0)
    end = now.replace(hour=eh, minute=em, second=es, microsecond=0)
    return start <= now < end


def seconds_until_next_window(now: Optional[datetime] = None) -> float:
    """Seconds from `now` to the next monitoring window start (Beijing time)."""
    now = now or beijing_now()
    sh, sm, ss = WINDOW_START
    for offset in range(0, 8):
        d = (now + timedelta(days=offset)).date()
        candidate = datetime(
            d.year, d.month, d.day, sh, sm, ss, tzinfo=BEIJING_TZ
        )
        if candidate.weekday() in MONITOR_WEEKDAYS and candidate > now:
            return max(0.0, (candidate - now).total_seconds())
    return 60.0


# ---------------------------------------------------------------------------
# Title matching
# ---------------------------------------------------------------------------

_DAY_VARIANTS = {
    "tuesday": ("tuesday", "tue"),
    "wednesday": ("wednesday", "wed"),
    # Thursday posts sometimes say "Today & Weekend" heading into the weekend.
    "thursday": ("thursday", "thu", "today", "weekend"),
    # Friday posts often say "Friday & Weekend" or just "Weekend".
    "friday": ("friday", "fri", "weekend", "today"),
}


def title_matches(title: str, keyword: str) -> bool:
    """True if `title` contains "24:24" and today's weekday variant."""
    t = title.lower()
    if "24:24" not in t and "24\uff1a24" not in t:
        return False
    variants = _DAY_VARIANTS.get(keyword, (keyword,))
    return any(v in t for v in variants)


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------

class ForumScraper:
    def __init__(self, config: AppConfig) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
                "Accept-Language": "en-US,en;q=0.9",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            }
        )
        self.timeout = config.http_timeout_seconds

    def fetch(self, url: str) -> Optional[str]:
        try:
            r = self.session.get(url, timeout=self.timeout)
        except requests.RequestException as exc:
            LOGGER.warning("GET %s failed: %s", url, exc)
            return None
        if r.status_code != 200:
            LOGGER.warning("GET %s -> HTTP %s", url, r.status_code)
            return None
        return r.text

    def list_threads(self) -> List[Thread]:
        html = self.fetch(FORUM_URL)
        if not html:
            return []
        soup = BeautifulSoup(html, "html.parser")

        # Invision Power Suite (IPS) - several markups depending on version.
        selectors = (
            "li.ipsDataItem h4.ipsDataItem_title a",
            "li.ipsDataItem .ipsDataItem_title a",
            "h4.ipsDataItem_title a",
            "span.ipsDataItem_title a",
        )
        anchors: List = []
        for sel in selectors:
            anchors = soup.select(sel)
            if anchors:
                break
        if not anchors:
            anchors = [
                a for a in soup.find_all("a", href=True)
                if "/forum/topic/" in a["href"]
            ]

        threads: List[Thread] = []
        seen_urls: Set[str] = set()
        for a in anchors:
            href = a.get("href", "").strip()
            title = (a.get("title") or a.get_text() or "").strip()
            if not href or not title or href in seen_urls:
                continue
            seen_urls.add(href)
            if href.startswith("/"):
                href = "https://www.fohcigars.com" + href
            threads.append(Thread(title=title, url=href))
        return threads

    def extract_products(self, thread_url: str) -> List[Product]:
        html = self.fetch(thread_url)
        if not html:
            return []
        soup = BeautifulSoup(html, "html.parser")
        body = (
            soup.select_one('[data-role="commentContent"]')
            or soup.select_one(".cPost_contentWrap")
            or soup.select_one("article")
            or soup
        )

        # Gather candidate lines: structural elements first, then raw lines.
        raw_lines: List[str] = []
        for el in body.find_all(["li", "p", "tr", "div"]):
            text = el.get_text(" ", strip=True)
            if text and text not in raw_lines:
                raw_lines.append(text)
        if not raw_lines:
            raw_lines = [
                ln.strip()
                for ln in body.get_text("\n").splitlines()
                if ln.strip()
            ]

        price_re = re.compile(r"(\$\s?\d{1,4}(?:[.,]\d{2})?)")
        products: List[Product] = []
        seen_names: Set[str] = set()
        for raw in raw_lines:
            m = price_re.search(raw)
            if not m:
                continue
            price = m.group(1).replace(" ", "")
            before = price_re.split(raw, maxsplit=1)[0]
            name = re.sub(r"[\s\-\u2013\u2014:|]+$", "", before).strip()
            if not name:
                continue
            key = name.lower()
            if key in seen_names:
                continue
            seen_names.add(key)
            products.append(Product(name=name, price=price, raw_line=raw))
        return products


# ---------------------------------------------------------------------------
# Mailer
# ---------------------------------------------------------------------------

class Mailer:
    def __init__(self, config: AppConfig) -> None:
        self.cfg = config

    def _open(self) -> smtplib.SMTP:
        if self.cfg.smtp_use_ssl:
            context = ssl.create_default_context()
            return smtplib.SMTP_SSL(
                self.cfg.smtp_host, self.cfg.smtp_port, context=context, timeout=15
            )
        s = smtplib.SMTP(self.cfg.smtp_host, self.cfg.smtp_port, timeout=15)
        s.ehlo()
        s.starttls(context=ssl.create_default_context())
        s.ehlo()
        return s

    def _send(self, subject: str, body: str) -> None:
        msg = MIMEMultipart()
        msg["From"] = f'"{self.cfg.sender_name}" <{self.cfg.smtp_user}>'
        msg["To"] = ", ".join(self.cfg.recipients)
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))
        with self._open() as s:
            s.login(self.cfg.smtp_user, self.cfg.smtp_password)
            s.send_message(msg)

    def send_product_alert(self, product: Product, thread: Thread) -> None:
        subject = f"[24:24] {product.name} - {product.price}"
        body = (
            f"Thread : {thread.title}\n"
            f"URL    : {thread.url}\n"
            f"\n"
            f"Product: {product.name}\n"
            f"Price  : {product.price}\n"
            f"\n"
            f"Raw line:\n{product.raw_line}\n"
        )
        self._send(subject, body)
        LOGGER.info("Emailed: %s", subject)

    def send_test(self) -> None:
        self._send(
            "[24:24] SMTP test",
            f"Test email from monitor at Beijing time {beijing_now():%Y-%m-%d %H:%M:%S}.\n",
        )
        LOGGER.info("Test email sent to %s", ", ".join(self.cfg.recipients))


# ---------------------------------------------------------------------------
# Seen-state (dedup) store
# ---------------------------------------------------------------------------

class SeenStore:
    """Append-only, thread-safe set persisted as one key per line."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._data: Set[str] = set()
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                self._data = {ln.strip() for ln in f if ln.strip()}

    def add_if_new(self, key: str) -> bool:
        with self._lock:
            if key in self._data:
                return False
            self._data.add(key)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(key + "\n")
            return True


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------

class Monitor:
    def __init__(self, config: AppConfig) -> None:
        self.cfg = config
        self.scraper = ForumScraper(config)
        self.mailer = Mailer(config)
        self.seen_threads = SeenStore(config.dedup_state_path)
        self.seen_products = SeenStore(
            config.dedup_state_path.with_name("seen_products.txt")
        )
        self.stop_event = threading.Event()

    # ---- polling ---------------------------------------------------------

    def _poll_once(self, worker_id: int) -> None:
        keyword = today_keyword()
        if not keyword:
            return
        threads = self.scraper.list_threads()
        LOGGER.debug("[w%d] fetched %d threads", worker_id, len(threads))
        for t in threads:
            if not title_matches(t.title, keyword):
                continue
            if not self.seen_threads.add_if_new(t.url):
                continue
            LOGGER.info("[w%d] MATCH: %s -> %s", worker_id, t.title, t.url)
            try:
                self._handle_thread(t)
            except Exception:
                LOGGER.exception("Error handling thread %s", t.url)

    def _handle_thread(self, thread: Thread) -> None:
        products = self.scraper.extract_products(thread.url)
        if not products:
            LOGGER.warning("No products parsed from %s", thread.url)
            # Still notify so the user is aware
            fallback = Product(
                name="(see thread)", price="?", raw_line="(unable to parse products)"
            )
            try:
                self.mailer.send_product_alert(fallback, thread)
            except Exception:
                LOGGER.exception("Failed to send fallback email")
            return
        LOGGER.info("Parsed %d products from %s", len(products), thread.url)
        for p in products:
            key = f"{thread.url}::{p.name.lower()}"
            if not self.seen_products.add_if_new(key):
                continue
            try:
                self.mailer.send_product_alert(p, thread)
            except Exception:
                LOGGER.exception("Failed to send email for %s", p.name)

    # ---- worker threads --------------------------------------------------

    def _worker(self, worker_id: int, offset: float) -> None:
        if offset:
            self.stop_event.wait(offset)
        while not self.stop_event.is_set() and in_window():
            start = time.monotonic()
            try:
                self._poll_once(worker_id)
            except Exception:
                LOGGER.exception("Poll error in worker %d", worker_id)
            elapsed = time.monotonic() - start
            wait = max(0.0, self.cfg.poll_interval_seconds - elapsed)
            if wait:
                self.stop_event.wait(wait)

    def run_window(self) -> None:
        LOGGER.info("Entering monitor window at %s", beijing_now())
        workers = max(1, self.cfg.poll_workers)
        stagger = self.cfg.poll_interval_seconds / workers
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [
                ex.submit(self._worker, i, stagger * i) for i in range(workers)
            ]
            for f in futures:
                f.result()
        LOGGER.info("Exited monitor window at %s", beijing_now())

    def run_forever(self) -> None:
        LOGGER.info("Monitor started. Beijing time: %s", beijing_now())
        try:
            while not self.stop_event.is_set():
                if in_window():
                    self.run_window()
                    continue
                wait = seconds_until_next_window()
                LOGGER.info(
                    "Next window in %.0fs (at %s BJT)",
                    wait,
                    (beijing_now() + timedelta(seconds=wait)).strftime(
                        "%a %Y-%m-%d %H:%M:%S"
                    ),
                )
                # Sleep in chunks so the loop stays responsive.
                while wait > 0 and not self.stop_event.is_set():
                    chunk = min(wait, 30.0)
                    self.stop_event.wait(chunk)
                    wait -= chunk
        except KeyboardInterrupt:
            LOGGER.info("Interrupted; shutting down.")
            self.stop_event.set()


# ---------------------------------------------------------------------------
# Config + CLI
# ---------------------------------------------------------------------------

def load_config(path: Path) -> AppConfig:
    if not path.exists():
        raise SystemExit(
            f"Config not found: {path}\n"
            f"Copy config.example.yaml to {path} and fill in your values."
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    smtp = data.get("smtp") or {}
    recipients = data.get("recipients") or []
    if not smtp.get("user") or not smtp.get("password"):
        raise SystemExit("config.yaml: smtp.user and smtp.password are required.")
    if not recipients:
        raise SystemExit("config.yaml: at least one recipient is required.")

    state_path = Path(data.get("dedup_state_path", ".state/seen.txt"))
    return AppConfig(
        smtp_host=smtp.get("host", "smtp.gmail.com"),
        smtp_port=int(smtp.get("port", 465)),
        smtp_user=smtp["user"],
        smtp_password=smtp["password"],
        smtp_use_ssl=bool(smtp.get("use_ssl", True)),
        sender_name=smtp.get("sender_name", "FOHC 24:24 Monitor"),
        recipients=list(recipients),
        poll_workers=int(data.get("poll_workers", 3)),
        poll_interval_seconds=float(data.get("poll_interval_seconds", 2.0)),
        http_timeout_seconds=float(data.get("http_timeout_seconds", 10.0)),
        dedup_state_path=state_path,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="FOHCigars 24:24 forum monitor")
    p.add_argument(
        "-c", "--config", default=os.environ.get("FOHC_CONFIG", "config.yaml"),
        help="Path to config.yaml (default: config.yaml or $FOHC_CONFIG)",
    )
    p.add_argument(
        "--now", action="store_true",
        help="Run one poll immediately regardless of time window (for testing).",
    )
    p.add_argument(
        "--test-email", action="store_true",
        help="Send a dummy email using the SMTP config to verify credentials.",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable DEBUG logging.",
    )
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    cfg = load_config(Path(args.config))
    monitor = Monitor(cfg)

    if args.test_email:
        monitor.mailer.send_test()
        return 0

    if args.now:
        LOGGER.info("One-shot poll (window check bypassed)")
        keyword = today_keyword() or "tuesday"
        LOGGER.info("Today keyword: %s", keyword)
        threads = monitor.scraper.list_threads()
        LOGGER.info("Fetched %d threads", len(threads))
        for t in threads[:10]:
            LOGGER.info("  - %s", t.title)
        for t in threads:
            if title_matches(t.title, keyword):
                LOGGER.info("MATCH: %s -> %s", t.title, t.url)
                monitor._handle_thread(t)
        return 0

    monitor.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
