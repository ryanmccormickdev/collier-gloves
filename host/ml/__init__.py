"""Gesture TCN code from the ML attempt.

collier_live still loads the last trained model through ``manifest_runtime``
(its guesses show in the GUI; they only drive the music with ``--engine ml``).
``gesture_tcn`` / ``augment`` / ``tiny_tcn_legacy`` are also what the trainer in
``experiments/ml-attempt/`` imports. Saved ``.pt`` files reference
``ml.gesture_tcn`` by name, so keep this package name.
"""
