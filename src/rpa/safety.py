"""Shared RPA safety mechanisms for rpa_filler and rpa_importer (issue #21).

One module, one stop signal: `SafetyGuard` wraps a single `threading.Event`. Every
wait and action helper in rpa_filler/rpa_importer calls `check()` (or one of the
mechanism methods below, which call it for you) instead of touching the mouse,
keyboard or `time.sleep` directly. Once the signal is set, every later `check()`
raises `StopRun` with the reason the first mechanism gave; the run never
auto-recovers (for example by bringing Candata back to the front) -- a stop always
means a person looks.

Mechanisms:

* Global Esc hotkey stops immediately -- `SafetyGuard.arm_emergency_stop`
* Abort when Candata loses focus -- `SafetyGuard.check_focus`
* Explicit waits for controls, no fixed sleeps -- `SafetyGuard.wait_until`
* Unknown popup: screenshot and stop -- `SafetyGuard.check_for_unknown_windows`
* Pre-run warning not to touch mouse and keyboard -- `confirm_before_run`
* Only one fill runs at a time -- `acquire_single_instance_lock`

When typing a long field, type it in short pieces and call `check()` between them,
so Esc is noticed within one piece instead of only between whole fields.

Demonstrating each mechanism on the Candata machine itself (issue #21's "Done
when") still needs a person at that computer; this module provides the logic,
covered here with fakes (no pywinauto/keyboard/pyautogui import at module load --
only inside the real default callbacks).
"""

import threading
import time
from collections.abc import Callable, Iterable
from typing import Self


class StopRun(RuntimeError):
    """Raised by `SafetyGuard.check()` once the shared stop signal is set."""


class PreRunNotConfirmed(RuntimeError):
    """Raised when the operator did not give the typed pre-run confirmation."""


class AlreadyRunning(RuntimeError):
    """Raised when another fill already holds the single-instance lock."""


class SafetyGuard:
    """Ties the in-run safety mechanisms to one shared stop signal.

    `target_pid` is the Candata process every action and focus check is scoped
    to. `register`/`unregister`/`get_foreground_pid` are injected so this is
    testable without real keyboard hardware or a real desktop; the defaults call
    `keyboard` and `ctypes`, imported lazily so importing this module never
    requires them.
    """

    def __init__(
        self,
        target_pid: int,
        *,
        hotkey: str = "esc",
        register: Callable[[str, Callable[[], None]], object] | None = None,
        unregister: Callable[[object], None] | None = None,
        get_foreground_pid: Callable[[], int] | None = None,
    ):
        self.target_pid = target_pid
        self.hotkey = hotkey
        self._register = register or _default_register
        self._unregister = unregister or _default_unregister
        self._get_foreground_pid = get_foreground_pid or _default_foreground_pid
        self._handle: object | None = None
        self._stop_event = threading.Event()
        self._reason: str | None = None

    # -- the shared stop signal ----------------------------------------------

    @property
    def stopped(self) -> bool:
        return self._stop_event.is_set()

    def stop(self, reason: str) -> None:
        """Set the shared stop signal, if it is not already set.

        The first reason wins: once something has stopped the run, a second,
        possibly confusing symptom of the same stop (a focus check failing right
        after Esc was pressed) must not overwrite it.
        """
        if not self._stop_event.is_set():
            self._reason = reason
            self._stop_event.set()

    def check(self) -> None:
        """Raise `StopRun` if the shared stop signal is set."""
        if self._stop_event.is_set():
            raise StopRun(self._reason)

    # -- mechanism: global Esc hotkey ----------------------------------------

    def arm_emergency_stop(self) -> None:
        if self._handle is None:
            self._handle = self._register(
                self.hotkey, lambda: self.stop(f"'{self.hotkey}' was pressed")
            )

    def disarm_emergency_stop(self) -> None:
        if self._handle is not None:
            self._unregister(self._handle)
            self._handle = None

    def __enter__(self) -> Self:
        self.arm_emergency_stop()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.disarm_emergency_stop()

    # -- mechanism: focus -----------------------------------------------------

    def check_focus(self) -> None:
        """Stop the run if Candata's process is no longer in the foreground.

        Compares process IDs, not window handles, so Candata's own dialogs (the
        import file picker, a drop-down's list, a confirmation popup) do not look
        like lost focus just for getting a different hwnd; whether such a dialog
        is itself expected is for `check_for_unknown_windows`, not this check.
        Does not bring Candata back to the front: something else took focus, and
        a person should look.
        """
        self.check()
        current = self._get_foreground_pid()
        if current != self.target_pid:
            self.stop(f"Candata (pid={self.target_pid}) lost focus (foreground pid={current})")
            self.check()

    # -- mechanism: explicit waits --------------------------------------------

    def wait_until(
        self,
        predicate: Callable[[], object],
        timeout: float,
        *,
        what: str = "condition",
        poll_interval: float = 0.2,
        clock: Callable[[], float] = time.perf_counter,
        sleep: Callable[[float], None] = time.sleep,
    ) -> object:
        """Poll `predicate` until it is truthy, instead of a fixed `sleep(n)`.

        A fixed sleep is either too short (the control is not ready yet, so the
        next action fails) or wastes time on every run. This polls at
        `poll_interval`, checking the shared stop signal on every poll so a wait
        is interrupted immediately rather than only between actions, and stops
        the run (`StopRun`, naming `what`) if `predicate` is still falsy after
        `timeout` seconds.
        """
        self.check()
        deadline = clock() + timeout
        while True:
            value = predicate()
            if value:
                return value
            if clock() >= deadline:
                self.stop(f"{what} was not ready within {timeout:g}s")
                self.check()
            sleep(poll_interval)
            self.check()

    # -- mechanism: unknown popups ---------------------------------------------

    def check_for_unknown_windows(
        self,
        is_expected: Callable[[str], bool],
        *,
        list_titles: Callable[[], Iterable[str | None]],
        screenshot: Callable[[str], None],
        screenshot_path: str,
    ) -> None:
        """Stop the run for any open window `is_expected` rejects.

        A window neither Candata nor one the caller is waiting for (an error
        dialog, a Windows Update prompt, a crash report, an untitled modal) means
        something unplanned happened. A blank or missing title is passed to
        `is_expected` as `""` rather than skipped, since an untitled popup is
        exactly the kind of surprise this check exists to catch; a caller that
        does expect blank titles must say so explicitly. Never clicks "OK" or
        otherwise dismisses the window itself: `screenshot` is called with
        `screenshot_path` before the run stops, so a person can see what
        appeared.

        `list_titles` should list only Candata's own top-level windows, not the
        whole desktop: an unrelated app (Outlook, a browser) must not stop the
        run just for being open.
        """
        self.check()
        for title in list_titles():
            if not is_expected(title or ""):
                screenshot(screenshot_path)
                self.stop(f"unexpected window {title!r}; screenshot saved to {screenshot_path}")
                self.check()


def _default_register(hotkey: str, callback: Callable[[], None]) -> object:
    import keyboard

    return keyboard.add_hotkey(hotkey, callback)


def _default_unregister(handle: object) -> None:
    import keyboard

    keyboard.remove_hotkey(handle)


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


# -- mechanism: pre-run warning ------------------------------------------------

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


# -- mechanism: only one fill runs at a time -----------------------------------


def acquire_single_instance_lock(
    name: str = "Global\\CadAutofillFill",
    *,
    create_mutex: Callable[[str], tuple[object, bool]] | None = None,
) -> object:
    """Take a named OS mutex so only one fill runs at a time.

    Candata works on one entry at a time, so a second, concurrent fill would
    fight the first for the same controls. Returns a handle; raises
    `AlreadyRunning` if another process already holds the lock. The lock is
    released by the operating system when the process holding it exits, even
    after a crash, so a stale lock from a crashed run never needs manual
    cleanup; `release_single_instance_lock` only releases it early.

    `create_mutex` is injected so this is testable without creating a real,
    system-wide mutex; it returns `(handle, already_existed)`, matching
    `CreateMutexW` plus `GetLastError() == ERROR_ALREADY_EXISTS`.
    """
    create_mutex = create_mutex or _default_create_mutex
    handle, already_existed = create_mutex(name)
    if already_existed:
        raise AlreadyRunning(f"another fill is already running ({name!r})")
    return handle


def release_single_instance_lock(
    handle: object, *, close: Callable[[object], None] | None = None
) -> None:
    """Release a lock from `acquire_single_instance_lock` before process exit.

    Optional: the operating system also releases it when the process ends.
    """
    close = close or _default_close_handle
    close(handle)


def _default_create_mutex(name: str) -> tuple[object, bool]:
    import ctypes

    ERROR_ALREADY_EXISTS = 183
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, name)
    already_existed = ctypes.GetLastError() == ERROR_ALREADY_EXISTS
    return handle, already_existed


def _default_close_handle(handle: object) -> None:
    import ctypes

    ctypes.windll.kernel32.CloseHandle(handle)
