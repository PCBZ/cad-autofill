"""Demonstrate each shared RPA safety mechanism (issue #21) against a real window,
and print a PASS/FAIL summary to copy into that issue.

This is NOT a unit test: it drives real keyboard state and a real window, and
needs a person at the keyboard to follow the prompts (press Esc when asked,
click away when asked, ...). tests/test_rpa_safety.py already covers the logic
itself offline, with fakes; this script instead exercises the real `keyboard`,
`ctypes` and `pyautogui` default callbacks in `src/rpa/safety.py`.

Usage
-----
1. Open the window to test and leave it in the foreground, not minimized:
   Notepad first, then (separately) Candata on a new, unsaved entry.
2. Run, picking the window the same way tools/scan_candata_controls.py does:

       python tools/demo_rpa_safety.py --title notepad
       python tools/demo_rpa_safety.py --title candata

3. Follow each prompt. At the end, copy the printed summary into issue #21,
   once for Notepad and once for Candata.

The single-instance lock needs two consoles instead of a target window:

    python tools/demo_rpa_safety.py --lock-only        (run this first, leave it waiting)
    python tools/demo_rpa_safety.py --lock-only         (then this, in a second console)

Do not save or submit any Candata entry, and do not paste shipment data or
screenshots into the issue -- only the PASS/FAIL lines.
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.rpa.safety import (
    AlreadyRunning,
    SafetyGuard,
    StopRun,
    acquire_single_instance_lock,
    release_single_instance_lock,
)
from tools.scan_candata_controls import enumerate_windows, pick_target, print_candidates

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


def ask(prompt: str) -> None:
    input(f"\n>>> {prompt}\nPress Enter when ready: ")


def demo_emergency_stop(guard: SafetyGuard) -> None:
    print("\n--- Esc stops immediately ---")
    guard.arm_emergency_stop()
    try:
        ask("Press Esc now.")
        deadline = time.perf_counter() + 5
        while time.perf_counter() < deadline:
            try:
                guard.check()
            except StopRun as exc:
                record("Esc stops immediately", True, str(exc))
                return
            time.sleep(0.1)
        record("Esc stops immediately", False, "Esc was not detected within 5s")
    finally:
        guard.disarm_emergency_stop()


def demo_focus(guard: SafetyGuard) -> None:
    print("\n--- Abort when the target loses focus ---")
    try:
        guard.check_focus()
        record("Focus check passes while the target is in front", True)
    except StopRun as exc:
        record("Focus check passes while the target is in front", False, f"unexpected: {exc}")
        return

    ask("Click on a DIFFERENT window (e.g. this console) to take focus away.")
    try:
        guard.check_focus()
        record("Focus check catches losing focus", False, "lost focus was not detected")
    except StopRun as exc:
        record("Focus check catches losing focus", True, str(exc))


def demo_wait_until() -> None:
    print("\n--- Explicit waits, no fixed sleep ---")
    guard = SafetyGuard(target_pid=0)  # not focus-scoped for this part
    try:
        guard.wait_until(lambda: True, timeout=1, what="an already-ready condition")
        record("wait_until returns as soon as ready", True)
    except StopRun as exc:
        record("wait_until returns as soon as ready", False, str(exc))

    try:
        guard.wait_until(lambda: False, timeout=2, what="a control that never appears")
        record("wait_until times out and stops the run", False, "did not raise StopRun")
    except StopRun as exc:
        record("wait_until times out and stops the run", True, str(exc))


def _title_is_expected(title: str, expected_titles: list[str]) -> bool:
    return title == "" or any(t.lower() in title.lower() for t in expected_titles)


def demo_unknown_window(guard: SafetyGuard, expected_titles: list[str]) -> None:
    print("\n--- Unknown popup: screenshot and stop ---")
    out_dir = Path("output")
    out_dir.mkdir(exist_ok=True)
    screenshot_path = str(out_dir / "rpa_safety_demo_popup.png")

    def list_titles() -> list[str]:
        return [c.title for c in enumerate_windows() if not c.minimized]

    def screenshot(path: str) -> None:
        import pyautogui

        pyautogui.screenshot(path)

    def is_expected(title: str) -> bool:
        return _title_is_expected(title, expected_titles)

    try:
        guard.check_for_unknown_windows(
            is_expected,
            list_titles=list_titles,
            screenshot=screenshot,
            screenshot_path=screenshot_path,
        )
        record("No unexpected window present", True)
    except StopRun as exc:
        record("No unexpected window present", False, f"unexpected: {exc}")

    ask("Open an unrelated window (e.g. Calculator), then come back here.")
    try:
        guard.check_for_unknown_windows(
            is_expected,
            list_titles=list_titles,
            screenshot=screenshot,
            screenshot_path=screenshot_path,
        )
        record(
            "Unknown popup is detected and screenshotted", False, "the extra window was not flagged"
        )
    except StopRun as exc:
        record(
            "Unknown popup is detected and screenshotted",
            True,
            f"{exc}; screenshot at {screenshot_path}",
        )
    print("Close the extra window before continuing.")


def demo_pre_run_warning() -> None:
    print("\n--- Pre-run warning before touching mouse/keyboard ---")
    from src.rpa.safety import PreRunNotConfirmed, confirm_before_run

    try:
        confirm_before_run()
        record("Pre-run confirmation accepted on the exact word", True)
    except PreRunNotConfirmed as exc:
        record("Pre-run confirmation accepted on the exact word", False, str(exc))


def demo_single_instance_lock(lock_only: bool) -> None:
    print("\n--- Only one fill runs at a time ---")
    name = "Global\\CadAutofillDemoLock"
    try:
        handle = acquire_single_instance_lock(name)
    except AlreadyRunning as exc:
        record("Single-instance lock refuses a second instance", True, str(exc))
        return

    record("Single-instance lock: first instance acquires it", True)
    if lock_only:
        print("Holding the lock. Run this script again (--lock-only) in another console now.")
        ask("Ready to release the lock and exit.")
    release_single_instance_lock(handle)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--title", default="notepad", help="window title regex (default: notepad)")
    parser.add_argument("--process", default=None, help="process exe name regex")
    parser.add_argument(
        "--handle", default=None, type=lambda s: int(s, 0), help="exact window handle"
    )
    parser.add_argument(
        "--lock-only",
        action="store_true",
        help="only demonstrate the single-instance lock, then exit (needs two consoles)",
    )
    args = parser.parse_args(argv)

    if args.lock_only:
        demo_single_instance_lock(lock_only=True)
        return 0 if all(ok for _, ok, _ in RESULTS) else 3

    if sys.platform != "win32":
        print("This script must run on Windows.")
        return 1

    cands = enumerate_windows()
    target, others = pick_target(args, cands)
    if target is None:
        print(f"No window matched title /{args.title}/.")
        print_candidates(cands, "Top-level windows:")
        return 2
    print(f"Target: {target.exe} (pid {target.pid})  hwnd={target.hwnd:#x}  {target.title}")
    if others:
        print_candidates(others, "Other matching windows (use --handle to pick one):")

    # A fresh SafetyGuard per mechanism: StopRun is sticky by design (the first
    # reason wins, and a real run never auto-recovers from it), so reusing one
    # guard across these independent demos would make every check after the
    # first stop fail with that same, stale reason.
    demo_emergency_stop(SafetyGuard(target.pid))
    demo_focus(SafetyGuard(target.pid))
    demo_wait_until()
    demo_unknown_window(SafetyGuard(target.pid), expected_titles=[target.title or args.title])
    demo_pre_run_warning()
    demo_single_instance_lock(lock_only=False)

    print(f"\n=== Summary for {args.title!r} (copy into issue #21) ===")
    for name, ok, detail in RESULTS:
        line = f"- [{'PASS' if ok else 'FAIL'}] {name}"
        print(line + (f" -- {detail}" if detail else ""))

    return 0 if all(ok for _, ok, _ in RESULTS) else 3


if __name__ == "__main__":
    raise SystemExit(main())
