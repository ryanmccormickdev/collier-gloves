# Arduino sketches from before the gloves

Small sketches I wrote while learning the sensors, before any glove existed.
They're mostly library examples that I changed a little. The glove firmware
that actually ran is in [`../../firmware/`](../../firmware/).

| Sketch | Date | What it does |
|---|---|---|
| `proximity_9960/` | 2026-04-14 | Reads an APDS9960 proximity sensor and prints it. Adafruit's example, with the interrupt loop swapped for plain polling. |
| `dips/` | 2026-04-21 | A Teensy USB-MIDI controller. Four toggle switches turn four CC streams on and off, two buttons play notes, and the APDS9960 proximity reading drives CC 11 (expression). Built on PJRC's AnalogControlChange example. |
| `glove_experiments/` | 2026-05-01 | An I2C scanner on the ESP32-C3 (SDA on GPIO8, SCL on GPIO9), to check that the IMU shows up. |
| `rotation_vector_with_finger/` | 2026-05-05 | The BNO085's game rotation vector plus one flex sensor on an analog pin, printed to serial. Adafruit's BNO08x example plus the flex read. This is the first time the two glove sensors ran together. |

Libraries: Adafruit APDS9960, Adafruit BNO08x (and BusIO / Unified Sensor),
PJRC Bounce and usbMIDI (Teensyduino). The Adafruit examples are BSD licensed;
their headers are kept.
