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
import socket
import ssl
import sys
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Dict, List, Optional, Set

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
# IPS 4.x exposes an RSS feed at "<forum_url>.xml/". Much lighter than HTML.
RSS_URL = FORUM_URL.rstrip("/") + ".xml/"

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

# Pre-warm offsets (seconds before window start).
HTTP_PREWARM_SECONDS = 30   # fetch forum once to warm TCP/TLS + probe RSS
SMTP_PREWARM_SECONDS = 10   # open Gmail SMTP + AUTH LOGIN so first send is instant

# How many RSS fetches in a row may fail before we permanently flip to HTML
# for the rest of the current window. Prevents a dead CDN cache-shard from
# eating the whole 5 minutes.
RSS_FAIL_THRESHOLD = 3

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
    # Tightened defaults for high-traffic windows: 6 workers staggered across a
    # 1s interval (~167ms effective rate). Per-request timeout is 8s — raised
    # from 3s after a real drop showed Cloudflare+origin easily crossing 5s
    # under load, which caused every single poll to give up.
    poll_workers: int = 6
    poll_interval_seconds: float = 1.0
    http_timeout_seconds: float = 8.0
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
    """Fetches the forum index. Prefers the RSS feed (lighter, faster) when
    the endpoint is reachable; falls back to parsing the HTML page.

    All real fetches append a ``?_=<ts>_<n>`` cache-buster so Cloudflare's
    edge cache can't serve us a stale feed during the drop window (the
    observed failure mode on 2026-04-15's Wednesday drop).
    """

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
        # None = not yet probed; True/False after warm_up() / first RSS attempt.
        self._rss_works: Optional[bool] = None
        # In-window RSS health tracking. Reset on each window open.
        self._rss_consecutive_fails: int = 0
        self._use_html_override: bool = False
        self._req_counter: int = 0

    # ---- lifecycle ------------------------------------------------------

    def reset_window_state(self) -> None:
        """Clear in-window RSS health counters. Called at each window open
        so an HTML-override from a previous window doesn't stick."""
        self._rss_consecutive_fails = 0
        self._use_html_override = False

    def _bust(self, url: str) -> str:
        """Append a unique cache-buster query parameter so Cloudflare can't
        serve us a cached copy. Millisecond ts + per-scraper counter keeps
        URLs unique even across workers in the same millisecond."""
        self._req_counter += 1
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}_={int(time.time() * 1000)}{self._req_counter}"

    # ---- low-level fetch ------------------------------------------------

    def fetch(self, url: str, retries: int = 2) -> Optional[str]:
        """GET with up to `retries` retries on network error."""
        last_exc: Optional[Exception] = None
        attempts = max(1, retries + 1)
        for attempt in range(attempts):
            try:
                r = self.session.get(url, timeout=self.timeout)
            except (requests.RequestException, socket.timeout) as exc:
                last_exc = exc
                continue
            if r.status_code == 200:
                return r.text
            LOGGER.warning("GET %s -> HTTP %s", url, r.status_code)
            return None
        LOGGER.warning(
            "GET %s failed after %d attempts: %s", url, attempts, last_exc
        )
        return None

    # ---- warm-up / RSS probing -----------------------------------------

    def warm_up(self) -> None:
        """Pre-establish TCP/TLS to fohcigars and decide RSS vs HTML path.
        Called once at T-30s before the window opens so the first in-window
        request skips the DNS/TLS handshake."""
        LOGGER.info("Pre-warm: probing RSS and warming HTTP connection")
        self._probe_rss()
        # Issue one HTML fetch too so both paths keep a warm keep-alive slot.
        self.fetch(FORUM_URL)

    def _probe_rss(self) -> bool:
        try:
            r = self.session.get(self._bust(RSS_URL), timeout=self.timeout)
        except (requests.RequestException, socket.timeout) as exc:
            LOGGER.info("RSS probe: network error (%s); using HTML fallback", exc)
            self._rss_works = False
            return False
        if r.status_code != 200:
            LOGGER.info("RSS probe: HTTP %s; using HTML fallback", r.status_code)
            self._rss_works = False
            return False
        if "<rss" not in r.text[:2048] and "<feed" not in r.text[:2048]:
            LOGGER.info("RSS probe: unexpected body; using HTML fallback")
            self._rss_works = False
            return False
        try:
            ET.fromstring(r.text)
        except ET.ParseError as exc:
            LOGGER.info("RSS probe: parse error (%s); using HTML fallback", exc)
            self._rss_works = False
            return False
        LOGGER.info("RSS probe: OK, using %s", RSS_URL)
        self._rss_works = True
        return True

    # ---- thread listing -------------------------------------------------

    def list_threads(self) -> List[Thread]:
        """Return current thread list using the preferred source.

        A cheap substring pre-filter on the raw response skips full parsing
        when no "24:24" appears anywhere — which is ~99.9% of polls. DEBUG
        logging emits one line per poll showing bytes and pre-filter result
        so silent "no match" isn't opaque in the logs.
        """
        if self._rss_works is None:
            # First call without explicit warm-up: probe lazily.
            self._probe_rss()

        use_rss = bool(self._rss_works) and not self._use_html_override

        if use_rss:
            body = self.fetch(self._bust(RSS_URL))
            if not body:
                self._rss_consecutive_fails += 1
                if (self._rss_consecutive_fails >= RSS_FAIL_THRESHOLD
                        and not self._use_html_override):
                    LOGGER.warning(
                        "RSS failed %d times in a row; switching to HTML "
                        "for the rest of this window",
                        self._rss_consecutive_fails,
                    )
                    self._use_html_override = True
                return []
            # RSS actually returned something — reset the window-fail counter.
            self._rss_consecutive_fails = 0
            has_match = "24:24" in body or "24\uff1a24" in body
            LOGGER.debug(
                "RSS %d bytes; 24:24_in_body=%s", len(body), has_match
            )
            if not has_match:
                return []
            threads = self._parse_rss(body)
            LOGGER.debug("RSS parsed %d threads", len(threads))
            return threads

        body = self.fetch(self._bust(FORUM_URL))
        if not body:
            return []
        has_match = "24:24" in body or "24\uff1a24" in body
        LOGGER.debug(
            "HTML %d bytes; 24:24_in_body=%s", len(body), has_match
        )
        if not has_match:
            return []
        threads = self._parse_html(body)
        LOGGER.debug("HTML parsed %d threads", len(threads))
        return threads

    # ---- parsers --------------------------------------------------------

    def _parse_rss(self, xml_text: str) -> List[Thread]:
        threads: List[Thread] = []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            LOGGER.warning("RSS parse failed: %s (falling back to HTML)", exc)
            self._rss_works = False
            return []

        # RSS 2.0 path: <rss><channel><item><title>/<link>
        for item in root.iter("item"):
            title_el = item.find("title")
            link_el = item.find("link")
            title = (title_el.text or "").strip() if title_el is not None else ""
            url = (link_el.text or "").strip() if link_el is not None else ""
            if title and url:
                threads.append(Thread(title=title, url=url))

        # Atom path (some IPS builds): <feed><entry><title>/<link href="...">
        if not threads:
            atom_ns = "{http://www.w3.org/2005/Atom}"
            for entry in root.iter(atom_ns + "entry"):
                title_el = entry.find(atom_ns + "title")
                link_el = entry.find(atom_ns + "link")
                title = (title_el.text or "").strip() if title_el is not None else ""
                url = (link_el.get("href") or "").strip() if link_el is not None else ""
                if title and url:
                    threads.append(Thread(title=title, url=url))
        return threads

    def _parse_html(self, html: str) -> List[Thread]:
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
    """SMTP sender with a reusable, pre-warmable connection.

    - ``prewarm()`` opens the SSL/TLS channel and runs AUTH LOGIN *before* the
      monitoring window so the first real send pays zero handshake cost.
    - ``prebuild_bodies()`` pre-serialises one MIME byte blob per cigar so
      ``send_order()`` only does a ``sendmail()`` round-trip on match.
    - ``send_order()`` uses the pre-warmed connection under a single lock
      (SMTP is not thread-safe) and transparently reconnects if the socket
      died while waiting.
    - ``close_prewarm()`` tears the connection down after the window.
    """

    # Drop the warm connection if it's been idle this long - Gmail boots
    # sessions around ~10 minutes but we're paranoid because the whole point
    # of pre-warm is *fresh* state going into the window.
    _MAX_IDLE_SECONDS = 300.0

    def __init__(self, config: AppConfig) -> None:
        self.cfg = config
        self._lock = threading.Lock()
        self._conn: Optional[smtplib.SMTP] = None
        self._last_used: float = 0.0
        self._prebuilt: Dict[str, bytes] = {}
        self._from_addr = f'"{self.cfg.sender_name}" <{self.cfg.smtp_user}>'

    # ---- connection management -----------------------------------------

    def _open(self) -> smtplib.SMTP:
        if self.cfg.smtp_use_ssl:
            context = ssl.create_default_context()
            s = smtplib.SMTP_SSL(
                self.cfg.smtp_host, self.cfg.smtp_port, context=context, timeout=15
            )
        else:
            s = smtplib.SMTP(self.cfg.smtp_host, self.cfg.smtp_port, timeout=15)
            s.ehlo()
            s.starttls(context=ssl.create_default_context())
            s.ehlo()
        s.login(self.cfg.smtp_user, self.cfg.smtp_password)
        return s

    def _ensure_connection(self) -> smtplib.SMTP:
        """Return a live, logged-in SMTP connection. Caller must hold _lock."""
        if self._conn is not None:
            idle = time.monotonic() - self._last_used
            if idle < self._MAX_IDLE_SECONDS:
                try:
                    self._conn.noop()
                    return self._conn
                except (smtplib.SMTPException, OSError):
                    LOGGER.info("SMTP NOOP failed, reopening connection")
            else:
                LOGGER.info("SMTP idle %.0fs, reopening connection", idle)
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        LOGGER.debug("Opening SMTP connection to %s:%d",
                     self.cfg.smtp_host, self.cfg.smtp_port)
        self._conn = self._open()
        return self._conn

    def prewarm(self) -> None:
        """Open + AUTH the SMTP connection ahead of the window (idempotent)."""
        with self._lock:
            if self._conn is not None:
                return
            LOGGER.info("Pre-warm: opening SMTP + AUTH LOGIN")
            try:
                self._conn = self._open()
                self._last_used = time.monotonic()
            except Exception:
                LOGGER.exception("SMTP pre-warm failed (will retry on first send)")
                self._conn = None

    def close_prewarm(self) -> None:
        """Close the pre-warmed connection (safe if none open)."""
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.quit()
            except Exception:
                try:
                    self._conn.close()
                except Exception:
                    pass
            self._conn = None

    # ---- message building ----------------------------------------------

    def _build(self, subject: str, body: str) -> MIMEMultipart:
        msg = MIMEMultipart()
        msg["From"] = self._from_addr
        msg["To"] = ", ".join(self.cfg.recipients)
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))
        return msg

    def prebuild_bodies(self) -> None:
        """Serialise one MIME byte blob per cigar once at startup."""
        prebuilt: Dict[str, bytes] = {}
        for cigar in self.cfg.cigars:
            subject = self.cfg.subject_template.format(cigar=cigar)
            body = self.cfg.body_template.format(cigar=cigar)
            prebuilt[cigar] = self._build(subject, body).as_bytes()
        self._prebuilt = prebuilt
        LOGGER.info("Pre-built %d MIME messages", len(prebuilt))

    # ---- sending -------------------------------------------------------

    def send_order(self, cigar: str) -> str:
        """Send one pre-built order email. Returns the subject on success."""
        data = self._prebuilt.get(cigar)
        subject = self.cfg.subject_template.format(cigar=cigar)
        with self._lock:
            smtp = self._ensure_connection()
            try:
                if data is not None:
                    smtp.sendmail(self.cfg.smtp_user, self.cfg.recipients, data)
                else:
                    # Fallback if prebuild hasn't run (e.g. --test paths).
                    body = self.cfg.body_template.format(cigar=cigar)
                    smtp.send_message(self._build(subject, body))
            except (smtplib.SMTPServerDisconnected, smtplib.SMTPConnectError,
                    OSError) as exc:
                LOGGER.warning("SMTP send hit connection error (%s); retrying once", exc)
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
                smtp = self._ensure_connection()
                if data is not None:
                    smtp.sendmail(self.cfg.smtp_user, self.cfg.recipients, data)
                else:
                    body = self.cfg.body_template.format(cigar=cigar)
                    smtp.send_message(self._build(subject, body))
            self._last_used = time.monotonic()
        return subject

    def send_test(self) -> None:
        with self._lock:
            smtp = self._ensure_connection()
            msg = self._build(
                "[24:24] SMTP test",
                f"Test email from monitor at Beijing time "
                f"{beijing_now():%Y-%m-%d %H:%M:%S}.\n",
            )
            smtp.send_message(msg)
            self._last_used = time.monotonic()
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
        self.mailer.prebuild_bodies()
        # One seen-store keyed by "<thread_url>::<cigar_lower>". A cigar is
        # marked seen only after its email is successfully delivered, so
        # transient SMTP failures are retried on the next poll.
        self.seen = SeenStore(config.dedup_state_path)
        self._logged_matches: Set[str] = set()
        self._logged_matches_lock = threading.Lock()
        self.stop_event = threading.Event()
        # Signalled once every cigar for a matched thread has been sent, so
        # workers can exit the poll loop early instead of burning the window.
        self.all_sent_event = threading.Event()

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
        if LOGGER.isEnabledFor(logging.DEBUG):
            matched = sum(1 for t in threads if title_matches(t.title, keyword))
            LOGGER.debug(
                "[w%d] fetched=%d matched=%d keyword=%s",
                worker_id, len(threads), matched, keyword,
            )
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

        # Mailer.send_order() serialises sends on its own lock and reuses the
        # pre-warmed connection, so we can call it directly per cigar.
        for cigar in outstanding:
            key = self._key(thread.url, cigar)
            # Recheck in case another worker already sent this one.
            if self.seen.contains(key):
                continue
            try:
                subject = self.mailer.send_order(cigar)
            except smtplib.SMTPException:
                LOGGER.exception("Failed to send order for %s", cigar)
                continue
            except Exception:
                LOGGER.exception("Unexpected error sending order for %s", cigar)
                continue
            self.seen.add(key)
            LOGGER.info("Emailed: %s", subject)

        # If every cigar for this thread is now sent, tell workers to stop.
        remaining = [
            c for c in self.cfg.cigars
            if not self.seen.contains(self._key(thread.url, c))
        ]
        if not remaining:
            self.all_sent_event.set()

    # ---- worker threads --------------------------------------------------

    def _should_stop(self) -> bool:
        return (
            self.stop_event.is_set()
            or self.all_sent_event.is_set()
            or not in_window()
        )

    def _worker(self, worker_id: int, start_at: float) -> None:
        """Poll in a loop until the window closes or all cigars are sent.

        ``start_at`` is an absolute ``time.monotonic()`` timestamp — workers
        line up against the true window boundary (not "now + offset") so the
        first batch of polls fires at the top of the window.
        """
        delay = max(0.0, start_at - time.monotonic())
        if delay:
            if self.stop_event.wait(delay):
                return
        while not self._should_stop():
            start = time.monotonic()
            try:
                self._poll_once(worker_id)
            except Exception:
                LOGGER.exception("Poll error in worker %d", worker_id)
            if self._should_stop():
                return
            elapsed = time.monotonic() - start
            wait = max(0.0, self.cfg.poll_interval_seconds - elapsed)
            if wait:
                # Wake on either stop signal OR all-sent signal.
                if self.stop_event.wait(wait):
                    return
                if self.all_sent_event.is_set():
                    return

    def _window_start_monotonic(self) -> float:
        """Monotonic timestamp corresponding to today's window open time.
        If we're already inside the window, returns ``now`` (start immediately).
        """
        now = beijing_now()
        window = WINDOWS.get(now.weekday())
        if not window:
            return time.monotonic()
        (sh, sm, ss), _ = window
        start_dt = now.replace(hour=sh, minute=sm, second=ss, microsecond=0)
        delta = (start_dt - now).total_seconds()
        return time.monotonic() + max(0.0, delta)

    def run_window(self) -> None:
        LOGGER.info("Entering monitor window at %s", beijing_now())
        self.all_sent_event.clear()
        # Reset in-window RSS health tracking so a previous window's
        # HTML-override doesn't carry over.
        self.scraper.reset_window_state()
        workers = max(1, self.cfg.poll_workers)
        stagger = self.cfg.poll_interval_seconds / workers
        window_start = self._window_start_monotonic()
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [
                ex.submit(self._worker, i, window_start + stagger * i)
                for i in range(workers)
            ]
            for f in futures:
                f.result()
        LOGGER.info("Exited monitor window at %s", beijing_now())
        # Close the pre-warmed SMTP so we don't hold a Gmail session for hours.
        self.mailer.close_prewarm()

    def run_forever(self) -> None:
        LOGGER.info("Monitor started. Beijing time: %s", beijing_now())
        http_warmed = False
        smtp_warmed = False
        try:
            while not self.stop_event.is_set():
                if in_window():
                    self.run_window()
                    http_warmed = False
                    smtp_warmed = False
                    continue
                wait = seconds_until_next_window()
                LOGGER.info(
                    "Next window in %.0fs (at %s BJT)",
                    wait,
                    (beijing_now() + timedelta(seconds=wait)).strftime(
                        "%a %Y-%m-%d %H:%M:%S"
                    ),
                )
                # Sleep in chunks so pre-warm checkpoints fire on time.
                while wait > 0 and not self.stop_event.is_set():
                    if not http_warmed and wait <= HTTP_PREWARM_SECONDS:
                        try:
                            self.scraper.warm_up()
                        except Exception:
                            LOGGER.exception("HTTP pre-warm failed")
                        http_warmed = True
                    if not smtp_warmed and wait <= SMTP_PREWARM_SECONDS:
                        try:
                            self.mailer.prewarm()
                        except Exception:
                            LOGGER.exception("SMTP pre-warm failed")
                        smtp_warmed = True
                    # Choose chunk size to hit the next prewarm boundary.
                    next_boundary = 30.0
                    if not http_warmed and wait > HTTP_PREWARM_SECONDS:
                        next_boundary = min(next_boundary, wait - HTTP_PREWARM_SECONDS)
                    if not smtp_warmed and wait > SMTP_PREWARM_SECONDS:
                        next_boundary = min(next_boundary, wait - SMTP_PREWARM_SECONDS)
                    chunk = max(0.1, min(wait, next_boundary))
                    self.stop_event.wait(chunk)
                    wait -= chunk
        except KeyboardInterrupt:
            LOGGER.info("Interrupted; shutting down.")
            self.stop_event.set()
        finally:
            self.mailer.close_prewarm()


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
        poll_workers=int(data.get("poll_workers", 6)),
        poll_interval_seconds=float(data.get("poll_interval_seconds", 1.0)),
        http_timeout_seconds=float(data.get("http_timeout_seconds", 8.0)),
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
        try:
            threads = monitor.scraper.list_threads()
            LOGGER.info("Fetched %d threads", len(threads))
            for t in threads[:10]:
                LOGGER.info("  - %s", t.title)
            for t in threads:
                if title_matches(t.title, keyword):
                    monitor._handle_thread(t, worker_id=0)
        finally:
            monitor.mailer.close_prewarm()
        return 0

    monitor.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
