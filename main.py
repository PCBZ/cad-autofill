"""cad-autofill command line.

    python main.py fetch                      Gmail label -> inbox/<shipment>/
    python main.py extract inbox/<shipment>   PDFs -> shipment JSON, validation report, import csv
    python main.py fill inbox/<shipment>      Candata: fill header, import lines, stop before submit

`extract` resumes by itself after a validation failure: once every entry in
output/<shipment>/corrections.toml is filled in, running it again applies the
corrections and validates without calling the extraction crew. To start a
shipment over, delete output/<shipment>/.

Exit codes: 0 done, 1 settings error, 2 bad arguments, 3 not implemented yet,
4 stopped for a person (a scanned PDF, or validation issues to correct).

Settings are always read from config/settings.toml. Only the standard library
is imported at module level, so `fill` runs on the Candata computer with
requirements-fill.txt alone (no CrewAI, no dotenv).
"""

import sys

if sys.version_info < (3, 11):
    sys.exit("cad-autofill needs Python 3.11 or newer (config files are read with tomllib).")

import argparse
import os
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
SETTINGS_FILE = PROJECT_ROOT / "config" / "settings.toml"

# CrewAI telemetry, tracing and the update check stay off whatever .env says: traces can
# contain shipment text, and the update check is a request to pypi.org on every run.
# Set before anything imports crewai (tools/check_crewai_offline.py verifies this).
CREWAI_OFF = {
    "CREWAI_DISABLE_TELEMETRY": "true",
    "CREWAI_TRACING_ENABLED": "false",
    "CREWAI_DISABLE_VERSION_CHECK": "true",
}


def apply_switches() -> None:
    os.environ.update(CREWAI_OFF)
    # Our own run trace (src/telemetry.py) uses the OpenTelemetry SDK, which does nothing
    # at all when OTEL_SDK_DISABLED=true. CrewAI is kept quiet by the switches above.
    os.environ.pop("OTEL_SDK_DISABLED", None)


apply_switches()

EXIT_SETTINGS = 1  # settings.toml missing or invalid, or the model key missing (argparse uses 2 for bad arguments)
EXIT_NOT_IMPLEMENTED = 3
EXIT_NEEDS_PERSON = 4  # stopped on purpose: a scanned PDF, or open validation issues


def load_env() -> None:
    """Load .env for the commands that call external services (fetch, extract)."""
    from dotenv import load_dotenv  # requirements-extract.txt

    load_dotenv(PROJECT_ROOT / ".env", override=False)
    apply_switches()


def load_settings() -> dict:
    with SETTINGS_FILE.open("rb") as f:
        return tomllib.load(f)


def not_implemented(message: str) -> int:
    print(message, file=sys.stderr)
    return EXIT_NOT_IMPLEMENTED


def fetch(settings: dict) -> int:
    load_env()
    return not_implemented("`fetch` is not implemented yet (see issue #33).")


def extract(settings: dict, shipment: Path) -> int:
    load_env()
    model = settings["models"]["extraction"]
    if model.startswith("openrouter/") and not os.environ.get("OPENROUTER_API_KEY"):
        print("OPENROUTER_API_KEY is not set (put it in .env; see README).", file=sys.stderr)
        return EXIT_SETTINGS
    # These import CrewAI, which fill does not need.
    from src.crews.extraction.crew import make_extractor
    from src.flow import OUTCOME_DONE, run_extract

    output_dir = PROJECT_ROOT / settings["paths"]["output"] / shipment.name
    try:
        state = run_extract(settings, shipment, output_dir, extractor=make_extractor(settings))
    except NotImplementedError as e:
        return not_implemented(str(e))
    for message in state.messages:
        print(message)
    return 0 if state.outcome == OUTCOME_DONE else EXIT_NEEDS_PERSON


def fill(settings: dict, shipment: Path) -> int:
    return not_implemented("`fill` is not implemented yet (see issue #28).")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Fill Candata CAD entries from shipment documents. Always stops before submission.",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")
    sub.add_parser("fetch", help="fetch new shipment emails from Gmail into inbox/")
    for name, help_text in [
        ("extract", "extract one shipment (resumes after a filled-in corrections.toml)"),
        ("fill", "fill Candata and import the lines, stop before submit (Candata computer)"),
    ]:
        sub.add_parser(name, help=help_text).add_argument(
            "shipment", type=Path, help="shipment folder, e.g. inbox/<shipment>"
        )
    args = parser.parse_args(argv)
    if args.command != "fetch" and not args.shipment.is_dir():
        parser.error(f"not a folder: {args.shipment}")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        settings = load_settings()
    except FileNotFoundError:
        print(f"settings file not found: {SETTINGS_FILE}", file=sys.stderr)
        return EXIT_SETTINGS
    except tomllib.TOMLDecodeError as e:
        print(f"settings file is not valid TOML: {SETTINGS_FILE}: {e}", file=sys.stderr)
        return EXIT_SETTINGS
    if args.command == "fetch":
        return fetch(settings)
    if args.command == "extract":
        return extract(settings, args.shipment)
    return fill(settings, args.shipment)


if __name__ == "__main__":
    sys.exit(main())
