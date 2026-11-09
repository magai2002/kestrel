"""Configuration: the one place environment variables are read.

Rule from the spec (Section 2, "lessons from previous projects"): a
``dotenv`` load-order bug once read secrets at import time before
``.env`` was loaded. The fix we apply everywhere in this repo: **no
module-level reads of ``os.environ``.** A single ``Settings`` object is
constructed once, at process start (in ``cli.py``), and passed down
explicitly as a function argument. Nothing in ``data/``, ``core/`` or
later ``execution/``/``risk/`` imports ``Settings`` or touches
``os.environ`` directly — they take plain values as parameters, which
also makes them trivial to unit test without env-var gymnastics.
"""
