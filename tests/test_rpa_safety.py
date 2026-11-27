"""Offline tests for the shared RPA safety mechanisms (issue #21); no Windows UI,
keyboard hardware or real Candata window needed -- every boundary is a fake."""

import unittest

from src.rpa_safety import (
    CONFIRMATION,
    EmergencyStop,
    EmergencyStopRequested,
    FocusLost,
    PreRunNotConfirmed,
    UnexpectedWindow,
    check_focus,
    check_for_unknown_windows,
    confirm_before_run,
    wait_until,
)


class EmergencyStopTests(unittest.TestCase):
    def fake_registrar(self):
        callbacks = {}

        def register(hotkey, callback):
            callbacks[hotkey] = callback
            return hotkey

        def unregister(handle):
            del callbacks[handle]

        return callbacks, register, unregister

    def test_check_passes_until_the_hotkey_fires(self):
        callbacks, register, unregister = self.fake_registrar()
        stop = EmergencyStop(register=register, unregister=unregister)
        stop.arm()
        stop.check()  # does not raise
        callbacks["esc"]()  # simulates the operator pressing Esc
        with self.assertRaises(EmergencyStopRequested):
            stop.check()

    def test_arm_is_idempotent(self):
        _callbacks, register, unregister = self.fake_registrar()
        calls = []
        stop = EmergencyStop(
            register=lambda h, cb: (calls.append(1), register(h, cb))[1],
            unregister=unregister,
        )
        stop.arm()
        stop.arm()
        self.assertEqual(len(calls), 1)

    def test_context_manager_disarms_on_exit(self):
        callbacks, register, unregister = self.fake_registrar()
        with EmergencyStop(register=register, unregister=unregister):
            self.assertIn("esc", callbacks)
        self.assertNotIn("esc", callbacks)

    def test_disarm_without_arm_is_a_no_op(self):
        _, register, unregister = self.fake_registrar()
        stop = EmergencyStop(register=register, unregister=unregister)
        stop.disarm()  # does not raise


class FocusTests(unittest.TestCase):
    def test_same_process_foreground_passes(self):
        # Also covers Candata's own dialogs (file picker, drop-down, confirmation):
        # a different hwnd, but still Candata's process, so this is not lost focus.
        check_focus(1234, get_foreground_pid=lambda: 1234)  # does not raise

    def test_different_process_foreground_raises(self):
        with self.assertRaises(FocusLost):
            check_focus(1234, get_foreground_pid=lambda: 5678)


class WaitUntilTests(unittest.TestCase):
    def test_returns_as_soon_as_the_predicate_is_truthy(self):
        values = iter([False, False, "ready"])
        sleeps = []
        result = wait_until(
            lambda: next(values),
            timeout=10,
            clock=lambda: 0,
            sleep=sleeps.append,
        )
        self.assertEqual(result, "ready")
        self.assertEqual(sleeps, [0.2, 0.2])

    def test_times_out_without_raising_early(self):
        ticks = iter([0, 0, 1, 2, 3])  # advances past the 2.5s deadline on the 4th check
        with self.assertRaises(TimeoutError):
            wait_until(lambda: False, timeout=2.5, clock=lambda: next(ticks), sleep=lambda s: None)

    def test_stop_check_is_polled_on_every_iteration(self):
        calls = []

        def stop_check():
            calls.append(1)
            if len(calls) == 2:
                raise EmergencyStopRequested("esc")

        with self.assertRaises(EmergencyStopRequested):
            wait_until(
                lambda: False,
                timeout=10,
                clock=lambda: 0,
                sleep=lambda s: None,
                stop_check=stop_check,
            )
        self.assertEqual(len(calls), 2)


class UnknownWindowTests(unittest.TestCase):
    def test_only_expected_windows_is_silent(self):
        check_for_unknown_windows(
            lambda title: title in {"Candata", ""},
            list_titles=lambda: ["Candata", ""],
            screenshot=lambda path: (_ for _ in ()).throw(AssertionError("should not screenshot")),
            screenshot_path="unused.png",
        )  # does not raise

    def test_unexpected_window_screenshots_then_raises(self):
        shots = []
        with self.assertRaises(UnexpectedWindow) as ctx:
            check_for_unknown_windows(
                lambda title: title == "Candata",
                list_titles=lambda: ["Candata", "Windows Update"],
                screenshot=shots.append,
                screenshot_path="popup.png",
            )
        self.assertEqual(shots, ["popup.png"])
        self.assertIn("Windows Update", str(ctx.exception))

    def test_a_blank_or_missing_title_is_unexpected_by_default(self):
        # An untitled modal is exactly the kind of surprise this check exists to
        # catch, so a blank/missing title must not be let through unconditionally.
        for titles in (["", "Candata"], [None, "Candata"]):
            with self.subTest(titles=titles):
                shots = []
                with self.assertRaises(UnexpectedWindow):
                    check_for_unknown_windows(
                        lambda title: title == "Candata",
                        list_titles=lambda titles=titles: titles,
                        screenshot=shots.append,
                        screenshot_path="popup.png",
                    )
                self.assertEqual(shots, ["popup.png"])

    def test_a_blank_title_can_be_explicitly_allowed(self):
        check_for_unknown_windows(
            lambda title: title in {"", "Candata"},
            list_titles=lambda: ["", "Candata"],
            screenshot=lambda path: (_ for _ in ()).throw(AssertionError("should not screenshot")),
            screenshot_path="unused.png",
        )  # does not raise


class PreRunConfirmationTests(unittest.TestCase):
    def test_exact_confirmation_passes(self):
        warnings = []
        confirm_before_run(confirm=lambda prompt: CONFIRMATION, warn=warnings.append)
        self.assertEqual(len(warnings), 1)

    def test_confirmation_is_case_and_space_insensitive(self):
        confirm_before_run(
            confirm=lambda prompt: f"  {CONFIRMATION.upper()}  ", warn=lambda msg: None
        )

    def test_anything_else_is_rejected(self):
        for answer in ("", "yes", "go!", "\n"):
            with self.subTest(answer=answer), self.assertRaises(PreRunNotConfirmed):
                confirm_before_run(
                    confirm=lambda prompt, answer=answer: answer, warn=lambda msg: None
                )


if __name__ == "__main__":
    unittest.main()
