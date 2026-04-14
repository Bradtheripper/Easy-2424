# FOHCigars 24:24 Monitor

Watches `https://www.fohcigars.com/forum/forum/1-cigars-discussion-forum-quotthe-water-holequot/`
during the Beijing-time windows below. As soon as a matching `24:24` thread
is detected on the forum index, sends one order email per cigar from a fixed
list using a configurable template. **Thread contents are not fetched** —
the alert is based purely on the index title.

| Weekday (Beijing) | Window       | Title must contain          |
| ----------------- | ------------ | --------------------------- |
| Tuesday           | 08:27-08:32  | `24:24` + `Tuesday`         |
| Wednesday         | 08:27-08:32  | `24:24` + `Wednesday`       |
| Thursday          | 08:27-08:32  | `24:24` + `Thursday` / `Today` / `Weekend` |
| Friday            | 06:27-06:32  | `24:24` + `Friday` / `Weekend` / `Today`   |

Multi-threaded inside the window: 6 workers polling every 1 s with staggered
start offsets aligned to the window's top-of-second ≈ one HTTP request every
~167 ms. Performance add-ons for the high-traffic drop:

- **RSS-first**: prefers the forum's `.xml/` feed (lighter, parses much
  faster than BeautifulSoup on the HTML page). Falls back to HTML
  automatically if the feed is missing or returns non-XML.
- **Raw-text pre-filter**: the body is scanned for the literal `24:24`
  before any parsing, so off-cycle polls cost only a `str.find()`.
- **HTTP pre-warm at T-30 s**: one fetch + RSS probe so the first
  in-window request skips DNS / TLS / keep-alive cold start.
- **SMTP pre-warm at T-10 s**: opens the SSL channel and runs AUTH LOGIN
  against Gmail so the first order email is a plain `sendmail()` round-trip.
- **Pre-built MIME**: one MIME byte blob per cigar is serialised at
  startup; sends use `smtp.sendmail(from, to, bytes)` (no re-encoding).
- **All-sent fast exit**: once every cigar for the matched thread is sent,
  remaining workers stop immediately instead of burning out the window.

All emails go out under a single persistent SMTP connection per window;
sends are serialised by a lock so concurrent workers can't double-send.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp config.example.yaml config.yaml
# edit config.yaml: Gmail account + App Password + recipients
```

### Gmail App Password

Regular Gmail passwords do not work over SMTP. Generate a 16-character App
Password: <https://support.google.com/accounts/answer/185833>. Paste it into
`smtp.password` (no spaces).

## Verify

```bash
# 1. Send a dummy email - confirms SMTP works.
python monitor.py --test-email

# 2. Fetch the forum once right now (ignores the time window) and show
#    what would match for today's weekday keyword.
python monitor.py --now -v
```

## Run for real

```bash
python monitor.py
```

The process is long-running. It sleeps between windows, pre-warms HTTP at
T-30 s and SMTP at T-10 s, then spins up the worker pool at the top of the
window (08:27-08:32 Beijing time Tue-Thu, 06:27-06:32 Fri).

### systemd (example)

```ini
# /etc/systemd/system/fohc24.service
[Unit]
Description=FOHC 24:24 Monitor
After=network-online.target

[Service]
Type=simple
User=fohc
WorkingDirectory=/opt/fohc24
ExecStart=/opt/fohc24/.venv/bin/python /opt/fohc24/monitor.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now fohc24
journalctl -u fohc24 -f
```

## Email template

`subject_template` and `body_template` in `config.yaml` both support the
`{cigar}` placeholder. Each cigar in the `cigars:` list triggers one email
per matching thread, with `{cigar}` substituted by its name.

Default subject: `24:24 <cigar-name>`. Default body is a ready-to-send order
request addressed to Diana; edit `config.yaml` to personalise it.

## Notes

- The scraper prefers the IPS RSS feed (`<forum-url>.xml/`) and falls back
  to HTML with IPS-style selectors plus a generic `a[href*="/forum/topic/"]`
  catch-all, so small theme changes should not break it.
- When a match is detected, all emails for that match are sent through a
  single pre-warmed SMTP connection (one login, N messages) to minimise
  latency and avoid Gmail's per-connection throttles. A NOOP + lazy
  reconnect guards against a dropped socket.
- `dedup_state_path` records already-sent `(thread_url, cigar)` pairs. A
  cigar is marked "sent" only after SMTP returns success, so transient
  failures are retried on the next poll. Delete the file to reset.
- Testing with `--now` will fire real emails — consider trimming `cigars:`
  or using a throwaway recipient before the first run.
- HTTP timeout is aggressive (3 s) with one fast retry; a single slow poll
  won't eat the whole window.
- Be mindful of the forum's rules and rate limits; ~167 ms between requests
  is still well below human browsing cadence, but don't raise it further.
