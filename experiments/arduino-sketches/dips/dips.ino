/* USB MIDI CC Messages with Toggle Mute


This example is based off of the PJRC AnalogControlChange Example. It adds
a bank of toggle switches to turn on and off messages sent as CC messages.
This aids the mapping of CC Values within DAWs.
*/


#include <Bounce.h>


// the MIDI channel number to send messages
const int channel = 1;

Bounce button21 = Bounce(21, 5);
Bounce button20 = Bounce(20, 5);




// the MIDI continuous controller for each analog input
const int controllerA0 = 10;  // 10 = pan position
const int controllerA1 = 11;  // 11 = volume/expression
const int controllerA2 = 91;  // 91 = reverb level
const int controllerA3 = 93;  // 93 = chorus level

// Pin Number for Toggle Switches
int togglePin1 = 1;
int togglePin2 = 2;
int togglePin3 = 3;
int togglePin4 = 4;

#include "Adafruit_APDS9960.h"

//the pin that the interrupt is attached to
#define INT_PIN 9

//create the APDS9960 object
Adafruit_APDS9960 apds;

void setup() {
  Serial.begin(115200);

  if (!apds.begin()) {
    Serial.println("failed to initialize device! Please check your wiring.");
  }
  else Serial.println("Device initialized!");

  //enable proximity mode
  apds.enableProximity(true);

  //set the interrupt threshold to fire when proximity reading goes above 175
  // apds.setProximityInterruptThreshold(0, 175);

  //enable the proximity interrupt
  // apds.enableProximityInterrupt();


  pinMode(1, INPUT_PULLUP);
  pinMode(2, INPUT_PULLUP);
  pinMode(3, INPUT_PULLUP);
  pinMode(4, INPUT_PULLUP);

  pinMode(21, INPUT_PULLUP);
  pinMode(20, INPUT_PULLUP);

}


// store previously sent values, to detect changes
int previousA0 = -1;
int previousA1 = -1;
int previousA2 = -1;
int previousA3 = -1;


elapsedMillis msec = 0;


void loop() {
  // only check the analog inputs 50 times per second,
  // to prevent a flood of MIDI messages
  if (msec >= 5) {
    msec = 0;
    int n0 = analogRead(A0) / 8;
    int n1 = apds.readProximity() / 8;
    int n2 = analogRead(A2) / 8;
    int n3 = analogRead(A3) / 8;

    Serial.println(n1);
    button21.update();
    button20.update();

    if (button21.fallingEdge()) {
      usbMIDI.sendNoteOn(60, 99, channel);  // 71 = B5
    }
    if (button21.risingEdge()) {
      usbMIDI.sendNoteOff(60, 0, channel);  // 60 = C4
    }

    if (button20.fallingEdge()) {
      usbMIDI.sendNoteOn(64, 99, channel);  // 71 = B5
    }
    if (button20.risingEdge()) {
      usbMIDI.sendNoteOff(64, 0, channel);  // 60 = C4
    }



    int toggleState1 = digitalRead(togglePin1);
    int toggleState2 = digitalRead(togglePin2);
    // int toggleState2 = apds.readProximity();
    int toggleState3 = digitalRead(togglePin3);
    int toggleState4 = digitalRead(togglePin4);

    // only transmit MIDI messages if analog input changed
    if (toggleState1 == 0) {
      if (n0 != previousA0) {
        usbMIDI.sendControlChange(controllerA0, n0, channel);
        previousA0 = n0;
      }
    }
    if (toggleState2 == 0) {
      if (n1 != previousA1) {
        usbMIDI.sendControlChange(controllerA1, n1, channel);
        previousA1 = n1;
        Serial.println("Midi Value sent");
      }
    }
    if (toggleState3 == 0) {
      if (n2 != previousA2) {
        usbMIDI.sendControlChange(controllerA2, n2, channel);
        previousA2 = n2;
      }
    }
    if (toggleState4 == 0) {
      if (n3 != previousA3) {
        usbMIDI.sendControlChange(controllerA3, n3, channel);
        previousA3 = n3;
      }
    }
  }


  // MIDI Controllers should discard incoming MIDI messages.
  // http://forum.pjrc.com/threads/24179-Teensy-3-Ableton-Analog-CC-causes-midi-crash
  while (usbMIDI.read()) {
    // ignore incoming messages
  }
}
