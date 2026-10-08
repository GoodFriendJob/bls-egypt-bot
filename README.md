# BLS Spain Egypt — Appointment Bot

Monitors BLS Spain Egypt visa appointment availability for **Cairo** and
**Alexandria** (Short Stay / Tourist), books a slot automatically when one
appears, and notifies you on Telegram. Ships with an Arabic/English RTL
dashboard.

---

## 1. What it does

| Step | Behaviour |
|---|---|
| Login | Two-step email → password, honeypot-safe, session cached in `session/cookies.json` |
| Monitor | Polls each location every `poll_interval` seconds |
| Book | On detection: selects slot, fills applicant form, uploads documents |
| OTP | Auto-read from email over IMAP, or sent to you on Telegram for SMS |
| Manual steps | Liveness/facial verification pauses the bot and alerts you; `/resume` continues |
| Dashboard | `http://127.0.0.1:5000` — status, applicant data, live log, Start/Stop |

---

## 2. Windows setup

```powershell
# 1. Python 3.11+ — check with:
python --version

# 2. From the project folder, create and activate a virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1
# If activation is blocked:
#   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned

# 3. Install dependencies and the Chromium browser
pip install -r requirements.txt
python -m playwright install chromium

# 4. Create your config
copy config.example.yaml config.yaml
notepad config.yaml

# 5. Put the client documents in place
#    docs\passport.pdf   and   docs\photo.jpg

# 6. Run
python bot.py
```

### Useful flags

```powershell
python bot.py --headful          # visible browser — use this for first run / debugging
python bot.py --once             # one availability check, then exit
python bot.py --no-dashboard     # bot only, no Flask UI
python bot.py --config other.yaml
```

Stop the bot with **Ctrl+C**, the dashboard **Stop** button, or Telegram **/stop**.

---

## 3. VPS setup (Ubuntu 22.04 / Debian 12)

An Egyptian VPS is recommended — the portal is accessed from Egypt.

```bash
# 1. System packages
sudo apt update && sudo apt install -y python3 python3-venv python3-pip git

# 2. Project
git clone <your-repo-url> bls-egypt-bot && cd bls-egypt-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. Chromium + its system libraries
playwright install --with-deps chromium

# 4. Config + documents
cp config.example.yaml config.yaml
nano config.yaml          # headless: true on a VPS
# upload docs/passport.pdf and docs/photo.jpg (scp / sftp)

# 5. Test run
python bot.py --once
```

### Run it 24/7 with systemd

```bash
sudo nano /etc/systemd/system/bls-bot.service
```

```ini
[Unit]
Description=BLS Spain Egypt Appointment Bot
After=network-online.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/home/YOUR_USER/bls-egypt-bot
ExecStart=/home/YOUR_USER/bls-egypt-bot/.venv/bin/python bot.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now bls-bot
sudo systemctl status bls-bot
journalctl -u bls-bot -f        # live logs
```

To reach the dashboard from your own machine, tunnel it rather than exposing the
port (there is no authentication on the UI):

```bash
ssh -L 5000:127.0.0.1:5000 YOUR_USER@YOUR_VPS_IP
# then open http://127.0.0.1:5000 locally
```

---

## 4. Configuration

Every field is documented inline in [config.example.yaml](config.example.yaml).
The ones you must set:

| Key | Notes |
|---|---|
| `bls.email` / `bls.password` | BLS portal account |
| `applicant.*` | Full name, passport number, DOB (`DD/MM/YYYY`), phone, email |
| `applicant.documents.*` | Paths relative to the project root |
| `telegram.token` | From [@BotFather](https://t.me/BotFather) |
| `telegram.chat_id` | From [@userinfobot](https://t.me/userinfobot) |
| `otp.imap_user` / `otp.imap_pass` | Gmail: use an **App Password**, not your account password |

`poll_interval: 90` is a sensible default. Going much below ~60s raises the
chance of being rate-limited or flagged.

---

## 5. Telegram commands

| Command | Effect |
|---|---|
| `/status` | Current monitoring status per location |
| `/resume` | Continue after a manual step (liveness, captcha, upload) |
| `/stop` | Stop the bot gracefully |
| `/help` | Command list |

Alerts sent to you: `SLOT_FOUND`, `MANUAL_REQUIRED`, `APPOINTMENT_CONFIRMED`,
`ERROR`, and a `STATUS` heartbeat every 6 hours (`telegram.heartbeat_hours`).

If the SMS OTP method is configured, reply to the bot's chat with the digits and
it will type them into the portal.

---

## 6. Project layout

```
bls-egypt-bot/
├── bot.py                   # entry point: config, logging, dashboard, loop
├── config.example.yaml      # documented template → copy to config.yaml
├── requirements.txt
├── modules/
│   ├── auth.py              # login + session persistence (honeypot-safe)
│   ├── monitor.py           # polling loop per location
│   ├── booking.py           # slot selection + applicant form
│   ├── documents.py         # file uploads (input + drag-and-drop fallback)
│   ├── notifier.py          # Telegram alerts + command listener
│   ├── otp.py               # IMAP auto-read / Telegram fallback
│   ├── state.py             # thread-safe shared state for the dashboard
│   └── utils.py             # delays, visibility checks, screenshots, retries
├── dashboard/
│   ├── app.py               # Flask app + JSON API
│   ├── templates/index.html # RTL/LTR UI with language toggle
│   └── static/style.css
├── session/cookies.json     # saved login (gitignored)
├── logs/bot.log             # rotating log (gitignored)
├── logs/screenshots/        # auto-captured on unexpected pages
└── docs/                    # client documents (gitignored)
```

---

## 7. Anti-bot handling

The login page renders **10 `input[type="text"]` fields**; nine are honeypots.
**Ids, names, _and the real field's position_ are all regenerated on every page
load** (confirmed: Field 0 on one load, Field 3 on the next). Consequently:

- The only stable discriminator is the wrapper: every input sits in a
  `div.mb-3`, and only the real field's wrapper computes to `display: block` —
  honeypot wrappers are `display: none`. This is what
  `utils.find_real_input()` checks, and it is how every field is located.
- **Never** `.first`, never a fixed index, never an id or name.
- A computed-visibility fallback scan (rejecting `display:none`, `opacity:0`,
  clipped and offscreen elements) runs only if the wrapper check finds nothing.
- `playwright-stealth` is applied to every context and page.
- All actions are spaced by random 0.5–2s delays, and text is typed per
  keystroke rather than bulk-filled.
- Unexpected pages are screenshotted to `logs/screenshots/` and alerted.

See the `Portal Research Findings` section of [CLAUDE.md](CLAUDE.md) for the raw
DevTools notes.

---

## 8. Still to confirm from the live portal

These are implemented against the documented flow but **not yet verified** — they
are marked with `TODO(unconfirmed)` in the source:

| Area | File | What to capture |
|---|---|---|
| Password step markup | [modules/auth.py](modules/auth.py) | Is the real box `input[type="password"]` or a text input? Are honeypots present? Submit button id? |
| Post-login landing page | [modules/auth.py](modules/auth.py) | Real account URL + an element unique to an authenticated session |
| Appointment page | [modules/booking.py](modules/booking.py), [modules/monitor.py](modules/monitor.py) | URL, location/visa dropdowns, calendar widget, how an available day is marked |
| Applicant form | [modules/booking.py](modules/booking.py) | Exact field labels; whether honeypots appear here too |
| Upload step | [modules/documents.py](modules/documents.py) | Same page or separate; number of inputs and their labels |
| OTP screen | [modules/otp.py](modules/otp.py) | Single input vs split digit boxes |

Run `python bot.py --headful --once` and watch the browser to fill these in.

---

## 9. Troubleshooting

| Symptom | Fix |
|---|---|
| `portal returned HTTP 403 ... (server: awselb/2.0)` | **The IP is blocked, not the bot.** See §11 — you need an Egyptian IP |
| `Config not found` | `copy config.example.yaml config.yaml` |
| `refusing to start — fix the config problems` | Read the warnings above it; `--ignore-config-warnings` overrides |
| Browser never launches | `python -m playwright install chromium` |
| Login fails immediately | Run `--headful` and watch; check for a captcha, verify credentials |
| `Field 0 was not fillable` in the log | BLS changed the DOM order — re-inspect and update `auth.py` |
| No Telegram messages | Send `/start` to your bot once; re-check `token` and `chat_id` |
| OTP never arrives | Gmail needs an App Password; confirm `otp.sender_contains` matches the real sender |
| Dashboard not loading | Check the port is free; `--no-dashboard` to rule it out |

Logs: `logs/bot.log`. Screenshots of unexpected pages: `logs/screenshots/`.

---

## 10. Run logs — collecting them from the Egypt machine

Every run writes a complete transcript, so a run on one machine can be reviewed
on another.

**What gets saved automatically, every run:**

| Artifact | Path |
|---|---|
| Full terminal transcript (one file per run) | `logs/run_YYYYMMDD_HHMMSS.txt` |
| Rolling combined log | `logs/bot.log` |
| Screenshots — captcha attempts, login failures, HTTP errors, unexpected pages | `logs/screenshots/` |
| Page/DOM dumps on captcha or markup problems | `logs/dom/` |
| CAPTCHA grid images sent to the vision model | `logs/captcha_samples/` |

The transcript mirrors stdout **and** stderr, so it also captures output that
does not go through the logger (the Flask banner, raw tracebacks, library
warnings). Colour codes are stripped so the file stays readable.

**Workflow**

1. Egypt PC: run `start_headful.bat` (or `start.bat`)
2. Logs and screenshots are written automatically — nothing to do
3. Egypt PC: run `push_logs.bat`
4. Other PC: `git pull`, then read `logs/run_*.txt`

`push_logs.bat` resolves its own folder, so it works wherever the repo is cloned.
First-time git setup on the Egypt machine:

```bat
git init
git remote add origin YOUR_REPO_URL
git branch -M main
git add .
git commit -m "initial"
git push -u origin main
```

> **Keep this repository private.** `logs/` is deliberately **not** ignored so
> the artifacts can be shared — but screenshots and DOM dumps capture whatever
> was on screen, which includes applicant name, passport number and date of
> birth once the booking form is reached. `config.yaml`, `session/` and `docs/`
> remain ignored and are never committed.

### How the CAPTCHA is solved

The BLS login CAPTCHA is a 3x3 grid; the prompt says "Please select all boxes
with number NNN". Two separate tricks have to be beaten:

**The prompt** — 31 prompts are stacked at one position
(`.box-label { position:absolute; top:20px }`), all with a *different* number.
30 of them are painted in the container's own background colour (`#F0FFF0`) and
only one keeps readable dark text. The bot picks the label with the highest
text/background **contrast ratio** — literally "the one a human can read".
Document order is useless: on a live run the real prompt sat at index 18 while
index 0 held a decoy.

**The tiles** — each tile is a lone base64 `<img>` with no text, `alt` or
`data-*` attribute, and the images are unique on every load, so neither DOM
scraping nor a hash lookup can work. The digits exist only as pixels. The bot
screenshots just the grid and asks **GPT-4o vision** which positions show the
target number, then maps those positions back to tile ids and clicks them.

Set `openai.api_key` in `config.yaml` to enable this. Without a key the bot
falls back to pausing and asking for a manual solve over Telegram. Up to 3
attempts are made; a rejected answer reloads the grid with a new number.

> Grid screenshots are clipped to the tiles alone — the email address and other
> account details on that page are deliberately excluded, so nothing
> identifying is sent to the API or committed to `logs/captcha_samples/`.

### What a captcha attempt records

Each attempt logs a block containing the target number, how many tiles were
visible, every tile's id and resolved number, which tiles matched, a per-click
`img-selected` confirmation, the `SelectedImages` value before submit, and the
outcome (`SOLVED` / `REJECTED` / `ABORTED`). A screenshot is taken before each
submit and after each rejection, so the log and the images can be read together.

## 11. The portal blocks non-Egyptian IPs — confirmed

Verified 2026-10-08. `https://egypt.blsspainglobal.com/` returns:

```
HTTP/1.1 403 Forbidden
Server: awselb/2.0
```

to requests from outside Egypt. Confirmed characteristics:

- It is **IP-level**, applied at BLS's AWS load balancer *before* the app. The
  403 body is a bare nginx-style error document — no login form, no honeypots,
  nothing to automate.
- It is **not** browser fingerprinting. The identical 403 comes back from a plain
  `urllib` request with no browser involved, so stealth settings cannot help.
- Datacenter/hosting ASNs are blocked even outside Egypt — a US hosting IP
  (B2 Net Solutions) was rejected.

**What works:** running from an Egyptian residential IP — the client's own
machine, an Egyptian VPS, or ProtonVPN with the SecureCore Egypt server (the
setup CLAUDE.md specifies for development).

To route through a proxy instead, fill in `config.yaml`:

```yaml
proxy:
  enabled: true
  server: "http://HOST:PORT"      # or socks5://HOST:PORT
  username: ""
  password: ""
```

The bot now detects this state explicitly: `auth._goto()` checks the HTTP status
on every navigation and raises `PortalUnreachableError` immediately rather than
retrying a login against an error page.

## 12. Security notes

- `config.yaml`, `session/`, `logs/` and `docs/` are gitignored — never commit them.
- No credentials appear anywhere in the source; everything comes from `config.yaml`.
- The dashboard has no authentication and binds to `127.0.0.1` by default. Do not
  expose it publicly — use the SSH tunnel shown above.
- Telegram commands are accepted only from the configured `chat_id`.
