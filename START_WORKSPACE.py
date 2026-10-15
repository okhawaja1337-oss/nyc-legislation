#!/usr/bin/env python3
"""
Start the District 49 workspace.

Double-click START_WORKSPACE.cmd on Windows, or run this file directly.
Everything runs on this computer: no host, no account, no internet required
for the work already loaded.
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

if sys.version_info < (3, 9):
    print("This needs Python 3.9 or newer. Install it from python.org, "
          "then run this again.")
    input("Press Enter to close.")
    sys.exit(1)

try:
    from universe.workspace.launch import main
except Exception as exc:                       # a missing file, usually
    print(f"\n  The workspace could not start: {type(exc).__name__}: {exc}")
    print("  Make sure the whole folder was extracted, not just this file.")
    input("\n  Press Enter to close.")
    sys.exit(1)

if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"\n  The workspace stopped: {type(exc).__name__}: {exc}")
        input("\n  Press Enter to close.")
        sys.exit(1)
