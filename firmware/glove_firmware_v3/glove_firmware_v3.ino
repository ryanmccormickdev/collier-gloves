/*
 * Glove Firmware  v3  (ESP32-C3 SuperMini)
 *
 * Changes from v2:
 *   - Orientation PRIMARY source = GAME rotation vector (gyro+accel, NO mag).
 *     Immune to stage magnetic disturbance; only slow gyro drift, which the
 *     "point forward" re-zero in collier corrects. This is the fix for the
 *     hands drifting tens of degrees apart over a performance.
 *   - ARVR-stabilized rotation vector carried alongside as an absolute,
 *     jump-suppressed reference plus a heading-accuracy estimate.
 *   - Streams fusion accuracy (radians) + status bytes so the app can gate
 *     calibration and surface drift.
 *   - 100 Hz packet rate (was 50) to match collier's 60 Hz pipeline tick.
 *   - New fields are appended AFTER the flex array, so any v2-layout receiver
 *     keeps flex at f[13:23] and only needs a packet-size bump.
 *
 * Hardware / wiring: identical to v2 (BNO085 over I2C, CD74HC4051 mux, 2 thumb ADCs).
 *
 * Libraries (Arduino Library Manager):
 *   - Adafruit BNO08x
 *   - NimBLE-Arduino  (>= 2.x, by h2zero)
 *
 * Tools menu (unchanged from v2):
 *   - Board: "ESP32C3 Dev Module"
 *   - USB CDC On Boot: Enabled
 *   - JTAG Adapter: Disabled
 *   - Flash Size: 4MB
 *
 * For the second glove, change GLOVE_ID and GLOVE_NAME so they AGREE:
 *   id 0 -> "Glove-L"   ,   id 1 -> "Glove-R".
 */

#include <Wire.h>
#include <Adafruit_BNO08x.h>
#include <NimBLEDevice.h>

// =====================================================================
//                             CONFIGURATION
// =====================================================================
#define GLOVE_ID    0
#define GLOVE_NAME  "Glove-L"      // keep in sync with GLOVE_ID (0=L, 1=R)

// --- I2C ---
#define I2C_SDA     6
#define I2C_SCL     7

// --- CD74HC4051 8-channel mux ---
#define MUX_OUT     4    // analog out of mux -> ESP32 ADC
#define MUX_A       10   // select bit 0
#define MUX_B       20   // select bit 1
#define MUX_C       21   // select bit 2

// --- Direct thumb ADC inputs ---
#define THUMB_LO    2    // A0
#define THUMB_HI    3    // A1

// --- Timing / sampling ---
#define MUX_SETTLE_US     50      // analog settling after mux select change
#define ADC_OVERSAMPLE    8       // averages per analog read (helps ADC noise)
#define SEND_INTERVAL_MS  10      // 100 Hz packet rate (matches collier tick)
#define IMU_REPORT_US     10000   // 100 Hz: game RV, accel, gyro
#define ABS_REPORT_US     20000   // 50 Hz: absolute (stabilized) RV reference

// --- BLE ---
#define SERVICE_UUID  "5a3e8f00-1c4e-4b8e-9b2c-7e1f8d9c3a1b"
#define CHAR_UUID     "5a3e8f01-1c4e-4b8e-9b2c-7e1f8d9c3a1b"

// =====================================================================
//                            PACKET STRUCTURE
//   Python struct format:  "<BBH4f3f3f10H4ffBB"  ->  86 bytes
//   v2 fields keep identical offsets; v3 fields are appended after flex.
// =====================================================================
#pragma pack(push, 1)
struct GlovePacket {
  uint8_t  glove_id;
  uint8_t  seq;
  uint16_t timestamp_ms;
  float    qw, qx, qy, qz;     // PRIMARY: game rotation vector (no magnetometer)
  float    ax, ay, az;         // linear accel  (m/s^2, gravity removed)
  float    gx, gy, gz;         // gyroscope     (rad/s)
  uint16_t flex[10];           // [0..7]=mux ch 0..7, [8]=thumb_lo, [9]=thumb_hi
  float    sw, sx, sy, sz;     // ABSOLUTE: ARVR-stabilized RV (mag, jump-suppressed)
  float    rv_accuracy;        // estimated heading accuracy of stabilized RV (radians)
  uint8_t  rv_status;          // stabilized RV fusion accuracy 0..3
  uint8_t  game_status;        // game RV fusion accuracy 0..3
};
#pragma pack(pop)
static_assert(sizeof(GlovePacket) == 86, "packet size mismatch");

// =====================================================================
//                                 STATE
// =====================================================================
Adafruit_BNO08x       bno;
sh2_SensorValue_t     sensorValue;
GlovePacket           packet = {};
NimBLECharacteristic *txChar = nullptr;
volatile bool         deviceConnected = false;

// =====================================================================
//                           BLE CALLBACKS
// =====================================================================
class ServerCallbacks : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer *server, NimBLEConnInfo &info) override {
    deviceConnected = true;
    server->updateConnParams(info.getConnHandle(), 6, 12, 0, 100);
    Serial.println(F("[BLE] connected"));
  }
  void onDisconnect(NimBLEServer *server, NimBLEConnInfo &info, int reason) override {
    deviceConnected = false;
    Serial.printf("[BLE] disconnected (reason 0x%02x), re-advertising\n", reason);
    NimBLEDevice::startAdvertising();
  }
};

// =====================================================================
//                         BNO085 REPORT SETUP
// =====================================================================
// The Adafruit BNO08x library silently drops reports when enableReport() is
// called repeatedly with no delay in between, so keep the 50 ms spacing.
static void enableImuReports() {
  if (!bno.enableReport(SH2_GAME_ROTATION_VECTOR, IMU_REPORT_US))
    Serial.println(F("[BNO] enable game rotation vector FAILED"));
  delay(50);
  if (!bno.enableReport(SH2_ARVR_STABILIZED_RV, ABS_REPORT_US))
    Serial.println(F("[BNO] enable ARVR-stabilized RV FAILED"));
  delay(50);
  if (!bno.enableReport(SH2_LINEAR_ACCELERATION, IMU_REPORT_US))
    Serial.println(F("[BNO] enable linear acceleration FAILED"));
  delay(50);
  if (!bno.enableReport(SH2_GYROSCOPE_CALIBRATED, IMU_REPORT_US))
    Serial.println(F("[BNO] enable gyroscope FAILED"));
  delay(50);
}

// =====================================================================
//                         ANALOG SCANNING
// =====================================================================
static inline uint16_t readAvg(uint8_t pin) {
  uint32_t sum = 0;
  for (uint8_t i = 0; i < ADC_OVERSAMPLE; i++) sum += analogRead(pin);
  return (uint16_t)(sum / ADC_OVERSAMPLE);
}

static uint16_t readMuxChannel(uint8_t ch) {
  digitalWrite(MUX_A, (ch >> 0) & 1);
  digitalWrite(MUX_B, (ch >> 1) & 1);
  digitalWrite(MUX_C, (ch >> 2) & 1);
  delayMicroseconds(MUX_SETTLE_US);
  return readAvg(MUX_OUT);
}

static void scanAllChannels() {
  for (uint8_t i = 0; i < 8; i++) packet.flex[i] = readMuxChannel(i);
  packet.flex[8] = readAvg(THUMB_LO);
  packet.flex[9] = readAvg(THUMB_HI);
}

// =====================================================================
//                                SETUP
// =====================================================================
void setup() {
  Serial.begin(115200);
  delay(1500);
  Serial.printf("\n=== %s v3 starting (ESP32-C3, glove_id=%d) ===\n",
                GLOVE_NAME, GLOVE_ID);

  analogReadResolution(12);    // 0..4095

  pinMode(MUX_A, OUTPUT);
  pinMode(MUX_B, OUTPUT);
  pinMode(MUX_C, OUTPUT);
  digitalWrite(MUX_A, 0);
  digitalWrite(MUX_B, 0);
  digitalWrite(MUX_C, 0);

  Wire.begin(I2C_SDA, I2C_SCL);
  Wire.setClock(400000);
  if (!bno.begin_I2C(0x4A, &Wire)) {
    Serial.println(F("[BNO] not found at 0x4A - halting"));
    while (true) delay(1000);
  }
  enableImuReports();
  Serial.println(F("[BNO] ready"));

  NimBLEDevice::init(GLOVE_NAME);
  NimBLEDevice::setPower(ESP_PWR_LVL_P9);
  NimBLEDevice::setMTU(255);

  NimBLEServer *server = NimBLEDevice::createServer();
  server->setCallbacks(new ServerCallbacks());

  NimBLEService *service = server->createService(SERVICE_UUID);
  txChar = service->createCharacteristic(
    CHAR_UUID, NIMBLE_PROPERTY::READ | NIMBLE_PROPERTY::NOTIFY);
  service->start();

  NimBLEAdvertising *adv = NimBLEDevice::getAdvertising();
  adv->addServiceUUID(SERVICE_UUID);
  adv->setName(GLOVE_NAME);
  adv->enableScanResponse(true);
  NimBLEDevice::startAdvertising();
  Serial.println(F("[BLE] advertising"));

  packet.glove_id = GLOVE_ID;
}

// =====================================================================
//                                 LOOP
// =====================================================================
static uint32_t gameN = 0, absN = 0, accN = 0, gyroN = 0;

void loop() {
  if (bno.wasReset()) {
    Serial.println(F("[BNO] reset detected, re-enabling reports"));
    enableImuReports();
  }

  while (bno.getSensorEvent(&sensorValue)) {
    switch (sensorValue.sensorId) {
      case SH2_GAME_ROTATION_VECTOR:
        gameN++;
        packet.qw = sensorValue.un.gameRotationVector.real;
        packet.qx = sensorValue.un.gameRotationVector.i;
        packet.qy = sensorValue.un.gameRotationVector.j;
        packet.qz = sensorValue.un.gameRotationVector.k;
        packet.game_status = sensorValue.status & 0x03;
        break;
      case SH2_ARVR_STABILIZED_RV:
        absN++;
        packet.sw = sensorValue.un.arvrStabilizedRV.real;
        packet.sx = sensorValue.un.arvrStabilizedRV.i;
        packet.sy = sensorValue.un.arvrStabilizedRV.j;
        packet.sz = sensorValue.un.arvrStabilizedRV.k;
        packet.rv_accuracy = sensorValue.un.arvrStabilizedRV.accuracy;
        packet.rv_status = sensorValue.status & 0x03;
        break;
      case SH2_LINEAR_ACCELERATION:
        accN++;
        packet.ax = sensorValue.un.linearAcceleration.x;
        packet.ay = sensorValue.un.linearAcceleration.y;
        packet.az = sensorValue.un.linearAcceleration.z;
        break;
      case SH2_GYROSCOPE_CALIBRATED:
        gyroN++;
        packet.gx = sensorValue.un.gyroscope.x;
        packet.gy = sensorValue.un.gyroscope.y;
        packet.gz = sensorValue.un.gyroscope.z;
        break;
      default: break;
    }
  }

  static uint32_t lastSendMs = 0, lastStatMs = 0, pktsSent = 0;
  uint32_t now = millis();

  if (now - lastSendMs >= SEND_INTERVAL_MS) {
    lastSendMs = now;
    scanAllChannels();
    packet.timestamp_ms = (uint16_t)(now & 0xFFFF);
    packet.seq++;
    if (deviceConnected && txChar) {
      txChar->setValue((uint8_t*)&packet, sizeof(packet));
      txChar->notify();
      pktsSent++;
    }
  }

  if (now - lastStatMs >= 1000) {
    lastStatMs = now;
    Serial.printf("[%s] conn=%d tx=%lu/s imu=[game=%lu abs=%lu acc=%lu gyro=%lu]/s "
                  "stat=[g=%u rv=%u acc=%.3frad]\n",
      GLOVE_NAME, (int)deviceConnected, (unsigned long)pktsSent,
      (unsigned long)gameN, (unsigned long)absN,
      (unsigned long)accN, (unsigned long)gyroN,
      packet.game_status, packet.rv_status, packet.rv_accuracy);
    Serial.printf("    mux=[%4u %4u %4u %4u %4u %4u %4u %4u]  thumb=[%4u %4u]\n",
      packet.flex[0], packet.flex[1], packet.flex[2], packet.flex[3],
      packet.flex[4], packet.flex[5], packet.flex[6], packet.flex[7],
      packet.flex[8], packet.flex[9]);
    pktsSent = 0;
    gameN = absN = accN = gyroN = 0;
  }
}
