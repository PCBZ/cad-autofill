"""scan_candata_controls.walk_controls on fake control trees (no Windows, no pywinauto)."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import scan_candata_controls as scan


class Ctrl:
    """A fake control: a name, a kind, and children."""

    def __init__(self, name, kind="other", children=()):
        self.name, self.kind, self._children = name, kind, list(children)
        self.children_calls = 0

    def children(self):
        self.children_calls += 1
        return self._children


def build(ctrl):
    return SimpleNamespace(name=ctrl.name, kind=ctrl.kind)


def tree():
    #  root
    #  |- a
    #  |  |- a1
    #  |  `- a2
    #  `- b
    #     `- b1
    return Ctrl(
        "root",
        children=[
            Ctrl("a", children=[Ctrl("a1"), Ctrl("a2")]),
            Ctrl("b", children=[Ctrl("b1")]),
        ],
    )


def walk(root, **kwargs):
    rows = []
    options = dict(max_controls=100, max_depth=25, time_limit=60)
    options.update(kwargs)
    stopped = scan.walk_controls(root, build, rows.append, **options)
    return [r.name for r in rows], stopped


class WalkTests(unittest.TestCase):
    def test_reads_every_control_depth_first(self):
        names, stopped = walk(tree())
        self.assertEqual(names, ["a", "a1", "a2", "b", "b1"])
        self.assertEqual(stopped, "")

    def test_stops_at_the_control_limit_and_keeps_what_it_read(self):
        names, stopped = walk(tree(), max_controls=3)
        self.assertEqual(names, ["a", "a1", "a2"])
        self.assertEqual(stopped, "control limit 3")

    def test_limit_equal_to_the_size_still_reports_the_limit(self):
        # Reading the last allowed control does not prove there was nothing after it
        # (its own children are never enumerated), so this is not reported as "read
        # everything".
        names, stopped = walk(tree(), max_controls=5)
        self.assertEqual((len(names), stopped), (5, "control limit 5"))

    def test_stops_at_the_time_limit(self):
        ticks = iter(range(0, 1000))  # the fake clock advances 1 s per check
        names, stopped = walk(tree(), time_limit=2.5, clock=lambda: next(ticks))
        self.assertTrue(stopped.startswith("time limit"))
        self.assertLess(len(names), 5)

    def test_does_not_go_deeper_than_max_depth(self):
        names, _ = walk(tree(), max_depth=1)
        self.assertEqual(names, ["a", "b"])

    def test_grid_contents_are_skipped_by_default(self):
        cells = Ctrl("grid", kind="grid", children=[Ctrl(f"cell{i}") for i in range(5000)])
        root = Ctrl("root", children=[cells, Ctrl("after")])
        names, stopped = walk(root)
        self.assertEqual(names, ["grid", "after"])
        self.assertEqual(stopped, "")
        self.assertEqual(cells.children_calls, 0)  # never even listed the 5000 cells

    def test_grid_contents_are_read_with_the_flag(self):
        grid = Ctrl("grid", kind="grid", children=[Ctrl("c1"), Ctrl("c2")])
        names, _ = walk(Ctrl("root", children=[grid]), descend_grids=True)
        self.assertEqual(names, ["grid", "c1", "c2"])

    def test_rows_are_delivered_as_they_are_read(self):
        # If something raises partway (Ctrl+C), earlier rows are already in the callback.
        seen = []

        def explode_on_b(ctrl):
            if ctrl.name == "b":
                raise KeyboardInterrupt
            return build(ctrl)

        with self.assertRaises(KeyboardInterrupt):
            scan.walk_controls(
                tree(),
                explode_on_b,
                seen.append,
                max_controls=100,
                max_depth=25,
                time_limit=60,
            )
        self.assertEqual([r.name for r in seen], ["a", "a1", "a2"])

    def test_a_control_whose_children_cannot_be_listed_is_reported_as_incomplete(self):
        broken = Ctrl("broken")
        broken.children = lambda: (_ for _ in ()).throw(RuntimeError("COM error"))
        names, stopped = walk(Ctrl("root", children=[broken, Ctrl("next")]))
        self.assertEqual(names, ["broken", "next"])  # siblings are still read
        self.assertEqual(stopped, "1 control(s) had unreadable children")

    def test_progress_is_reported_every_few_controls(self):
        counts = []
        with patch.object(scan, "PROGRESS_EVERY", 2):
            walk(tree(), progress=counts.append)
        self.assertEqual(counts, [2, 4])


class ArgumentTests(unittest.TestCase):
    def test_defaults_are_fast(self):
        args = scan.parse_args([])
        self.assertEqual(args.backend, "uia")
        self.assertFalse(args.tree)
        self.assertFalse(args.grids)
        self.assertEqual((args.max_controls, args.time_limit), (1500, 120))

    def test_everything_can_be_asked_for(self):
        args = scan.parse_args(
            [
                "--backend",
                "both",
                "--tree",
                "--grids",
                "--max-controls",
                "5000",
                "--time-limit",
                "600",
            ]
        )
        self.assertEqual(args.backend, "both")
        self.assertTrue(args.tree and args.grids)
        self.assertEqual((args.max_controls, args.time_limit), (5000, 600))

    def test_tree_and_mask_values_together_is_rejected(self):
        # --tree dumps the full control tree unmasked, which would defeat --mask-values.
        with self.assertRaises(SystemExit):
            scan.parse_args(["--tree", "--mask-values"])


if __name__ == "__main__":
    unittest.main()
