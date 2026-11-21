# cad-autofill

Fills Candata CAD entries from B/L, commercial invoice and EMF documents:
AI extraction, a generated invoice-lines Excel import, and RPA that types the
shipment header into the Candata Global Windows client. A licensed broker
reviews and submits every CAD; the program always stops before submission.

## Step 0: check that Candata's controls can be automated

Before any RPA code is written, confirm that the Candata client exposes its
fields to UI Automation. `tools/scan_candata_controls.py` does this. It is
read-only: it never clicks, types, focuses or moves a window.

### Requirements

- The Windows 10/11 machine where Candata runs. Running it on a machine that
  only shows Candata through Remote Desktop or Citrix shows nothing useful.
- 64-bit Python 3.10+ and `pip install pywinauto`
- Candata logged in, its window open and **not minimized**
- Display scaling at 100% (the scan still works otherwise, but the RPA design
  assumes 100%)

### Run

1. Open the screen to check, for example the shipment header.
2. Type recognizable **test** values into a few fields (text boxes and
   drop-downs). The scan reads them back to confirm read-back works.
3. Open a terminal at the **same privilege level as Candata**. If Candata was
   started "as administrator", open the terminal as administrator too.
4. From the project root, run:

   ```
   python tools/scan_candata_controls.py --screen header
   ```

5. Repeat for every screen the automation touches:

   ```
   python tools/scan_candata_controls.py --screen invoice
   python tools/scan_candata_controls.py --screen import_dialog
   ```

   `import_dialog` means the file dialog that opens after clicking the
   invoice import button.

If the window is not found, list the top-level windows and pick Candata by
process name or handle:

```
python tools/scan_candata_controls.py --list
python tools/scan_candata_controls.py --screen header --process candata
python tools/scan_candata_controls.py --screen header --handle 0x1A2B3C
```

If real shipment data is on screen, add `--mask-values` so field values are
not written to disk.

### Output

Results go to `output/control_scans/<screen>/` (ignored by git):

| File | Contents |
| --- | --- |
| `summary.txt` | Environment, warnings, per-backend metrics, verdict, list of inputs |
| `uia_controls.csv` | Every control: type, AutomationId, Name, parent, read-back value |
| `win32_controls.csv` | The same, using the win32 backend |
| `uia_tree.txt`, `win32_tree.txt` | Full control trees from pywinauto |

### What to check

Open `summary.txt` first.

1. **Target window.** The `process` and `window title` lines must be the
   Candata client, not a browser tab or login window. If "Other matching
   windows" are listed, confirm the right one was scanned.
2. **Warnings.** Resolve any before reading the results:
   - *runs elevated but this script does not*: rerun as administrator.
   - *Remote Desktop / Citrix*: run the script on the remote machine.
   - *Display scale is not 100%*: fix before RPA runs. The scan result still holds.
3. **Verdict.**

   | Verdict | Meaning | RPA approach |
   | --- | --- | --- |
   | GOOD | 80%+ of inputs have an AutomationId or a stable Name | Locate by control (tier 1) |
   | PARTIAL | Inputs exposed, many without stable identifiers | Tab navigation from known controls for those fields (tier 2) |
   | POOR | Very few inputs exposed | Tab navigation or coordinates (tier 2/3), or wrong screen |
   | NONE | No inputs exposed | Check warnings and window first. If those are fine, the UI is likely custom-drawn |

4. **Header fields.** In `uia_controls.csv`, filter `kind` = `input`. Find
   each header field entered by hand today (B/L number, CCN, carrier code,
   port of entry, ETA, vendor, currency, ...). For each, note whether it has
   an AutomationId or a Name that is its label. A blank or repeated
   AutomationId and a Name equal to the field value are not usable locators.
5. **Read-back.** The `value` column should show the test values you typed.
   `unreadable` or `empty` on a filled field means the RPA cannot verify that
   field after typing it.
6. **Drop-downs.** Port, country and currency fields should appear as
   `ComboBox`. If they appear as `Edit` or `Custom`, note them: they need
   special handling.
7. **Import dialog.** The file name box and Open button should be found. This
   is normally a standard Windows dialog.

Send the `summary.txt` files (and the CSVs if the verdict is not GOOD) to the
developer. They decide how `rpa_filler` and `rpa_importer` locate each field
and fill `config/header_fields.yaml`.

`top classes` in the summary names the UI toolkit (for example
`WindowsForms10.*`, `Tcx*` for DevExpress VCL). It helps when the verdict is
not GOOD.
