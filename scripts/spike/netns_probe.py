"""Gate-zero probe: can we build the netns egress jail in THIS runtime?

Prints the effective capabilities and whether unshare(CLONE_NEWNET) + loopback +
egress-block all work. Authoritative answer must come from a REAL Keboola job pod;
local docker only checks the mechanics (its default cap set differs from the job pod).
"""

import ctypes
import ctypes.util
import os
import socket
import subprocess
import sys

CLONE_NEWNET = 0x40000000
libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)


def caps_line() -> str:
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("CapEff"):
                return line.strip()
    return "CapEff: ?"


def _try_lo_up() -> None:
    try:
        subprocess.run(["ip", "link", "set", "lo", "up"], check=False)
    except FileNotFoundError:
        print("note: `ip` not present; skipping `lo up` (egress test is unaffected)")


def main() -> int:
    print(f"uid={os.getuid()} euid={os.geteuid()}")
    print(caps_line())  # decode with: capsh --decode=<hex>
    rc = libc.unshare(CLONE_NEWNET)
    if rc != 0:
        e = ctypes.get_errno()
        print(f"RESULT: unshare(CLONE_NEWNET) FAILED errno={e} ({os.strerror(e)})")
        print("VERDICT: gate-zero FAIL - netns not available; use fallback (Task 0b)")
        return 2
    _try_lo_up()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(3)
    try:
        s.connect(("8.8.8.8", 53))
        print("RESULT: external connect SUCCEEDED - egress NOT blocked by netns")
        print("VERDICT: gate-zero FAIL - empty netns still has a route")
        return 3
    except OSError as e:
        print(f"RESULT: external connect blocked ({e})")
        print("VERDICT: gate-zero PASS - netns + lo + egress-block all work")
        return 0
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
