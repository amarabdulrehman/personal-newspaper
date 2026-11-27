/*
 * ===================================================================
 *  Personal Newspaper — all-in-one ESP32 firmware (ESP32 DevKit V1)
 * ===================================================================
 *  The ESP32 is the server:
 *    - polls the Cloudflare Worker relay for your WhatsApp messages
 *      (voice notes arrive already transcribed by the relay)
 *    - asks Gemini what to do with each message (structured JSON)
 *    - keeps habits, tasks and scheduled prints in flash (LittleFS)
 *    - prints the morning paper at MORNING_HOUR (or late, after a
 *      power cut, within 6 hours)
 *    - prints scheduled reminders, presence check-ins and local triggers
 *    - replies to you on WhatsApp through the relay
 *
 *  Libraries: ArduinoJson v7 (Library Manager). Everything else ships
 *  with the ESP32 core (tested with core 3.3.x).
 *
 *  Wiring:
 *    Printer RX  -> GPIO 17 (TX2)     Printer TX -> GPIO 16 (RX2)
 *    Printer GND -> GND; printer power from its own 5-9 V >= 2 A supply
 *    LD2410B OUT -> GPIO 27 (optional)
 * ===================================================================
 */

#if __has_include("config.h")
#include "config.h"
#else
#error "Copy config.example.h to config.h (same folder) and fill in your Wi-Fi and keys."
#endif

// Defaults for settings added later, so an older config.h still compiles.
#ifndef PRAYER_TIMES_ENABLED
#define PRAYER_TIMES_ENABLED true
#endif
#ifndef PRAYER_METHOD
#define PRAYER_METHOD 1
#endif
#ifndef PRAYER_SCHOOL
#define PRAYER_SCHOOL 1
#endif
#ifndef PRINTER_STYLES
#define PRINTER_STYLES true
#endif
#ifndef BUTTON_ENABLED
#define BUTTON_ENABLED false
#endif
#ifndef BUTTON_PIN
#define BUTTON_PIN 4
#endif

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <WebServer.h>
#include <ESPmDNS.h>
#include <LittleFS.h>
#include <ArduinoJson.h>
#include <esp_system.h>
#include <time.h>
#include <vector>

// ---------------------------------------------------------------------------
//  Constants
// ---------------------------------------------------------------------------
#define PRINTER_RX            16
#define PRINTER_TX            17
#define PRESENCE_PIN          27
#define LINE_DELAY_MS         40      // pause per printed line; raise if prints garble
#define FEED_LINES            4

#define INBOX_POLL_MS         6000    // how often to check WhatsApp messages
#define TICK_MS               20000   // how often to check morning/scheduled prints
#define MORNING_GRACE_MIN     360     // print a missed morning paper up to 6 h late
#define TITLE_MAX             60
#define MAX_HABITS            30
#define MAX_TASKS             60
#define MAX_SCHEDULED         20

#define HEARTBEAT_MS          300000  // save "still powered" timestamp every 5 min
#define MIN_OUTAGE_SEC        180     // shorter gaps aren't reported
#define MAX_OUTAGES           20
#define LONG_PRESS_MS         1200

static const int WIDTH = 32;          // characters per line on 58 mm paper
static const char* DB_PATH = "/data.json";
static const char* HB_PATH = "/heartbeat";

// In-text style markers, turned into ESC/POS commands by printText().
// A line starting with STYLE_BIG prints double width+height (16 chars/line),
// STYLE_BOLD prints bold. Markers never reach the paper.
#define STYLE_BIG  '\x01'
#define STYLE_BOLD '\x02'

// ---------------------------------------------------------------------------
//  State
// ---------------------------------------------------------------------------
// db = {"habits":[...], "tasks":[{"t","d"}], "sched":[{"at","title","lines"}],
//       "morning":"YYYY-MM-DD", "streaks":{"Habit":{"n":12,"last":"YYYY-MM-DD"}},
//       "outages":[{"s":epoch,"e":epoch}]}
JsonDocument db;
WebServer server(80);

unsigned long lastInboxPoll = 0;
unsigned long lastTick = 0;
unsigned long lastPresence = 0;
unsigned long lastHeartbeat = 0;
bool lastPresenceState = false;
time_t lastEveningCheckin = 0;
time_t pendingOutageStart = 0;   // set at boot, resolved once the clock is synced
bool buttonDown = false;
unsigned long buttonPressedAt = 0;

// ===========================================================================
//  Time helpers
// ===========================================================================
bool timeValid() { return time(nullptr) > 1700000000; }

String fmtTime(const char* fmt, time_t t) {
  struct tm tmv;
  localtime_r(&t, &tmv);
  char buf[48];
  strftime(buf, sizeof(buf), fmt, &tmv);
  return String(buf);
}

String todayStr() { return fmtTime("%Y-%m-%d", time(nullptr)); }

bool parseDate(const String& s, int& y, int& m, int& d) {
  return sscanf(s.c_str(), "%4d-%2d-%2d", &y, &m, &d) == 3 && y > 2000 && m >= 1 && m <= 12 && d >= 1 && d <= 31;
}

String validDateOrToday(const String& s) {
  int y, m, d;
  if (!parseDate(s, y, m, d)) return todayStr();
  char buf[11];
  snprintf(buf, sizeof(buf), "%04d-%02d-%02d", y, m, d);
  return String(buf);
}

// "YYYY-MM-DD HH:MM" (local time) -> epoch, 0 if invalid
time_t parseLocalDateTime(const String& s) {
  int y, mo, d, h, mi;
  if (sscanf(s.c_str(), "%4d-%2d-%2d%*[ T]%2d:%2d", &y, &mo, &d, &h, &mi) != 5) return 0;
  struct tm tmv = {};
  tmv.tm_year = y - 1900; tmv.tm_mon = mo - 1; tmv.tm_mday = d;
  tmv.tm_hour = h; tmv.tm_min = mi; tmv.tm_isdst = -1;
  return mktime(&tmv);
}

// ===========================================================================
//  Text / receipt formatting (32 columns, ASCII only)
// ===========================================================================
String toAscii(const String& in) {
  String out;
  out.reserve(in.length());
  const uint8_t* s = (const uint8_t*)in.c_str();
  size_t n = in.length();
  for (size_t i = 0; i < n;) {
    uint8_t c = s[i];
    if (c < 0x80) {
      if (c == '\t') out += ' ';
      else if (c >= 0x20 || c == '\n') out += (char)c;
      i++;
      continue;
    }
    uint32_t cp; int len;
    if ((c & 0xE0) == 0xC0)      { cp = c & 0x1F; len = 2; }
    else if ((c & 0xF0) == 0xE0) { cp = c & 0x0F; len = 3; }
    else if ((c & 0xF8) == 0xF0) { cp = c & 0x07; len = 4; }
    else { i++; continue; }
    if (i + len > n) break;
    for (int k = 1; k < len; k++) cp = (cp << 6) | (s[i + k] & 0x3F);
    i += len;
    switch (cp) {
      case 0x00A0: case 0x00B0: out += ' '; break;
      case 0x2022: case 0x00B7: out += '*'; break;
      case 0x2013: case 0x2014: case 0x2212: out += '-'; break;
      case 0x2018: case 0x2019: case 0x2032: out += '\''; break;
      case 0x201C: case 0x201D: out += '"'; break;
      case 0x2026: out += "..."; break;
      case 0x2192: out += "->"; break;
      case 0x2190: out += "<-"; break;
      case 0x2713: case 0x2714: case 0x00D7: out += 'x'; break;
      case 0x20A8: out += "Rs"; break;
      default:
        if (cp >= 0xE0 && cp <= 0xE5) out += 'a';
        else if (cp >= 0xE8 && cp <= 0xEB) out += 'e';
        else if (cp >= 0xEC && cp <= 0xEF) out += 'i';
        else if (cp >= 0xF2 && cp <= 0xF6) out += 'o';
        else if (cp >= 0xF9 && cp <= 0xFC) out += 'u';
        break;  // anything else (emoji, Urdu script) is dropped
    }
  }
  return out;
}

String upperStr(String s) { s.toUpperCase(); return s; }

String centerLine(const String& text) {
  String t = upperStr(toAscii(text));
  t.trim();
  if ((int)t.length() > WIDTH) t = t.substring(0, WIDTH);
  String pad;
  for (int i = 0; i < (WIDTH - (int)t.length()) / 2; i++) pad += ' ';
  return pad + t;
}

String repeatChar(char c, int n) {
  String s;
  for (int i = 0; i < n; i++) s += c;
  return s;
}

String sectionHeader(const String& title) {
  String tag = " [ " + upperStr(toAscii(title)) + " ] ";
  if ((int)tag.length() >= WIDTH - 3) return ("-" + tag).substring(0, WIDTH);
  return "---" + tag + repeatChar('-', WIDTH - tag.length() - 3);
}

// Word-wrap `text` into lines of WIDTH, with `first` before the first line
// and `next` before continuation lines (hanging indent).
void wrapInto(std::vector<String>& out, const String& text, const String& first, const String& next) {
  String t = text;
  t.trim();
  String line = first;
  bool empty = true;
  int i = 0;
  while (i < (int)t.length()) {
    while (i < (int)t.length() && t[i] == ' ') i++;
    int j = i;
    while (j < (int)t.length() && t[j] != ' ') j++;
    if (j == i) break;
    String word = t.substring(i, j);
    i = j;
    while (word.length() > 0) {
      int room = WIDTH - (int)line.length() - (empty ? 0 : 1);
      if ((int)word.length() <= room) {
        if (!empty) line += ' ';
        line += word;
        empty = false;
        word = "";
      } else if (!empty) {
        out.push_back(line);
        line = next;
        empty = true;
      } else {  // a single word longer than the line: hard split
        int fit = WIDTH - (int)line.length();
        line += word.substring(0, fit);
        word = word.substring(fit);
        out.push_back(line);
        line = next;
      }
    }
  }
  if (!empty) out.push_back(line);
}

String bold(const String& line) { return String(STYLE_BOLD) + line; }

// Headline: double width+height when it fits in 16 chars, else bold.
String headline(const String& text) {
  String t = upperStr(toAscii(text));
  t.trim();
  if ((int)t.length() > WIDTH / 2) return bold(centerLine(t));
  String pad;
  for (int i = 0; i < (WIDTH / 2 - (int)t.length()) / 2; i++) pad += ' ';
  return String(STYLE_BIG) + pad + t;
}

String buildReceipt(const String& title, const std::vector<String>& body,
                    const String& subtitle, const String& badge) {
  std::vector<String> lines;
  lines.push_back(repeatChar('=', WIDTH));
  lines.push_back(headline(title));
  lines.push_back(repeatChar('=', WIDTH));
  lines.push_back(bold(centerLine(USER_NAME)));
  lines.push_back(centerLine(subtitle));
  if (timeValid()) lines.push_back(centerLine(fmtTime("%a %b %d, %Y | %H:%M", time(nullptr))));
  lines.push_back(repeatChar('-', WIDTH));

  for (const String& raw : body) {
    String l = toAscii(raw);
    l.trim();
    if (l.length() == 0) continue;
    if (l.startsWith("[SECTION:")) {
      String name = l.substring(9);
      if (name.endsWith("]")) name.remove(name.length() - 1);
      lines.push_back(bold(sectionHeader(name)));
    } else if (l.startsWith("[ ]") || l.startsWith("[]") || l.startsWith("* ") || l.startsWith("- ")) {
      int cut = l.startsWith("[ ]") ? 3 : 2;
      String item = l.substring(cut);
      wrapInto(lines, item, "[ ] ", "    ");
    } else if (l.startsWith("---") || l.startsWith("===")) {
      lines.push_back(l.substring(0, min((int)l.length(), WIDTH)));
    } else {
      wrapInto(lines, l, "", "");
    }
  }
  lines.push_back(repeatChar('-', WIDTH));
  lines.push_back(bold(centerLine(badge)));
  lines.push_back(repeatChar('=', WIDTH));

  String out;
  for (const String& l : lines) out += l + "\n";
  return out;
}

// ===========================================================================
//  Printer
// ===========================================================================
void printText(const String& text) {
  Serial2.write(0x1B);  // ESC @ : reset printer formatting
  Serial2.write('@');
  delay(50);
  int start = 0;
  while (start < (int)text.length()) {
    int nl = text.indexOf('\n', start);
    if (nl < 0) nl = text.length();
    String line = text.substring(start, nl);
    char style = line.length() ? line[0] : 0;
    if (style == STYLE_BIG || style == STYLE_BOLD) line.remove(0, 1);
    if (PRINTER_STYLES && style == STYLE_BIG) {
      Serial2.write(0x1D); Serial2.write('!'); Serial2.write(0x11);  // GS ! : double width+height
    } else if (PRINTER_STYLES && style == STYLE_BOLD) {
      Serial2.write(0x1B); Serial2.write('E'); Serial2.write(1);     // ESC E 1 : bold on
    }
    Serial2.print(line);
    Serial2.print('\n');
    if (PRINTER_STYLES && style == STYLE_BIG) { Serial2.write(0x1D); Serial2.write('!'); Serial2.write(0); }
    if (PRINTER_STYLES && style == STYLE_BOLD) { Serial2.write(0x1B); Serial2.write('E'); Serial2.write(0); }
    Serial2.flush();
    delay(style == STYLE_BIG ? LINE_DELAY_MS * 2 : LINE_DELAY_MS);
    start = nl + 1;
  }
  for (int i = 0; i < FEED_LINES; i++) { Serial2.print('\n'); delay(LINE_DELAY_MS); }
  Serial2.flush();
  Serial.println("[PRINT] done");
}

void printReceipt(const String& title, const std::vector<String>& body,
                  const String& subtitle, const String& badge) {
  String r = buildReceipt(title, body, subtitle, badge);
  Serial.print(r);
  printText(r);
}

// ===========================================================================
//  Storage (LittleFS JSON)
// ===========================================================================
void ensureDbShape() {
  if (!db["habits"].is<JsonArray>()) db["habits"].to<JsonArray>();
  if (!db["tasks"].is<JsonArray>()) db["tasks"].to<JsonArray>();
  if (!db["sched"].is<JsonArray>()) db["sched"].to<JsonArray>();
  if (!db["morning"].is<const char*>()) db["morning"] = "";
  if (!db["streaks"].is<JsonObject>()) db["streaks"].to<JsonObject>();
  if (!db["outages"].is<JsonArray>()) db["outages"].to<JsonArray>();
}

void loadDb() {
  File f = LittleFS.open(DB_PATH, "r");
  if (f) {
    DeserializationError e = deserializeJson(db, f);
    f.close();
    if (e) { Serial.printf("[DB] corrupt (%s), starting fresh\n", e.c_str()); db.clear(); }
  }
  ensureDbShape();
}

void saveDb() {
  File f = LittleFS.open("/data.tmp", "w");
  if (!f) { Serial.println("[DB] save failed"); return; }
  serializeJson(db, f);
  f.close();
  LittleFS.remove(DB_PATH);
  LittleFS.rename("/data.tmp", DB_PATH);
}

String cleanTitle(const String& raw) {
  String t = toAscii(raw);
  t.trim();
  while (t.indexOf("  ") >= 0) t.replace("  ", " ");
  if ((int)t.length() > TITLE_MAX) {
    int cut = t.lastIndexOf(' ', TITLE_MAX);
    t = t.substring(0, cut > 10 ? cut : TITLE_MAX);
  }
  return t.length() ? t : String("Untitled");
}

// --- fuzzy matching: "done with assignment" -> "Submit assignment" ---------
bool isStopword(const String& w) {
  static const char* STOP[] = {"the", "and", "for", "with", "done", "task", "habit", "my", "to", "of", "on", "in", "at"};
  for (const char* s : STOP) if (w == s) return true;
  return false;
}

std::vector<String> wordsOf(const String& text) {
  std::vector<String> words;
  String t = toAscii(text);
  t.toLowerCase();
  String cur;
  for (int i = 0; i <= (int)t.length(); i++) {
    char c = i < (int)t.length() ? t[i] : ' ';
    if (isalnum((unsigned char)c)) cur += c;
    else {
      if (cur.length() >= 3 && !isStopword(cur)) words.push_back(cur);
      cur = "";
    }
  }
  return words;
}

// Returns index of best match in `titles`, or -1.
int bestMatch(const String& query, const std::vector<String>& titles) {
  String q = toAscii(query);
  q.trim();
  q.toLowerCase();
  if (!q.length()) return -1;
  for (size_t i = 0; i < titles.size(); i++) {
    String t = titles[i];
    t.toLowerCase();
    if (t == q) return i;
  }
  std::vector<String> qw = wordsOf(q);
  int best = -1, bestScore = 0;
  for (size_t i = 0; i < titles.size(); i++) {
    std::vector<String> tw = wordsOf(titles[i]);
    int score = 0;
    for (const String& a : qw)
      for (const String& b : tw)
        if (a.startsWith(b) || b.startsWith(a)) { score++; break; }
    if (score > bestScore) { bestScore = score; best = i; }
  }
  return best;
}

// ===========================================================================
//  HTTP helpers
// ===========================================================================
// Returns HTTP status (negative on connection error); response body in `resp`.
int httpsRequest(const String& url, const char* method, const String& body,
                 const char* hdrName, const char* hdrValue, String& resp, uint32_t timeoutMs) {
  WiFiClientSecure client;
  client.setInsecure();  // home device; avoids shipping CA bundles
  HTTPClient http;
  if (!http.begin(client, url)) return -1;
  http.setConnectTimeout(10000);
  http.setTimeout(timeoutMs);
  if (hdrName) http.addHeader(hdrName, hdrValue);
  if (body.length()) http.addHeader("Content-Type", "application/json");
  int code = strcmp(method, "POST") == 0 ? http.POST(body) : http.GET();
  resp = code > 0 ? http.getString() : http.errorToString(code);
  http.end();
  return code;
}

int relayRequest(const char* path, const char* method, const String& body, String& resp) {
  return httpsRequest(String(RELAY_URL) + path, method, body, "X-API-Key", DEVICE_API_KEY, resp, 15000);
}

void sendWhatsApp(const String& text) {
  JsonDocument d;
  d["text"] = text;
  String body, resp;
  serializeJson(d, body);
  int code = relayRequest("/reply", "POST", body, resp);
  Serial.printf("[REPLY] HTTP %d\n", code);
}

// ===========================================================================
//  Weather (Open-Meteo, no key)
// ===========================================================================
const char* weatherText(int code) {
  switch (code) {
    case 0: return "Clear"; case 1: return "Mostly clear"; case 2: return "Partly cloudy";
    case 3: return "Overcast"; case 45: case 48: return "Fog";
    case 51: case 53: case 55: return "Drizzle";
    case 61: return "Light rain"; case 63: return "Rain"; case 65: return "Heavy rain";
    case 71: case 73: case 75: return "Snow";
    case 80: case 81: case 82: return "Showers";
    case 95: case 96: case 99: return "Thunderstorm";
    default: return "";
  }
}

void addWeather(std::vector<String>& body) {
  String url = String("https://api.open-meteo.com/v1/forecast?latitude=") + WEATHER_LAT +
               "&longitude=" + WEATHER_LON +
               "&current=temperature_2m,weather_code"
               "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max"
               "&timezone=auto&forecast_days=1";
  String resp;
  int code = httpsRequest(url, "GET", "", nullptr, nullptr, resp, 10000);
  JsonDocument w;
  if (code != 200 || deserializeJson(w, resp)) {
    body.push_back(String(CITY_NAME) + ": weather unavailable");
    return;
  }
  int cur = lround(w["current"]["temperature_2m"].as<float>());
  int hi = lround(w["daily"]["temperature_2m_max"][0].as<float>());
  int lo = lround(w["daily"]["temperature_2m_min"][0].as<float>());
  int rain = w["daily"]["precipitation_probability_max"][0] | 0;
  body.push_back(String(CITY_NAME) + ": " + cur + "C " + weatherText(w["current"]["weather_code"] | -1));
  body.push_back("High " + String(hi) + "C / Low " + String(lo) + "C | Rain " + String(rain) + "%");
}

// ===========================================================================
//  Receipts
// ===========================================================================
// ===========================================================================
//  Prayer times + Hijri date (api.aladhan.com, no key)
// ===========================================================================
void addPrayerTimes(std::vector<String>& body) {
  if (!PRAYER_TIMES_ENABLED) return;
  String url = "https://api.aladhan.com/v1/timings/" + fmtTime("%d-%m-%Y", time(nullptr)) +
               "?latitude=" WEATHER_LAT "&longitude=" WEATHER_LON
               "&method=" + String(PRAYER_METHOD) + "&school=" + String(PRAYER_SCHOOL);
  String resp;
  int code = httpsRequest(url, "GET", "", nullptr, nullptr, resp, 10000);
  JsonDocument filter;
  filter["data"]["timings"] = true;
  filter["data"]["date"]["hijri"]["day"] = true;
  filter["data"]["date"]["hijri"]["month"]["en"] = true;
  filter["data"]["date"]["hijri"]["year"] = true;
  JsonDocument p;
  if (code != 200 || deserializeJson(p, resp, DeserializationOption::Filter(filter))) {
    Serial.printf("[PRAYER] HTTP %d\n", code);
    return;  // nice-to-have: skip the section rather than print an error
  }
  JsonObject t = p["data"]["timings"];
  JsonObject h = p["data"]["date"]["hijri"];
  body.push_back("[SECTION:PRAYER TIMES]");
  body.push_back(String(h["day"] | "") + " " + String(h["month"]["en"] | "") + " " + String(h["year"] | "") + " AH");
  const char* names[] = {"Fajr", "Sunrise", "Dhuhr", "Asr", "Maghrib", "Isha"};
  for (int i = 0; i < 6; i += 2) {
    char line[40];
    snprintf(line, sizeof(line), "%-7s %.5s    %-7s %.5s",
             names[i], (const char*)(t[names[i]] | "--:--"),
             names[i + 1], (const char*)(t[names[i + 1]] | "--:--"));
    body.push_back(line);
  }
}

// ===========================================================================
//  Habit streaks
// ===========================================================================
String yesterdayStr() { return fmtTime("%Y-%m-%d", time(nullptr) - 86400); }

// Streak counts while the habit was done today or yesterday; otherwise 0.
int currentStreak(const String& habit) {
  JsonObject s = db["streaks"][habit];
  if (s.isNull()) return 0;
  String last = s["last"] | "";
  return (last == todayStr() || last == yesterdayStr()) ? (s["n"] | 0) : 0;
}

String habitLine(const String& habit) {
  int n = currentStreak(habit);
  return "[ ] " + habit + (n >= 2 ? " (x" + String(n) + ")" : "");
}

// Marks a habit done today; returns the new streak length.
int markHabitDone(const String& habit) {
  JsonObject streaks = db["streaks"];
  int n = 0, best = 0;
  String last;
  if (streaks[habit].is<JsonObject>()) {
    n = streaks[habit]["n"] | 0;
    best = streaks[habit]["best"] | 0;
    last = streaks[habit]["last"] | "";
  }
  if (last == todayStr()) return n;
  n = (last == yesterdayStr()) ? n + 1 : 1;
  JsonObject s = streaks[habit].to<JsonObject>();
  s["n"] = n;
  s["best"] = max(best, n);
  s["last"] = todayStr();
  return n;
}

// ===========================================================================
//  Power-cut tracking (load-shedding report)
// ===========================================================================
// While powered, the ESP32 saves the time every 5 minutes. After a power-on
// reset, the gap between that saved time and "now" was an outage.
void saveHeartbeat() {
  if (!timeValid()) return;
  File f = LittleFS.open(HB_PATH, "w");
  if (f) { f.print((long)time(nullptr)); f.close(); }
}

time_t readHeartbeat() {
  File f = LittleFS.open(HB_PATH, "r");
  if (!f) return 0;
  long v = f.parseInt();
  f.close();
  return (time_t)v;
}

String duration(long secs) {
  long m = secs / 60;
  return m >= 60 ? String(m / 60) + "h" + String(m % 60) + "m" : String(m) + "m";
}

void resolveOutage() {
  if (!pendingOutageStart || !timeValid()) return;
  time_t now = time(nullptr);
  if (now - pendingOutageStart >= MIN_OUTAGE_SEC) {
    JsonArray out = db["outages"];
    JsonObject o = out.add<JsonObject>();
    o["s"] = (long long)pendingOutageStart;
    o["e"] = (long long)now;
    while (out.size() > MAX_OUTAGES) out.remove(0);
    saveDb();
    Serial.println("[POWER] outage recorded: " + duration(now - pendingOutageStart));
  }
  pendingOutageStart = 0;
  saveHeartbeat();
}

void addPowerReport(std::vector<String>& body) {
  time_t now = time(nullptr);
  long total = 0;
  std::vector<String> rows;
  for (JsonObject o : db["outages"].as<JsonArray>()) {
    time_t s = (time_t)(o["s"] | 0LL), e = (time_t)(o["e"] | 0LL);
    if (e < now - 86400) continue;
    total += e - s;
    rows.push_back("~" + fmtTime("%H:%M", s) + " - " + fmtTime("%H:%M", e) + "  (" + duration(e - s) + ")");
  }
  if (rows.empty()) return;
  body.push_back("[SECTION:POWER REPORT]");
  for (const String& r : rows) body.push_back(r);
  body.push_back("Total last 24h: " + duration(total));
}

String outagesText() {
  JsonArray out = db["outages"];
  if (!out.size()) return "No power cuts recorded.";
  String month = fmtTime("%Y-%m", time(nullptr)), s = "*Recent power cuts:*";
  long monthTotal = 0;
  for (JsonObject o : out) {
    time_t a = (time_t)(o["s"] | 0LL), b = (time_t)(o["e"] | 0LL);
    s += "\n  - " + fmtTime("%a %d %b %H:%M", a) + " -> " + fmtTime("%H:%M", b) + " (" + duration(b - a) + ")";
    if (fmtTime("%Y-%m", a) == month) monthTotal += b - a;
  }
  return s + "\nThis month: " + duration(monthTotal);
}

void printMorningPaper() {
  String today = todayStr();
  std::vector<String> body;
  body.push_back("[SECTION:WEATHER]");
  addWeather(body);
  addPrayerTimes(body);
  addPowerReport(body);

  body.push_back("[SECTION:DAILY HABITS]");
  JsonArray habits = db["habits"];
  if (habits.size() == 0) body.push_back("No habits yet - add some on WhatsApp.");
  for (JsonVariant h : habits) body.push_back(habitLine(h.as<String>()));

  body.push_back("[SECTION:TODAY'S TASKS]");
  std::vector<String> overdue;
  int dueToday = 0;
  for (JsonObject t : db["tasks"].as<JsonArray>()) {
    String d = t["d"] | "";
    if (d == today) { body.push_back("[ ] " + t["t"].as<String>()); dueToday++; }
    else if (d < today) overdue.push_back("[ ] " + t["t"].as<String>() + " (" + d.substring(5) + ")");
  }
  if (!dueToday) body.push_back("Nothing due today.");
  if (overdue.size()) {
    body.push_back("[SECTION:OVERDUE]");
    for (const String& o : overdue) body.push_back(o);
  }
  printReceipt("MORNING EDITION", body, "DAILY BRIEF", "OWN THE DAY");
}

void printTemplate(const String& zone) {
  std::vector<String> b;
  time_t now = time(nullptr);
  if (zone == "desk") {
    b.push_back("Window: " + fmtTime("%H:%M", now) + " -> " + fmtTime("%H:%M", now + 45 * 60));
    b.push_back("[SECTION:FOCUS PROTOCOL]");
    b.push_back("[ ] Single high-value objective");
    b.push_back("[ ] Phone on Do Not Disturb");
    b.push_back("[ ] Close all unrelated tabs");
    b.push_back("[ ] Hydrate & maintain posture");
    b.push_back("[ ] 5-min post-sprint debrief");
    printReceipt("DEEP WORK SPRINT", b, "FOCUS TICKET (45 MIN)", "ZERO DISTRACTIONS");
  } else if (zone == "door") {
    b.push_back("[SECTION:DEPARTURE CHECKLIST]");
    b.push_back("[ ] House & Car Keys");
    b.push_back("[ ] Wallet, CNIC & Cards");
    b.push_back("[ ] University Student ID");
    b.push_back("[ ] Laptop & USB-C Charger");
    b.push_back("[ ] Earbuds & Power Bank");
    printReceipt("POCKET CHECK", b, "DEPARTURE MANIFEST", "SAFE TRAVELS");
  } else if (zone == "bedside") {
    String tomorrow = fmtTime("%Y-%m-%d", now + 86400);
    b.push_back("Target Date: " + tomorrow);
    b.push_back("[SECTION:TOMORROW'S AGENDA]");
    int n = 0;
    for (JsonObject t : db["tasks"].as<JsonArray>())
      if (tomorrow == (t["d"] | "")) { b.push_back("[ ] " + t["t"].as<String>()); n++; }
    if (!n) b.push_back("No fixed tasks scheduled.");
    b.push_back("[SECTION:NIGHTLY RECOVERY]");
    b.push_back("[ ] Set wake-up alarm");
    b.push_back("[ ] Phone docked away from bed");
    b.push_back("[ ] Target 8h restorative sleep");
    printReceipt("TOMORROW PREVIEW", b, "NIGHTLY BRIEFING", "REST DEEPLY");
  } else if (zone == "evening") {
    String today = todayStr();
    b.push_back("[SECTION:REMAINING TASKS]");
    int n = 0;
    for (JsonObject t : db["tasks"].as<JsonArray>())
      if (String(t["d"] | "") <= today) { b.push_back("[ ] " + t["t"].as<String>()); n++; }
    if (!n) b.push_back("All tasks completed!");
    if (db["habits"].size()) {
      b.push_back("[SECTION:DAILY HABITS CHECK]");
      for (JsonVariant h : db["habits"].as<JsonArray>()) {
        String name = h.as<String>();
        if (String(db["streaks"][name]["last"] | "") == today) b.push_back("[x] " + name + " - done");
        else b.push_back(habitLine(name));
      }
    }
    b.push_back("[SECTION:EVENING PROTOCOL]");
    b.push_back("[ ] Log daily accomplishments");
    b.push_back("[ ] Review tomorrow's priorities");
    b.push_back("[ ] Digital wind-down");
    printReceipt("EVENING CHECK-IN", b, "WELCOME BACK", "WRAP UP STRONG");
  } else if (zone == "today") {
    String today = todayStr();
    b.push_back("[SECTION:TO DO]");
    int n = 0;
    for (JsonObject t : db["tasks"].as<JsonArray>()) {
      String d = t["d"] | "";
      if (d == today) { b.push_back("[ ] " + t["t"].as<String>()); n++; }
      else if (d < today) { b.push_back("[ ] " + t["t"].as<String>() + " (late)"); n++; }
    }
    if (!n) b.push_back("Nothing due. Enjoy the day!");
    std::vector<String> pending;
    for (JsonVariant h : db["habits"].as<JsonArray>()) {
      String name = h.as<String>();
      if (String(db["streaks"][name]["last"] | "") != today) pending.push_back(habitLine(name));
    }
    if (pending.size()) {
      b.push_back("[SECTION:HABITS LEFT]");
      for (const String& p : pending) b.push_back(p);
    }
    printReceipt("TO-DO", b, "RIGHT NOW", "ONE THING AT A TIME");
  } else if (zone == "morning") {
    printMorningPaper();
  }
}

// ===========================================================================
//  Actions
// ===========================================================================
String listText() {
  String out;
  JsonArray habits = db["habits"];
  JsonArray tasks = db["tasks"];
  String today = todayStr();
  if (habits.size()) {
    out += "*Daily habits:*\n";
    for (JsonVariant h : habits) out += "  - " + h.as<String>() + "\n";
  }
  if (tasks.size()) {
    out += "*Open tasks:*\n";
    for (JsonObject t : tasks) {
      String d = t["d"] | "";
      out += "  - " + t["t"].as<String>() + " - " + d + (d < today ? " (OVERDUE)" : "") + "\n";
    }
  }
  return out.length() ? out : String("No habits or tasks yet. Send me some!");
}

void sortTasks() {
  // simple insertion sort by due date (lists are small)
  JsonArray tasks = db["tasks"];
  std::vector<std::pair<String, String>> v;
  for (JsonObject t : tasks) v.push_back({t["d"].as<String>(), t["t"].as<String>()});
  for (size_t i = 1; i < v.size(); i++)
    for (size_t j = i; j > 0 && v[j].first < v[j - 1].first; j--) std::swap(v[j], v[j - 1]);
  tasks.clear();
  for (auto& p : v) { JsonObject o = tasks.add<JsonObject>(); o["t"] = p.second; o["d"] = p.first; }
}

String actAddDaily(JsonArray items) {
  JsonArray habits = db["habits"];
  String out = "Added daily habit(s):";
  for (JsonObject it : items) {
    if (habits.size() >= MAX_HABITS) { out += "\n(habit limit reached)"; break; }
    String t = cleanTitle(it["title"] | "");
    habits.add(t);
    out += "\n- " + t;
  }
  saveDb();
  return out;
}

String actAddDated(JsonArray items) {
  JsonArray tasks = db["tasks"];
  String out = "Added task(s):";
  for (JsonObject it : items) {
    if (tasks.size() >= MAX_TASKS) { out += "\n(task limit reached)"; break; }
    String t = cleanTitle(it["title"] | "");
    String d = validDateOrToday(it["due_date"] | "");
    JsonObject o = tasks.add<JsonObject>();
    o["t"] = t;
    o["d"] = d;
    out += "\n- " + t + " (due " + d + ")";
  }
  sortTasks();
  saveDb();
  return out;
}

// Removes matching entries from a list. For tasks, entries are objects with "t".
String actRemove(JsonArray items, bool tasksList) {
  JsonArray arr = tasksList ? db["tasks"].as<JsonArray>() : db["habits"].as<JsonArray>();
  String done, missing;
  for (JsonObject it : items) {
    std::vector<String> titles;
    for (JsonVariant v : arr) titles.push_back(tasksList ? v["t"].as<String>() : v.as<String>());
    String q = it["title"] | "";
    int idx = bestMatch(q, titles);
    if (idx >= 0) {
      done += "\n- " + titles[idx];
      if (!tasksList) db["streaks"].remove(titles[idx]);
      arr.remove(idx);
    }
    else missing += String(missing.length() ? ", " : "") + q;
  }
  saveDb();
  String out;
  if (done.length()) out += String(tasksList ? "Marked done:" : "Removed habit(s):") + done;
  if (missing.length()) out += String(out.length() ? "\n" : "") + "Couldn't find: " + missing + ". Send 'list' to see them.";
  return out.length() ? out : String("Nothing changed.");
}

String actHabitDone(JsonArray items) {
  std::vector<String> titles;
  for (JsonVariant v : db["habits"].as<JsonArray>()) titles.push_back(v.as<String>());
  String out, missing;
  for (JsonObject it : items) {
    String q = it["title"] | "";
    int idx = bestMatch(q, titles);
    if (idx < 0) { missing += String(missing.length() ? ", " : "") + q; continue; }
    int n = markHabitDone(titles[idx]);
    int best = db["streaks"][titles[idx]]["best"] | n;
    out += "\n- " + titles[idx] + ": day " + String(n) + (n >= best && n >= 3 ? " (new best!)" : "");
  }
  saveDb();
  String r = out.length() ? "Nice! Streaks:" + out : String("");
  if (missing.length()) r += String(r.length() ? "\n" : "") + "Not a habit yet: " + missing + ". Say 'add habit ...' first.";
  return r.length() ? r : String("Nothing changed.");
}

String streaksText() {
  JsonArray habits = db["habits"];
  if (!habits.size()) return "No habits yet.";
  String s = "*Streaks:*";
  for (JsonVariant h : habits) {
    String name = h.as<String>();
    int best = db["streaks"][name]["best"] | 0;
    s += "\n  - " + name + ": " + String(currentStreak(name)) + " days (best " + String(best) + ")";
  }
  return s;
}

std::vector<String> toLines(JsonArray arr) {
  std::vector<String> v;
  for (JsonVariant l : arr) v.push_back(l.as<String>());
  return v;
}

String actSchedule(JsonDocument& d) {
  String when = d["schedule_at"] | "";
  time_t at = parseLocalDateTime(when);
  if (!at) return "Couldn't understand the time '" + when + "'. Try e.g. 'at 7:30 PM'.";
  String title = d["receipt_title"] | "REMINDER";
  if (at <= time(nullptr) + 30) {
    printReceipt(title, toLines(d["receipt_lines"].as<JsonArray>()), "PERSONAL NEWSPAPER", "REMINDER");
    return "That time has passed, so I printed it now.";
  }
  JsonArray sched = db["sched"];
  if (sched.size() >= MAX_SCHEDULED) return "Too many scheduled prints (max " + String(MAX_SCHEDULED) + ").";
  JsonObject s = sched.add<JsonObject>();
  s["at"] = (long long)at;
  s["title"] = title;
  JsonArray lines = s["lines"].to<JsonArray>();
  for (JsonVariant l : d["receipt_lines"].as<JsonArray>()) lines.add(toAscii(l.as<String>()));
  saveDb();
  return "Will print at " + fmtTime("%a %d %b, %I:%M %p", at) + ".";
}

String runDecision(JsonDocument& d) {
  String action = d["action"] | "chat";
  String reply = d["reply"] | "";
  JsonArray items = d["items"].as<JsonArray>();
  Serial.printf("[ACTION] %s\n", action.c_str());

  if (action == "add_daily") return actAddDaily(items);
  if (action == "add_dated") return actAddDated(items);
  if (action == "complete_task") return actRemove(items, true);
  if (action == "remove_habit") return actRemove(items, false);
  if (action == "habit_done") return actHabitDone(items);
  if (action == "list_tasks") return listText();
  if (action == "print_morning") { printMorningPaper(); return "Printed today's morning paper."; }
  if (action == "schedule_print") return actSchedule(d);
  if (action == "print_now") {
    printReceipt(d["receipt_title"] | "NOTE", toLines(d["receipt_lines"].as<JsonArray>()), "PERSONAL NEWSPAPER", "OFFICIAL DISPATCH");
    return reply.length() ? reply : String("Printed.");
  }
  return reply.length() ? reply : String("OK.");
}

// ===========================================================================
//  Gemini
// ===========================================================================
static const char* DECISION_SCHEMA = R"JSON({
  "type": "OBJECT",
  "properties": {
    "action": {"type": "STRING", "enum": ["add_daily","add_dated","complete_task","remove_habit","habit_done","print_now","print_morning","schedule_print","list_tasks","chat"]},
    "items": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {"title": {"type": "STRING"}, "due_date": {"type": "STRING"}}, "required": ["title"]}},
    "receipt_title": {"type": "STRING"},
    "receipt_lines": {"type": "ARRAY", "items": {"type": "STRING"}},
    "schedule_at": {"type": "STRING"},
    "reply": {"type": "STRING"}
  },
  "required": ["action", "reply"]
})JSON";

String systemPrompt() {
  time_t now = time(nullptr);
  String p;
  p.reserve(3000);
  p += "You are the assistant behind " USER_NAME "'s thermal receipt printer. ";
  p += "Now: " + fmtTime("%Y-%m-%d (%A) %H:%M", now) + " local time (" TZ_INFO ").\n\n";
  p += "Turn the user's WhatsApp message into exactly ONE action:\n"
       "- add_daily: recurring daily habit(s) -> 'items' (title, short).\n"
       "- add_dated: task(s) with a deadline -> 'items' with title and due_date YYYY-MM-DD "
       "(resolve 'tomorrow', 'Friday' etc. from today; default today).\n"
       "- complete_task: user finished or wants to delete task(s) -> 'items' with the EXACT existing task title(s) from the list below.\n"
       "- remove_habit: stop habit(s) -> 'items' with the EXACT existing habit title(s) from the list below.\n"
       "- habit_done: user did a habit today (e.g. 'did gym', 'read today') -> 'items' with the EXACT existing habit title(s).\n"
       "- print_now: print something now (list, note, plan) -> receipt_title + receipt_lines.\n"
       "- print_morning: re-print today's morning paper.\n"
       "- schedule_print: print later -> schedule_at as 'YYYY-MM-DD HH:MM' local time, plus receipt_title + receipt_lines.\n"
       "- list_tasks: user asks what habits/tasks they have.\n"
       "- chat: anything else (question, greeting) -> just answer in 'reply'.\n\n"
       "Receipt rules (58mm paper, 32 chars per line): receipt_title UPPERCASE, max 26 chars. "
       "Actionable/checklist/shopping lines start with '[ ] '. A line '[SECTION:NAME]' starts a section. "
       "Keep lines short, never drop details, quantities or times. Plain English ASCII only: "
       "no emoji, no Urdu script (use Roman Urdu).\n"
       "reply: one short friendly WhatsApp message.\n\n";
  p += "Current habits:";
  for (JsonVariant h : db["habits"].as<JsonArray>()) p += "\n- " + h.as<String>();
  if (!db["habits"].size()) p += " none";
  p += "\nOpen tasks:";
  for (JsonObject t : db["tasks"].as<JsonArray>()) p += "\n- " + t["t"].as<String>() + " (due " + t["d"].as<String>() + ")";
  if (!db["tasks"].size()) p += " none";
  return p;
}

bool askGemini(const String& userText, JsonDocument& decision, String& err) {
  JsonDocument req;
  req["system_instruction"]["parts"][0]["text"] = systemPrompt();
  JsonObject content = req["contents"].add<JsonObject>();
  content["role"] = "user";
  content["parts"][0]["text"] = userText;
  JsonObject gc = req["generationConfig"].to<JsonObject>();
  gc["responseMimeType"] = "application/json";
  gc["responseSchema"] = serialized(DECISION_SCHEMA);
  gc["temperature"] = 0.3;
  gc["thinkingConfig"]["thinkingBudget"] = 0;

  String body, resp;
  serializeJson(req, body);
  String url = String("https://generativelanguage.googleapis.com/v1beta/models/") + GEMINI_MODEL + ":generateContent";
  int code = httpsRequest(url, "POST", body, "x-goog-api-key", GEMINI_API_KEY, resp, 30000);
  if (code != 200) {
    err = "Gemini error " + String(code) + ": " + resp.substring(0, 160);
    return false;
  }

  JsonDocument filter;
  filter["candidates"][0]["content"]["parts"][0]["text"] = true;
  JsonDocument r;
  DeserializationError e = deserializeJson(r, resp, DeserializationOption::Filter(filter));
  if (e) { err = String("Bad Gemini response: ") + e.c_str(); return false; }
  const char* text = r["candidates"][0]["content"]["parts"][0]["text"] | "";
  e = deserializeJson(decision, text);
  if (e) { err = String("Gemini returned invalid JSON: ") + e.c_str(); return false; }
  return true;
}

// ===========================================================================
//  Incoming WhatsApp messages
// ===========================================================================
String handleMessage(const String& text) {
  String t = text;
  t.trim();
  String lower = t;
  lower.toLowerCase();

  // Instant commands that work even if Gemini is down.
  if (lower == "list") return listText();
  if (lower == "streaks") return streaksText();
  if (lower == "outages" || lower == "power") return outagesText();
  if (lower == "todo" || lower == "to-do") { printTemplate("today"); return "Printed your to-do list."; }
  if (lower == "paper" || lower == "morning") { printMorningPaper(); return "Printed today's morning paper."; }
  if (lower == "status") {
    return "Online. IP " + WiFi.localIP().toString() + ", free heap " + String(ESP.getFreeHeap()) +
           ", " + String(db["habits"].size()) + " habits, " + String(db["tasks"].size()) + " tasks, " +
           String(db["sched"].size()) + " scheduled.";
  }
  if (lower == "help") {
    return "Just talk normally, e.g.\n- add habit: read 10 pages\n- submit report on Friday\n"
           "- done with the report\n- did gym today\n- print: milk, eggs, bread\n- remind me to call Ammi at 8pm\n"
           "Shortcuts: list, todo, paper, streaks, outages, status";
  }

  JsonDocument decision;
  String err;
  if (!askGemini(t, decision, err)) {
    Serial.println("[GEMINI] " + err);
    return "Sorry, I couldn't reach Gemini (" + err.substring(0, 120) + "). Try again in a minute.";
  }
  return runDecision(decision);
}

void pollInbox() {
  String resp;
  int code = relayRequest("/inbox", "GET", "", resp);
  if (code != 200) {
    Serial.printf("[INBOX] HTTP %d %s\n", code, resp.substring(0, 80).c_str());
    return;
  }
  JsonDocument inbox;
  if (deserializeJson(inbox, resp)) return;
  for (JsonObject m : inbox["messages"].as<JsonArray>()) {
    String id = m["id"] | "";
    String text = m["text"] | "";
    Serial.println("[INBOX] " + text);
    String reply = handleMessage(text);
    sendWhatsApp(reply);

    JsonDocument ack;
    ack["ids"][0] = id;
    String body, r;
    serializeJson(ack, body);
    relayRequest("/inbox/ack", "POST", body, r);
  }
}

// ===========================================================================
//  Periodic checks: morning paper + scheduled prints
// ===========================================================================
void tick() {
  if (!timeValid()) return;
  resolveOutage();  // no-op unless we just came back from a power cut
  time_t now = time(nullptr);
  struct tm tmv;
  localtime_r(&now, &tmv);

  int minutes = tmv.tm_hour * 60 + tmv.tm_min;
  int target = MORNING_HOUR * 60 + MORNING_MINUTE;
  String today = todayStr();
  if (minutes >= target && minutes < target + MORNING_GRACE_MIN && today != (db["morning"] | "")) {
    db["morning"] = today;
    saveDb();
    printMorningPaper();
  }

  JsonArray sched = db["sched"];
  bool changed = false;
  for (int i = (int)sched.size() - 1; i >= 0; i--) {
    JsonObject s = sched[i];
    time_t at = (time_t)(s["at"] | 0LL);
    if (at && at <= now) {
      if (now - at < 6 * 3600) {
        printReceipt(s["title"] | "REMINDER", toLines(s["lines"].as<JsonArray>()), "PERSONAL NEWSPAPER", "REMINDER");
      }
      sched.remove(i);
      changed = true;
    }
  }
  if (changed) saveDb();
}

// ===========================================================================
//  Local web server (phone NFC tags / shortcuts on home Wi-Fi)
// ===========================================================================
void handleTrigger() {
  if (server.arg("key") != LOCAL_TRIGGER_KEY) { server.send(403, "text/plain", "forbidden"); return; }
  String zone = server.arg("zone");
  if (zone != "desk" && zone != "door" && zone != "bedside" && zone != "evening" && zone != "morning" && zone != "today") {
    server.send(404, "text/plain", "zones: desk, door, bedside, evening, morning, today");
    return;
  }
  server.send(200, "text/plain", "printing " + zone);
  printTemplate(zone);
}

void handleRoot() {
  String s = "Personal Newspaper\n\nTime: " + (timeValid() ? fmtTime("%Y-%m-%d %H:%M", time(nullptr)) : String("not synced")) +
             "\nHabits: " + String(db["habits"].size()) + "\nTasks: " + String(db["tasks"].size()) +
             "\nScheduled: " + String(db["sched"].size()) + "\nLast morning paper: " + (db["morning"] | "never") +
             "\nFree heap: " + String(ESP.getFreeHeap()) + "\n";
  server.send(200, "text/plain", s);
}

// ===========================================================================
//  Wi-Fi
// ===========================================================================
void ensureWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;
  Serial.println("[WIFI] connecting...");
  WiFi.disconnect();
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 15000) delay(250);
  if (WiFi.status() == WL_CONNECTED) Serial.printf("[WIFI] IP %s\n", WiFi.localIP().toString().c_str());
}

// ===========================================================================
//  Setup / loop
// ===========================================================================
void setup() {
  Serial.begin(115200);
  Serial.println("\n=== Personal Newspaper ===");
  Serial2.begin(PRINTER_BAUD, SERIAL_8N1, PRINTER_RX, PRINTER_TX);
  pinMode(PRESENCE_PIN, INPUT_PULLDOWN);
  if (BUTTON_ENABLED) pinMode(BUTTON_PIN, INPUT_PULLUP);

  if (!LittleFS.begin(true)) Serial.println("[FS] mount failed");
  loadDb();

  // A power-on (or brown-out) reset after running before = the power was cut.
  esp_reset_reason_t why = esp_reset_reason();
  if (why == ESP_RST_POWERON || why == ESP_RST_BROWNOUT) pendingOutageStart = readHeartbeat();

  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  ensureWiFi();

  configTzTime(TZ_INFO, "pool.ntp.org", "time.google.com");
  for (int i = 0; i < 40 && !timeValid(); i++) delay(250);
  Serial.println(timeValid() ? "[TIME] " + fmtTime("%Y-%m-%d %H:%M", time(nullptr)) : String("[TIME] not synced yet"));
  resolveOutage();

  if (MDNS.begin("newspaper")) MDNS.addService("http", "tcp", 80);
  server.on("/", handleRoot);
  server.on("/trigger", handleTrigger);
  server.begin();

  if (esp_reset_reason() == ESP_RST_POWERON) {
    printText(repeatChar('=', WIDTH) + "\n" + centerLine("NEWSPAPER ONLINE") + "\n" +
              centerLine(WiFi.status() == WL_CONNECTED ? "IP " + WiFi.localIP().toString() : String("NO WI-FI")) + "\n" +
              repeatChar('=', WIDTH));
  }
}

void loop() {
  ensureWiFi();
  server.handleClient();

  unsigned long now = millis();
  if (WiFi.status() == WL_CONNECTED && now - lastInboxPoll >= INBOX_POLL_MS) {
    lastInboxPoll = now;
    pollInbox();
  }
  if (now - lastTick >= TICK_MS) {
    lastTick = now;
    tick();
  }
  if (now - lastHeartbeat >= HEARTBEAT_MS && !pendingOutageStart) {
    lastHeartbeat = now;
    saveHeartbeat();
  }

  // Physical button: short press = today's to-do, long press = morning paper.
  if (BUTTON_ENABLED) {
    bool down = digitalRead(BUTTON_PIN) == LOW;
    if (down && !buttonDown && now - buttonPressedAt > 300) {
      buttonDown = true;
      buttonPressedAt = now;
    } else if (!down && buttonDown) {
      buttonDown = false;
      unsigned long held = millis() - buttonPressedAt;
      if (held >= LONG_PRESS_MS) printMorningPaper();
      else if (held > 40) printTemplate("today");
      buttonPressedAt = millis();  // debounce the release
    }
  }

  if (PRESENCE_ENABLED) {
    bool present = digitalRead(PRESENCE_PIN) == HIGH;
    if (present && !lastPresenceState && now - lastPresence > 30000) {
      lastPresence = now;
      time_t t = time(nullptr);
      struct tm tmv;
      localtime_r(&t, &tmv);
      if (timeValid() && tmv.tm_hour >= 16 && tmv.tm_hour < 23 && t - lastEveningCheckin > 4 * 3600) {
        lastEveningCheckin = t;
        printTemplate("evening");
      }
    }
    lastPresenceState = present;
  }

  delay(20);
}
