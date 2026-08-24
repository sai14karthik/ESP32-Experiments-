/*#include <WiFi.h>

const char *ssid = "SpectrumSetup-EB9C";
const char *password = "unitedvideo788";

static const unsigned long kSampleMs = 100;
static const unsigned long kReconnectMs = 3000;

static unsigned long lastSampleMs = 0;
static unsigned long lastReconnectMs = 0;

static void connectWifi() {
  WiFi.disconnect(true, false);
  delay(50);
  WiFi.begin(ssid, password);
}

void setup() {
  Serial.begin(115200);
  Serial.setDebugOutput(false);
  delay(800);

  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  connectWifi();
}

void loop() {
  const unsigned long now = millis();

  if (WiFi.status() != WL_CONNECTED) {
    if (now - lastReconnectMs >= kReconnectMs) {
      lastReconnectMs = now;
      connectWifi();
    }
    return;
  }

  if (now - lastSampleMs < kSampleMs) {
    return;
  }
  lastSampleMs = now;

  if (WiFi.SSID() != ssid) {
    return;
  }

  // One number per line. Arduino Serial Plotter graphs this.
  // Do not add labels or commas — IDE Plotter treats "-" as a split.
  Serial.println(WiFi.RSSI());

  if((WiFi.RSSI() < -50){
    Serial.println("Wifi is covered i guess ");
    
  }
}
*/


#include <WiFi.h>

const char *ssid = "SpectrumSetup-EB9C";
const char *password = "unitedvideo788";

static const unsigned long kSampleMs = 100;
static const unsigned long kReconnectMs = 3000;

static unsigned long lastSampleMs = 0;
static unsigned long lastReconnectMs = 0;

static void connectWifi() {
  WiFi.disconnect(true, false);
  delay(50);
  WiFi.begin(ssid, password);
}

void setup() {
  Serial.begin(115200);
  Serial.setDebugOutput(false);
  delay(800);

  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  connectWifi();
}

void loop() {
  const unsigned long now = millis();

  if (WiFi.status() != WL_CONNECTED) {
    if (now - lastReconnectMs >= kReconnectMs) {
      lastReconnectMs = now;
      connectWifi();
    }

    return;
  }

  if (now - lastSampleMs < kSampleMs) {
    return;
  }

  lastSampleMs = now;

  if (WiFi.SSID() != ssid) {
    return;
  }

  int rssi = WiFi.RSSI();

  if (rssi < -56) {
    Serial.println("Its covered I guess");
  }
else {
    Serial.println(rssi);

}
}