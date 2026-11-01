# Gesture ML diagnostic findings (1st pass)

Generated from `apps/diag_dataset.py` + `apps/diag_offline_predict.py` against
the *active* manifest (`collier_live/models/active.json` →
`gesture_tcn_20260601_232854.manifest.json`, the 11:28 pm model the user said
was the best). Full reports are saved under `artifacts/reports/`.

## TL;DR

The model itself is fine. The pipeline around it has at least **five separate
problems**, three of which are visible *on the training data itself* and don't
require a live test to confirm.

1. **No flex calibration was saved with the training clips or with the model.**
   At inference time the system uses a cold `SessionFlexTracker` that has to
   relearn the per-finger ADC range from scratch every session. Worse, this
   same drift was active *during* recording, so different clips of the same
   gesture have different bend normalizations.
2. **Several gesture pairs are physically near-identical in the chosen
   feature set.** scale\_up vs scale\_down on the L hand has a best-feature
   separability of 0.44; semitone\_up vs semitone\_down is 0.42. Below ~0.5
   you should expect frequent flips.
3. **Postprocess fires multiple commands per gesture** even on the training
   clips themselves. ~20–35% of gesture clips would trigger ≥2 commands when
   replayed through the live postprocessor. The reasons are visible per-tick.
4. **Per-hand independent classifiers can't represent "collective" gestures
   properly.** Each side's model has to identify a collective by looking at
   only its own hand. There's no fusion step, and there's a vocabulary class
   per side for every collective variant.
5. **`collective_volume_*` clips poison the "none" class.** Sixty-two clips
   were recorded with palm-down, hand-rotation motion, then trained as `none`
   because the active vocabulary excludes volume. The "none" prototype now
   includes "hand at palm-down, rotating", which is exactly what `scale_*` and
   `semitone_*` look like.

The "sometimes it works great, sometimes it makes no sense" feeling comes from
the interaction of (1) + (3): when the live flex envelope happens to settle in
a regime close to whatever the model saw most often, predictions feel sharp.
When the envelope is in a different regime, or when natural release/return
motion sweeps through neighboring classes, multiple commands fire and
everything goes sideways.

---

## What the offline analysis actually shows

### Per-window confusion matrix (good)

For *windows that lie inside the labeled gesture span*, the trained TCN does
its job. Recall is 0.92–1.00 across all classes on both hands. So this is
**not a model-capacity problem**.

### Per-clip postprocess simulation (bad)

`apps/diag_offline_predict.py` replays each clip through the same postprocess
(agreement\_k=2, refractory 0.45 s, global cooldown 0.15 s) and counts how
many discrete commands would be emitted. On the training clips:

| true class               | L: 2× fires | R: 2× fires | 3+ fires |
|--------------------------|-------------|-------------|----------|
| scale\_up                | 5/40        | 7/40        | 1/40     |
| scale\_down              | 7/40        | 5/40        | —/+1     |
| semitone\_up             | 6/40        | 8/40        | 2/40     |
| semitone\_down           | 11/40       | 6/40        | —        |
| collective\_scale\_up    | 10/30       | 5/30        | —        |
| collective\_scale\_down  | 11/30       | 2/30        | —        |
| collective\_semitone\_up | 4/30        | 6/30        | 2/30     |

These are *training-set* clips, not unseen live data. The fixes for this live
entirely inside `collier_live/postprocess.py` and the inference threshold
gating — see "Recommended changes" below.

Typical double-fire pattern (from `apps/diag_clip_view.py`
`scale_up_both_sync_20260601_171219`):

* t=72–86: model predicts `scale_up` (correct, fires command).
* t=92–122: hand is *holding* the new pose. Model now sees a still hand at
  `palm_roll≈-166°`, openness≈0.3, vert\_vel near 0. It predicts
  `semitone_up` with confidence ~0.6 → fires *another* command.
* t=130–138: model flickers back to `scale_up` → fires *another* command.
* t=142–148: hand is *returning* to neutral (palm rolling back). Model
  predicts `semitone_down` → fires *another* command.

So a single physical gesture produces up to 5 OSC events. The model is doing
exactly what you'd expect — it's classifying every window — but the
postprocessor is not designed to swallow the "release + return" motion that
naturally follows any real gesture.

### Class separability (root-cause of confusions)

`apps/diag_dataset.py` computes `|μ_a - μ_b| / (σ_a + σ_b)` per feature for
each suspect class pair. Highlights:

| pair (side)                                  | best feature   | score |
|----------------------------------------------|----------------|-------|
| scale\_up ↔ scale\_down [L]                  | vert\_vel      | 0.44  |
| semitone\_up ↔ semitone\_down [L]            | gyro\_across   | 0.42  |
| semitone\_up ↔ semitone\_down [R]            | gyro\_across   | 0.34  |
| scale\_up ↔ semitone\_up [L]                 | lin\_accel\_mag| 0.33  |
| scale\_up ↔ semitone\_up [R]                 | openness       | 0.37  |
| scale\_down ↔ semitone\_down [L]             | vel\_horiz\_mag| 0.39  |
| scale\_up ↔ collective\_scale\_up [L]        | forward\_up    | 1.15  |
| scale\_up ↔ collective\_scale\_up [R]        | forward\_up    | 0.85  |
| collective\_scale\_down ↔ collective\_semitone\_down [L] | wrist\_tilt | 0.71 |
| collective\_volume\_up ↔ collective\_scale\_up [L] | wrist\_tilt | 1.97 |

Interpretation:

* Anything <0.5 means the two classes basically live in the same chunk of
  feature space and the model will routinely flip between them on
  near-boundary motion. That's the user's "confuses scale\_up and semitone\_up
  for seemingly no reason".
* scale/collective\_scale separability is fine in principle (forward\_up ≈
  1.1), but only at the extremes of the orientation. A half-rolled hand falls
  in no-man's-land.
* `collective_volume_*` was recorded with very low overall finger bend
  (means around 0.05–0.15 across all fingers) but trained as `none`. That
  shoves those 62 clips into the `none` cluster, and now `none` is no longer
  "neutral hand" — it's "neutral hand or palm-down hand with closed fingers
  rotating". This is part of why spawn\_note vs none is brittle.

### Recording-session drift

`diag_dataset.py` also splits each (class, side, finger) into "first quarter
of recordings" vs "last quarter" by clip timestamp. Several show very large
drift (Δ in 0–1 normalized bend units):

* `scale_up R bend_pinky`: 0.41 → 0.64 (Δ +0.23)
* `scale_up R bend_ring`:  0.15 → 0.41 (Δ +0.26)
* `collective_semitone_down R bend_pinky`: 0.35 → 0.75 (Δ +0.39)
* `collective_volume_up R bend_pinky`: 0.18 → 0.31 (Δ +0.13)

These deltas are huge (a quarter to a half of the full 0–1 range). They are
exactly what you'd expect from `EnvelopeMinMax` slowly converging on the
true per-finger ADC range over a 3-hour session. The model learned a *band*
of acceptable bend distributions for each class; whichever band the live
session happens to land in determines whether predictions feel sharp.

### Dataset shape

* 510 clips, fixed forward calibration (single L=-178.7°, R=134.0° pose).
  The model has never seen the user pointing in a different direction.
* Clip lengths range 90–1249 frames (most around 180; outliers ≥400 likely
  have junk at the end — see report).
* `none` class is built from: 28 explicit none clips + 100 off-hand
  recordings + 62 squashed volume clips + every "outside the marked span"
  window. The volume contribution is the problematic one.

---

## Diagnostic tools you can run yourself

All in `apps/`, no glove required for the first three:

```bash
python apps/diag_dataset.py --out artifacts/reports/diag_dataset.txt
python apps/diag_offline_predict.py            # active manifest
python apps/diag_offline_predict.py --manifest gesture_trainer_data/models/gesture_tcn_20260602_001602.manifest.json
python apps/diag_clip_view.py scale_up_both_sync_20260601_171219 --side L
```

Glove-connected tools:

```bash
# 20-sec capture of feature rows + predictions while you do the gesture.
python apps/diag_live_dump.py --secs 20 --label scale_up_L_only

# Compare a captured run to the training distribution.
python apps/diag_live_vs_clips.py artifacts/reports/live_dumps/<file>.jsonl \
    --class scale_up
```

`diag_live_dump.py` reuses `collier_live`'s transport and feature pipeline so
the numbers it dumps are *exactly* what the model is fed live. The
`live_flex_range` it records can be plugged into a new manifest by hand to
short-circuit the cold-start problem.

---

## Recommended fixes (highest leverage first)

### Quick wins (no retraining)

1. **Capture a live flex range right now and inject it into the active
   manifest.** Run `python apps/diag_live_dump.py --secs 25 --label calib`,
   wiggle every finger fully open and fully closed several times, then copy
   the resulting `live_flex_range` from the JSONL header into the manifest's
   `"flex_range"` field. The conductor already applies that to the V3
   conditioner on startup — it just has nothing to apply right now. *This
   alone should make predictions stable across sessions even before any
   retraining.*

2. **Fix the postprocess so one physical gesture = one command.** In
   `collier_live/postprocess.py`:
   - Increase `agreement_k` to ~5 (≈80 ms at 60 Hz).
   - Increase `per_class_refractory_s` to ~1.0 s (so the natural release of
     a 1 s gesture doesn't refire its own class).
   - **Increase `global_cooldown_s` to ~0.8 s.** This is the big one. Right
     now the gap between two different-label fires is only 150 ms, which is
     exactly the duration of the typical "release" or "return" motion that
     produces a wrong-class prediction.
   - Add a confidence-hysteresis rule: require `conf >= threshold + 0.05` to
     start an episode, and `conf < threshold - 0.10` to end it. (The current
     gate flickers around threshold and that's the source of the
     scale ↔ semitone double-fires.)

3. **Stop letting `collective_volume_*` clips be trained as "none".** Either
   add the two volume classes to the active vocabulary, or filter those
   clips out of windowing entirely (in
   `glove_gesture_trainer.build_windowed_dataset`, skip clips whose
   `gesture_class` is not in `active_vocab` and where `excluded_as_none` is
   true — but treat them as "drop", not "none"). Right now they pull the
   `none` prototype toward palm-down rotating motion.

### Bigger fixes (require retraining; you said you can't recollect data)

4. **Tighten the active vocabulary.** Drop classes the model can't reliably
   tell apart, then debug live before adding them back. Proposed Phase-1
   minimum: `none, spawn_note, scale_up, scale_down, collective_scale_up,
   collective_scale_down`. That gets you up/down on each hand independently
   plus collective up/down, which is the demo the user described as working
   "okay" already. Add semitone variants in a Phase-2 pass once the rest is
   solid.

5. **Use a two-hand fusion classifier for collective gestures instead of two
   independent per-hand TCNs.** A simple option: train per-hand models on
   `{none, spawn_note, scale_up, scale_down, semitone_up, semitone_down}`
   only (no collective at all), and infer "collective" only when **both
   sides** simultaneously predict the same class AND `palm_roll` of each
   hand is in the "rolled-up" hemisphere. This replaces 10-way per-hand
   classification with 6-way (much easier) plus a deterministic rule for
   the collective variant. It also matches how the user *thinks* about the
   collective gesture ("same motion, rolled palm").

6. **Don't use `EnvelopeMinMax` at all in `glove_ml_core`. Switch to a
   fixed, recorded per-glove calibration that gets saved with every clip
   and every manifest.** This is the structural fix; the quick win in (1)
   only works because the gloves' ADC ranges are fairly stable.

7. **Use longer windows (96–128 frames ≈ 1.6–2.1 s).** The current 64-frame
   window doesn't contain the whole gesture, and that's part of why the
   model keeps re-evaluating the same gesture in pieces.

### Augmentation

The user noted that the no-augmentation 11:28 pm model is the best so far.
This is consistent with what we see: most "augmentation" options in
`ml/augment.py` either add Gaussian noise to already-noisy features
(`gaussian_noise`, `bend_bias`) or perturb orientation features
(`orientation_noise`) that are actually load-bearing for distinguishing
scale ↔ collective\_scale. A safer single-knob augmentation is `time_warp`
in the ±10% range; everything else is currently hurting.

---

## What I'd still like to see (from you)

I have answers from offline data for the postprocess/data problems. The one
thing I cannot prove from clips alone is the **live calibration mismatch**,
because the clips are saved as already-processed feature rows. Two captures
would close that loop:

* `python apps/diag_live_dump.py --secs 25 --label calib_warmup`
  → wiggle every finger fully open / closed several times across 25 s.
* `python apps/diag_live_dump.py --secs 30 --label probe`
  → after warmup, perform a clean `scale_up`, then `scale_down`, then
  `semitone_up`, then `semitone_down`, then `collective_scale_up`,
  separated by ~3 s holds at neutral.

Then `python apps/diag_live_vs_clips.py <probe.jsonl> --class scale_up` will
print, per side, the live feature mean vs the training mean for the same
class. Anything with `|z| > 1.5` is a real distribution mismatch that needs
the calibration fix above. I'll iterate from there.
