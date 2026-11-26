/*
 * ============================================================
 *  ESP32 Smart Thermal Receipt Printer + Presence Sensor
 * ============================================================
 *  Hardware:
 *    - ESP32 DevKit V1
 *    - QR204 58mm TTL Thermal Printer on Serial2
 *        RX = GPIO 16   (ESP32 receives FROM printer — rarely used)
 *        TX = GPIO 17   (ESP32 sends TO printer)
 *        Baud: 9600     (see NOTE below to switch to 19200)
 *        Power the printer from its own 5-9 V supply (>= 2 A),
 *        NOT from the ESP32 — and connect the grounds together.
 *    - HLK-LD2410B Presence Sensor (optional)
 *        OUT pin -> GPIO 27 (HIGH = presence detected)
 *
 *  Behaviour:
 *    1. Connects to Wi-Fi.
 *    2. Polls GET /api/next-print every 5 s with X-API-Key header.
 *       When a receipt arrives it resets the printer (ESC @), sends the
 *       text line-by-line (paced so the printer's small buffer never
 *       overflows), feeds paper, then POSTs /api/ack/{id}. The server
 *       re-sends anything that isn't acknowledged, so a Wi-Fi drop
 *       never loses a receipt.
 *    3. On a rising edge of GPIO 27 (30 s debounce) POSTs
 *       /api/trigger/presence.
 * ============================================================
 */

#include <WiFi.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>
#include <esp_system.h>

// =============================================================
//  USER CONFIGURATION — edit these before uploading
// =============================================================
const char* WIFI_SSID       = "YOUR_WIFI_SSID";
const char* WIFI_PASSWORD   = "YOUR_WIFI_PASSWORD";

// Backend URL, no trailing slash. http:// (same network) or https:// (tunnel/cloud).
const char* SERVER_URL      = "http://192.168.1.100:8000";

// Must match PRINTER_API_KEY in backend/.env
const char* PRINTER_API_KEY = "CHANGE_ME";

// Set to false if you have no LD2410B sensor wired (avoids false triggers
// from a floating pin).
#define PRESENCE_ENABLED true

// =============================================================
//  PRINTER CONFIG
// =============================================================
// NOTE: The QR204 ships at 9600 or 19200 baud. Hold the feed button while
//       powering on to print a self-test page that shows the baud rate.
#define PRINTER_BAUD     9600
#define PRINTER_RX       16
#define PRINTER_TX       17
#define LINE_DELAY_MS    40     // pause after each line; raise if prints garble
#define FEED_LINES       4      // blank lines after a receipt for tearing

// =============================================================
//  OTHER CONFIG
// =============================================================
#define PRESENCE_PIN          27
#define PRESENCE_DEBOUNCE_MS  30000
#define POLL_INTERVAL_MS      5000

unsigned long lastPollTime        = 0;
unsigned long lastPresenceTrigger = 0;
bool          lastPresenceState   = false;

// =============================================================
//  HTTP helper — works for both http:// and https:// SERVER_URL
// =============================================================
WiFiClient       plainClient;
WiFiClientSecure secureClient;

bool beginRequest(HTTPClient& http, const String& path) {
  String url = String(SERVER_URL) + path;
  bool ok;
  if (url.startsWith("https://")) {
    secureClient.setInsecure();  // home device: skip CA pinning
    ok = http.begin(secureClient, url);
  } else {
    ok = http.begin(plainClient, url);
  }
  http.addHeader("X-API-Key", PRINTER_API_KEY);
  http.setTimeout(15000);
  return ok;
}

// =============================================================
//  Printer helpers
// =============================================================
void printerReset() {
  Serial2.write(0x1B);  // ESC
  Serial2.write('@');   // initialise printer (clears old formatting)
  delay(50);
}

void printReceipt(const String& text) {
  printerReset();
  int start = 0;
  while (start < (int)text.length()) {
    int nl = text.indexOf('\n', start);
    if (nl < 0) nl = text.length();
    Serial2.print(text.substring(start, nl));
    Serial2.print('\n');
    Serial2.flush();          // wait until bytes have left the UART
    delay(LINE_DELAY_MS);     // let the print head catch up
    start = nl + 1;
  }
  for (int i = 0; i < FEED_LINES; i++) {
    Serial2.print('\n');
    delay(LINE_DELAY_MS);
  }
  Serial2.flush();
}

// =============================================================
//  Poll backend for the next receipt, print it, acknowledge it
// =============================================================
void ackPrint(const String& printId) {
  HTTPClient http;
  if (!beginRequest(http, "/api/ack/" + printId)) return;
  int code = http.POST("");
  Serial.printf("[ACK] #%s -> HTTP %d\n", printId.c_str(), code);
  http.end();
}

void pollPrintQueue() {
  if (WiFi.status() != WL_CONNECTED) return;

  HTTPClient http;
  if (!beginRequest(http, "/api/next-print")) return;
  const char* wanted[] = {"X-Print-Id"};
  http.collectHeaders(wanted, 1);

  int code = http.GET();
  if (code == 200) {
    String body = http.getString();
    String printId = http.header("X-Print-Id");
    http.end();

    if (body.length() > 0) {
      Serial.printf("[PRINT] #%s, %d bytes\n", printId.c_str(), body.length());
      printReceipt(body);
      if (printId.length() > 0) ackPrint(printId);
    }
    return;
  }

  if (code < 0) {
    Serial.printf("[POLL] HTTP error: %s\n", http.errorToString(code).c_str());
  } else {
    Serial.printf("[POLL] HTTP %d\n", code);
  }
  http.end();
}

// =============================================================
//  Presence trigger
// =============================================================
void sendPresenceTrigger() {
  if (WiFi.status() != WL_CONNECTED) return;
  HTTPClient http;
  if (!beginRequest(http, "/api/trigger/presence")) return;
  http.addHeader("Content-Type", "application/json");
  int code = http.POST("{}");
  Serial.printf("[PRESENCE] HTTP %d %s\n", code, code == 200 ? http.getString().c_str() : "");
  http.end();
}

// =============================================================
//  Wi-Fi
// =============================================================
void ensureWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;
  Serial.println("[WIFI] Connecting...");
  WiFi.disconnect();
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 15000) {
    delay(250);
  }
  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("[WIFI] Connected, IP %s\n", WiFi.localIP().toString().c_str());
  } else {
    Serial.println("[WIFI] Failed, will retry");
  }
}

// =============================================================
//  SETUP / LOOP
// =============================================================
void setup() {
  Serial.begin(115200);
  Serial.println("\n=== ESP32 Thermal Printer Starting ===");

  Serial2.begin(PRINTER_BAUD, SERIAL_8N1, PRINTER_RX, PRINTER_TX);
  pinMode(PRESENCE_PIN, INPUT_PULLDOWN);

  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  ensureWiFi();

  // Only print the "online" slip after a real power-on, not on every
  // watchdog/Wi-Fi reset, so a flaky network doesn't waste paper.
  if (esp_reset_reason() == ESP_RST_POWERON) {
    printReceipt(String("================================\n") +
                 "     PRINTER ONLINE & READY\n" +
                 (WiFi.status() == WL_CONNECTED ? "  IP " + WiFi.localIP().toString() : String("  WI-FI NOT CONNECTED")) + "\n" +
                 "================================");
  }
}

void loop() {
  unsigned long now = millis();

  ensureWiFi();

  if (now - lastPollTime >= POLL_INTERVAL_MS) {
    lastPollTime = now;
    pollPrintQueue();
  }

  if (PRESENCE_ENABLED) {
    bool present = digitalRead(PRESENCE_PIN) == HIGH;
    if (present && !lastPresenceState && now - lastPresenceTrigger >= PRESENCE_DEBOUNCE_MS) {
      Serial.println("[PRESENCE] Detected");
      sendPresenceTrigger();
      lastPresenceTrigger = now;
    }
    lastPresenceState = present;
  }

  delay(100);
}
