# FOHCigars 24:24 Monitor

Watches `https://www.fohcigars.com/forum/forum/1-cigars-discussion-forum-quotthe-water-holequot/`
during the Beijing-time windows below and emails one alert per product as soon
as a matching `24:24` thread appears.

| Weekday (Beijing) | Window       | Title must contain          |
| ----------------- | ------------ | --------------------------- |
| Tuesday           | 08:27-08:32  | `24:24` + `Tuesday`         |
| Wednesday         | 08:27-08:32  | `24:24` + `Wednesday`       |
| Thursday          | 08:27-08:32  | `24:24` + `Thursday` / `Today` / `Weekend` |
| Friday            | 08:27-08:32  | `24:24` + `Friday` / `Weekend` / `Today`   |

Multi-threaded inside the window: 3 workers polling every 2 s with staggered
start offsets ≈ one HTTP request every ~0.67 s.

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

The process is long-running. It sleeps between windows and only spins up the
worker pool inside 08:27-08:32 Beijing time on Tue-Fri.

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

## Notes

- The server uses Invision Power Suite (IPS). The scraper tries a few CSS
  selectors and falls back to any `a[href*="/forum/topic/"]`, so small theme
  changes should not break it.
- Product parsing looks for lines containing a `$XX.XX` price in the first
  post and takes the text before the price as the product name. If parsing
  fails, one fallback email is sent that links to the thread.
- `dedup_state_path` records already-sent thread URLs and product keys. Delete
  the file to reset (e.g. if you want to re-test against an old thread).
- Be mindful of the forum's rules and rate limits; the default poll rate is
  already modest.
