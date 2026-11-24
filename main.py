"""cad-autofill command line.

    python main.py fetch                      Gmail label -> inbox/<shipment>/
    python main.py extract inbox/<shipment>   PDFs -> shipment JSON, validation report, xlsx
    python main.py fill inbox/<shipment>      Candata: fill header, import lines, stop before submit

Only the standard library is imported at module level, so `fill` runs on the
Candata computer with requirements-fill.txt alone (no CrewAI, no dotenv).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_SETTINGS = PROJECT_ROOT / "config" / "settings.yaml"

# CrewAI telemetry and tracing stay off whatever .env says: traces can contain
# shipment text. Set before anything imports crewai.
CREWAI_OFF = {
    "CREWAI_DISABLE_TELEMETRY": "true",
    "OTEL_SDK_DISABLED": "true",
    "CREWAI_TRACING_ENABLED": "false",
}
os.environ.update(CREWAI_OFF)

EXIT_OK = 0
EXIT_USAGE = 1          # bad settings path (argparse uses 2 for bad arguments)
EXIT_NOT_IMPLEMENTED = 3


def load_env() -> None:
    """Load .env for the commands that call external services (fetch, extract)."""
    from dotenv import load_dotenv  # requirements-extract.txt

    load_dotenv(PROJECT_ROOT / ".env", override=False)
    os.environ.update(CREWAI_OFF)


def shipment_dir(value: str) -> Path:
    path = Path(value)
    if not path.is_dir():
        raise argparse.ArgumentTypeError(f"not a folder: {value}")
    return path


def not_implemented(command: str, issue: str) -> int:
    print(f"`{command}` is not implemented yet (see issue {issue}).", file=sys.stderr)
    return EXIT_NOT_IMPLEMENTED


def cmd_fetch(args: argparse.Namespace) -> int:
    load_env()
    return not_implemented("fetch", "#33")


def cmd_extract(args: argparse.Namespace) -> int:
    load_env()
    return not_implemented("extract", "#12")


def cmd_fill(args: argparse.Namespace) -> int:
    return not_implemented("fill", "#28")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Fill Candata CAD entries from shipment documents. "
        "Always stops before submission.",
    )
    parser.add_argument(
        "--settings", type=Path, default=DEFAULT_SETTINGS,
        help="settings file (default: config/settings.yaml)",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    fetch = sub.add_parser(
        "fetch", help="fetch new shipment emails from Gmail into inbox/",
        description="Read new emails under the Gmail label from allowed senders and "
        "save each one as inbox/<YYYYMMDD>-<message id>/ (PDFs + email.json).",
    )
    fetch.set_defaults(func=cmd_fetch)

    extract = sub.add_parser(
        "extract", help="extract one shipment into JSON, a validation report and the import xlsx",
        description="Run ShipmentFlow on one shipment folder. Results go to output/<shipment>/.",
    )
    extract.add_argument("shipment", type=shipment_dir, help="shipment folder, e.g. inbox/<shipment>")
    extract.set_defaults(func=cmd_extract)

    fill = sub.add_parser(
        "fill", help="fill the Candata header and import the lines (Candata computer)",
        description="Fill the shipment header in Candata, import the invoice lines, "
        "save screenshots and stop before submission. Reads the output of extract.",
    )
    fill.add_argument("shipment", type=shipment_dir, help="shipment folder, e.g. inbox/<shipment>")
    fill.set_defaults(func=cmd_fill)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.settings.is_file():
        print(f"settings file not found: {args.settings}", file=sys.stderr)
        return EXIT_USAGE
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
