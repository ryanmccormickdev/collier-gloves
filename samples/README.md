# Sample captures

A few small files to show the data formats. The full dataset (510 clips, about
18 MB) and the raw live dumps are not in this repo.

## `clips/` — two labelled clips from the ML attempt

Recorded on 2026-06-01 with `experiments/ml-attempt/glove_gesture_trainer.py`.
Each `.npz` (load with `numpy.load(path, allow_pickle=False)`) holds:

- `X_L`, `X_R`: float32 `(frames, 27)` feature rows per hand at 60 Hz. The 27
  feature names are stored in `feature_names` (`features_v3_handframe`):
  IMU-derived motion and orientation, then five finger bends and five
  finger-oscillation values.
- `gesture_class`, `mode` (`left_only`, `right_only`, `both_sync`,
  `both_collective`), and per hand `label_*`, `start_*`, `end_*`, `labeled_*`
  (the labelled gesture span in frames).
- `forward_L`, `forward_R`: the calibrated forward headings in degrees.

| File | Gesture | Labelled span |
|---|---|---|
| `scale_up_both_sync_20260601_171219.npz` | both hands, scale up | frames 45–97 on each hand |
| `spawn_note_right_only_20260601_165548.npz` | right hand, spawn note | right hand frames 45–101 |

The first clip is the one `experiments/ml-attempt/docs/diagnosis_findings.md`
walks through as a double-fire example. To try the trainer or the
diagnostics on these, copy them to
`experiments/ml-attempt/gesture_trainer_data/clips/`.

## `pose_profiles/` — recorded chest poses

- `chest.json`: written by `tools/record_filter_pose.py` (recorded
  2026-06-02 11:57) while holding both hands at the chest: per-hand stats and
  suggested pitch/roll/gyro bands.
- `chest_union.json`: the union of two such recordings (`chest_pose_a`,
  `chest_pose_b`, not included). Its bands are the defaults in
  `host/collier_live/filter_pose.py` that gate `/expr/filters`.
