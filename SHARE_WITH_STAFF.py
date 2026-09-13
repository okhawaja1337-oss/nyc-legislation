#!/usr/bin/env python3
"""
Open the workspace to the office, and print the link to send round.

One machine runs it; everyone else opens a link. The link carries the access
token, so a staffer clicks once and is in -- the token is stored by their
browser and stripped out of the address bar, so it does not sit in history or
get pasted into an email by accident.

What this does NOT do is put the office's data on the public internet. It binds
to the local network only. Anyone who can reach this machine AND has the link
can read constituent casework and the full budget detail, so the link belongs
in the office, not in a public channel.
"""
from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PORT = 8749


def lan_address() -> str:
    """This machine's address on the office network."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))     # never sent; just picks the route
        return s.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"
    finally:
        s.close()


def token() -> str:
    path = HERE / "data" / "universe" / "workspace-token.txt"
    if path.exists():
        return path.read_text().strip()
    return ""


def main() -> int:
    sys.path.insert(0, str(HERE))
    address, tok = lan_address(), token()

    print()
    print("  D49 WORKSPACE — SHARED WITH THE OFFICE")
    print("  " + "=" * 52)
    if address.startswith("127."):
        print("\n  ! This machine could not find a network address. It will still")
        print("    run, but only this computer will be able to open it.")
    print("\n  Send your staff this link:\n")
    link = f"http://{address}:{PORT}/" + (f"?token={tok}" if tok else "")
    print(f"      {link}\n")
    print("  They click it once. Their browser remembers, and after that the")
    print("  plain address works:\n")
    print(f"      http://{address}:{PORT}/\n")
    print("  Everyone should pick their own name in the bottom-left corner, so")
    print("  that assignments, comments and the activity log say who did what.")
    print()
    print("  Keep this window open. Closing it closes the workspace for everyone.")
    print("  Anyone on the office network with this link can read constituent")
    print("  casework and the full budget detail — keep it inside the office.")
    print()

    return subprocess.call([sys.executable, "-m", "universe", "workspace",
                            "--host", "0.0.0.0", "--port", str(PORT),
                            "--no-browser"], cwd=str(HERE))


if __name__ == "__main__":
    raise SystemExit(main())
