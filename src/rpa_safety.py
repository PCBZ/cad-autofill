"""Shared RPA safety mechanisms for rpa_filler and rpa_importer (issue #21).

Five mechanisms, each kept independent of pywinauto/keyboard/pyautogui so it can be
exercised with fakes, without a real Candata window:

* Global Esc hotkey stops immediately -- `EmergencyStop`
* Abort when Candata loses focus -- `check_focus`
* Explicit waits for controls, no fixed sleeps -- `wait_until`
* Unknown popup: screenshot and stop -- `check_for_unknown_windows`
* Pre-run warning not to touch mouse and keyboard -- `confirm_before_run`

Demonstrating each mechanism on the Candata machine itself (issue #21's "Done when")
still needs a person at that computer; this module provides the logic, covered here
with fakes, for rpa_filler and rpa_importer to call into.
"""

import time
from collections.abc import Callable, Iterable
from typing import Self


class EmergencyStopRequested(RuntimeError):
    """Raised when the operator pressed the emergency-stop hotkey."""


class FocusLost(RuntimeError):
    """Raised when the target window is no longer the foreground window."""


class UnexpectedWindow(RuntimeError):
    """Raised when a window outside the expected set appears."""


class PreRunNotConfirmed(RuntimeError):
    """Raised when the operator did not give the typed pre-run confirmation."""


class EmergencyStop:
    """Arms a global hotkey (default Esc) that sets a flag the instant it is pressed.

    The flag is only read later, at `check()`: between a key press and the next
    `check()` call, whatever rpa_filler/rpa_importer was doing keeps running for at
    most one step, since pywinauto actions cannot be interrupted mid-call. Callers
    should call `check()` at every safe point: before each action and inside
    `wait_until` loops (pass `stop.check` as `stop_check`).

    `register`/`unregister` are injected so this is testable without real keyboard
    hardware; the defaults call the `keyboard` package, imported lazily so importing
    this module, or using the other four mechanisms, never requires it.
    """

    def __init__(
        self,
        hotkey: str = "esc",
        *,
        register: Callable[[str, Callable[[], None]], object] | None = None,
        unregister: Callable[[object], None] | None = None,
    ):
        self.hotkey = hotkey
        self._register = register or _default_register
        self._unregister = unregister or _default_unregister
        self._handle: object | None = None
        self.requested = False

    def arm(self) -> None:
        if self._handle is None:
            self._handle = self._register(self.hotkey, self._trigger)

    def disarm(self) -> None:
        if self._handle is not None:
            self._unregister(self._handle)
            self._handle = None

    def _trigger(self) -> None:
        self.requested = True

    def check(self) -> None:
        if self.requested:
            raise EmergencyStopRequested(f"'{self.hotkey}' was pressed; stopping immediately")

    def __enter__(self) -> Self:
        self.arm()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.disarm()


def _default_register(hotkey: str, callback: Callable[[], None]) -> object:
    import keyboard

    return keyboard.add_hotkey(hotkey, callback)


def _default_unregister(handle: object) -> None:
    import keyboard

    keyboard.remove_hotkey(handle)


def check_focus(
    target_pid: int,
    *,
    get_foreground_pid: Callable[[], int] | None = None,
) -> None:
    """Raise `FocusLost` if the foreground window does not belong to Candata's process.

    Call this before each action: a click or keystroke sent while some other process
    has focus (the operator's own click, an unrelated app stealing focus) lands on
    the wrong window instead of Candata. This compares process IDs, not window
    handles, so Candata's own dialogs (the import file picker, a drop-down's list,
    a confirmation popup) do not look like lost focus just because they are a
    different hwnd; whether such a dialog is itself expected is for
    `check_for_unknown_windows` to decide, not this check.
    """
    get_foreground_pid = get_foreground_pid or _default_foreground_pid
    current = get_foreground_pid()
    if current != target_pid:
        raise FocusLost(
            f"Candata (pid={target_pid}) is no longer focused (foreground pid={current})"
        )


def _default_foreground_pid() -> int:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    # The ctypes default return type is c_int (signed); left at that default, a
    # handle with its high bit set comes back negative.
    user32.GetForegroundWindow.restype = wintypes.HWND
    hwnd = user32.GetForegroundWindow()
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def wait_until(
    predicate: Callable[[], object],
    timeout: float,
    *,
    poll_interval: float = 0.2,
    clock: Callable[[], float] = time.perf_counter,
    sleep: Callable[[float], None] = time.sleep,
    stop_check: Callable[[], None] | None = None,
) -> object:
    """Poll `predicate` until it is truthy, instead of a fixed `sleep(n)`.

    A fixed sleep is either too short (the control is not ready yet, so the next
    action fails) or wastes time on every run. This polls at `poll_interval` and
    returns as soon as `predicate` succeeds, or raises `TimeoutError` after
    `timeout` seconds. `stop_check`, when given, is called on every poll (pass
    `EmergencyStop.check`) so a wait is interrupted immediately rather than only
    between actions.
    """
    deadline = clock() + timeout
    while True:
        if stop_check is not None:
            stop_check()
        value = predicate()
        if value:
            return value
        if clock() >= deadline:
            raise TimeoutError(f"condition not met within {timeout:g}s")
        sleep(poll_interval)


def check_for_unknown_windows(
    is_expected: Callable[[str], bool],
    *,
    list_titles: Callable[[], Iterable[str | None]],
    screenshot: Callable[[str], None],
    screenshot_path: str,
) -> None:
    """Raise `UnexpectedWindow` for any open window `is_expected` rejects.

    A window neither Candata nor one the caller is waiting for (an error dialog, a
    Windows Update prompt, a crash report, an untitled modal) means something
    unplanned happened. A blank or missing title is passed to `is_expected` as `""`
    rather than skipped, since an untitled popup is exactly the kind of surprise
    this check exists to catch; a caller that does expect blank titles (some
    Candata dialogs may have none) must say so explicitly, for example with
    `is_expected=lambda title: title in {"", "Candata", ...}`.

    `list_titles` should list only Candata's own top-level windows, not the whole
    desktop: an unrelated app (Outlook, a browser) must not stop the run just for
    being open. `screenshot` is called with `screenshot_path` before raising, so
    the operator can see what appeared; `list_titles` and `screenshot` are injected
    so this needs no real desktop to test.
    """
    for title in list_titles():
        if not is_expected(title or ""):
            screenshot(screenshot_path)
            raise UnexpectedWindow(
                f"unexpected window {title!r}; screenshot saved to {screenshot_path}"
            )


PRE_RUN_WARNING = (
    "About to fill the Candata screen with real mouse clicks and keystrokes.\n"
    "Do not touch the mouse or keyboard until this finishes, or press Esc to stop."
)
CONFIRMATION = "go"


def confirm_before_run(
    *,
    confirm: Callable[[str], str] = input,
    warn: Callable[[str], None] = print,
) -> None:
    """Warn the operator and require a typed confirmation before the first action.

    Raises `PreRunNotConfirmed` on anything but the exact confirmation word, so a
    run never starts on a stray Enter key.
    """
    warn(PRE_RUN_WARNING)
    answer = confirm(f"Type '{CONFIRMATION}' to continue: ")
    if answer.strip().lower() != CONFIRMATION:
        raise PreRunNotConfirmed("pre-run confirmation was not given")
