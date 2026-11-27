"""
Scan the Candata Global Windows client's control tree to assess RPA feasibility.

The script is read-only: it enumerates windows and reads control properties and
values. It never clicks, types, focuses, restores or moves any window.

Usage
-----
1. pip install pywinauto
2. Open Candata and navigate to the screen to assess (e.g. the shipment header).
   Type a few recognizable TEST values into fields so read-back can be checked.
   Leave the window open and not minimized.
3. Run at the same privilege level as Candata. If Candata was started
   "as administrator", run this from an elevated prompt as well:

       python tools/scan_candata_controls.py --screen header

4. Repeat for every screen the automation will touch, for example:

       python tools/scan_candata_controls.py --screen invoice
       python tools/scan_candata_controls.py --screen import_dialog

5. Review output/control_scans/<screen>/:

       summary.txt          environment, warnings, metrics and verdict
       uia_controls.csv     every control with locator fields and read-back value
       win32_controls.csv   same, using the win32 backend (only with --backend win32|both)
       uia_tree.txt         pywinauto print_control_identifiers() dump (only with --tree)
       win32_tree.txt

A scan reads at most 1500 controls or 120 seconds per backend, prints progress, and
keeps what it has read if it stops or you press Ctrl+C. Rows are written as they are read.

Useful options
--------------
  --list              list top-level windows (with process names) and exit
  --title REGEX       window title regex, case-insensitive (default: "candata")
  --process REGEX     only windows whose process exe name matches
  --handle HWND       scan exactly this window (decimal or 0x hex)
  --backend NAME      uia, win32 or both (default: uia)
  --max-controls N    stop after N controls per backend (default 1500)
  --time-limit SEC    stop after SEC seconds per backend (default 120)
  --grids             also read inside data grids (thousands of cells; off by default)
  --tree              also write the control tree dump (slow)
  --mask-values       record only whether a value was readable, not the value
  --out DIR           output root (default: <project>/output/control_scans)

Exit codes: 0 ok, 1 environment problem, 2 no window found, 3 window minimized.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import functools
import io
import os
import re
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROGRESS_EVERY = 50
DEFAULT_OUT = PROJECT_ROOT / "output" / "control_scans"

# Verdict thresholds. These are heuristics; always read the CSV as well.
MIN_INPUTS = 5  # fewer exposed inputs than this counts as "poor"
GOOD_ADDRESSABLE_RATIO = 0.8  # share of inputs with a stable id or label

# Windows whose title may contain "candata" but are not the Candata client:
# browsers (help center), terminals (this script's own command line),
# editors and Explorer (project folder name), PDF viewers, Excel.
EXCLUDED_EXES = {
    "chrome.exe",
    "msedge.exe",
    "firefox.exe",
    "iexplore.exe",
    "opera.exe",
    "brave.exe",
    "cmd.exe",
    "powershell.exe",
    "pwsh.exe",
    "conhost.exe",
    "windowsterminal.exe",
    "openconsole.exe",
    "code.exe",
    "pycharm64.exe",
    "devenv.exe",
    "notepad.exe",
    "notepad++.exe",
    "explorer.exe",
    "acrobat.exe",
    "acrord32.exe",
    "excel.exe",
    "python.exe",
    "pythonw.exe",
}

# If the matched window belongs to one of these, Candata is running remotely
# and its controls are not visible to UI Automation on this machine.
REMOTE_CLIENT_EXES = {
    "mstsc.exe": "Remote Desktop",
    "msrdc.exe": "Remote Desktop (RemoteApp)",
    "wfica32.exe": "Citrix Workspace",
    "cdviewer.exe": "Citrix Workspace",
    "vmware-view.exe": "VMware Horizon",
}

UIA_INPUT_TYPES = {"Edit", "ComboBox", "Spinner"}
UIA_TOGGLE_TYPES = {"CheckBox", "RadioButton"}
UIA_GRID_TYPES = {"DataGrid", "Table"}
UIA_BUTTON_TYPES = {"Button", "SplitButton"}

# win32 class names vary by UI toolkit (WinForms, Delphi/VCL, DevExpress, ...),
# so inputs are also detected by substrings of the lowercased class name.
WIN32_INPUT_HINTS = ("edit", "combo", "textbox", "maskedbox", "datetimepick", "spin")
WIN32_GRID_HINTS = ("grid", "listview", "table")

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_ELEVATION_CLASS = 20


# --------------------------------------------------------------------------
# Data structures
# --------------------------------------------------------------------------


@dataclass
class Candidate:
    hwnd: int
    title: str
    pid: int
    exe: str
    minimized: bool

    @property
    def excluded(self) -> bool:
        return self.exe.lower() in EXCLUDED_EXES


@dataclass
class ControlRow:
    backend: str
    kind: str  # input, combo_part, toggle, grid, button, custom, other
    control_type: str  # uia control type, or win32 friendly class name
    class_name: str
    automation_id: str  # uia AutomationId; win32 control id (often unstable)
    name: str  # uia Name (usually the label); win32 window text
    parent_type: str
    value: str
    value_status: str  # ok, empty, unreadable, n/a
    enabled: str
    visible: str
    rect: str


@dataclass
class BackendMetrics:
    backend: str
    ok: bool = True
    error: str = ""
    total: int = 0
    inputs: int = 0
    toggles: int = 0
    grids: int = 0
    buttons: int = 0
    custom: int = 0
    addressable: int | None = None  # uia only
    name_is_value: int | None = None  # uia only: Name mirrors the value
    duplicate_ids: int | None = None  # uia only
    values_read: int = 0
    values_empty: int = 0
    values_unreadable: int = 0
    tree_seconds: float = 0.0
    tree_dumped: bool = False
    walk_seconds: float = 0.0
    stopped: str = ""  # why the walk ended early ("" = every control was read)
    top_classes: list[tuple[str, int]] = field(default_factory=list)


# --------------------------------------------------------------------------
# Win32 helpers (ctypes, loaded lazily so the module imports on any OS)
# --------------------------------------------------------------------------


@functools.lru_cache(maxsize=None)
def _api() -> SimpleNamespace:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL

    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL

    get_dpi = getattr(user32, "GetDpiForWindow", None)  # Windows 10 1607+
    if get_dpi is not None:
        get_dpi.argtypes = [wintypes.HWND]
        get_dpi.restype = wintypes.UINT

    return SimpleNamespace(
        ctypes=ctypes,
        wintypes=wintypes,
        user32=user32,
        kernel32=kernel32,
        advapi32=advapi32,
        get_dpi=get_dpi,
    )


def window_pid(hwnd: int) -> int:
    api = _api()
    pid = api.wintypes.DWORD()
    api.user32.GetWindowThreadProcessId(hwnd, api.ctypes.byref(pid))
    return pid.value


def is_minimized(hwnd: int) -> bool:
    return bool(_api().user32.IsIconic(hwnd))


def window_dpi(hwnd: int) -> int | None:
    get_dpi = _api().get_dpi
    if get_dpi is None:
        return None
    return get_dpi(hwnd) or None


def process_exe(pid: int) -> str:
    api = _api()
    handle = api.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return "?"
    try:
        buf = api.ctypes.create_unicode_buffer(1024)
        size = api.wintypes.DWORD(len(buf))
        if api.kernel32.QueryFullProcessImageNameW(handle, 0, buf, api.ctypes.byref(size)):
            return os.path.basename(buf.value)
        return "?"
    finally:
        api.kernel32.CloseHandle(handle)


def process_elevated(pid: int) -> bool | None:
    """True/False if the process token's elevation can be read, else None."""
    api = _api()
    handle = api.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        token = api.wintypes.HANDLE()
        if not api.advapi32.OpenProcessToken(handle, TOKEN_QUERY, api.ctypes.byref(token)):
            return None
        try:
            elevation = api.wintypes.DWORD()
            size = api.wintypes.DWORD()
            ok = api.advapi32.GetTokenInformation(
                token,
                TOKEN_ELEVATION_CLASS,
                api.ctypes.byref(elevation),
                api.ctypes.sizeof(elevation),
                api.ctypes.byref(size),
            )
            return bool(elevation.value) if ok else None
        finally:
            api.kernel32.CloseHandle(token)
    finally:
        api.kernel32.CloseHandle(handle)


# --------------------------------------------------------------------------
# Generic helpers
# --------------------------------------------------------------------------


def safe(fn, default=""):
    try:
        value = fn()
    except Exception:
        return default
    return default if value is None else value


def truncate(text: str, width: int) -> str:
    text = text.replace("\r", " ").replace("\n", " ")
    return text if len(text) <= width else text[: width - 1] + "~"


def fmt_bool(value) -> str:
    if value is None:
        return "unknown"
    return "yes" if value else "no"


# --------------------------------------------------------------------------
# Window selection
# --------------------------------------------------------------------------


def enumerate_windows() -> list[Candidate]:
    from pywinauto import Desktop

    result = []
    for w in Desktop(backend="win32").windows():
        hwnd = w.handle
        pid = window_pid(hwnd)
        result.append(
            Candidate(
                hwnd=hwnd,
                title=safe(w.window_text),
                pid=pid,
                exe=process_exe(pid),
                minimized=is_minimized(hwnd),
            )
        )
    return result


def print_candidates(cands: list[Candidate], header: str) -> None:
    print(header)
    for c in cands:
        if not c.title.strip():
            continue
        tags = []
        if c.minimized:
            tags.append("minimized")
        if c.excluded:
            tags.append("excluded")
        tag = f" [{', '.join(tags)}]" if tags else ""
        print(f"  hwnd={c.hwnd:#x}  pid={c.pid:<6}  {c.exe:<24} {truncate(c.title, 70)}{tag}")


def pick_target(args, cands: list[Candidate]) -> tuple[Candidate | None, list[Candidate]]:
    """Return (target, other matching candidates)."""
    if args.handle is not None:
        for c in cands:
            if c.hwnd == args.handle:
                return c, []
        pid = window_pid(args.handle)
        if not pid:
            return None, []
        return Candidate(args.handle, "", pid, process_exe(pid), is_minimized(args.handle)), []

    title_re = re.compile(args.title, re.IGNORECASE)
    proc_re = re.compile(args.process, re.IGNORECASE) if args.process else None

    matches = []
    for c in cands:
        if not title_re.search(c.title):
            continue
        if proc_re:
            if not proc_re.search(c.exe):
                continue
        elif c.excluded:
            continue
        matches.append(c)

    # Prefer restored windows, then processes whose exe name mentions candata.
    matches.sort(key=lambda c: (c.minimized, "candata" not in c.exe.lower()))
    if not matches:
        return None, []
    return matches[0], matches[1:]


# --------------------------------------------------------------------------
# Scanning
# --------------------------------------------------------------------------


def classify(backend: str, control_type: str, class_name: str) -> str:
    cls = class_name.lower()
    if backend == "uia":
        if control_type in UIA_INPUT_TYPES:
            return "input"
        if control_type in UIA_TOGGLE_TYPES:
            return "toggle"
        if control_type in UIA_GRID_TYPES:
            return "grid"
        if control_type in UIA_BUTTON_TYPES:
            return "button"
        if control_type == "Custom":
            return "custom"
        return "other"

    if control_type in ("CheckBox", "RadioButton"):
        return "toggle"
    if control_type in ("Edit", "ComboBox") or any(h in cls for h in WIN32_INPUT_HINTS):
        return "input"
    if control_type == "ListView" or any(h in cls for h in WIN32_GRID_HINTS):
        return "grid"
    if control_type == "Button":
        return "button"
    return "other"


def read_value_uia(ctrl, control_type: str) -> tuple[str, bool]:
    """Return (value, readable). Never falls back to Name, which is the label."""
    try:
        return str(ctrl.iface_value.CurrentValue or ""), True
    except Exception:
        pass
    if control_type == "ComboBox":
        try:
            return str(ctrl.selected_text() or ""), True
        except Exception:
            pass
    try:
        return str(ctrl.iface_text.DocumentRange.GetText(-1) or ""), True
    except Exception:
        pass
    return "", False


def read_value_win32(ctrl, control_type: str) -> tuple[str, bool]:
    if control_type == "ComboBox":
        try:
            return str(ctrl.selected_text() or ""), True
        except Exception:
            pass
    try:
        return str(ctrl.window_text() or ""), True
    except Exception:
        return "", False


def build_row(backend: str, ctrl, mask_values: bool) -> ControlRow:
    if backend == "uia":
        info = ctrl.element_info
        control_type = safe(lambda: info.control_type)
        class_name = safe(lambda: info.class_name)
        automation_id = str(safe(lambda: info.automation_id))
        name = safe(lambda: info.name)
        parent_type = safe(lambda: info.parent.control_type)
    else:
        control_type = safe(ctrl.friendly_class_name)
        class_name = safe(ctrl.class_name)
        automation_id = str(safe(ctrl.control_id))
        name = safe(ctrl.window_text)
        parent_type = safe(lambda: ctrl.parent().friendly_class_name())

    kind = classify(backend, control_type, class_name)
    # The edit box inside an editable combo box or spinner is part of that
    # control; count the outer control only.
    if kind == "input" and control_type == "Edit" and parent_type in ("ComboBox", "Spinner"):
        kind = "combo_part"

    value, status = "", "n/a"
    if kind == "input":
        reader = read_value_uia if backend == "uia" else read_value_win32
        value, readable = reader(ctrl, control_type)
        status = "unreadable" if not readable else ("ok" if value else "empty")

    if mask_values and value:
        # Keep a name that merely mirrors the value from leaking it either.
        if name == value:
            name = f"<{len(name)} chars, same as value>"
        value = f"<{len(value)} chars>"

    return ControlRow(
        backend=backend,
        kind=kind,
        control_type=str(control_type),
        class_name=str(class_name),
        automation_id=automation_id,
        name=str(name),
        parent_type=str(parent_type),
        value=value,
        value_status=status,
        enabled=fmt_bool(safe(ctrl.is_enabled, None)),
        visible=fmt_bool(safe(ctrl.is_visible, None)),
        rect=str(safe(ctrl.rectangle)),
    )


def compute_metrics(backend: str, rows: list[ControlRow]) -> BackendMetrics:
    m = BackendMetrics(backend=backend, total=len(rows))
    inputs = [r for r in rows if r.kind == "input"]
    m.inputs = len(inputs)
    m.toggles = sum(r.kind == "toggle" for r in rows)
    m.grids = sum(r.kind == "grid" for r in rows)
    m.buttons = sum(r.kind == "button" for r in rows)
    m.custom = sum(r.kind == "custom" for r in rows)
    m.values_read = sum(r.value_status == "ok" for r in inputs)
    m.values_empty = sum(r.value_status == "empty" for r in inputs)
    m.values_unreadable = sum(r.value_status == "unreadable" for r in inputs)
    m.top_classes = Counter(r.class_name for r in rows if r.class_name).most_common(15)

    if backend == "uia":
        # A Name that equals the current value changes as the user types, so it
        # is not a usable locator.
        def name_is_value(r: ControlRow) -> bool:
            return bool(r.name) and (r.name == r.value or r.name.endswith("same as value>"))

        m.name_is_value = sum(name_is_value(r) for r in inputs)
        m.addressable = sum(
            bool(r.automation_id) or (bool(r.name) and not name_is_value(r)) for r in inputs
        )
        ids = Counter(r.automation_id for r in inputs if r.automation_id)
        m.duplicate_ids = sum(n for n in ids.values() if n > 1)
    return m


def walk_controls(
    root,
    build,
    on_row,
    *,
    max_controls: int,
    max_depth: int,
    time_limit: float,
    descend_grids: bool = False,
    progress=None,
    clock=time.perf_counter,
) -> str:
    """Depth-first walk below `root`, reading each control with `build`.

    Unlike pywinauto's descendants(), which lists the whole tree before returning (minutes
    on a screen with a data grid), this reads one control at a time. It stops at
    `max_controls` controls or after `time_limit` seconds, and every row is passed to
    `on_row` as soon as it is read, so a stopped or interrupted scan keeps what it has.
    Returns "" when every control was read, otherwise why the walk stopped (including
    when a control's children could not be enumerated, since that subtree may hold
    unread controls).
    """
    deadline = clock() + time_limit
    stack = [(child, 1) for child in reversed(root.children())]
    count = 0
    unreadable_children = 0
    while stack:
        if count >= max_controls:
            return f"control limit {max_controls}"
        if clock() > deadline:
            return f"time limit {time_limit:g}s"
        ctrl, depth = stack.pop()
        row = build(ctrl)
        on_row(row)
        count += 1
        if progress and count % PROGRESS_EVERY == 0:
            progress(count)
        if count >= max_controls:
            # Stop now: do not spend time enumerating this control's children, and
            # do not claim completion, since whether it had more is now unknown.
            return f"control limit {max_controls}"
        if row.kind == "grid" and not descend_grids:
            continue  # a data grid can hold thousands of cells
        if depth < max_depth:
            try:
                children = ctrl.children()
            except Exception:
                children = []
                unreadable_children += 1
            stack.extend((c, depth + 1) for c in reversed(children))
    if unreadable_children:
        return f"{unreadable_children} control(s) had unreadable children"
    return ""


def scan_backend(
    backend: str, hwnd: int, out_dir: Path, mask_values: bool, limits: argparse.Namespace
) -> tuple[BackendMetrics, list[ControlRow]]:
    from pywinauto import Desktop

    spec = Desktop(backend=backend).window(handle=hwnd)
    wrapper = spec.wrapper_object()

    tree_seconds = 0.0
    if limits.tree:
        print(f"  {backend}: dumping the control tree (slow; skip it by leaving out --tree)...")
        tree_path = out_dir / f"{backend}_tree.txt"
        start = time.perf_counter()
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                spec.print_control_identifiers(depth=None)
            tree_text = buf.getvalue()
        except Exception as exc:
            tree_text = buf.getvalue() + f"\n\n[tree dump failed: {exc!r}]\n"
        tree_seconds = time.perf_counter() - start
        tree_path.write_text(tree_text, encoding="utf-8")

    rows: list[ControlRow] = []
    start = time.perf_counter()
    csv_path = out_dir / f"{backend}_controls.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=[f.name for f in fields(ControlRow)])
        writer.writeheader()

        def on_row(row: ControlRow) -> None:
            rows.append(row)
            writer.writerow(asdict(row))
            fh.flush()

        def progress(count: int) -> None:
            print(f"  {backend}: {count} controls, {time.perf_counter() - start:.0f}s", flush=True)

        try:
            stopped = walk_controls(
                wrapper,
                lambda c: build_row(backend, c, mask_values),
                on_row,
                max_controls=limits.max_controls,
                max_depth=limits.max_depth,
                time_limit=limits.time_limit,
                descend_grids=limits.grids,
                progress=progress,
            )
        except KeyboardInterrupt:
            stopped = "interrupted with Ctrl+C"
    walk_seconds = time.perf_counter() - start

    metrics = compute_metrics(backend, rows)
    metrics.tree_seconds = tree_seconds
    metrics.tree_dumped = limits.tree
    metrics.walk_seconds = walk_seconds
    metrics.stopped = stopped
    return metrics, rows


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def collect_environment(target: Candidate) -> tuple[dict[str, str], list[str]]:
    import pywinauto

    script_elev = process_elevated(os.getpid())
    target_elev = process_elevated(target.pid)
    dpi = window_dpi(target.hwnd)
    rect = "?"
    with contextlib.suppress(Exception):
        from pywinauto import Desktop

        rect = str(Desktop(backend="win32").window(handle=target.hwnd).wrapper_object().rectangle())

    env = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "python": f"{sys.version.split()[0]} ({'64' if sys.maxsize > 2**32 else '32'}-bit)",
        "pywinauto": getattr(pywinauto, "__version__", "?"),
        "window title": target.title,
        "window handle": f"{target.hwnd:#x}",
        "window rect": rect,
        "process": f"{target.exe} (pid {target.pid})",
        "display scale": f"{round(dpi / 96 * 100)}%" if dpi else "unknown",
        "script elevated": fmt_bool(script_elev),
        "target elevated": fmt_bool(target_elev),
    }

    warnings = []
    if target.exe.lower() in REMOTE_CLIENT_EXES:
        warnings.append(
            f"The window belongs to {REMOTE_CLIENT_EXES[target.exe.lower()]} ({target.exe}). "
            "Candata runs on a remote machine, so its controls are not visible here. "
            "Run this script on the machine where Candata actually runs."
        )
    if target_elev and not script_elev:
        warnings.append(
            "Candata runs elevated but this script does not. UI Automation will see "
            "little or nothing inside the window. Rerun from an administrator prompt."
        )
    elif target_elev is None and not script_elev:
        warnings.append(
            "Could not read Candata's privilege level. If it was started as "
            "administrator, rerun this script from an administrator prompt."
        )
    if dpi and dpi != 96:
        warnings.append(
            f"Display scale is {round(dpi / 96 * 100)}%, not 100%. The design requires "
            "100% for stable coordinates; locator-based results are still valid."
        )
    if sys.maxsize <= 2**32:
        warnings.append("Python is 32-bit. Use 64-bit Python as the design requires.")
    return env, warnings


def verdict(results: dict[str, BackendMetrics]) -> list[str]:
    ok = {b: m for b, m in results.items() if m.ok}
    if not ok:
        return ["UNKNOWN: every backend failed; see errors above."]

    best_inputs = max(m.inputs for m in ok.values())
    lines = []
    if best_inputs == 0:
        lines.append(
            "NONE: no input controls exposed. Likely causes, in order: wrong window or "
            "screen; privilege mismatch; remote session (RDP/Citrix); custom-drawn UI. "
            "Check the warnings, then the class names under 'top classes'."
        )
        return lines

    uia = ok.get("uia")
    if (
        uia
        and uia.inputs >= MIN_INPUTS
        and uia.addressable is not None
        and uia.addressable / uia.inputs >= GOOD_ADDRESSABLE_RATIO
    ):
        lines.append(
            f"GOOD: {uia.addressable}/{uia.inputs} UIA inputs have an AutomationId or a "
            "stable Name. Tier 1 (locate by control) is feasible for this screen."
        )
    elif best_inputs >= MIN_INPUTS:
        lines.append(
            "PARTIAL: inputs are exposed but many lack a stable AutomationId or Name. "
            "Plan for Tab navigation from known anchor controls (tier 2) for those "
            "fields; check the CSV for which ones."
        )
    else:
        lines.append(
            f"POOR: at most {best_inputs} input controls exposed. Expect Tab navigation "
            "or coordinates (tier 2/3), or confirm this is the right screen."
        )

    if uia and uia.duplicate_ids:
        lines.append(
            f"NOTE: {uia.duplicate_ids} UIA inputs share an AutomationId with another "
            "input; those need an extra criterion (parent, index) to locate."
        )
    if uia and uia.name_is_value:
        lines.append(
            f"NOTE: {uia.name_is_value} UIA inputs expose their value as Name; "
            "Name is not a usable locator for them."
        )

    # Use the same backend as the input table. Mixing max(read) and min(unreadable)
    # across backends can hide failures when another backend exposes no controls.
    best = max(ok.values(), key=lambda m: (m.inputs, m.backend == "uia"))
    if best.values_read == 0:
        lines.append(
            "READ-BACK: no non-empty values were read. Type test values into a few "
            "fields and rerun. If still empty, per-field read-back verification will "
            "need another method (e.g. select-all + copy)."
        )
    else:
        lines.append(
            f"READ-BACK ({best.backend}): {best.values_read} input values read back; "
            f"{best.values_unreadable} inputs unreadable. Compare the "
            "values in the CSV with what you typed."
        )
    return lines


def metrics_lines(m: BackendMetrics) -> list[str]:
    if not m.ok:
        return [f"[{m.backend}] FAILED: {m.error}"]
    lines = [
        f"[{m.backend}] controls={m.total}  inputs={m.inputs}  toggles={m.toggles}  "
        f"grids={m.grids}  buttons={m.buttons}  custom={m.custom}",
        f"[{m.backend}] values: read={m.values_read}  empty={m.values_empty}  "
        f"unreadable={m.values_unreadable}",
    ]
    if m.addressable is not None:
        lines.append(
            f"[{m.backend}] addressable inputs={m.addressable}/{m.inputs}  "
            f"name-is-value={m.name_is_value}  duplicate ids={m.duplicate_ids}"
        )
    tree = f"tree dump {m.tree_seconds:.1f}s" if m.tree_dumped else "tree dump skipped"
    lines.append(f"[{m.backend}] {tree}, walk {m.walk_seconds:.1f}s")
    if m.stopped:
        lines.append(
            f"[{m.backend}] PARTIAL: stopped early ({m.stopped}); only the controls above were read"
        )
    lines.append(
        f"[{m.backend}] top classes: " + ", ".join(f"{name} x{n}" for name, n in m.top_classes)
    )
    return lines


def input_table(rows: list[ControlRow], limit: int = 40) -> list[str]:
    inputs = [r for r in rows if r.kind == "input"]
    lines = [f"{'type':<10} {'automation_id':<28} {'name':<28} {'status':<10} value"]
    for r in inputs[:limit]:
        lines.append(
            f"{truncate(r.control_type, 10):<10} {truncate(r.automation_id, 28):<28} "
            f"{truncate(r.name, 28):<28} {r.value_status:<10} {truncate(r.value, 30)}"
        )
    if len(inputs) > limit:
        lines.append(f"... {len(inputs) - limit} more in the CSV")
    return lines


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Read-only scan of the Candata client's controls for RPA feasibility."
    )
    p.add_argument(
        "--screen",
        default="screen",
        help="label for this screen, used as the output folder name (e.g. header)",
    )
    p.add_argument("--title", default="candata", help="window title regex, case-insensitive")
    p.add_argument("--process", default=None, help="process exe name regex, e.g. 'candata'")
    p.add_argument(
        "--handle",
        default=None,
        type=lambda s: int(s, 0),
        help="exact window handle to scan (decimal or 0x hex)",
    )
    p.add_argument(
        "--backend",
        choices=("uia", "win32", "both"),
        default="uia",
        help="default uia. Use win32 or both if uia exposes few inputs (each backend adds a scan)",
    )
    p.add_argument(
        "--max-controls",
        type=int,
        default=1500,
        help="stop after this many controls per backend (default 1500)",
    )
    p.add_argument(
        "--time-limit",
        type=float,
        default=120,
        help="stop after this many seconds per backend (default 120)",
    )
    p.add_argument("--max-depth", type=int, default=25, help="deepest level to read (default 25)")
    p.add_argument(
        "--grids",
        action="store_true",
        help="also read inside data grids (can be thousands of cells; off by default)",
    )
    p.add_argument(
        "--tree",
        action="store_true",
        help="also write the pywinauto control tree dump (slow; the CSV is usually enough)",
    )
    p.add_argument(
        "--mask-values",
        action="store_true",
        help="do not write field values, only whether they were readable",
    )
    p.add_argument("--list", action="store_true", help="list top-level windows and exit")
    p.add_argument("--out", default=str(DEFAULT_OUT), help="output root directory")
    args = p.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", args.screen):
        p.error("--screen may contain only letters, digits, '_', '-' and '.'")
    if args.tree and args.mask_values:
        p.error("--tree dumps field values unmasked; it cannot be combined with --mask-values")
    return args


def main(argv=None) -> int:
    if sys.platform != "win32":
        print("This script must run on Windows, on the machine where Candata runs.")
        return 1
    args = parse_args(argv)
    try:
        import pywinauto  # noqa: F401
    except ImportError:
        print("pywinauto is not installed. Run: pip install pywinauto")
        return 1

    cands = enumerate_windows()
    if args.list:
        print_candidates(cands, "Top-level windows:")
        return 0

    target, others = pick_target(args, cands)
    if target is None:
        if args.handle is not None:
            print(f"No window with handle {args.handle:#x}.")
        else:
            print(
                f"No window matched title /{args.title}/"
                + (f" and process /{args.process}/" if args.process else "")
                + ". Browsers, terminals and editors are excluded unless --process is given."
            )
        print_candidates(cands, "\nTop-level windows:")
        print("\nRerun with --title, --process or --handle.")
        return 2

    print(f"Target: {target.exe} (pid {target.pid})  hwnd={target.hwnd:#x}  {target.title}")
    if others:
        print_candidates(others, "Other matching windows (use --handle to pick one):")

    if target.minimized:
        print("The window is minimized. Restore it and rerun; this script does not touch windows.")
        return 3

    env, warnings = collect_environment(target)
    out_dir = Path(args.out) / args.screen
    out_dir.mkdir(parents=True, exist_ok=True)

    backends = ("uia", "win32") if args.backend == "both" else (args.backend,)
    print(
        f"Reading at most {args.max_controls} controls or {args.time_limit:g}s per backend. "
        "Press Ctrl+C to stop early; what was read so far is kept."
    )
    results: dict[str, BackendMetrics] = {}
    rows_by_backend: dict[str, list[ControlRow]] = {}
    for backend in backends:
        print(f"Scanning with {backend} backend...")
        try:
            results[backend], rows_by_backend[backend] = scan_backend(
                backend, target.hwnd, out_dir, args.mask_values, args
            )
        except Exception as exc:
            results[backend] = BackendMetrics(backend=backend, ok=False, error=repr(exc))

    report = [f"Candata control scan: screen '{args.screen}'", ""]
    report += [f"{k:<16} {v}" for k, v in env.items()]
    if others:
        report += ["", "Other matching windows were found; verify the target is correct:"]
        report += [f"  hwnd={c.hwnd:#x} {c.exe} {c.title}" for c in others]
    report += ["", "Warnings:"] + ([f"  - {w}" for w in warnings] or ["  none"])
    report += ["", "Metrics:"]
    for m in results.values():
        report += ["  " + line for line in metrics_lines(m)]
    report += ["", "Verdict:"] + ["  " + line for line in verdict(results)]
    if any(m.stopped for m in results.values()):
        report += [
            "  CAUTION: the scan stopped early, so the counts above cover only the controls "
            "read. If inputs you expect are missing, rerun with a higher --max-controls or "
            "--time-limit, or add --grids."
        ]

    # Show inputs from the backend that exposed the most of them.
    ok = [m for m in results.values() if m.ok]
    if ok:
        best = max(ok, key=lambda m: (m.inputs, m.backend == "uia"))
        if best.inputs:
            report += ["", f"Inputs ({best.backend}):"]
            report += ["  " + line for line in input_table(rows_by_backend[best.backend])]

    report += ["", f"Files written to {out_dir}"]
    text = "\n".join(report)
    (out_dir / "summary.txt").write_text(text + "\n", encoding="utf-8")
    print()
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
