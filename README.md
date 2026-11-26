# 📰 Personal Newspaper — WhatsApp → ESP32 Thermal Printer

Every morning an ESP32 prints a personal newspaper on a 58mm thermal receipt
printer: weather, daily habits, today's tasks and anything overdue. During the
day you control it from WhatsApp — text or voice notes — and Gemini turns your
message into tasks, habits or printouts.

```
[ Your WhatsApp ] ──text / voice──> [ Meta Cloud API ]
                                           │ webhook (signed)
                                           ▼
                                 [ FastAPI backend ]
                                  ├── Gemini (structured JSON)
                                  ├── SQLite (printer.db, jobs.db)
                                  └── APScheduler (Asia/Karachi)
                                           ▲
                    polls every 5 s, acks  │
[ ESP32 ] ─────────────────────────────────┘
  ├── QR204 58mm printer (Serial2, GPIO 16/17)
  └── LD2410B presence sensor (GPIO 27, optional)
```

## What you can send on WhatsApp

| You say | What happens |
|---|---|
| "Add a habit: read 10 pages" | Daily habit, printed every morning |
| "Submit assignment on Friday" | Dated task, appears on that day's paper (and as overdue after) |
| "Done with the assignment" | Task marked complete |
| "Stop the reading habit" | Habit removed |
| "Print: milk, eggs, bread" | Prints a checklist right now |
| "Remind me to call Ammi at 8pm" | Prints at 20:00 PKT |
| "Print my morning paper" | Re-prints today's paper now |
| "What's on my list?" | WhatsApp reply with habits and open tasks |

## Morning edition (default 07:00 PKT)

- Weather for your city: current, high/low, chance of rain (Open-Meteo, no key)
- Daily habits as checkboxes
- Tasks due today, plus an **OVERDUE** section for unfinished older tasks

If the server is off at 07:00, the paper prints as soon as it comes back
(within 6 hours).

## Reliability & security

- **No lost receipts** — the ESP32 acknowledges each print (`POST /api/ack/{id}`);
  unacknowledged receipts are re-sent (up to 3 times).
- **No duplicate prints** — WhatsApp message ids are de-duplicated and the
  webhook replies to Meta immediately.
- **Signed webhooks** — set `WHATSAPP_APP_SECRET` so only Meta can post to
  `/webhook/whatsapp`; only `ALLOWED_PHONE_NUMBER` is obeyed.
- **ASCII-safe output** — characters the printer can't render are converted or dropped.
- The browser simulator (`/` and `/api/demo/*`) is off unless `DEMO_ENABLED=true`.
  Keep it off when the server is exposed to the internet.

## Setup

### 1. Backend

```bash
cd backend
python -m venv venv
.\venv\Scripts\activate          # Windows
pip install -r requirements.txt
cp .env.example .env             # then fill it in
uvicorn main:app --host 0.0.0.0 --port 8000
```

The server must be running at the morning-paper time and reachable by Meta
(public HTTPS URL, e.g. Cloudflare Tunnel or ngrok) and by the ESP32.
Check `GET /health` to see when the next paper is scheduled.

In the Meta App Dashboard set the webhook URL to
`https://<your-domain>/webhook/whatsapp`, the verify token to
`WHATSAPP_VERIFY_TOKEN`, and subscribe to **messages**. Use a permanent System
User access token; the temporary one expires after 24 hours.

To try it in a browser on your own machine, set `DEMO_ENABLED=true` and open
`http://localhost:8000/`.

### 2. ESP32 firmware

1. Open `firmware/esp32_printer.ino` in the Arduino IDE (board: **ESP32 Dev Module**).
2. Set `WIFI_SSID`, `WIFI_PASSWORD`, `SERVER_URL` and `PRINTER_API_KEY` (same as `.env`).
3. Set `PRESENCE_ENABLED false` if you have no LD2410B.
4. Wiring:

   | Component | ESP32 | Note |
   |---|---|---|
   | Printer RX | GPIO 17 (TX2) | ESP32 → printer |
   | Printer TX | GPIO 16 (RX2) | printer → ESP32 |
   | Printer GND | GND | common ground |
   | Printer VIN | — | own 5–9 V ≥2 A supply, **not** the ESP32 |
   | LD2410B OUT | GPIO 27 | optional |
   | LD2410B VCC/GND | 5V / GND | optional |

5. Upload. On power-on it prints `PRINTER ONLINE & READY` with its IP.

If prints come out garbled, check the baud rate on the printer's self-test page
(hold feed while powering on) and/or raise `LINE_DELAY_MS`.

### Other triggers (need `X-API-Key`)

- `POST /api/trigger/nfc/desk` — 45-minute deep-work ticket
- `POST /api/trigger/nfc/door` — leaving-home checklist
- `POST /api/trigger/nfc/bedside` — tomorrow's tasks
- `POST /api/trigger/presence` — evening check-in (16:00–23:00, 4 h cooldown)

The NFC endpoints can be called from phone NFC-tag automations (iOS Shortcuts / Android Tasker).

## License

MIT
