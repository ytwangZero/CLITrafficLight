/*
 * CLI Traffic Light firmware
 *
 * Listens for single-character commands (R/Y/G/O/M/U/W) over USB serial and
 * WiFi at the same time, driving a 3-color LED "traffic light" plus an
 * optional buzzer.
 *
 * Commands:
 *   R -> waiting (red blinks + buzzer for 5s, then keeps blinking silently
 *        until the next command arrives -- not a fixed duration)
 *   Y -> working (yellow solid)
 *   G -> done (green solid, two beeps)
 *   O -> idle (all off)
 *   M / U -> mute / unmute buzzer (persisted in NVS, survives reboot)
 *   W -> forget saved WiFi credentials and restart into setup mode
 *
 * WiFi setup: on first boot (or after W), the device opens a temporary
 * access point with a captive portal for entering WiFi credentials.
 * Requires the WiFiManager library (tzapu).
 *
 * Wiring: see README.md. R->GPIO25, Y->GPIO26, G->GPIO27, buzzer->GPIO14.
 */

#include <Preferences.h>
#include <WiFiManager.h>
#include <WebServer.h>
#include <ESPmDNS.h>

const int PIN_RED = 25;
const int PIN_YELLOW = 26;
const int PIN_GREEN = 27;
const int PIN_BUZZER = 14;

const char* AP_SETUP_NAME = "CLITrafficLight-Setup"; // temporary AP shown during WiFi setup
const char* MDNS_NAME = "cli-light"; // reachable at http://cli-light.local once connected
const unsigned long WIFI_PORTAL_TIMEOUT_S = 180; // give up and fall back to serial after this long

WiFiManager wifiManager;
WebServer server(80);
bool wifiReady = false;
volatile char pendingWifiCommand = 0;

Preferences prefs;
bool buzzerMuted = false;

void setLeds(bool r, bool y, bool g) {
  digitalWrite(PIN_RED, r ? HIGH : LOW);
  digitalWrite(PIN_YELLOW, y ? HIGH : LOW);
  digitalWrite(PIN_GREEN, g ? HIGH : LOW);
}

void buzzerBeep(int onMs, int offMs, int times) {
  for (int i = 0; i < times; i++) {
    if (!buzzerMuted) digitalWrite(PIN_BUZZER, HIGH);
    delay(onMs);
    digitalWrite(PIN_BUZZER, LOW);
    if (i < times - 1) delay(offMs);
  }
}

const unsigned long BUZZER_DURATION_MS = 5000;
const unsigned long BLINK_INTERVAL_MS = 300;

void enterWaiting() {
  setLeds(true, false, false);
  unsigned long start = millis();
  bool on = true;
  while (true) {
    if (wifiReady) server.handleClient(); // keep serving HTTP while blinking
    if (Serial.available() || pendingWifiCommand != 0) {
      // a new command arrived (serial or WiFi) -> stop blinking/buzzing now
      digitalWrite(PIN_BUZZER, LOW);
      return;
    }
    on = !on;
    digitalWrite(PIN_RED, on ? HIGH : LOW);
    bool withinBuzzerWindow = (millis() - start) < BUZZER_DURATION_MS;
    if (withinBuzzerWindow && !buzzerMuted) {
      digitalWrite(PIN_BUZZER, on ? HIGH : LOW);
    } else {
      digitalWrite(PIN_BUZZER, LOW);
    }
    delay(BLINK_INTERVAL_MS);
  }
}

void enterWorking() {
  setLeds(false, true, false);
}

void enterDone() {
  setLeds(false, false, true);
  buzzerBeep(150, 150, 2);
}

void enterIdle() {
  setLeds(false, false, false);
}

void setMuted(bool muted) {
  buzzerMuted = muted;
  prefs.putBool("muted", muted);
  Serial.println(muted ? "buzzer muted" : "buzzer unmuted");
}

void resetWifiAndRestart() {
  Serial.println("[wifi] clearing saved credentials, restarting into setup mode in 3s...");
  wifiManager.resetSettings();
  delay(3000);
  ESP.restart();
}

void dispatch(char c) {
  switch (c) {
    case 'R': enterWaiting(); break;
    case 'Y': enterWorking(); break;
    case 'G': enterDone(); break;
    case 'O': enterIdle(); break;
    case 'M': setMuted(true); break;
    case 'U': setMuted(false); break;
    case 'W': resetWifiAndRestart(); break;
    default: break; // ignore newlines etc.
  }
}

void handleCmd() {
  if (server.hasArg("c") && server.arg("c").length() > 0) {
    pendingWifiCommand = server.arg("c")[0];
    server.send(200, "text/plain", "ok");
  } else {
    server.send(400, "text/plain", "missing c param, e.g. /cmd?c=R");
  }
}

void setupWifi() {
  // Tries saved credentials first; if that fails (or none are saved), opens
  // the setup AP and waits up to WIFI_PORTAL_TIMEOUT_S before giving up.
  wifiManager.setConfigPortalTimeout(WIFI_PORTAL_TIMEOUT_S);
  Serial.print("[wifi] connecting, will open setup AP \"");
  Serial.print(AP_SETUP_NAME);
  Serial.println("\" if needed...");

  bool connected = wifiManager.autoConnect(AP_SETUP_NAME);
  if (!connected) {
    Serial.println("[wifi] no WiFi configured or setup timed out, serial still works");
    return;
  }

  // Modem sleep is on by default and can add multi-second latency spikes to
  // requests since the radio isn't always listening.
  WiFi.setSleep(false);

  Serial.print("[wifi] connected, IP: ");
  Serial.println(WiFi.localIP());
  if (MDNS.begin(MDNS_NAME)) {
    Serial.print("[wifi] also reachable at http://");
    Serial.print(MDNS_NAME);
    Serial.println(".local/cmd?c=R");
  }
  server.on("/cmd", handleCmd);
  server.begin();
  wifiReady = true;
}

void setup() {
  Serial.begin(115200);
  pinMode(PIN_RED, OUTPUT);
  pinMode(PIN_YELLOW, OUTPUT);
  pinMode(PIN_GREEN, OUTPUT);
  pinMode(PIN_BUZZER, OUTPUT);
  digitalWrite(PIN_BUZZER, LOW);

  prefs.begin("trafficlight", false);
  buzzerMuted = prefs.getBool("muted", false);

  setLeds(false, false, false);
  Serial.println("ready");

  setupWifi();
}

void loop() {
  if (wifiReady) server.handleClient();

  if (Serial.available()) {
    char c = Serial.read();
    // The Python bridge/app write commands as "R\n" over USB serial. Drain
    // that trailing newline/CR here, before dispatch() -- otherwise it's
    // still sitting in the buffer the instant enterWaiting() checks
    // Serial.available() to see if a new command arrived, and its blink
    // loop exits immediately (solid red, no blink/buzz) instead of running.
    delay(2); // give the trailing byte a moment to arrive over USB
    while (Serial.available() && (Serial.peek() == '\n' || Serial.peek() == '\r')) {
      Serial.read();
    }
    dispatch(c);
  } else if (pendingWifiCommand != 0) {
    char c = pendingWifiCommand;
    pendingWifiCommand = 0;
    dispatch(c);
  }
}
