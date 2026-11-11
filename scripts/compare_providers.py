#!/usr/bin/env python3
"""Thin wrapper: python scripts/compare_providers.py [args] == python -m log_on.serving.compare [args]."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from log_on.serving.compare import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
