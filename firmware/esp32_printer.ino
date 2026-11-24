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
 *    - HLK-LD2410B Presence Sensor
 *        OUT pin → GPIO 27 (INPUT, HIGH = presence detected)
 *        VCC → 3.3 V or 5 V (check module spec)
 *        GND → GND
 *
 *  Behaviour:
 *    1. Connects to Wi-Fi.
 *    2. Polls GET /api/next-print every 5 s with X-API-Key header.
 *       If a non-empty body is returned (HTTP 200), sends the text
 *       to the thermal printer over Serial2 followed by 3 newlines
 *       for clean paper tearing.
 *    3. Monitors GPIO 27 for the LD2410B presence sensor.
 *       When HIGH (with 30 s local debounce), POSTs to
 *       /api/trigger/presence with X-API-Key header.
 * ============================================================
 */

#include <WiFi.h>
#include <HTTPClient.h>

// =============================================================
//  USER CONFIGURATION — edit these before uploading
// =============================================================

// Wi-Fi credentials
const char* WIFI_SSID     = "YOUR_WIFI_SSID";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";

// Backend server URL (no trailing slash)
// Examples:
//   Local network:  "http://192.168.1.100:8000"
//   Ngrok tunnel:   "https://xxxx-xx-xx.ngrok-free.app"
const char* SERVER_URL    = "http://192.168.1.100:8000";

// Must match PRINTER_API_KEY in backend .env
const char* PRINTER_API_KEY = "esp32_local_secret_99";

// =============================================================
//  PRINTER SERIAL CONFIG
// =============================================================
// NOTE: The QR204 printer ships at either 9600 or 19200 baud.
//       Run a printer self-test (hold the feed button while
//       powering on) — it prints its baud rate on the test page.
//       If it says 19200, change the value below to 19200.
#define PRINTER_BAUD 9600

#define PRINTER_RX 16   // ESP32 GPIO 16 — connected to printer TX
#define PRINTER_TX 17   // ESP32 GPIO 17 — connected to printer RX

// =============================================================
//  PRESENCE SENSOR CONFIG
// =============================================================
#define PRESENCE_PIN          27
#define PRESENCE_DEBOUNCE_MS  30000   // 30 seconds

// =============================================================
//  POLLING INTERVAL
// =============================================================
#define POLL_INTERVAL_MS      5000    // 5 seconds

// =============================================================
//  GLOBAL STATE
// =============================================================
unsigned long lastPollTime         = 0;
unsigned long lastPresenceTrigger  = 0;
bool          lastPresenceState    = false;

// =============================================================
//  SETUP
// =============================================================
void setup() {
  // Debug serial (USB)
  Serial.begin(115200);
  Serial.println();
  Serial.println("=================================");
  Serial.println(" ESP32 Thermal Printer Starting");
  Serial.println("=================================");

  // Printer serial
  Serial2.begin(PRINTER_BAUD, SERIAL_8N1, PRINTER_RX, PRINTER_TX);
  Serial.printf("Printer serial on TX=%d RX=%d @ %d baud\n",
                 PRINTER_TX, PRINTER_RX, PRINTER_BAUD);

  // Presence sensor pin
  pinMode(PRESENCE_PIN, INPUT);

  // Connect to Wi-Fi
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.printf("Connecting to Wi-Fi \"%s\"", WIFI_SSID);

  unsigned long wifiStart = millis();
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
    if (millis() - wifiStart > 20000) {
      Serial.println("\nWi-Fi connection timeout — restarting...");
      ESP.restart();
    }
  }
  Serial.println();
  Serial.printf("Connected!  IP: %s\n", WiFi.localIP().toString().c_str());

  // Print a startup receipt so you know the printer works
  Serial2.println("================================");
  Serial2.println("    PRINTER ONLINE & READY");
  Serial2.println("================================");
  Serial2.println("\n\n\n");
}

// =============================================================
//  HELPER: Poll backend for next print job
// =============================================================
void pollPrintQueue() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[POLL] Wi-Fi disconnected — skipping");
    return;
  }

  HTTPClient http;
  String url = String(SERVER_URL) + "/api/next-print";
  http.begin(url);
  http.addHeader("X-API-Key", PRINTER_API_KEY);
  http.setTimeout(10000);

  int httpCode = http.GET();

  if (httpCode == 200) {
    String body = http.getString();

    if (body.length() > 0) {
      Serial.printf("[PRINT] Received %d bytes — sending to printer\n", body.length());

      // Send receipt text to thermal printer
      Serial2.print(body);

      // Feed 3 blank lines for clean tearing
      Serial2.print("\n\n\n");

      Serial.println("[PRINT] Done.");
    } else {
      // Empty queue — nothing to print
      // (silent, no spam)
    }
  } else if (httpCode < 0) {
    Serial.printf("[POLL] HTTP error: %s\n", http.errorToString(httpCode).c_str());
  } else {
    Serial.printf("[POLL] HTTP %d\n", httpCode);
  }

  http.end();
}

// =============================================================
//  HELPER: Send presence trigger to backend
// =============================================================
void sendPresenceTrigger() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[PRESENCE] Wi-Fi disconnected — skipping");
    return;
  }

  HTTPClient http;
  String url = String(SERVER_URL) + "/api/trigger/presence";
  http.begin(url);
  http.addHeader("X-API-Key", PRINTER_API_KEY);
  http.addHeader("Content-Type", "application/json");
  http.setTimeout(10000);

  int httpCode = http.POST("{}");

  if (httpCode == 200) {
    String resp = http.getString();
    Serial.printf("[PRESENCE] Trigger sent — server replied: %s\n", resp.c_str());
  } else {
    Serial.printf("[PRESENCE] HTTP %d\n", httpCode);
  }

  http.end();
}

// =============================================================
//  MAIN LOOP
// =============================================================
void loop() {
  unsigned long now = millis();

  // --- 1. Poll print queue every POLL_INTERVAL_MS ----------------
  if (now - lastPollTime >= POLL_INTERVAL_MS) {
    lastPollTime = now;
    pollPrintQueue();
  }

  // --- 2. Monitor presence sensor --------------------------------
  bool presenceDetected = digitalRead(PRESENCE_PIN) == HIGH;

  if (presenceDetected && !lastPresenceState) {
    // Rising edge — presence just detected
    if (now - lastPresenceTrigger >= PRESENCE_DEBOUNCE_MS) {
      Serial.println("[PRESENCE] Detected — sending trigger");
      sendPresenceTrigger();
      lastPresenceTrigger = now;
    } else {
      Serial.println("[PRESENCE] Detected — debounce active, skipping");
    }
  }
  lastPresenceState = presenceDetected;

  // --- 3. Reconnect Wi-Fi if dropped -----------------------------
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[WIFI] Connection lost — reconnecting...");
    WiFi.disconnect();
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    unsigned long reconnectStart = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - reconnectStart < 10000) {
      delay(500);
    }
    if (WiFi.status() == WL_CONNECTED) {
      Serial.printf("[WIFI] Reconnected!  IP: %s\n", WiFi.localIP().toString().c_str());
    } else {
      Serial.println("[WIFI] Reconnect failed — will retry next loop");
    }
  }

  delay(100);  // Small yield to avoid busy-looping
}
