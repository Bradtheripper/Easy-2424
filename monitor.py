#!/usr/bin/env python3
"""
FOHCigars 24:24 Forum Monitor.

Monitors the "Water Hole" sub-forum during Beijing-time windows and, as soon
as a matching 24:24 thread appears on the index, sends one order email per
cigar from a fixed list using a configurable body template. Thread contents
are NOT fetched; matching is done purely on the index title.

Monitoring windows (Beijing time):
  Tuesday   08:27-08:32  -> titles containing "24:24" + "Tuesday"
  Wednesday 08:27-08:32  -> titles containing "24:24" + "Wednesday"
  Thursday  08:27-08:32  -> titles containing "24:24" + "Thursday"
                            (or "Today" / "Weekend" variants)
  Friday    06:27-06:32  -> titles containing "24:24" + "Friday"
                            (or "Weekend" / "Today" variants)

Usage:
    cp config.example.yaml config.yaml   # fill in SMTP, recipients, cigars
    python monitor.py                    # long-running loop
    python monitor.py --now              # do one poll immediately (for testing)
    python monitor.py --test-email       # send a dummy email to verify SMTP
"""

from __future__ import annotations

import argparse
import logging
import os
import smtplib
import ssl
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
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

# Per-weekday Beijing-time monitoring windows.
# Python weekday(): Monday=0, Tuesday=1, ..., Sunday=6
# Tue/Wed/Thu fire at 08:27-08:32; Friday fires one hour earlier at 06:27-06:32.
WINDOWS = {
    1: ((8, 27, 0), (8, 32, 0)),  # Tuesday
    2: ((8, 27, 0), (8, 32, 0)),  # Wednesday
    3: ((8, 27, 0), (8, 32, 0)),  # Thursday
    4: ((6, 27, 0), (6, 32, 0)),  # Friday
}

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
class AppConfig:
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_use_ssl: bool
    sender_name: str
    recipients: List[str]
    subject_template: str
    body_template: str
    cigars: List[str]
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
    window = WINDOWS.get(now.weekday())
    if not window:
        return False
    (sh, sm, ss), (eh, em, es) = window
    start = now.replace(hour=sh, minute=sm, second=ss, microsecond=0)
    end = now.replace(hour=eh, minute=em, second=es, microsecond=0)
    return start <= now < end


def seconds_until_next_window(now: Optional[datetime] = None) -> float:
    """Seconds from `now` to the next monitoring window start (Beijing time)."""
    now = now or beijing_now()
    for offset in range(0, 8):
        d = (now + timedelta(days=offset)).date()
        window = WINDOWS.get(d.weekday())
        if not window:
            continue
        (sh, sm, ss), _ = window
        candidate = datetime(d.year, d.month, d.day, sh, sm, ss, tzinfo=BEIJING_TZ)
        if candidate > now:
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


# ---------------------------------------------------------------------------
# Mailer
# ---------------------------------------------------------------------------

class Mailer:
    """SMTP sender. `session()` opens one connection for many messages; all
    sends are serialised by a single lock to avoid concurrent-send races."""

    def __init__(self, config: AppConfig) -> None:
        self.cfg = config
        self._lock = threading.Lock()

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

    def _build(self, subject: str, body: str) -> MIMEMultipart:
        msg = MIMEMultipart()
        msg["From"] = f'"{self.cfg.sender_name}" <{self.cfg.smtp_user}>'
        msg["To"] = ", ".join(self.cfg.recipients)
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))
        return msg

    @contextmanager
    def session(self):
        """Open SMTP once, login once; yield a helper that sends individual
        cigar order emails. Releases the connection on exit."""
        with self._lock:
            with self._open() as smtp:
                smtp.login(self.cfg.smtp_user, self.cfg.smtp_password)
                yield _MailSession(self, smtp)

    def send_test(self) -> None:
        with self._lock:
            with self._open() as s:
                s.login(self.cfg.smtp_user, self.cfg.smtp_password)
                msg = self._build(
                    "[24:24] SMTP test",
                    f"Test email from monitor at Beijing time "
                    f"{beijing_now():%Y-%m-%d %H:%M:%S}.\n",
                )
                s.send_message(msg)
        LOGGER.info("Test email sent to %s", ", ".join(self.cfg.recipients))


class _MailSession:
    """Helper yielded by `Mailer.session()`. Sends one email per cigar under
    the active SMTP connection."""

    def __init__(self, mailer: "Mailer", smtp: smtplib.SMTP) -> None:
        self._mailer = mailer
        self._smtp = smtp

    def send_order(self, cigar: str) -> str:
        cfg = self._mailer.cfg
        subject = cfg.subject_template.format(cigar=cigar)
        body = cfg.body_template.format(cigar=cigar)
        self._smtp.send_message(self._mailer._build(subject, body))
        return subject


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

    def contains(self, key: str) -> bool:
        with self._lock:
            return key in self._data

    def add(self, key: str) -> None:
        with self._lock:
            if key in self._data:
                return
            self._data.add(key)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(key + "\n")


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------

class Monitor:
    def __init__(self, config: AppConfig) -> None:
        self.cfg = config
        self.scraper = ForumScraper(config)
        self.mailer = Mailer(config)
        # One seen-store keyed by "<thread_url>::<cigar_lower>". A cigar is
        # marked seen only after its email is successfully delivered, so
        # transient SMTP failures are retried on the next poll.
        self.seen = SeenStore(config.dedup_state_path)
        self._logged_matches: Set[str] = set()
        self._logged_matches_lock = threading.Lock()
        self.stop_event = threading.Event()

    # ---- helpers ---------------------------------------------------------

    @staticmethod
    def _key(url: str, cigar: str) -> str:
        return f"{url}::{cigar.lower()}"

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
            try:
                self._handle_thread(t, worker_id)
            except Exception:
                LOGGER.exception("Error handling thread %s", t.url)

    def _handle_thread(self, thread: Thread, worker_id: int) -> None:
        # Cheap pre-check outside the SMTP lock: any cigar left to send?
        outstanding = [
            c for c in self.cfg.cigars
            if not self.seen.contains(self._key(thread.url, c))
        ]
        if not outstanding:
            return

        with self._logged_matches_lock:
            if thread.url not in self._logged_matches:
                self._logged_matches.add(thread.url)
                LOGGER.info(
                    "[w%d] MATCH: %s (%d emails to send) -> %s",
                    worker_id, thread.title, len(outstanding), thread.url,
                )

        # Batch all emails under one SMTP login. The Mailer serialises
        # sessions so concurrent workers queue up rather than racing.
        try:
            with self.mailer.session() as session:
                for cigar in outstanding:
                    key = self._key(thread.url, cigar)
                    # Recheck inside the SMTP lock: another worker may have
                    # sent this cigar while we were waiting for the lock.
                    if self.seen.contains(key):
                        continue
                    try:
                        subject = session.send_order(cigar)
                    except smtplib.SMTPException:
                        LOGGER.exception("Failed to send order for %s", cigar)
                        continue
                    self.seen.add(key)
                    LOGGER.info("Emailed: %s", subject)
        except Exception:
            LOGGER.exception("SMTP session failed for %s", thread.url)

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
    cigars = data.get("cigars") or []
    subject_template = data.get("subject_template", "24:24 {cigar}")
    body_template = data.get("body_template")

    if not smtp.get("user") or not smtp.get("password"):
        raise SystemExit("config.yaml: smtp.user and smtp.password are required.")
    if not recipients:
        raise SystemExit("config.yaml: at least one recipient is required.")
    if not cigars:
        raise SystemExit("config.yaml: the `cigars` list is empty.")
    if not body_template:
        raise SystemExit("config.yaml: `body_template` is required.")
    if "{cigar}" not in body_template:
        raise SystemExit(
            "config.yaml: body_template must contain the `{cigar}` placeholder."
        )

    state_path = Path(data.get("dedup_state_path", ".state/seen.txt"))
    return AppConfig(
        smtp_host=smtp.get("host", "smtp.gmail.com"),
        smtp_port=int(smtp.get("port", 465)),
        smtp_user=smtp["user"],
        smtp_password=smtp["password"],
        smtp_use_ssl=bool(smtp.get("use_ssl", True)),
        sender_name=smtp.get("sender_name", "FOHC 24:24 Monitor"),
        recipients=list(recipients),
        subject_template=subject_template,
        body_template=body_template,
        cigars=list(cigars),
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
                monitor._handle_thread(t, worker_id=0)
        return 0

    monitor.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
