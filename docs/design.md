# Candata CAD Autofill: System Design

English | [中文](design.zh.md)

Nov 21, 2026 · Wenshuang (Nov 22 update: the AI part is now orchestrated with CrewAI. Nov 23 update: OpenRouter for all models including Jev; Gmail intake; the investigation crew is replaced by human review of validation failures)

> This English version is the source of truth. When the design changes, update this file first, then bring the Chinese version in line.

## Background and goals

The system picks up shipment emails from Gmail and gives the three customs documents attached to each, the bill of lading (B/L), the commercial invoice (CI) and the EMF, to AI for extraction. A program then fills the Candata Global Windows client by simulating mouse and keyboard input, imports the invoice lines from an Excel file, and leaves the CAD for a broker to review and submit. The AI part is orchestrated with CrewAI: a Flow runs the fixed sequence of steps, and a Crew handles extraction. When validation fails, a person corrects the values and the run resumes (human in the loop).

Why this approach:

- Candata does not offer us an API. The only way to write data is through its user interface.
- Candata supports importing invoice lines from Excel/CSV, so product lines do not have to be typed one by one.
- Candata's built-in AI invoice recognition does not work well, and it covers only the commercial invoice, not the B/L or EMF.

Out of scope: the program does not submit CADs, does not correct HS codes, does not build a product library, and does not use Candata's Shipment Workbook Import.

## Overall flow

A shipment goes through six steps from receiving the email to just before submission. Only step 6 needs a person.

1. **Fetch from Gmail**: the program reads new emails under a set Gmail label from allowed senders, saves the PDF attachments and the email text into `inbox/<shipment>/`, and records which messages it has processed.
2. **AI extraction**: pymupdf reads the text layer of the three PDFs (scanned PDFs go to manual handling, or to an optional local OCR fallback). The extraction crew then takes over: B/L, CI and EMF specialists each read one document, and a reconciler merges their output into one structured JSON with a header part and an invoice-lines part. Code cross-checks the result. If the check fails, the run stops and a person corrects the values in a corrections file; the run then resumes from validation. After validation passes, Jev checks the document types, HS codes and units of measure.
3. **Generate the product Excel**: the lines part is written as an xlsx in Candata's invoice-lines import template. HS codes are copied exactly as they appear on the invoice.
4. **Fill the form**: the program opens the shipment screen in Candata and fills each header field with real mouse clicks and keystrokes.
5. **Import products**: the program clicks the import button, enters the path of the Excel file from step 3 in the file dialog, and completes the import.
6. **Human review**: the program stops before submission and saves screenshots. The broker checks the entry, edits fields such as HS codes, and submits it themselves.

If extraction fails validation, or any other step goes wrong, the program stops and reports the problem. It never carries on to the next step. A validation failure is resolved by a person (see "Human review of validation failures").

## Module design

The system has seven modules. The first four do not depend on Candata and can be developed and tested on any computer. The last three must run on the Windows computer where Candata is installed. gmail_reader is plain code that runs before the Flow and fills `inbox/`. The next three are chained by a CrewAI Flow (see "CrewAI orchestration" below). The last three are plain code and do not use CrewAI.

| Module | Input | Output | Key points |
| --- | --- | --- | --- |
| gmail_reader | Gmail label | `inbox/<shipment>/` with PDFs and `email.json` | Gmail API with the read-only scope; one email is one shipment; only allowed senders; processed message IDs kept locally (see "Gmail intake" below) |
| extraction crew | Three PDFs | Shipment JSON | pymupdf reads the PDF text layer; `find_tables()` keeps the rows and columns of invoice tables. B/L, CI and EMF specialist agents extract in parallel and a reconciler agent merges, by default on Claude Haiku 5.5 through OpenRouter. Every task constrains its output with `output_pydantic` and checks format with a `guardrail`. Every field records its source document |
| validator | Shipment JSON | Validation report; on failure also `corrections.toml` | Plain code. Sum of line amounts = invoice total; B/L number, packages, gross weight and consignee agree across the three documents. On failure the run stops for a person (see "Human review of validation failures") |
| excel_writer | Lines part | Import xlsx | Column order and formats follow the Candata template, configured in TOML |
| rpa_filler | Header part | Filled shipment screen | pywinauto locates controls; `click_input()` + `type_keys()` act like a real user; every field is read back after typing |
| rpa_importer | xlsx path | Imported product lines | Clicks the import button and enters the path in the file dialog; checks the row count after import |
| reviewer | All results | Screenshots + log | Stops before submission for the broker to confirm |

There are three ways to fill a field, from most to least reliable. The program uses the first one whenever it can:

1. Locate the control by name (pywinauto UIA or win32 backend), then use real mouse clicks and keystrokes.
2. If the control cannot be found, navigate by Tab order from a known field.
3. Only as a last resort, use screen coordinates or image matching (pyautogui). This breaks when the resolution changes or the window moves.

Drop-downs and autocomplete fields (country codes, port codes, currency) are handled separately: type the value, wait for the candidate list to appear, and select the exact match instead of just pressing Enter.

### Gmail intake

`python main.py fetch` reads shipment emails and turns each into a folder in `inbox/` for `extract`. It is plain code, not an agent tool: reading the mailbox needs no model judgment.

- **Access**: Gmail API with OAuth and the `gmail.readonly` scope. The program cannot send, delete, or relabel mail. The OAuth client file and the saved token are kept in `secrets/`, which is not committed.
- **What it reads**: messages under the label set in `settings.toml` (for example `CAD/Incoming`, applied by a Gmail filter on the sender addresses), received after the last run, from senders on the allowlist in `settings.toml`. Other messages are skipped and listed in the fetch log.
- **What it writes**: `inbox/<YYYYMMDD>-<message id>/` with every PDF attachment and an `email.json` holding the message ID, thread ID, sender, subject, received time and plain-text body.
- **Processed messages**: with a read-only scope the program cannot mark mail as done, so processed message IDs are kept in `output/gmail_state.json`. Running `fetch` again never creates a second folder for the same message.
- **One email, one shipment**: this is the default. Emails with no PDF, or with more or fewer than three documents, are still saved but flagged; Jev's document type check then reports which document is missing or extra. Documents split across several emails in one thread are merged by thread ID when `settings.toml` turns that on.
- **Other attachments**: only PDFs are read. Zip files, images, and links to shared drives are not opened; the email is flagged for a person.
- **Email body**: passed to the reconciler as extra context, mainly for client references and instructions. A header value taken from the body has `source: "email"` and is highlighted at review. Body text, like PDF text, is treated as data: agents have no tools, so text that tries to instruct the model cannot trigger any action.
- **Account type**: if the mailbox is on Google Workspace, set the OAuth consent screen to Internal: no Google verification is needed and the token does not expire weekly. A personal @gmail.com account can only use an External app in Testing mode, where the token expires every 7 days and someone has to sign in again; for that case, IMAP with an app password is the fallback.

### CrewAI orchestration

The order of steps is fixed, so a Flow chains them. Only extraction needs model judgment, so only extraction uses a Crew. Form filling and import (RPA) are not part of the Flow and are not exposed as a tool to any agent: a model must never decide whether or how many times to click.

```
extract:           load → extract (extraction crew) → validate ─ passed ─→ classify (Jev) → write_excel → end
                                                               └ failed → write_review → stop; a person fills corrections.toml
extract --resume:  load → apply_corrections ────────→ validate → …
```

| Flow step | Implemented as | Notes |
| --- | --- | --- |
| load | `@start()` + `@router`, code | Fresh run: pymupdf reads the three PDFs (too little text means a scanned PDF: stop and hand to a person), then `fresh`. With `--resume`: loads the saved shipment JSON, then `resume` |
| extract | `@listen("fresh")`, extraction crew | Outputs the shipment JSON |
| apply_corrections | `@listen("resume")`, code | Applies the values from `corrections.toml`, marked `source: "human"` |
| validate | `@router` on `or_(extract, apply_corrections)`, code | Returns `passed` or `failed` |
| write_review | `@listen("failed")`, code | Writes the validation report and `corrections.toml`, then stops |
| classify | `@listen("passed")`, code | Calls Jev directly, not through an agent |
| write_excel | `@listen`, code | Writes the xlsx and the final validation report |

- The Flow state is a pydantic model (`ShipmentFlow(Flow[ShipmentState])`) holding the shipment JSON, the validation report and the corrections. When a shipment finishes, it is written to `output/<shipment>/`.
- No `@persist`: the two computers hand off through JSON files, and a stopped run resumes from the shipment JSON saved in `output/<shipment>/`, so the extraction crew is not called again.

**Extraction crew**: roles are split by document, matching how a brokerage actually divides the work.

| Agent | Input | Output |
| --- | --- | --- |
| B/L specialist | B/L text | B/L number, shipper and consignee, ports of loading and discharge, packages and package type, gross weight, vessel and voyage, container numbers |
| CI specialist | CI text and tables | Invoice header (invoice number, date, vendor and purchaser, currency, incoterms, total) and invoice lines |
| EMF specialist | EMF text | CCN, carrier code, port of entry, ETA, plus B/L number, packages and gross weight for cross-checking |
| Reconciler | Output of the three specialists | Merges into one shipment JSON; normalizes different spellings of the same party (for example a consignee's full and short names); lists conflicting fields |

- The three specialist tasks use `async_execution=True` to run in parallel, and the reconciler task receives their output through `context`. The process is sequential; no manager agent (hierarchical process) is needed.
- Every task uses `output_pydantic` with a model from `schema.py`. A `guardrail` function checks required fields and formats (dates, numbers, country codes) and sends failures back for a retry, up to `guardrail_max_retries` (default 3).
- The reconciler only merges and normalizes. Hard rules such as the totals check and three-document agreement are checked by the validator in code, not by a model.

**Human review of validation failures** (human in the loop): when validation fails, the Flow stops instead of asking a model to fix the data. A person (the broker or an operations colleague) resolves each issue.

1. `write_review` writes to `output/<shipment>/`:
   - `validation_report.md`: each issue, the value found on each document, and the document and page it came from;
   - `corrections.toml`: one entry per issue, prefilled with the field and the values found, and an empty `value`.
2. The person checks the PDFs. For each entry they either fill `value` with the correct value, or set `accept: true` with a `note` when the documents really differ and the extracted value should stand.
3. `python main.py extract inbox/<shipment> --resume` applies the corrections and validates again without calling the extraction crew. If it passes, the Flow continues to classify and write_excel. If issues remain, a new report and corrections file are written.

```toml
[[correction]]
field = "header.gross_weight"
issue = "B/L and EMF disagree"
found = { bl = 12400, emf = 1240 }
value = 1240
note = "B/L says 1,240.00 KGS; extraction misread it"

[[correction]]
field = "invoice_total"
issue = "sum of lines 2,480.00 does not equal invoice total 2,500.00"
found = { lines_sum = 2480.0, ci = 2500.0 }
accept = true
note = "freight 20.00 is on the invoice total but not a line"
```

- Corrected values have `source: "human"` and are listed under `review` in the shipment JSON with the old value, new value and note. The final review screenshots highlight them.
- An accepted issue is not a pass: it stays in the report so the broker sees it again before submission.
- `fetch` and `extract` print the shipments waiting for review, so a stopped shipment is not forgotten.
- An AI investigation step was considered and left out: it is the only place a model would change data, it is hard to test, and the broker reviews every CAD anyway. If the failure rate measured on samples (#13) turns out high, a step on a low-cost model can be added before `write_review`.

**Configuration**

- Each crew keeps its agents' role, goal and backstory and its task descriptions in its own `config/agents.toml` and `config/tasks.toml`; code reads them with `tomllib` and passes the text to CrewAI's `Agent` and `Task`, so CrewAI's YAML-based `@CrewBase` loader is not used. The model for each agent is set in `settings.toml`.
- All models go through OpenRouter with one `OPENROUTER_API_KEY`. CrewAI's `LLM` class reaches OpenRouter through LiteLLM (`crewai[litellm]`). Model names have the form `openrouter/<vendor>/<model>`: `openrouter/anthropic/claude-haiku-5.5` for the extraction crew. Switching to another vendor's model only changes `settings.toml`.
- In the OpenRouter account's privacy settings, allow only providers that do not train on prompts, and set a credit limit on the key.
- Telemetry and tracing are turned off in `.env`: `CREWAI_DISABLE_TELEMETRY=true`, `OTEL_SDK_DISABLED=true`, `CREWAI_TRACING_ENABLED=false`. Traces can contain prompts and document content and must not be uploaded.

Sources: [CrewAI Flows](https://docs.crewai.com/en/concepts/flows), [Tasks](https://docs.crewai.com/en/concepts/tasks), [LLMs](https://docs.crewai.com/en/concepts/llms), [Telemetry](https://docs.crewai.com/en/telemetry), [Tracing](https://docs.crewai.com/en/observability/tracing), [Installation](https://docs.crewai.com/en/installation)

### Jev classification

After pymupdf extracts the text and the extraction crew turns it into JSON, classification is handed to TypeSafe AI's Jev. Jev takes text (and, from version 1.14, images) and returns structured answers with calibrated confidence to questions declared in advance, which suits decisions made in code. It runs in the Flow's classify step, after validation passes and before the Excel file is generated, and is called directly from code, not through an agent.

| Task | Jev question | Input | How the result is used |
| --- | --- | --- | --- |
| Document type | Choice: B/L, CI, EMF, other | First-page text of each PDF | Sorts documents automatically; warns when one is missing or misplaced |
| HS code check | Choice: match, mismatch, cannot tell | Goods description + HS code on the CI and its tariff description | Mismatched lines are marked red in the validation report for a person to handle |
| HS code suggestion | Layered Choice: chapter → heading → subheading → 10-digit tariff item | Goods description + candidate list for each layer | Written to the report as a suggestion column; the import still uses the CI value, and a person decides whether to adopt it |
| Unit mapping | Choice | Unit on the CI + Candata unit code table | Written into the import Excel |

- A Choice allows at most 255 options, so HS suggestions must be chosen layer by layer. The candidates for each layer come from the Canadian Customs Tariff and need to be prepared as local data files in `config/tariff/`.
- Every answer has a confidence. Below the threshold, the field is left blank and marked red. The threshold is set in `settings.toml` and tuned on sample shipments.
- **Access through OpenRouter**: Jev is on OpenRouter with no waitlist and no TypeSafe account, using the same `OPENROUTER_API_KEY` and billed to the same account. The price is the same as from TypeSafe: $0.042 per million input tokens, output free. `classifier/jev.py` uses the TypeSafe Python SDK with its base URL set to `https://openrouter.ai/api` (OpenRouter's System One API). Jev is not a chat model and does not work through the chat completions endpoint, so it is not called through CrewAI.
- **Pin the model version**: use `typesafe/jev-1.14`, not the `~typesafe/jev-latest` alias. Probabilities change between versions, so a new version means tuning the thresholds again on the samples.
- **Keep requests small**: requests over about 32,000 bytes of text run in TypeSafe's long-context mode, which is slower and less accurate. Send one invoice line per HS request and one layer per HS suggestion step.
- **Fallback**: OpenRouter serves other decision models on the same Decisions API (for example `openai/gpt-6-luna-decisions`, `cloudflare/clef`). The classifier stays behind a common interface, so one of these can replace Jev by changing the model name and retuning the thresholds.
- **Jev Router is something else**: `typesafe/jev-router` is a different product. It uses Jev to pick a chat model for each request and is not used here.

Sources: [Jev on OpenRouter](https://openrouter.ai/docs/guides/community/jev), [Jev 1.14 model page](https://openrouter.ai/typesafe/jev-1.14), [TypeSafe SDK on OpenRouter](https://openrouter.ai/docs/guides/community/typesafe-sdk), [TrueFoundry on Jev](https://www.truefoundry.com/de/blog/typesafe-ai-jev)

## Data structure

All modules share one shipment JSON, which is also the core of the Flow state. `header` feeds the form filler and `lines` feeds the Excel writer. `email` identifies the Gmail message the shipment came from. Every field has a `source` naming the document it came from (`bl`, `ci`, `emf`, `email` for the email body, or `human` for a value corrected at review), and `review` records what a person corrected or accepted after a validation failure. The field list is a first draft, to be finalized against the Candata screens and import template.

| Field | Source | Used for |
| --- | --- | --- |
| bl_number | B/L (cross-checked with EMF and CI) | Header |
| cargo_control_number (CCN) | EMF | Header |
| carrier_code | EMF | Header |
| port_of_entry | EMF | Header |
| eta | EMF | Header |
| shipper / consignee | B/L (checked against CI) | Header |
| port_of_loading / discharge | B/L | Header |
| packages / package_type | B/L (checked against EMF) | Header |
| gross_weight | B/L (checked against EMF) | Header |
| vessel_voyage / container | B/L | Header |
| invoice_number / date | CI | Header |
| vendor / purchaser | CI | Header |
| currency / incoterms | CI | Header |
| invoice_total | CI | Validation |
| description | CI | Lines |
| hs_code | CI (as printed) | Lines |
| country_of_origin | CI | Lines |
| quantity / unit | CI | Lines |
| unit_price / amount | CI | Lines |

Example (some fields omitted):

```json
{
  "email": {"message_id": "18c2f0a9d3e4b5c6", "thread_id": "18c2f0a9d3e4b5c6",
            "from": "docs@client.example", "subject": "", "received": "2026-11-23T14:05:00Z"},
  "header": {
    "bl_number": {"value": "MAEU123456789", "source": "bl"},
    "cargo_control_number": {"value": "", "source": "emf"},
    "currency": {"value": "USD", "source": "ci"}
  },
  "lines": [
    {"line_no": 1, "description": "", "hs_code": "", "country_of_origin": "CN",
     "quantity": 100, "unit": "PCS", "unit_price": 2.5, "amount": 250.0}
  ],
  "validation": {"passed": true, "issues": []},
  "review": [
    {"field": "header.gross_weight", "action": "corrected", "old": 12400, "new": 1240,
     "by": "human", "note": "B/L says 1,240.00 KGS; extraction misread it"}
  ]
}
```

## Dependencies

The runtime is Windows 10/11 with 64-bit Python 3.11–3.13 (3.12 recommended; 3.11 is the first with `tomllib` in the standard library, and CrewAI currently requires <3.14), plus an OpenRouter API key and a Gmail OAuth client. The versions below are minimums; pin exact versions once integration testing passes.

### Python packages

| Package | Minimum version | Purpose | Module |
| --- | --- | --- | --- |
| pymupdf | 1.24 | Read the PDF text layer, extract tables with `find_tables()`, detect scanned PDFs | text |
| google-api-python-client | 2.100 | Gmail API: list messages, download attachments | gmail_reader |
| google-auth-oauthlib | 1.2 | Gmail OAuth sign-in and token refresh | gmail_reader |
| crewai | 1.15 | Flow orchestration, extraction crew, LLM calls through OpenRouter (`crewai[litellm]`) | flow, crews |
| typesafe-sdk | 0.7.5 | Call Jev through OpenRouter's System One API | classifier |
| pydantic | 2.5 | Define and validate the shipment JSON, Flow state and task outputs | schema, flow, validator |
| openpyxl | 3.1 | Generate the import xlsx | excel_writer |
| pywinauto | 0.6.8 | Find Candata controls, simulate mouse and keyboard | rpa_filler, rpa_importer |
| pyautogui | 0.9.54 | Coordinate-click fallback, screenshots | rpa_filler, reviewer |
| Pillow | 10.0 | Save and process screenshots | reviewer |
| keyboard | 0.13.5 | Global emergency stop hotkey (Esc) | rpa_filler |
| tomli-w | 1.0 | Write `corrections.toml` (reading TOML uses the standard library's `tomllib`) | review |
| python-dotenv | 1.0 | Read credentials and CrewAI environment variables from `.env` | flow, classifier |

crewai is installed with the `litellm` extra, since all models go through OpenRouter. The model for each agent is set in `settings.toml`, so switching models only changes config. The classifier still defines a common interface so another decision model on OpenRouter can replace Jev if needed. When a scanned PDF has no readable text, the default is to hand it to a person. If scanned PDFs turn out to be common, add the free local PaddleOCR (`paddleocr` + `paddlepaddle`) as a fallback.

Dependencies are split into two files: extraction can run on any computer, and a Candata computer that only fills forms does not need CrewAI.

`requirements-extract.txt` (`main.py fetch` and `main.py extract`):

```
# Gmail intake
google-api-python-client>=2.100
google-auth-oauthlib>=1.2

# Text extraction
pymupdf>=1.24
# paddleocr + paddlepaddle   # local OCR fallback for scanned PDFs

# Orchestration and extraction: all models through OpenRouter; pin after integration
crewai[litellm]>=1.15        # Claude Haiku 5.5 via OpenRouter

# Classification
typesafe-sdk>=0.7.5          # Jev via OpenRouter

pydantic>=2.5
openpyxl>=3.1
tomli-w>=1.0                 # write corrections.toml (reading TOML uses stdlib tomllib)
python-dotenv>=1.0
```

`requirements-fill.txt` (`main.py fill`, Candata computer):

```
pydantic>=2.5
pywinauto>=0.6.8
pyautogui>=0.9.54
Pillow>=10.0
keyboard>=0.13.5
```

Install (install both when both stages run on the same computer):

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-extract.txt
.venv\Scripts\python.exe -m pip install -r requirements-fill.txt
```

### System and tools

- **Candata Global Windows client**: logged in. The script must run at the same privilege level as the client. If the client was started as administrator, the script must run as administrator too, or pywinauto cannot read its controls.
- **Display settings**: scaling fixed at 100%, Candata window maximized, no screen lock or remote disconnect during a run.
- **Inspect.exe** (part of the Windows SDK) or Accessibility Insights: for viewing control names and hierarchy during development.
- **Microsoft Excel**: optional, only for viewing the generated xlsx by hand.

### External services

- **OpenRouter**: the only model provider. One `OPENROUTER_API_KEY` covers the extraction crew (Claude Haiku 5.5) and Jev. Token prices have no markup; credit purchases carry a 5.5% fee (minimum $0.80 per purchase).
- **Gmail API**: a Google Cloud project with the Gmail API enabled and an OAuth client of type Desktop app. The first `fetch` opens a browser for the mailbox owner to approve read-only access. No API key.
- **CrewAI**: an open-source library that runs locally; no CrewAI account needed. Telemetry and tracing are turned off as described in "CrewAI orchestration".
- **Jev (TypeSafe AI)**: called through OpenRouter, so no TypeSafe account or key is needed.
- Text extraction runs locally and does not depend on an external OCR service.
- These services bill by usage, and model names are kept in config so they are easy to change. Document text is sent to external services, so confirm this meets the company's and clients' data handling requirements before use.

Sign-up steps and `.env` variable names are in the README, section "Accounts and API keys".

## Project structure and configuration

Field mappings, UI locators and Excel column order all live in TOML files under `config/`. When the Candata screens or template change, only config changes, not code. Agent and task prompts live in each crew's own `config/`.

```
cad-autofill/
├── config/
│   ├── settings.toml        # model per agent, Jev version, guardrail retries, confidence thresholds,
│   │                        # Gmail label and sender allowlist, folders, timeouts
│   ├── header_fields.toml   # JSON field → Candata control locator → format rules
│   ├── excel_template.toml  # column order and formats of the Candata invoice import template
│   └── tariff/              # layered Canadian Customs Tariff candidates, Candata unit code table
├── src/
│   ├── gmail_reader.py      # fetch: Gmail label → inbox/<shipment>/ (PDFs + email.json)
│   ├── text/
│   │   ├── pdf_text.py      # pymupdf text layer and table extraction, scanned-PDF detection
│   │   └── paddle.py        # local OCR fallback
│   ├── flow.py              # ShipmentFlow: load → extract → validate → classify → write_excel
│   ├── crews/
│   │   └── extraction/
│   │       ├── config/agents.toml   # B/L, CI and EMF specialists and reconciler
│   │       ├── config/tasks.toml
│   │       ├── guardrails.py        # required-field and format checks
│   │       └── crew.py
│   ├── review.py            # validation report, corrections.toml, applying corrections on --resume
│   ├── classifier/
│   │   ├── base.py          # classifier interface: document type, HS check and suggestion, unit mapping
│   │   └── jev.py
│   ├── schema.py            # pydantic models: shipment, Flow state, corrections and review records
│   ├── validator.py
│   ├── excel_writer.py
│   ├── rpa_filler.py
│   ├── rpa_importer.py
│   └── reviewer.py
├── tools/
│   └── scan_candata_controls.py
├── inbox/                   # one subfolder per shipment: PDFs + email.json; not committed
├── output/                  # extracted text, JSON, xlsx, validation reports, screenshots, logs,
│                            # gmail_state.json; not committed
├── secrets/                 # gmail_client.json (OAuth client), gmail_token.json; not committed
├── main.py
├── requirements-extract.txt
├── requirements-fill.txt
└── .env                     # OPENROUTER_API_KEY, CREWAI_DISABLE_TELEMETRY, OTEL_SDK_DISABLED,
                             # CREWAI_TRACING_ENABLED; not committed
```

`header_fields.toml` example:

```toml
[bl_number]
locator = { auto_id = "txtBillOfLading" }  # to be confirmed by the control scan
fallback = { tab_from = "txtShipmentNo", tabs = 3 }
type = "text"

[port_of_entry]
locator = { title = "Port", control_type = "ComboBox" }
type = "combobox"  # type, wait for candidates, pick the exact match
format = "{value:0>4}"
```

There are three commands. `python main.py fetch` pulls new shipment emails from Gmail into `inbox/`. `python main.py extract inbox/<shipment>` runs ShipmentFlow: it only extracts and generates the Excel file, and can run on any computer. After a validation failure, `python main.py extract inbox/<shipment> --resume` continues from the person's corrections. `python main.py fill inbox/<shipment>` runs on the Candata computer: it fills the form and imports the lines, reading the JSON and xlsx produced by `extract`, and does not use CrewAI.

## Risks and notes

The biggest risk is that Candata's controls cannot be detected. That decides how reliable the RPA can be, so it should be checked first.

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Controls not detectable (custom-drawn UI) | Only Tab navigation or coordinates remain, which makes wrong entries easy | Run `scan_candata_controls.py` first; read back every field after typing |
| Candata upgrade changes the UI | Locators break | Keep all locators in config; check key controls exist at startup |
| AI extraction errors | Wrong header or line data | Cross-validation; mark low-confidence fields red; submit only after human review |
| Multi-agent output is unstable and uses more tokens | Repeated runs on the same shipment may differ; cost is higher than a single call | `output_pydantic` + guardrails constrain format; hard rules checked in code; measure tokens and pass rate on samples before choosing models |
| Many shipments fail validation | People spend time filling corrections files | Measure the failure rate on samples (#13); improve prompts and guardrails for the fields that fail most; add a low-cost AI step only if still needed |
| A person enters a wrong correction | Wrong value in the CAD | Corrections are validated again in code; corrected and accepted fields are highlighted at final review |
| CrewAI changes quickly | Upgrades change the API | Pin the version; run regression on samples before upgrading |
| CrewAI telemetry or tracing uploads data | Document content leaves the company | Turn off telemetry and tracing with environment variables; confirm nothing is reported externally before go-live |
| HS codes on the CI are the exporting country's 6/8-digit codes | Import may fail, or not match Canada's 10-digit tariff items | Import as printed and correct by hand; leave blank if the import rejects them |
| Someone moves the mouse or types during a run | Data entered in the wrong place | Esc emergency stop; warning dialog before a run; abort on focus loss |
| Popups or slow loading | Script hangs or acts too early | Wait explicitly for controls at every step, no fixed sleeps; screenshot and stop on unknown popups |
| Client documents sent to external APIs | Data compliance | Confirm the company's and clients' data handling policies before go-live; in OpenRouter, allow only providers that do not train on prompts |
| Text in an email or PDF tries to instruct the model (prompt injection) | Wrong values extracted | Agents have no tools and no RPA; only allowed senders are fetched; values from the email body are highlighted; hard rules checked in code; broker reviews everything |
| Documents for one shipment spread over several emails, or one email covers several shipments | Missing or mixed documents in a folder | Jev document type check flags missing or extra documents; optional merge by thread ID; otherwise regroup by hand |
| Gmail token expires or access is revoked | `fetch` fails | Workspace mailbox with an Internal OAuth app; `fetch` reports the sign-in error clearly; personal Gmail falls back to IMAP with an app password |
| Automation may be restricted by the software license | Contract risk | Ask Candata whether the service agreement allows it |

pymupdf only reads the text layer of digital PDFs. Scanned or image-only PDFs have no readable text; when the program detects too little text, it hands the shipment to a person. Before go-live, measure the share of scanned PDFs, and enable the PaddleOCR fallback if it is high.

A licensed broker is responsible for every CAD. The program always stops before submission, and only a person submits.

## Cost estimate

With pymupdf reading the text layer and CrewAI running multi-agent extraction, API cost is about $0.011 per shipment, or about $11 a month at 1,000 shipments. That is about one ninth of the earlier Azure OCR + Gemini design (about $0.10 per shipment). A single-call extraction would cost about $0.006 per shipment; the difference is the cost of multiple agents.

| Item | Price (per million tokens, input / output) | Usage per shipment (assumed) | Cost per shipment (USD) |
| --- | --- | --- | --- |
| Text extraction: pymupdf | Free, runs locally | 5 pages | 0 |
| Extraction crew: Claude Haiku 5.5 | $0.10 / $0.50 | 4 calls (3 specialists + reconciler), 15,000–20,000 input and 8,000–10,000 output tokens | about 0.006–0.007 |
| Classification: Jev | Input $0.042 | About 100,000 tokens (20 lines, including layered HS suggestions) | about 0.004 |
| **Total** | | | **about 0.011** |

- Assumptions: 5 pages per shipment (B/L 1–2 pages, CI 1–3 pages, EMF 1 page), about 1,500 tokens of text per page. Haiku 5.5's tokenizer produces about 30% more tokens for the same text; this is included in the upper end of the range. Extraction uses more tokens than a single call because every agent carries CrewAI's role prompt, and the reconciler reads the three specialists' output again.
- Haiku 5.5's low price applies only to prompts under 100,000 tokens. Every prompt here is 10,000–20,000 tokens, well below that limit.
- Validation failures add no API cost: `--resume` does not call the extraction crew again. Guardrail retries are the main variable and are not included above. Revise after measuring tokens and validation pass rate on sample shipments.
- Scanned PDFs handed to a person cost nothing in API fees; the PaddleOCR fallback is also free and uses only local CPU.
- All prices above are OpenRouter's, which match the vendors' own. OpenRouter adds a 5.5% fee on each credit purchase, so the total is about $0.011 per shipment.
- The Gmail API is free at this volume.
- Not included: development time, the Windows computer running Candata, the Candata license. CrewAI is an open-source library and free. Jev is new, so its pricing may change.

Sources: [Claude pricing](https://platform.claude.com/docs/en/about-claude/pricing), [Haiku 5.5 tiered pricing](https://mixed-news.com/en/?p=35958), [Haiku 5.5 tokenizer token changes](https://mixed-news.com/en/?p=35955), [OpenRouter models API](https://openrouter.ai/api/v1/models), [Jev 1.14 on OpenRouter](https://openrouter.ai/typesafe/jev-1.14)

## Open items and next steps

Before writing the form-filling and Excel modules, the following must be in hand.

- [ ] Run `scan_candata_controls.py` on the Candata shipment header screen to confirm the controls can be detected
- [x] Get the column headings and format requirements of Candata's invoice-lines import template (help center article: Shipment Invoice details loading via Excel/CSV spreadsheets)
- [x] List the fields copied by hand from the B/L and EMF into Candata, and their names on screen
- [x] Confirm the exact EMF format (a PDF exported from ACI eManifest, or something else)
- [x] Confirm whether Candata checks the number of HS code digits on import
- [x] Prepare 3–5 redacted sample shipments to test extraction accuracy
- [x] Choose the LLM provider (OpenRouter, Nov 23), prepare credentials and complete data compliance approval
- [ ] Create the OpenRouter key with a credit limit and set the privacy settings
- [ ] Confirm which mailbox and label receive shipment documents, whether it is on Google Workspace, and which senders to allow
- [ ] Create the Google Cloud project, enable the Gmail API and create the OAuth client
- [ ] Run the extraction crew end to end on 1–2 sample shipments and measure token use and validation pass rate
- [ ] Pin the CrewAI version and confirm telemetry and tracing are off

Development order:

1. Flow skeleton + extraction crew + validator + excel_writer: run samples from extraction to xlsx and verify by importing by hand.
2. gmail_reader: fetch real shipment emails into `inbox/` and run `extract` on them.
3. Human review loop: validation report, `corrections.toml` and `--resume`, tested on samples with real conflicts or deliberately corrupted values.
4. rpa_importer: automate the Excel import. This step has few UI actions, so it is a good first test of RPA feasibility.
5. rpa_filler: add header fields step by step, text boxes first, then drop-downs and autocomplete.
6. reviewer + end-to-end wiring, then a small pilot batch.
