# Candata CAD Autofill: System Design

English | [中文](design.zh.md)

Nov 21, 2026 · Wenshuang (Nov 22 update: the AI part is now orchestrated with CrewAI. Nov 23 update: OpenRouter for all models including Jev; Gmail intake)

> This English version is the source of truth. When the design changes, update this file first, then bring the Chinese version in line.

## Background and goals

The system picks up shipment emails from Gmail and gives the three customs documents attached to each, the bill of lading (B/L), the commercial invoice (CI) and the EMF, to AI for extraction. A program then fills the Candata Global Windows client by simulating mouse and keyboard input, imports the invoice lines from an Excel file, and leaves the CAD for a broker to review and submit. The AI part is orchestrated with CrewAI: a Flow runs the fixed sequence of steps, and Crews handle only extraction and the investigation of validation failures.

Why this approach:

- Candata does not offer us an API. The only way to write data is through its user interface.
- Candata supports importing invoice lines from Excel/CSV, so product lines do not have to be typed one by one.
- Candata's built-in AI invoice recognition does not work well, and it covers only the commercial invoice, not the B/L or EMF.

Out of scope: the program does not submit CADs, does not correct HS codes, does not build a product library, and does not use Candata's Shipment Workbook Import.

## Overall flow

A shipment goes through six steps from receiving the email to just before submission. Only step 6 needs a person.

1. **Fetch from Gmail**: the program reads new emails under a set Gmail label from allowed senders, saves the PDF attachments and the email text into `inbox/<shipment>/`, and records which messages it has processed.
2. **AI extraction**: pymupdf reads the text layer of the three PDFs (scanned PDFs go to manual handling, or to an optional local OCR fallback). The extraction crew then takes over: B/L, CI and EMF specialists each read one document, and a reconciler merges their output into one structured JSON with a header part and an invoice-lines part. Code cross-checks the result. If the check fails, the investigation crew re-examines the documents with tools. After validation passes, Jev checks the document types, HS codes and units of measure.
3. **Generate the product Excel**: the lines part is written as an xlsx in Candata's invoice-lines import template. HS codes are copied exactly as they appear on the invoice.
4. **Fill the form**: the program opens the shipment screen in Candata and fills each header field with real mouse clicks and keystrokes.
5. **Import products**: the program clicks the import button, enters the path of the Excel file from step 3 in the file dialog, and completes the import.
6. **Human review**: the program stops before submission and saves screenshots. The broker checks the entry, edits fields such as HS codes, and submits it themselves.

If extraction fails validation, the investigation crew runs first. If validation still fails after investigation, or any other step goes wrong, the program stops and reports the problem. It never carries on to the next step.

## Module design

The system has eight modules. The first five do not depend on Candata and can be developed and tested on any computer. The last three must run on the Windows computer where Candata is installed. gmail_reader is plain code that runs before the Flow and fills `inbox/`. The next four are chained by a CrewAI Flow (see "CrewAI orchestration" below). The last three are plain code and do not use CrewAI.

| Module | Input | Output | Key points |
| --- | --- | --- | --- |
| gmail_reader | Gmail label | `inbox/<shipment>/` with PDFs and `email.json` | Gmail API with the read-only scope; one email is one shipment; only allowed senders; processed message IDs kept locally (see "Gmail intake" below) |
| extraction crew | Three PDFs | Shipment JSON | pymupdf reads the PDF text layer; `find_tables()` keeps the rows and columns of invoice tables. B/L, CI and EMF specialist agents extract in parallel and a reconciler agent merges, by default on Claude Haiku 5.5 through OpenRouter. Every task constrains its output with `output_pydantic` and checks format with a `guardrail`. Every field records its source document |
| validator | Shipment JSON | Validation report | Plain code. Sum of line amounts = invoice total; B/L number, packages, gross weight and consignee agree across the three documents |
| investigation crew | Shipment JSON + validation report + document text | Corrected JSON + investigation record | Runs only when validation fails, on Claude Sonnet 5.5. Has three read-only tools: reread a page, reread a table, recompute totals. Every correction states its evidence and is highlighted at review |
| excel_writer | Lines part | Import xlsx | Column order and formats follow the Candata template, configured in YAML |
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
- **What it reads**: messages under the label set in `settings.yaml` (for example `CAD/Incoming`, applied by a Gmail filter on the sender addresses), received after the last run, from senders on the allowlist in `settings.yaml`. Other messages are skipped and listed in the fetch log.
- **What it writes**: `inbox/<YYYYMMDD>-<message id>/` with every PDF attachment and an `email.json` holding the message ID, thread ID, sender, subject, received time and plain-text body.
- **Processed messages**: with a read-only scope the program cannot mark mail as done, so processed message IDs are kept in `output/gmail_state.json`. Running `fetch` again never creates a second folder for the same message.
- **One email, one shipment**: this is the default. Emails with no PDF, or with more or fewer than three documents, are still saved but flagged; Jev's document type check then reports which document is missing or extra. Documents split across several emails in one thread are merged by thread ID when `settings.yaml` turns that on.
- **Other attachments**: only PDFs are read. Zip files, images, and links to shared drives are not opened; the email is flagged for a person.
- **Email body**: passed to the reconciler as extra context, mainly for client references and instructions. A header value taken from the body has `source: "email"` and is highlighted at review. Body text, like PDF text, is treated as data: agents have only read-only tools, so text that tries to instruct the model cannot trigger any action.
- **Account type**: if the mailbox is on Google Workspace, set the OAuth consent screen to Internal: no Google verification is needed and the token does not expire weekly. A personal @gmail.com account can only use an External app in Testing mode, where the token expires every 7 days and someone has to sign in again; for that case, IMAP with an app password is the fallback.

### CrewAI orchestration

The order of steps is fixed, so a Flow chains them. Only extraction and investigation need model judgment, so only those two use Crews. Form filling and import (RPA) are not part of the Flow and are not exposed as a tool to any agent: a model must never decide whether or how many times to click.

```
load_texts → extract (extraction crew) → validate ─ passed ─→ classify (Jev) → write_excel → end
                                                  └ failed → investigate (investigation crew) → revalidate ─ passed ─→ classify …
                                                                                                           └ failed → stop, hand to a person
```

| Flow step | Implemented as | Notes |
| --- | --- | --- |
| load_texts | `@start()`, code | pymupdf reads the three PDFs; too little text means a scanned PDF, so stop and hand to a person |
| extract | `@listen`, extraction crew | Outputs the shipment JSON |
| validate | `@router`, code | Returns `passed` or `failed` |
| investigate | `@listen("failed")`, investigation crew | Outputs corrections and an investigation record |
| revalidate | `@router`, code | Passed goes to classify; failed stops |
| classify | `@listen(or_(...))`, code | Calls Jev directly, not through an agent |
| write_excel | `@listen`, code | Writes the xlsx, validation report and investigation record |

- The Flow state is a pydantic model (`ShipmentFlow(Flow[ShipmentState])`) holding the shipment JSON, the validation report and the investigation record. When a shipment finishes, it is written to `output/<shipment>/`.
- No `@persist`: the two computers hand off through JSON files, and extracting one shipment takes only tens of seconds, so a failed run is simply rerun from the start.

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

**Investigation crew**: a single investigator agent on Claude Sonnet 5.5, run only when validation fails.

| Tool | Purpose |
| --- | --- |
| `read_page_text(doc, page)` | Reread the text of one page of one document |
| `read_page_tables(doc, page)` | Reread the tables on a page with `find_tables()` |
| `recompute_totals(lines)` | Recompute the line total and compare it with the invoice total |

- Output is an `InvestigationResult` with one entry per validation issue. The result is `corrected` (extraction was wrong; gives the corrected value), `document_mismatch` (the documents themselves disagree) or `unresolved`, with evidence (document, page, quoted text).
- Code writes corrections back into the JSON and validates again. Fields changed by investigation are highlighted in the validation report and review screenshots for the broker to confirm.
- All three tools are read-only. The investigator cannot call form filling, import, or any write operation.

**Configuration**

- Each crew keeps its agents' role, goal and backstory and its task descriptions in its own `config/agents.yaml` and `config/tasks.yaml` (the layout generated by `crewai create crew <name> --classic`). The model for each agent is set in `settings.yaml`.
- All models go through OpenRouter with one `OPENROUTER_API_KEY`. CrewAI's `LLM` class reaches OpenRouter through LiteLLM (`crewai[litellm]`). Model names have the form `openrouter/<vendor>/<model>`: `openrouter/anthropic/claude-haiku-5.5` for the extraction crew and `openrouter/anthropic/claude-sonnet-5.5` for the investigation crew. Switching to another vendor's model only changes `settings.yaml`.
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
- Every answer has a confidence. Below the threshold, the field is left blank and marked red. The threshold is set in `settings.yaml` and tuned on sample shipments.
- **Access through OpenRouter**: Jev is on OpenRouter with no waitlist and no TypeSafe account, using the same `OPENROUTER_API_KEY` and billed to the same account. The price is the same as from TypeSafe: $0.042 per million input tokens, output free. `classifier/jev.py` uses the TypeSafe Python SDK with its base URL set to `https://openrouter.ai/api` (OpenRouter's System One API). Jev is not a chat model and does not work through the chat completions endpoint, so it is not called through CrewAI.
- **Pin the model version**: use `typesafe/jev-1.14`, not the `~typesafe/jev-latest` alias. Probabilities change between versions, so a new version means tuning the thresholds again on the samples.
- **Keep requests small**: requests over about 32,000 bytes of text run in TypeSafe's long-context mode, which is slower and less accurate. Send one invoice line per HS request and one layer per HS suggestion step.
- **Fallback**: OpenRouter serves other decision models on the same Decisions API (for example `openai/gpt-6-luna-decisions`, `cloudflare/clef`). The classifier stays behind a common interface, so one of these can replace Jev by changing the model name and retuning the thresholds.
- **Jev Router is something else**: `typesafe/jev-router` is a different product. It uses Jev to pick a chat model for each request and is not used here.

Sources: [Jev on OpenRouter](https://openrouter.ai/docs/guides/community/jev), [Jev 1.14 model page](https://openrouter.ai/typesafe/jev-1.14), [TypeSafe SDK on OpenRouter](https://openrouter.ai/docs/guides/community/typesafe-sdk), [TrueFoundry on Jev](https://www.truefoundry.com/de/blog/typesafe-ai-jev)

## Data structure

All modules share one shipment JSON, which is also the core of the Flow state. `header` feeds the form filler and `lines` feeds the Excel writer. `email` identifies the Gmail message the shipment came from. Every field has a `source` naming the document it came from (`bl`, `ci`, `emf`, or `email` for the email body), and `investigation` records what the investigation crew changed and on what evidence. The field list is a first draft, to be finalized against the Candata screens and import template.

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
  "investigation": [
    {"field": "header.gross_weight", "result": "corrected", "old": 12400, "new": 1240,
     "evidence": {"doc": "bl", "page": 1, "quote": "GROSS WEIGHT 1,240.00 KGS"}}
  ]
}
```

## Dependencies

The runtime is Windows 10/11 with 64-bit Python 3.10–3.13 (3.12 recommended; CrewAI currently requires >=3.10,<3.14), plus an OpenRouter API key and a Gmail OAuth client. The versions below are minimums; pin exact versions once integration testing passes.

### Python packages

| Package | Minimum version | Purpose | Module |
| --- | --- | --- | --- |
| pymupdf | 1.24 | Read the PDF text layer, extract tables with `find_tables()`, detect scanned PDFs | text |
| google-api-python-client | 2.100 | Gmail API: list messages, download attachments | gmail_reader |
| google-auth-oauthlib | 1.2 | Gmail OAuth sign-in and token refresh | gmail_reader |
| crewai | 1.15 | Flow orchestration, extraction and investigation crews, LLM calls through OpenRouter (`crewai[litellm]`) | flow, crews |
| typesafe-sdk | 0.7.5 | Call Jev through OpenRouter's System One API | classifier |
| pydantic | 2.5 | Define and validate the shipment JSON, Flow state and task outputs | schema, flow, validator |
| openpyxl | 3.1 | Generate the import xlsx | excel_writer |
| pywinauto | 0.6.8 | Find Candata controls, simulate mouse and keyboard | rpa_filler, rpa_importer |
| pyautogui | 0.9.54 | Coordinate-click fallback, screenshots | rpa_filler, reviewer |
| Pillow | 10.0 | Save and process screenshots | reviewer |
| keyboard | 0.13.5 | Global emergency stop hotkey (Esc) | rpa_filler |
| PyYAML | 6.0 | Read field mappings and UI locator config | all |
| python-dotenv | 1.0 | Read credentials and CrewAI environment variables from `.env` | flow, classifier |

crewai is installed with the `litellm` extra, since all models go through OpenRouter. The model for each agent is set in `settings.yaml`, so switching models only changes config. The classifier still defines a common interface so another decision model on OpenRouter can replace Jev if needed. When a scanned PDF has no readable text, the default is to hand it to a person. If scanned PDFs turn out to be common, add the free local PaddleOCR (`paddleocr` + `paddlepaddle`) as a fallback.

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
crewai[litellm]>=1.15        # Claude Haiku 5.5 / Sonnet 5.5 via OpenRouter

# Classification
typesafe-sdk>=0.7.5          # Jev via OpenRouter

pydantic>=2.5
openpyxl>=3.1
PyYAML>=6.0
python-dotenv>=1.0
```

`requirements-fill.txt` (`main.py fill`, Candata computer):

```
pydantic>=2.5
pywinauto>=0.6.8
pyautogui>=0.9.54
Pillow>=10.0
keyboard>=0.13.5
PyYAML>=6.0
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

- **OpenRouter**: the only model provider. One `OPENROUTER_API_KEY` covers the extraction crew (Claude Haiku 5.5), the investigation crew (Claude Sonnet 5.5) and Jev. Token prices have no markup; credit purchases carry a 5.5% fee (minimum $0.80 per purchase).
- **Gmail API**: a Google Cloud project with the Gmail API enabled and an OAuth client of type Desktop app. The first `fetch` opens a browser for the mailbox owner to approve read-only access. No API key.
- **CrewAI**: an open-source library that runs locally; no CrewAI account needed. Telemetry and tracing are turned off as described in "CrewAI orchestration".
- **Jev (TypeSafe AI)**: called through OpenRouter, so no TypeSafe account or key is needed.
- Text extraction runs locally and does not depend on an external OCR service.
- These services bill by usage, and model names are kept in config so they are easy to change. Document text is sent to external services, so confirm this meets the company's and clients' data handling requirements before use.

Sign-up steps and `.env` variable names are in the README, section "Accounts and API keys".

## Project structure and configuration

Field mappings, UI locators and Excel column order all live in YAML under `config/`. When the Candata screens or template change, only config changes, not code. Agent and task prompts live in each crew's own `config/`.

```
cad-autofill/
├── config/
│   ├── settings.yaml        # model per agent, Jev version, guardrail retries, confidence thresholds,
│   │                        # Gmail label and sender allowlist, folders, timeouts
│   ├── header_fields.yaml   # JSON field → Candata control locator → format rules
│   ├── excel_template.yaml  # column order and formats of the Candata invoice import template
│   └── tariff/              # layered Canadian Customs Tariff candidates, Candata unit code table
├── src/
│   ├── gmail_reader.py      # fetch: Gmail label → inbox/<shipment>/ (PDFs + email.json)
│   ├── text/
│   │   ├── pdf_text.py      # pymupdf text layer and table extraction, scanned-PDF detection
│   │   └── paddle.py        # local OCR fallback
│   ├── flow.py              # ShipmentFlow: load_texts → extract → validate → … → write_excel
│   ├── crews/
│   │   ├── extraction/
│   │   │   ├── config/agents.yaml   # B/L, CI and EMF specialists and reconciler
│   │   │   ├── config/tasks.yaml
│   │   │   ├── guardrails.py        # required-field and format checks
│   │   │   └── crew.py
│   │   └── investigation/
│   │       ├── config/agents.yaml   # investigator
│   │       ├── config/tasks.yaml
│   │       └── crew.py
│   ├── agent_tools/         # read-only investigation tools: read_page_text, read_page_tables, recompute_totals
│   ├── classifier/
│   │   ├── base.py          # classifier interface: document type, HS check and suggestion, unit mapping
│   │   └── jev.py
│   ├── schema.py            # pydantic models: shipment, Flow state, InvestigationResult
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

`header_fields.yaml` example:

```yaml
bl_number:
  locator: {auto_id: "txtBillOfLading"}   # to be confirmed by the control scan
  fallback: {tab_from: "txtShipmentNo", tabs: 3}
  type: text
port_of_entry:
  locator: {title: "Port", control_type: "ComboBox"}
  type: combobox                           # type, wait for candidates, pick the exact match
  format: "{value:0>4}"
```

There are three commands. `python main.py fetch` pulls new shipment emails from Gmail into `inbox/`. `python main.py extract inbox/<shipment>` runs ShipmentFlow: it only extracts and generates the Excel file, and can run on any computer. `python main.py fill inbox/<shipment>` runs on the Candata computer: it fills the form and imports the lines, reading the JSON and xlsx produced by `extract`, and does not use CrewAI.

## Risks and notes

The biggest risk is that Candata's controls cannot be detected. That decides how reliable the RPA can be, so it should be checked first.

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Controls not detectable (custom-drawn UI) | Only Tab navigation or coordinates remain, which makes wrong entries easy | Run `scan_candata_controls.py` first; read back every field after typing |
| Candata upgrade changes the UI | Locators break | Keep all locators in config; check key controls exist at startup |
| AI extraction errors | Wrong header or line data | Cross-validation; mark low-confidence fields red; submit only after human review |
| Multi-agent output is unstable and uses more tokens | Repeated runs on the same shipment may differ; cost is higher than a single call | `output_pydantic` + guardrails constrain format; hard rules checked in code; measure tokens and pass rate on samples before choosing models |
| Investigation crew corrupts data | A correct value gets "corrected" to a wrong one | Every correction must cite the source text; corrected fields are highlighted at review; the investigator has read-only tools only |
| CrewAI changes quickly | Upgrades change the API | Pin the version; run regression on samples before upgrading |
| CrewAI telemetry or tracing uploads data | Document content leaves the company | Turn off telemetry and tracing with environment variables; confirm nothing is reported externally before go-live |
| HS codes on the CI are the exporting country's 6/8-digit codes | Import may fail, or not match Canada's 10-digit tariff items | Import as printed and correct by hand; leave blank if the import rejects them |
| Someone moves the mouse or types during a run | Data entered in the wrong place | Esc emergency stop; warning dialog before a run; abort on focus loss |
| Popups or slow loading | Script hangs or acts too early | Wait explicitly for controls at every step, no fixed sleeps; screenshot and stop on unknown popups |
| Client documents sent to external APIs | Data compliance | Confirm the company's and clients' data handling policies before go-live; in OpenRouter, allow only providers that do not train on prompts |
| Text in an email or PDF tries to instruct the model (prompt injection) | Wrong values extracted | Agents have read-only tools and no RPA; only allowed senders are fetched; values from the email body are highlighted; hard rules checked in code; broker reviews everything |
| Documents for one shipment spread over several emails, or one email covers several shipments | Missing or mixed documents in a folder | Jev document type check flags missing or extra documents; optional merge by thread ID; otherwise regroup by hand |
| Gmail token expires or access is revoked | `fetch` fails | Workspace mailbox with an Internal OAuth app; `fetch` reports the sign-in error clearly; personal Gmail falls back to IMAP with an app password |
| Automation may be restricted by the software license | Contract risk | Ask Candata whether the service agreement allows it |

pymupdf only reads the text layer of digital PDFs. Scanned or image-only PDFs have no readable text; when the program detects too little text, it hands the shipment to a person. Before go-live, measure the share of scanned PDFs, and enable the PaddleOCR fallback if it is high.

A licensed broker is responsible for every CAD. The program always stops before submission, and only a person submits.

## Cost estimate

With pymupdf reading the text layer and CrewAI running multi-agent extraction, API cost is about $0.02 per shipment, or about $20 a month at 1,000 shipments. That is about one fifth of the earlier Azure OCR + Gemini design (about $0.10 per shipment). A single-call extraction would cost about $0.015 per shipment; the difference is the cost of multiple agents.

| Item | Price (per million tokens, input / output) | Usage per shipment (assumed) | Cost per shipment (USD) |
| --- | --- | --- | --- |
| Text extraction: pymupdf | Free, runs locally | 5 pages | 0 |
| Extraction crew: Claude Haiku 5.5 | $0.10 / $0.50 | 4 calls (3 specialists + reconciler), 15,000–20,000 input and 8,000–10,000 output tokens | about 0.006–0.007 |
| Investigation crew: Claude Sonnet 5.5 (assumed for 10% of shipments) | $2 / $10 | About 30,000 input and 3,000 output tokens per run (multi-turn tool calls resend the context) | about 0.009 (spread over all shipments) |
| Classification: Jev | Input $0.042 | About 100,000 tokens (20 lines, including layered HS suggestions) | about 0.004 |
| **Total** | | | **about 0.02** |

- Assumptions: 5 pages per shipment (B/L 1–2 pages, CI 1–3 pages, EMF 1 page), about 1,500 tokens of text per page. Haiku 5.5's tokenizer produces about 30% more tokens for the same text; this is included in the upper end of the range. Extraction uses more tokens than a single call because every agent carries CrewAI's role prompt, and the reconciler reads the three specialists' output again.
- Haiku 5.5's low price applies only to prompts under 100,000 tokens. Every prompt here is 10,000–20,000 tokens, well below that limit.
- The investigation rate is the biggest variable: with no investigation the total is about $0.01 per shipment; at a 30% investigation rate it is about $0.04. Guardrail retries also add usage and are not included above. Revise after measuring tokens and validation pass rate on sample shipments.
- Scanned PDFs handed to a person cost nothing in API fees; the PaddleOCR fallback is also free and uses only local CPU.
- All prices above are OpenRouter's, which match the vendors' own. OpenRouter adds a 5.5% fee on each credit purchase, so the total is about $0.021 per shipment.
- The Gmail API is free at this volume.
- Not included: development time, the Windows computer running Candata, the Candata license. CrewAI is an open-source library and free. Jev is new, so its pricing may change.

Sources: [Claude pricing](https://platform.claude.com/docs/en/about-claude/pricing), [Haiku 5.5 tiered pricing and Sonnet 5.5 prices](https://mixed-news.com/en/?p=35958), [Haiku 5.5 tokenizer token changes](https://mixed-news.com/en/?p=35955), [OpenRouter models API](https://openrouter.ai/api/v1/models), [Jev 1.14 on OpenRouter](https://openrouter.ai/typesafe/jev-1.14)

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
3. Investigation crew: test on samples with real conflicts or deliberately corrupted values, and confirm every correction cites the source text.
4. rpa_importer: automate the Excel import. This step has few UI actions, so it is a good first test of RPA feasibility.
5. rpa_filler: add header fields step by step, text boxes first, then drop-downs and autocomplete.
6. reviewer + end-to-end wiring, then a small pilot batch.
