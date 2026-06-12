#!/usr/bin/env python3
"""Automated checks for BUG DOOM. Run: python3 test_game.py

Exits 0 if all checks pass. Designed to survive internal refactors:
only relies on the module importing cleanly, parse_input semantics,
and the game running/quitting cleanly inside a pseudo-terminal.
"""

import fcntl
import os
import pty
import re
import select
import struct
import subprocess
import sys
import termios
import time

FAILURES = []


def check(name, ok, detail=""):
    status = "ok" if ok else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


def run_pty(cols, rows, inputs, seconds, settle=1.5):
    """Run the game in a pty, feed inputs spread over `seconds`, then quit.

    Returns (exit_code, decoded_output).
    """
    pid, fd = pty.fork()
    if pid == 0:
        os.environ["TERM"] = "xterm-256color"
        os.environ["COLORTERM"] = "truecolor"
        os.execvp(sys.executable, [sys.executable, "bug_doom.py"])

    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    # accumulate chunks in a list: quadratic bytes-concat here slows the
    # reader, fills the pty buffer, and blocks the game's stdout writes —
    # which depresses the very fps this harness is trying to measure
    chunks = []

    def drain(timeout):
        r, _, _ = select.select([fd], [], [], timeout)
        if r:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                return False
            if not chunk:
                return False
            chunks.append(chunk)
        return True

    time.sleep(settle)  # let the game boot
    step = seconds / max(1, len(inputs))
    for data in inputs:
        try:
            os.write(fd, data)
        except OSError:
            break
        deadline = time.time() + step
        while time.time() < deadline:
            if not drain(0.05):
                break
    try:
        os.write(fd, b"q")
    except OSError:
        pass
    code = None
    deadline = time.time() + 4.0
    while time.time() < deadline:
        drain(0.1)
        try:
            done, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            code = 0
            break
        if done:
            code = os.waitstatus_to_exitcode(status)
            break
    if code is None:
        os.kill(pid, 9)
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        code = -9
    while drain(0.2):
        pass
    try:
        os.close(fd)
    except OSError:
        pass
    return code, b"".join(chunks).decode("utf-8", "replace")


def main():
    print("BUG DOOM checks")

    # 1. module imports cleanly (main() is __main__-guarded)
    r = subprocess.run([sys.executable, "-c", "import bug_doom"],
                       capture_output=True, text=True, timeout=30)
    check("module imports", r.returncode == 0, r.stderr.strip()[-500:])
    if r.returncode != 0:
        report()

    # 2. input parser semantics
    import bug_doom as bd
    ev, rest = bd.parse_input(b"w \x1b[D")
    check("parser: keys + arrows", ev == ["w", " ", "LEFT"] and rest == b"", repr((ev, rest)))
    ev, _ = bd.parse_input(b"\x1b[<35;40;10M\x1b[<0;60;12M\x1b[<64;40;10M")
    check("parser: mouse move/click/scroll",
          ev == [("move", 40, 10, 3), ("click", 60, 12), ("scroll", 1)], repr(ev))
    ev, rest = bd.parse_input(b"\x1b[<35;4")
    ok = ev == [] and rest == b"\x1b[<35;4"
    ev2, rest2 = bd.parse_input(rest + b"0;10M")
    check("parser: split sequence", ok and ev2 == [("move", 40, 10, 3)] and rest2 == b"",
          repr((ev, rest, ev2, rest2)))

    # 3. gameplay session in a pty: move, turn, shoot, mouse — must not crash
    inputs = [b"w", b"w", b"a", b" ", b"\x1b[D", b"\x1b[D", b" ", b"d", b"s",
              b"\x1b[<35;40;17M", b"\x1b[<35;55;17M", b"\x1b[<0;55;17M",
              b"\x1b[<0;55;17m", b"\x1b[<32;58;17M", b"\x1b[<64;55;17M",
              b" ", b"\x1b[C", b"\x1b[C", b" ", b"w"]
    code, out = run_pty(120, 35, inputs, seconds=5.0)
    frames = out.count("\x1b[H")
    check("gameplay: clean exit", code == 0, f"exit={code}")
    check("gameplay: no traceback", "Traceback" not in out,
          out[out.find("Traceback"):][:500] if "Traceback" in out else "")
    check("gameplay: frames rendered", frames > 30, f"frames={frames}")
    check("gameplay: goodbye message", "Thanks for playing" in out)
    fps_vals = [int(m) for m in re.findall(r"(\d+) fps", out)]
    if fps_vals:
        steady = fps_vals[len(fps_vals) // 2:]
        check("gameplay: >=20 fps", max(steady) >= 20, f"fps={steady[-5:]}")

    # 4. tiny terminal: shows a notice instead of crashing, q still quits
    code, out = run_pty(30, 10, [b"w"], seconds=1.0)
    check("tiny terminal: clean exit", code == 0 and "Traceback" not in out, f"exit={code}")

    report()


def report():
    if FAILURES:
        print(f"\n{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
        sys.exit(1)
    print("\nAll checks passed.")
    sys.exit(0)


if __name__ == "__main__":
    main()
