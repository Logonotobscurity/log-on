"""`python -m log_on.trace` -> replay/inspect run traces (see log_on/telemetry/replay.py)."""
from .telemetry.replay import main

if __name__ == "__main__":
    raise SystemExit(main())
