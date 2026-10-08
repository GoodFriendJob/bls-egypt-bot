# BLS Spain Egypt Appointment Bot

## Project Overview
A Python Playwright automation bot that monitors BLS Spain Egypt visa appointment availability for Cairo and Alexandria, automatically books slots when found, and notifies the user via Telegram. Includes an Arabic/English RTL dashboard.

## Target Portal
URL: https://egypt.blsspainglobal.com/
Visa Type: Short Stay / Tourist
Locations: Cairo, Alexandria

## Tech Stack
- Python 3.11+
- Playwright (async) + playwright-stealth
- python-telegram-bot
- Flask (dashboard)
- PyYAML (config)
- loguru (logging)
- schedule or asyncio for polling loop
- imaplib for email OTP reading

## Project Structure
```
bls-egypt-bot/
├── CLAUDE.md
├── config.yaml              # credentials and settings (gitignored)
├── config.example.yaml      # template with all fields, no real values
├── bot.py                   # main async entry point
├── requirements.txt
├── .gitignore
├── session/
│   └── cookies.json         # saved browser session (gitignored)
├── modules/
│   ├── __init__.py
│   ├── auth.py              # login + session persistence
│   ├── monitor.py           # polling loop for Cairo + Alexandria
│   ├── booking.py           # slot selection + applicant form fill
│   ├── documents.py         # file upload automation
│   ├── notifier.py          # Telegram alerts + /resume command listener
│   └── otp.py               # OTP handling (email auto-read + Telegram fallback)
├── dashboard/
│   ├── app.py               # Flask app
│   ├── templates/
│   │   └── index.html       # Arabic/English RTL dashboard
│   └── static/
│       └── style.css
├── logs/
│   └── bot.log
└── docs/                    # client documents (gitignored)
```

## Configuration (config.yaml)
```yaml
bls:
  url: "https://egypt.blsspainglobal.com/"
  email: ""
  password: ""
  locations:
    - cairo
    - alexandria
  visa_type: "Short Stay / Tourist"

applicant:
  full_name: "Fady Melad Sedhom Zabib Sawairas"
  passport_number: "A44403798"
  nationality: "Egyptian"
  dob: "24/08/1989"
  phone: "+201055565536"
  email: "fadysawairas@gmail.com"
  documents:
    passport_scan: "docs/passport.pdf"
    photo: "docs/photo.jpg"

telegram:
  token: ""
  chat_id: ""

otp:
  method: "email"           # "email" or "sms"
  imap_host: "imap.gmail.com"
  imap_port: 993
  imap_user: ""
  imap_pass: ""

poll_interval: 90           # seconds between availability checks
headless: true              # set false for debugging
proxy:
  enabled: false
  server: ""
  username: ""
  password: ""
```

## Anti-Bot Obfuscation — CRITICAL
The BLS login page uses heavy bot-detection techniques:
- **10 honeypot email fields** — all labeled "Email", only one is real and visible
- **Randomized field IDs and names** on every page load — never target by ID
- **Always find the real field by visibility**, not by name or ID:

```python
# Correct approach — find visible input
inputs = await page.query_selector_all('input[type="text"]')
for inp in inputs:
    if await inp.is_visible():
        await inp.fill(email)
        break
```

- The login is **two-step**: email first → submit → then password on next step
- Use `playwright-stealth` on every browser context to reduce fingerprinting
- Use `asyncio` delays between actions to mimic human behavior (0.5–2s random)

## Module Specifications

### auth.py
- Launch Playwright with stealth plugin applied
- Support optional proxy from config.yaml
- Step 1: fill visible email field → click Verify button
- Step 2: fill visible password field → submit
- On success: save session to session/cookies.json via context.storage_state()
- On subsequent runs: load cookies first, navigate to dashboard, check if still logged in
- Re-login automatically if session expired
- Log all auth events via loguru

### monitor.py
- Run an async polling loop every poll_interval seconds
- For each location (cairo, alexandria):
  - Navigate to the appointment availability page
  - Detect whether any slots are available
  - Available = call booking.py immediately
  - Unavailable = log "no slots" with timestamp and continue
- On any page error, timeout, or unexpected redirect: log error, wait 30s, retry
- Update dashboard status on each cycle

### booking.py
- Triggered only when a slot is detected
- Select the location (cairo or alexandria)
- Select visa type: Short Stay / Tourist
- Click the available date/time slot
- Fill all applicant fields from config.yaml
- Upload documents via documents.py
- Handle OTP via otp.py
- Detect liveness/facial verification step → pause + Telegram alert + wait for /resume
- On successful confirmation: send Telegram success alert with appointment details
- Log every action with timestamp

### documents.py
- For standard file inputs: use page.set_input_files(selector, filepath)
- For custom upload widgets: simulate drag-and-drop or JS injection
- If upload step cannot be automated: pause + Telegram alert

### notifier.py
- Send Telegram message to chat_id from config
- Alert types:
  - SLOT_FOUND: location, date, time — sent immediately on detection
  - MANUAL_REQUIRED: reason (liveness / SMS OTP / custom upload) — pause bot
  - APPOINTMENT_CONFIRMED: full appointment details
  - ERROR: error description + auto-retry info
  - STATUS: periodic heartbeat every 6 hours (bot is running)
- Listen for /resume command via Telegram bot polling → resume booking flow
- Listen for /status command → reply with current monitoring status
- Listen for /stop command → gracefully stop the bot

### otp.py
- Check config otp.method
- If "email": poll IMAP inbox for BLS OTP email after OTP prompt appears (retry 10x with 5s delay)
- If "sms": detect OTP prompt → send Telegram alert asking user to reply with code → wait for Telegram reply → fill code
- Timeout after 5 minutes → send error alert

## Dashboard (Flask)
- Runs on localhost:5000
- RTL layout with Arabic/English language toggle
- Arabic: dir="rtl", lang="ar"
- Sections:
  - Monitoring status per location (Cairo / Alexandria) — running / slot found / error
  - Last checked timestamp per location
  - Applicant data summary
  - Live log table (timestamp, location, event, status) — newest first
  - Bot controls: Start / Stop buttons
- Auto-refresh every 30 seconds
- No authentication required (local use only)
- Style: clean, minimal, mobile-friendly

## Error Handling
- All modules use try/except with loguru logging
- On Playwright timeout: retry up to 3 times then send Telegram error alert
- On login failure: retry once, then send Telegram alert and pause
- On unexpected page content: screenshot the page, log it, send Telegram alert
- On network error: wait 60s then retry
- Never crash silently — every error must be logged and alerted

## Acceptance Criteria
1. Bot logs into BLS account and maintains session without manual re-login
2. Monitors Cairo and Alexandria Short Stay / Tourist slots continuously
3. When a slot is detected — selects it and fills applicant info automatically
4. Handles OTP automatically (email) or via Telegram reply (SMS)
5. Pauses and sends Telegram alert for liveness/facial verification
6. Resumes and confirms appointment after manual step completed
7. Automatically uploads applicant documents where BLS form supports it
8. RTL Arabic/English dashboard shows monitoring status, applicant data, live log
9. Full source code runs independently on Windows PC or VPS — no external dependency
10. Complete source code, config template, and setup instructions delivered

## Delivery
- requirements.txt with pinned versions
- config.example.yaml with all fields documented
- README.md with Windows setup steps and VPS setup steps
- .gitignore covering config.yaml, session/, logs/, docs/
- No hardcoded credentials anywhere in source code

## Notes
- config.yaml and session/cookies.json must never be committed to git
- Use random delays (0.5–2s) between Playwright actions throughout
- Take a screenshot on every unexpected page state and save to logs/screenshots/
- The bot must run 24/7 without human intervention except for manual verification steps
- Client is in Egypt — no geo-restriction when running on his machine or Egyptian VPS
- During development: use ProtonVPN with SecureCore Egypt server

## Portal Research Findings (DO NOT SKIP)

### Login Page — Confirmed via DevTools

The BLS login form has 10 `input[type="text"]` fields. Only Field 0 is real:

```
Field 0: display=block, visibility=visible, opacity=1, height=70px  ← REAL
Field 1: display=none  ← honeypot
Field 2: display=none  ← honeypot
...
Field 9: display=none  ← honeypot
```

**The real field position also randomizes on every page load** (confirmed: Field 0 on first load, Field 3 on second load). Never use `.first` or any fixed index.

**Always find the real field by computed display style:**

```python
async def find_real_input(page, input_type="text"):
    """
    BLS login: 10 fields, random IDs, random position.
    Only the real field has display:block on its parent div.
    Honeypots have display:none.
    """
    inputs = await page.query_selector_all(f'input[type="{input_type}"]')
    for inp in inputs:
        parent = await inp.evaluate_handle('el => el.closest("div.mb-3")')
        display = await parent.evaluate('el => window.getComputedStyle(el).display')
        if display == 'block':
            return inp
    return None

# Usage:
real_field = await find_real_input(page)
await real_field.fill(email)
```

### Login Flow
- Step 1: Fill email → click "Verify" button (`#btnVerify`)
- Step 2: Password page (structure TBD — same honeypot pattern expected)
- Form action: POST to `/Global/account/LoginSubmit`
- Hidden fields present: `__RequestVerificationToken`, `ResponseData`, `ReturnUrl`, `Id` — Playwright handles these automatically via form submit

### Password Step
- Not yet confirmed — inspect password page HTML and apply same `.first` selector logic
- Expected: same honeypot obfuscation pattern as email step

### Appointment Page
- Structure not yet confirmed — inspect after successful login
- Document: location selector (Cairo/Alexandria), visa type selector, calendar/slot UI
