"""Offline tests for the shared RPA safety mechanisms (issue #21); no Windows UI,
keyboard hardware or real Candata window needed -- every boundary is a fake."""

import unittest

from src.rpa.safety import (
    CONFIRMATION,
    AlreadyRunning,
    PreRunNotConfirmed,
    SafetyGuard,
    StopRun,
    acquire_single_instance_lock,
    confirm_before_run,
    release_single_instance_lock,
)


def fake_registrar():
    callbacks = {}

    def register(hotkey, callback):
        callbacks[hotkey] = callback
        return hotkey

    def unregister(handle):
        del callbacks[handle]

    return callbacks, register, unregister


def guard(target_pid=1234, foreground_pid=1234):
    callbacks, register, unregister = fake_registrar()
    g = SafetyGuard(
        target_pid,
        register=register,
        unregister=unregister,
        get_foreground_pid=lambda: foreground_pid,
    )
    return g, callbacks


class StopSignalTests(unittest.TestCase):
    def test_check_passes_until_something_stops_the_run(self):
        g, _ = guard()
        g.check()  # does not raise
        self.assertFalse(g.stopped)

    def test_stop_then_check_raises_with_the_reason(self):
        g, _ = guard()
        g.stop("something went wrong")
        self.assertTrue(g.stopped)
        with self.assertRaises(StopRun) as ctx:
            g.check()
        self.assertEqual(str(ctx.exception), "something went wrong")

    def test_the_first_reason_wins(self):
        g, _ = guard()
        g.stop("first")
        g.stop("second")
        with self.assertRaises(StopRun) as ctx:
            g.check()
        self.assertEqual(str(ctx.exception), "first")


class EmergencyStopTests(unittest.TestCase):
    def test_pressing_the_hotkey_stops_the_run(self):
        g, callbacks = guard()
        g.arm_emergency_stop()
        g.check()  # does not raise
        callbacks["esc"]()  # simulates the operator pressing Esc
        with self.assertRaises(StopRun):
            g.check()

    def test_arm_is_idempotent(self):
        calls = []
        _, register, unregister = fake_registrar()
        g = SafetyGuard(
            1234,
            register=lambda h, cb: (calls.append(1), register(h, cb))[1],
            unregister=unregister,
            get_foreground_pid=lambda: 1234,
        )
        g.arm_emergency_stop()
        g.arm_emergency_stop()
        self.assertEqual(len(calls), 1)

    def test_context_manager_disarms_on_exit(self):
        g, callbacks = guard()
        with g:
            self.assertIn("esc", callbacks)
        self.assertNotIn("esc", callbacks)

    def test_disarm_without_arm_is_a_no_op(self):
        g, _ = guard()
        g.disarm_emergency_stop()  # does not raise


class FocusTests(unittest.TestCase):
    def test_same_process_foreground_passes(self):
        # Also covers Candata's own dialogs (file picker, drop-down, confirmation):
        # a different hwnd, but still Candata's process, so this is not lost focus.
        g, _ = guard(target_pid=1234, foreground_pid=1234)
        g.check_focus()  # does not raise

    def test_different_process_foreground_stops_the_run(self):
        g, _ = guard(target_pid=1234, foreground_pid=5678)
        with self.assertRaises(StopRun):
            g.check_focus()
        self.assertTrue(g.stopped)

    def test_already_stopped_is_checked_first(self):
        g, _ = guard(target_pid=1234, foreground_pid=1234)
        g.stop("esc")
        with self.assertRaises(StopRun):
            g.check_focus()


class WaitUntilTests(unittest.TestCase):
    def test_returns_as_soon_as_the_predicate_is_truthy(self):
        g, _ = guard()
        values = iter([False, False, "ready"])
        sleeps = []
        result = g.wait_until(
            lambda: next(values),
            timeout=10,
            clock=lambda: 0,
            sleep=sleeps.append,
        )
        self.assertEqual(result, "ready")
        self.assertEqual(sleeps, [0.2, 0.2])

    def test_times_out_and_stops_the_run_naming_what(self):
        ticks = iter([0, 0, 1, 2, 3])  # advances past the 2.5s deadline on the 4th check
        g, _ = guard()
        with self.assertRaises(StopRun) as ctx:
            g.wait_until(
                lambda: False,
                timeout=2.5,
                what="the carrier field",
                clock=lambda: next(ticks),
                sleep=lambda s: None,
            )
        self.assertIn("the carrier field", str(ctx.exception))
        self.assertTrue(g.stopped)

    def test_the_stop_signal_is_polled_on_every_iteration(self):
        g, callbacks = guard()
        g.arm_emergency_stop()
        calls = []

        def predicate():
            calls.append(1)
            if len(calls) == 2:
                callbacks["esc"]()
            return False

        with self.assertRaises(StopRun):
            g.wait_until(predicate, timeout=10, clock=lambda: 0, sleep=lambda s: None)
        self.assertEqual(len(calls), 2)

    def test_an_already_stopped_guard_never_calls_the_predicate(self):
        g, _ = guard()
        g.stop("esc")
        with self.assertRaises(StopRun):
            g.wait_until(lambda: (_ for _ in ()).throw(AssertionError("should not be called")), 10)


class UnknownWindowTests(unittest.TestCase):
    def test_only_expected_windows_is_silent(self):
        g, _ = guard()
        g.check_for_unknown_windows(
            lambda title: title in {"Candata", ""},
            list_titles=lambda: ["Candata", ""],
            screenshot=lambda path: (_ for _ in ()).throw(AssertionError("should not screenshot")),
            screenshot_path="unused.png",
        )  # does not raise

    def test_unexpected_window_screenshots_then_stops_the_run(self):
        g, _ = guard()
        shots = []
        with self.assertRaises(StopRun) as ctx:
            g.check_for_unknown_windows(
                lambda title: title == "Candata",
                list_titles=lambda: ["Candata", "Windows Update"],
                screenshot=shots.append,
                screenshot_path="popup.png",
            )
        self.assertEqual(shots, ["popup.png"])
        self.assertIn("Windows Update", str(ctx.exception))
        self.assertTrue(g.stopped)

    def test_a_blank_or_missing_title_is_unexpected_by_default(self):
        # An untitled modal is exactly the kind of surprise this check exists to
        # catch, so a blank/missing title must not be let through unconditionally.
        for titles in (["", "Candata"], [None, "Candata"]):
            with self.subTest(titles=titles):
                g, _ = guard()
                shots = []
                with self.assertRaises(StopRun):
                    g.check_for_unknown_windows(
                        lambda title: title == "Candata",
                        list_titles=lambda titles=titles: titles,
                        screenshot=shots.append,
                        screenshot_path="popup.png",
                    )
                self.assertEqual(shots, ["popup.png"])

    def test_a_blank_title_can_be_explicitly_allowed(self):
        g, _ = guard()
        g.check_for_unknown_windows(
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


class SingleInstanceLockTests(unittest.TestCase):
    def fake_mutex(self):
        held = {}

        def create_mutex(name):
            already_existed = name in held
            held[name] = held.get(name, 0) + 1
            return (name, already_existed)

        return held, create_mutex

    def test_first_caller_gets_the_lock(self):
        _, create_mutex = self.fake_mutex()
        handle = acquire_single_instance_lock("test-lock", create_mutex=create_mutex)
        self.assertEqual(handle, "test-lock")

    def test_second_caller_is_refused(self):
        _, create_mutex = self.fake_mutex()
        acquire_single_instance_lock("test-lock", create_mutex=create_mutex)
        with self.assertRaises(AlreadyRunning):
            acquire_single_instance_lock("test-lock", create_mutex=create_mutex)

    def test_different_names_do_not_conflict(self):
        _, create_mutex = self.fake_mutex()
        acquire_single_instance_lock("lock-a", create_mutex=create_mutex)
        acquire_single_instance_lock("lock-b", create_mutex=create_mutex)  # does not raise

    def test_release_calls_close_with_the_handle(self):
        closed = []
        release_single_instance_lock("a-handle", close=closed.append)
        self.assertEqual(closed, ["a-handle"])


if __name__ == "__main__":
    unittest.main()
