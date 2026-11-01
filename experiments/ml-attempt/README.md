# The labelled-ML attempt (dropped before the performance)

Kept as history. None of this drives the music in the version that was
performed (see the top-level README).

## Why it was dropped

The model worked very well in the session it was trained in. Then I took the
gloves off and put them back on, everything read slightly differently, and
the model fell apart. A model trained on one wearing of the gloves didn't
transfer to the next.

The diagnosis notes from that night (`docs/diagnosis_findings.md`) found
these problems, among others:

- No flex calibration was saved with the clips or the model. Finger-bend
  ranges were relearned from scratch every session, and they drifted even
  during the recording session. Some bend means moved by a quarter to a
  half of the 0–1 range between early and late clips of the same gesture.
- Several gesture pairs were nearly identical in feature space (scale up vs
  scale down, semitone up vs down).
- The post-processor fired two or more commands for about 20–35% of gesture
  clips, even when replaying the training clips.

The final model's manifest records a last attempt at 04:16 on June 2: its
five finger-bend features were masked out (`calib_patches` in the manifest),
so it relied on the IMU features. About an hour later the live pipeline went
back to heuristics. For recognizing gestures, they use the flex sensors only
to tell whether a hand is closed; each gesture's meaning comes from the IMU.

## What was built

- **Data:** 510 labelled clips recorded on 2026-06-01 between 16:48 and 18:25,
  in 12 classes: `none`, `spawn_note`, `scale_up/down`, `semitone_up/down`,
  and `collective_` versions of scale, semitone and volume. Each class was
  recorded left-only, right-only, both hands in sync, or both hands
  "collective". Per hand and frame (60 Hz) there are 27 features: IMU motion
  and orientation in the hand's frame, five finger bends, and five
  finger-oscillation values. Two clips are in `../../samples/clips/`; the rest
  are not in this repo.
- **Model:** one small dilated causal TCN per hand (PyTorch,
  `host/ml/gesture_tcn.py`): 64-frame windows (about 1.07 s), stride 8. The
  last model, `gesture_tcn_20260602_030700`, uses 10 classes (volume dropped)
  and was trained with `apps/diag_retrain.py`. Its manifest records
  window-level validation accuracy of 0.95 (left) and 0.92 (right). These are
  offline scores on recorded windows; they say nothing about the live
  behaviour, which is what failed.
- That model is the one in `host/collier_live/models/`. The live conductor
  still loads it and shows its guesses in the GUI, but with the default
  `--engine pick` its output never triggers anything.

## Files

| Path | What it is |
|---|---|
| `glove_gesture_trainer.py` | One PyQt6 app with five tabs: connect and calibrate, record, label, train, live test. It has its own BLE and signal code; the model code comes from `host/ml/`. It stores data in `./gesture_trainer_data/` (created on start). |
| `scripts/gesture_train_diagnose.py` | Train and diagnose without the UI. |
| `apps/diag_dataset.py` | Per-class feature stats, class separability, and drift between early and late clips. |
| `apps/diag_offline_predict.py` | Replays every clip through the model and post-processor and counts commands per clip. |
| `apps/diag_clip_view.py` | Frame-by-frame view of one clip with predictions. |
| `apps/diag_live_dump.py`, `diag_live_record_gestures.py` | Record live feature rows and predictions with the gloves on. |
| `apps/diag_live_vs_clips.py`, `diag_live_compare_gestures.py`, `diag_live_replay.py` | Compare live recordings with the training distribution, or replay them through a manifest. |
| `apps/diag_calibrate_inject.py`, `diag_recenter_manifest.py` | Patch a manifest with a live flex range or re-centred feature means (the masking above). |
| `apps/diag_retrain.py` | Retrain with the settings from the diagnosis notes. |
| `apps/diag_pick_validate.py` | Replays labelled clips through the heuristic pick engine, as a check before switching the live path to it. |
| `apps/pipeline_inspect.py` | Terminal HUD for the ML live path. |
| `docs/diagnosis_findings.md` | The diagnosis notes (first pass), unedited. |
| `artifacts/reports/*.txt` | Saved output of `diag_dataset.py` and three `diag_offline_predict.py` runs. |

An earlier version of this pipeline (a calibration wizard, recorder, labeler,
trainer and runtime as separate apps, May 31) was replaced within a day by
`glove_gesture_trainer.py`. It isn't included.

## Running it (optional)

From the repo root:

```bash
pip install -e ".[ml]"
cd experiments/ml-attempt
python glove_gesture_trainer.py                    # needs both gloves
mkdir -p gesture_trainer_data/clips && cp ../../samples/clips/*.npz gesture_trainer_data/clips/
python apps/diag_pick_validate.py                  # no gloves needed
python apps/diag_offline_predict.py --manifest ../../host/collier_live/models/gesture_tcn_20260602_030700.manifest.json
```

The diagnostics read clips from `gesture_trainer_data/clips/`. Pass
`--manifest` explicitly, because the default lookup expects the original
single-folder layout. With only the two sample clips, the numbers from these
scripts mean nothing beyond showing the data format.
