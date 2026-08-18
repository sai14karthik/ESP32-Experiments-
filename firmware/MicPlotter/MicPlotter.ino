#include <Arduino.h>
#include <ESP_I2S.h>
#include <math.h>

// Seeed XIAO ESP32-S3 Sense PDM microphone
const int MIC_CLK = 42;
const int MIC_DATA = 41;

I2SClass i2s;

void setup() {
  Serial.begin(115200);
  delay(500);

  i2s.setPinsPdmRx(MIC_CLK, MIC_DATA);
  if (!i2s.begin(I2S_MODE_PDM_RX, 16000, I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO)) {
    Serial.println("Mic init failed");
    while (true) {
      delay(1000);
    }
  }

  Serial.println("Mic ready");
  Serial.println("Serial Monitor: text readout");
  Serial.println("Serial Plotter: graphs rms and peak (close Monitor first)");
}

void loop() {
  int16_t samples[256];
  int n = i2s.readBytes(reinterpret_cast<char *>(samples), sizeof(samples));
  if (n <= 0) {
    return;
  }

  int count = n / 2;
  double sumSquares = 0;
  int peakRaw = 0;
  for (int i = 0; i < count; i++) {
    int v = samples[i];
    sumSquares += (double)v * v;
    int a = abs(v);
    if (a > peakRaw) {
      peakRaw = a;
    }
  }

  const double fullScale = 32768.0;
  double rmsLin = sqrt(sumSquares / count) / fullScale;
  double peak = peakRaw / fullScale;
  if (rmsLin < 1e-9) {
    rmsLin = 1e-9;
  }
  double rmsDbfs = 20.0 * log10(rmsLin);

  // Human-readable for Serial Monitor, plus label:value for Serial Plotter.
  // Plotter only graphs the colon pairs (rms: and peak:).
  Serial.printf("rms=%.1f dBFS: peak=%.3f,rms:%.1f,peak:%.3f\n", rmsDbfs, peak, rmsDbfs, peak);
}
