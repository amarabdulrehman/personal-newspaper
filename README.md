# 📰 Personal Newspaper — WhatsApp → ESP32 Thermal Printer

Every morning an ESP32 prints a personal newspaper on a 58mm thermal receipt
printer: weather, daily habits, today's tasks and anything overdue. During the
day you talk to it on WhatsApp, by text or voice note, and Gemini turns your
message into tasks, habits or printouts.

**The ESP32 is the server.** No PC has to stay on. The only cloud piece is a
tiny free Cloudflare Worker that catches WhatsApp's webhook, because Meta can
only deliver messages to a public HTTPS address and the ESP32 sits behind
your home router.

```
 Your WhatsApp ──> Meta ──webhook──> Cloudflare Worker (free)
                                      │  checks signature, dedupes,
                                      │  transcribes voice notes (Gemini)
                                      ▼
                                   D1 inbox
                                      ▲  GET /inbox every 6 s
                                      │  POST /reply  ──> back to WhatsApp
 ┌────────────────────────────────────┴──────────────────┐
 │ ESP32 DevKit V1 (home Wi-Fi)                          │
 │  - Gemini decides what each message means             │
 │  - habits / tasks / reminders stored in flash         │
 │  - morning paper, reminders, NFC/presence triggers    │
 │  - QR204 printer on Serial2, LD2410B on GPIO 27       │
 └───────────────────────────────────────────────────────┘
```

## What you can send on WhatsApp

| You say | What happens |
|---|---|
| "Add a habit: read 10 pages" | Daily habit, printed every morning |
| "Submit assignment on Friday" | Task; shows on that day's paper, then as overdue |
| "Done with the assignment" | Task removed |
| "Stop the reading habit" | Habit removed |
| "Print: milk, eggs, bread" | Prints a checklist now |
| "Remind me to call Ammi at 8pm" | Prints at 20:00 |
| "Did gym today" | Habit streak +1; the paper shows `[ ] Gym (x12)` |
| Voice note saying any of the above | Same as typing it |
| `list` `todo` `paper` `streaks` `outages` `status` `help` | Instant shortcuts (work even if Gemini is down) |

## The morning edition

Prints at 07:00 (configurable). After a power cut it prints as soon as the
ESP32 is back, up to 6 hours late.

```
================================
MORNING EDITION          <- double-size headline
================================
       AMAR ABDUL REHMAN         <- bold
          DAILY BRIEF
    FRI NOV 27, 2026 | 07:00
--- [ WEATHER ] ----------------
Islamabad: 11C Clear
High 22C / Low 8C | Rain 0%
--- [ PRAYER TIMES ] -----------
6 Jumada al-Akhirah 1448 AH
Fajr    05:32    Sunrise 06:58
Dhuhr   11:59    Asr     14:42
Maghrib 16:59    Isha    18:24
--- [ POWER REPORT ] -----------
~03:10 - 07:25  (4h15m)
Total last 24h: 4h15m
--- [ DAILY HABITS ] -----------
[ ] Gym (x12)
[ ] Read 10 pages (x4)
--- [ TODAY'S TASKS ] ----------
[ ] Submit DSP assignment
--------------------------------
          OWN THE DAY
================================
```

- **Prayer times + Hijri date** from api.aladhan.com (Karachi method, Hanafi Asr; configurable).
- **Power report:** the ESP32 notices it was switched off by a power cut and
  reports when and for how long. `outages` on WhatsApp gives this month's total.
- **Habit streaks:** tell it what you did ("did gym", "read today"); streaks
  reset if you skip a day. `streaks` shows current and best.
- **Printer offline alert:** if the ESP32 hasn't checked in for 30 minutes, the
  Worker sends you a WhatsApp message, and another when it's back. (WhatsApp
  only allows free-form messages within 24 hours of your last message to the
  bot, so this alert only arrives if you've messaged it in the last day.)

## Setup

You need: ESP32 DevKit V1, QR204 (or similar 58mm TTL) printer with its own
5–9 V ≥ 2 A supply, a Meta WhatsApp Cloud API app, a Gemini API key, and a
free Cloudflare account.

### 1. Cloudflare Worker (the relay)

```bash
cd worker
npx wrangler login
npx wrangler d1 create newspaper-inbox          # copy the database_id into wrangler.toml
npx wrangler d1 execute newspaper-inbox --remote --file=schema.sql

# secrets (each command asks for the value)
npx wrangler secret put WHATSAPP_VERIFY_TOKEN     # any random string
npx wrangler secret put WHATSAPP_ACCESS_TOKEN     # permanent System User token
npx wrangler secret put WHATSAPP_APP_SECRET       # Meta App Settings > Basic
npx wrangler secret put WHATSAPP_PHONE_NUMBER_ID  # WhatsApp > API Setup
npx wrangler secret put ALLOWED_PHONE_NUMBER      # your number, e.g. 92300xxxxxxx
npx wrangler secret put GEMINI_API_KEY            # used to transcribe voice notes
npx wrangler secret put DEVICE_API_KEY            # long random string, also goes in config.h

npx wrangler deploy                               # prints https://personal-newspaper-relay.<you>.workers.dev
npm test                                          # optional: offline tests of the relay
```

In the Meta App Dashboard → WhatsApp → Configuration, set the callback URL to
`https://personal-newspaper-relay.<you>.workers.dev/webhook`, the verify token
to `WHATSAPP_VERIFY_TOKEN`, and subscribe to **messages**.

If you deployed before the offline alert existed, re-run the `schema.sql`
command above (it only adds the new `device` table) and `npx wrangler deploy`.

### 2. ESP32 firmware

1. Arduino IDE → Boards Manager: install **esp32 by Espressif** (3.x).
   Library Manager: install **ArduinoJson** (7.x).
2. In `firmware/personal_newspaper/`, copy `config.example.h` to `config.h` and fill it in:
   Wi-Fi, `RELAY_URL`, `DEVICE_API_KEY` (same as the Worker), `GEMINI_API_KEY`,
   your city/coordinates, morning time, printer baud.
   `config.h` is git-ignored, so your keys stay off GitHub.
3. Open `personal_newspaper.ino`, board **ESP32 Dev Module**, upload.
   The sketch uses about 92% of the default 1.3 MB app space. If you add
   features and get "Sketch too big", pick **Tools → Partition Scheme →
   Huge APP (3MB No OTA/1MB SPIFFS)**. LittleFS keeps working, but switching
   schemes erases the saved habits/tasks.
4. On power-on it prints `NEWSPAPER ONLINE` with its IP address.

Wiring:

| Component | ESP32 | Note |
|---|---|---|
| Printer RX | GPIO 17 (TX2) | ESP32 → printer |
| Printer TX | GPIO 16 (RX2) | printer → ESP32 |
| Printer GND | GND | common ground with the printer supply |
| Printer VIN | — | own 5–9 V ≥ 2 A supply, **not** the ESP32 |
| LD2410B OUT | GPIO 27 | optional; set `PRESENCE_ENABLED true` |
| Push button | GPIO 4 ↔ GND | optional; set `BUTTON_ENABLED true`. Short press = to-do slip, hold 1+ s = morning paper |

If headlines print as junk characters, your printer doesn't support the bold /
double-size commands: set `PRINTER_STYLES false` in `config.h`.

If prints come out garbled, check the baud rate on the printer self-test page
(hold the feed button while powering on) and/or raise `LINE_DELAY_MS`.

### 3. Local triggers (optional)

On your home Wi-Fi the ESP32 answers at `http://newspaper.local/` (or its IP):

- `http://newspaper.local/` shows status (time, counts, last morning paper)
- `http://newspaper.local/trigger?zone=desk&key=LOCAL_TRIGGER_KEY`
  prints a ticket; zones are `desk`, `door`, `bedside`, `evening`, `today` and `morning`

Point phone NFC-tag automations (iOS Shortcuts / Android Tasker) at these URLs.
With the presence sensor enabled, an evening check-in prints when you get home
(16:00–23:00, at most every 4 hours).

## Limits and notes

- **Slow first compile on Windows:** the first ESP32 build can take 30+ minutes
  because antivirus scans thousands of header files; later builds reuse the
  cache. If your `Documents\Arduino` folder is inside OneDrive, builds can stall
  while scanning it. Setting OneDrive to "Always keep on this device" for that
  folder helps.

- Free tiers: the ESP32 polls every 6 s (~14,400 Worker requests/day; the free
  limit is 100,000). D1 and Gemini Flash free tiers cover personal use.
- Data (habits, tasks, reminders) lives in the ESP32's flash in `/data.json`
  and survives reboots and power cuts. Re-flashing with "Erase all flash" wipes it.
- The ESP32 skips TLS certificate checks (`setInsecure`) to save memory. That's
  fine on a home network.
- `legacy/` holds the earlier Python/FastAPI backend and its thin ESP32 client.
  Use them if you'd rather host the brain on a PC or cloud server instead.

## License

MIT
