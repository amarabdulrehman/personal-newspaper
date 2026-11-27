// Copy this file to config.h (same folder) and fill it in.
// config.h is git-ignored so your keys never end up on GitHub.
#pragma once

// ---- Wi-Fi ---------------------------------------------------------------
#define WIFI_SSID        "YOUR_WIFI_SSID"
#define WIFI_PASSWORD    "YOUR_WIFI_PASSWORD"

// ---- Cloudflare Worker relay (see worker/README in the main README) -------
// No trailing slash.
#define RELAY_URL        "https://personal-newspaper-relay.YOUR-SUBDOMAIN.workers.dev"
#define DEVICE_API_KEY   "same-long-random-string-as-the-worker-DEVICE_API_KEY"

// ---- Google Gemini (https://aistudio.google.com/apikey) -------------------
#define GEMINI_API_KEY   "YOUR_GEMINI_API_KEY"
#define GEMINI_MODEL     "gemini-2.5-flash"

// ---- You ------------------------------------------------------------------
#define USER_NAME        "AMAR ABDUL REHMAN"
#define CITY_NAME        "Islamabad"
#define WEATHER_LAT      "33.6844"
#define WEATHER_LON      "73.0479"
#define TZ_INFO          "PKT-5"        // POSIX TZ string; PKT-5 = UTC+5
#define MORNING_HOUR     7
#define MORNING_MINUTE   0

// ---- Local triggers (phone NFC tags / shortcuts on your home Wi-Fi) -------
// http://newspaper.local/trigger?zone=desk&key=LOCAL_TRIGGER_KEY
#define LOCAL_TRIGGER_KEY "pick-a-short-secret"

// ---- Prayer times on the morning paper (api.aladhan.com, no key) -----------
#define PRAYER_TIMES_ENABLED true
#define PRAYER_METHOD    1      // 1 = University of Islamic Sciences, Karachi
#define PRAYER_SCHOOL    1      // Asr: 0 = Shafi, 1 = Hanafi

// ---- Hardware -------------------------------------------------------------
#define PRINTER_BAUD     9600   // 9600 or 19200 - see the printer self-test page
#define PRINTER_STYLES   true   // bold / double-size headlines; set false if your printer prints junk
#define PRESENCE_ENABLED false  // true if an LD2410B is wired to GPIO 27
#define BUTTON_ENABLED   false  // true if a push button is wired GPIO 4 -> GND
#define BUTTON_PIN       4      // short press = today's to-do, long press = morning paper
