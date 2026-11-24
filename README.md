# cad-autofill

Fills Candata CAD entries from B/L, commercial invoice and EMF documents
received by email: Gmail intake, AI extraction, a generated invoice-lines
Excel import, and RPA that types the shipment header into the Candata Global
Windows client. A licensed broker reviews and submits every CAD; the program
always stops before submission.

Design: [docs/design.md](docs/design.md) ([中文](docs/design.zh.md))

## Accounts and API keys

### Local Gmail connection test

The interactive connection test is in `tests/test_gmail_connection.py`.
Run it from the project root:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-gmail-test.txt
.\.venv\Scripts\python.exe tests\test_gmail_connection.py --expected-email you@company.com --client-secrets secrets\gmail_client.json --token-file secrets\gmail_test_token.json
```

It requires a Desktop app OAuth client JSON at `secrets/gmail_client.json` (an API key
alone is insufficient). Before connecting, it requires explicit confirmation.
Replace `you@company.com` with the approved mailbox shown in the confirmation.
The returned account must match before the token is saved. Cached tokens must
already be bound to that mailbox and declare only `gmail.metadata`; older unbound
tokens, missing scopes, and broader scopes are rejected before reuse or refresh.
For a rejected cache, choose a new, separate test token path and authorize again;
do not manually relabel its mailbox or scopes. Keep this token separate from the
`gmail.readonly` token used for message intake.
It requests Gmail metadata permission and only calls `users.getProfile`; it
does not list messages, read bodies, download attachments, or send data to AI.
The local `secrets/gmail_test_token.json` token file is git-ignored. This is a manual live
connection test, not a mocked unit test, and is not run by GitHub Actions.

Run the separate offline safety regression tests (no Gmail access):

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_gmail_connection_safety.py -v
```

Step 0 below needs none of these. They are needed from M1 (extraction) on.

| Service | Needed for | Account | Credential | Where it goes |
| --- | --- | --- | --- | --- |
| OpenRouter | Extraction crew and Jev | OpenRouter account with credits | API key | `.env`: `OPENROUTER_API_KEY` |
| Gmail API | `main.py fetch` reads shipment emails | Google Cloud project, plus the mailbox that receives the documents | OAuth client (Desktop app) and a saved token | `secrets/gmail_client.json`, `secrets/gmail_token.json` |
| Candata Global | Header fill and invoice import | Existing broker login on the Candata machine | None. The RPA uses the logged-in client | None |
| GitHub | Code and issues | Access to the private `PCBZ/cad-autofill` repo | None | None |
| CrewAI | Orchestration | **No account.** Runs locally | None | Telemetry and tracing off (below) |
| Jev (TypeSafe AI) | Document type, HS check and unit mapping | **No account.** Called through OpenRouter | The OpenRouter key | None extra |

### OpenRouter

1. Sign up at [openrouter.ai](https://openrouter.ai) and buy credits. Each
   purchase carries a 5.5% fee (minimum $0.80); token prices have no markup.
2. Under Keys, create a key with a credit limit, and put it in `.env` as
   `OPENROUTER_API_KEY`.
3. In the account's privacy settings, allow only providers that do not train
   on prompts.

The same key is used for:

- the extraction crew through CrewAI and LiteLLM:
  `openrouter/anthropic/claude-haiku-5.5`;
- Jev through the TypeSafe SDK with base URL `https://openrouter.ai/api`,
  model `typesafe/jev-1.14`. No TypeSafe account or waitlist is needed.

Expected cost is about $0.011 per shipment (see `docs/design.md`, cost
section).

### Gmail API

First confirm which mailbox and label receive the shipment documents, and
whether the mailbox is on Google Workspace.

1. In the [Google Cloud console](https://console.cloud.google.com), create a
   project and enable the **Gmail API**.
2. Configure the OAuth consent screen:
   - Workspace mailbox: user type **Internal**. No Google verification is
     needed and the token does not expire weekly. The Workspace admin may
     need to allow the app under API controls.
   - Personal @gmail.com mailbox: user type **External**, left in Testing
     mode, with the mailbox added as a test user. The token then expires
     every 7 days and someone has to sign in again. If that is not
     acceptable, use IMAP with an app password instead (see the design doc).
3. Add the scope `https://www.googleapis.com/auth/gmail.readonly` only. The
   program never sends, deletes or relabels mail.
4. Create an OAuth client ID of type **Desktop app**, download the JSON and
   save it as `secrets/gmail_client.json`.
5. The first `python main.py fetch` opens a browser. The mailbox owner signs
   in and approves read-only access; the token is saved to
   `secrets/gmail_token.json`.

In Gmail, create a filter on the senders' addresses that applies the label
(for example `CAD/Incoming`). Set the same label and the allowed senders in
`config/settings.toml`.

### Where the credentials go

- `.env` and `secrets/` sit at the project root on the machine that runs
  `fetch` and `extract`. Both are ignored by git; never commit them or paste
  keys or tokens into issues.
- The Candata machine only runs `main.py fill`. It needs no OpenRouter key or
  Gmail credentials unless it also runs `fetch` and `extract`.
- Always add these lines to `.env` so CrewAI sends no telemetry and no traces
  (traces can contain shipment text):

  ```
  CREWAI_DISABLE_TELEMETRY=true
  OTEL_SDK_DISABLED=true
  CREWAI_TRACING_ENABLED=false
  ```

### Before using real shipments

Shipment emails are read from Gmail, and document text is sent to OpenRouter
and the model providers behind it, including TypeSafe for Jev. Before
processing real shipments, get approval under the company's and clients' data
handling policies. Also confirm with Candata that UI automation of the client
is allowed under the service agreement (issue in milestone M0).

## Setup

Python 3.11–3.13 (3.12 recommended). 3.11 is the minimum because config files
are TOML, read with the standard library's `tomllib`; CrewAI does not support
3.14 yet.

On the computer that runs `fetch` and `extract` (any OS), create a virtual
environment with an installed 3.11, 3.12 or 3.13 interpreter and activate it.

macOS / Linux (replace `python3.12` with `python3.11` or `python3.13` if that
is what you have):

```bash
python3.12 -m venv .venv
```

```bash
source .venv/bin/activate
```

Windows (replace `-3.12` with `-3.11` or `-3.13` if needed):

```powershell
py -3.12 -m venv .venv
.venv\Scripts\activate
```

Then install the packages:

```bash
python -m pip install -r requirements-extract.txt
```

The Candata computer also installs `requirements-fill.txt` (it needs only
that file if it runs `fill` alone).

Then:

1. Copy `.env.example` to `.env` and set `OPENROUTER_API_KEY`.
2. Put the Gmail OAuth client in `secrets/gmail_client.json` (see "Gmail API" above).
3. Set the Gmail label and `allowed_senders` in `config/settings.toml`.

Commands (with the virtual environment active, so `python` is the `.venv`
interpreter; without activating, use `.venv/bin/python` or
`.venv\Scripts\python.exe` instead):

| Command | What it does | Status |
| --- | --- | --- |
| `python main.py fetch` | Gmail label → `inbox/<shipment>/` | Not implemented yet (#33) |
| `python main.py extract inbox/<shipment>` | PDFs → JSON, validation report, import xlsx. After a validation failure, fill in `output/<shipment>/corrections.toml` and run it again to continue | Not implemented yet (#12, #36) |
| `python main.py fill inbox/<shipment>` | Fill Candata, import lines, stop before submit | Not implemented yet (#28) |

`main.py` forces CrewAI telemetry and tracing off even if `.env` says
otherwise.

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
and fill `config/header_fields.toml`.

`top classes` in the summary names the UI toolkit (for example
`WindowsForms10.*`, `Tcx*` for DevExpress VCL). It helps when the verdict is
not GOOD.
