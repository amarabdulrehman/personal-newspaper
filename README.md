# 📰 WhatsApp-to-ESP32 Smart Thermal Receipt Printer

A complete physical productivity and notification system that prints daily morning digests, habit checklists, deep-work tickets, and task reminders on a 58mm thermal receipt printer via WhatsApp Cloud API, Gemini 2.5 Flash, APScheduler, and ESP32 with radar presence sensing.

---

## 🏗 System Architecture

```
[ WhatsApp User ] ──(Text / Voice)──> [ Meta Cloud API v25.0 ]
                                               │
                                               ▼ Webhook
                                    [ FastAPI Backend ]
                                     ├── Gemini 2.5 Flash (Pydantic Schema)
                                     ├── SQLite (printer.db + jobs.db)
                                     └── APScheduler (Asia/Karachi)
                                               │
                                               ▼ Polling / Triggers
[ ESP32 DevKit ] ◄──(Wi-Fi / HTTP)─────────────┘
  ├── QR204 58mm Thermal Printer (Serial2: GPIO 16/17)
  └── HLK-LD2410B mmWave Radar Presence Sensor (GPIO 27)
```

---

## 📂 Project Structure

```
personal-newspaper/
├── backend/
│   ├── main.py              # FastAPI app, Gemini classifier, DB, Scheduler, API endpoints
│   ├── requirements.txt     # Python dependencies
│   └── .env.example         # Template for environment secrets
├── firmware/
│   └── esp32_printer.ino    # ESP32 Arduino sketch for printer control & presence sensing
├── .gitignore
└── README.md
```

---

## 🚀 Features

### 1. `/backend` (Python FastAPI + SQLite + Gemini + APScheduler)
- **32-Character Thermal Layout**: Formats every receipt with dividers (`=`, `-`), centered uppercase headers, timestamps, and `[ ] ` checkboxes strictly wrapped to 32 columns.
- **WhatsApp Cloud API Integration**:
  - `GET /webhook/whatsapp`: Meta webhook handshake verification.
  - `POST /webhook/whatsapp`: Receives text messages and audio voice notes from the authorized sender (`ALLOWED_PHONE_NUMBER`).
  - Downloads voice notes directly from Meta Graph API (`v25.0`) and sends raw audio inline to Gemini for transcription + classification.
- **Gemini 2.5 Flash Structured Classifier**:
  - Injects current `Asia/Karachi` date, day of the week, and local time into the system prompt.
  - Returns structured `GeminiDecision` JSON covering 5 actions:
    1. `add_daily`: Saves recurring daily habits to `habits` table.
    2. `add_dated`: Saves dated tasks to `dated_tasks` table.
    3. `print_now`: Immediately formats and queues a custom receipt in `print_queue`.
    4. `schedule_print`: Parses relative/absolute natural language times (e.g. *"at 7:00 PM"*, *"in 45 mins"*) into PKT ISO timestamps and registers an APScheduler job.
    5. `list_tasks`: Formats active habits and pending tasks into a WhatsApp reply.
- **Automated Morning Newspaper (07:00 AM PKT Daily Cron)**:
  - Fetches Islamabad live weather from Open-Meteo.
  - Pulls daily habits and today's dated tasks.
  - Formats and enqueues the "MORNING EDITION" receipt into `print_queue`.
- **Hardware Triggers (`X-API-Key: PRINTER_API_KEY`)**:
  - `POST /api/trigger/nfc/desk`: 45-minute deep work sprint ticket.
  - `POST /api/trigger/nfc/door`: Pocket check checklist (Keys, Wallet, University ID, Laptop & Charger).
  - `POST /api/trigger/nfc/bedside`: Tomorrow's dated tasks preview.
  - `POST /api/trigger/presence`: Evening check-in (16:00–23:00 PKT window, 4-hour cooldown).
  - `GET /api/next-print`: Dequeues the oldest unprinted receipt (FIFO) and returns plaintext.

### 2. `/firmware` (ESP32 DevKit V1)
- **Wi-Fi Connectivity**: Auto-connect with reconnection loop and timeout recovery.
- **Thermal Printer Interface**:
  - Drives a QR204 58mm TTL thermal printer over `Serial2` (`RX=GPIO16`, `TX=GPIO17`).
  - Default baud rate: `9600` (easily changed to `19200` depending on printer self-test output).
  - Appends 3 newlines (`\n\n\n`) after every receipt for clean tearing.
- **Presence Detection**:
  - Monitors HLK-LD2410B sensor `OUT` pin on `GPIO 27` (`INPUT`).
  - 30-second local debounce prevents trigger spamming.
  - Dispatches `POST /api/trigger/presence` to the backend when motion is detected.

---

## 🛠 Quick Start

### 1. Backend Setup

```bash
cd backend

# Create virtual environment (optional but recommended)
python -m venv venv
# Windows:
.\venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure environment variables
cp .env.example .env
```

Edit `.env` with your credentials:
```ini
WHATSAPP_VERIFY_TOKEN=printer_secret_2026
WHATSAPP_ACCESS_TOKEN=your_meta_system_user_token
WHATSAPP_PHONE_NUMBER_ID=1428007273647455
ALLOWED_PHONE_NUMBER=923315355121
GEMINI_API_KEY=your_gemini_api_key
PRINTER_API_KEY=esp32_local_secret_99
```

Run the backend server:
```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Expose to public internet for WhatsApp Webhook (e.g. using ngrok or Cloudflare Tunnel):
```bash
ngrok http 8000
```
Then set your Webhook URL in Meta App Dashboard: `https://<your-ngrok-domain>/webhook/whatsapp`.

---

### 2. Firmware Flashing (ESP32)

1. Open `firmware/esp32_printer.ino` in the Arduino IDE.
2. Select **ESP32 Dev Module** from the Boards menu.
3. Update the credentials at the top of the file:
   ```cpp
   const char* WIFI_SSID     = "YOUR_WIFI_SSID";
   const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";
   const char* SERVER_URL    = "http://<YOUR_BACKEND_IP>:8000";
   const char* PRINTER_API_KEY = "esp32_local_secret_99";
   ```
4. Wiring:
   | Component | ESP32 Pin | Note |
   |---|---|---|
   | Thermal Printer RX | GPIO 17 (TX2) | ESP32 transmits to printer |
   | Thermal Printer TX | GPIO 16 (RX2) | ESP32 receives from printer |
   | Thermal Printer GND | GND | Common ground |
   | LD2410B OUT | GPIO 27 | Presence digital input |
   | LD2410B VCC/GND | 5V / GND | Sensor power |
5. Click **Upload**.

---

## 📜 License

MIT License. Feel free to customize and extend for your own personal newspaper and thermal printing workflows!
