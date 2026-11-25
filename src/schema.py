"""Pydantic models shared by every module: the shipment JSON, the Flow state and
the human-review records. Field names follow the "Data structure" section of the
design doc (wiki: Design).

Only pydantic and the standard library are imported, so `fill` can load these
models on the Candata computer too.
"""

import datetime as dt
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Where a value came from: the three documents, the email body, or a person.
Source = Literal["bl", "ci", "emf", "email", "human"]

# Header values are scalars. An empty string means "not found in the documents".
Scalar = str | int | float


class Strict(BaseModel):
    """Unknown keys are errors, so a misspelled field name fails loudly."""

    model_config = ConfigDict(extra="forbid")


class HeaderField(Strict):
    value: Scalar
    source: Source
    # What each document said, for fields that appear on more than one (bl_number,
    # packages, gross_weight, consignee). The validator compares these; `value` is the
    # one the reconciler chose, or what a person typed (source "human").
    found: dict[Source, Scalar] = {}
    confidence: float | None = Field(default=None, ge=0, le=1)
    page: int | None = Field(
        default=None, ge=1
    )  # page of the source document, for the validation report


class Email(Strict):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    message_id: str
    thread_id: str
    sender: str = Field(alias="from")
    subject: str = ""
    received: dt.datetime


class Header(Strict):
    """Fields typed into the Candata shipment screen. First draft: to be finalized
    against the Candata screens (issue #4)."""

    # B/L
    bl_number: HeaderField | None = None
    shipper: HeaderField | None = None
    consignee: HeaderField | None = None
    port_of_loading: HeaderField | None = None
    port_of_discharge: HeaderField | None = None
    packages: HeaderField | None = None
    package_type: HeaderField | None = None
    gross_weight: HeaderField | None = None
    vessel_voyage: HeaderField | None = None
    container: HeaderField | None = None  # several containers: one comma-separated string
    # EMF
    cargo_control_number: HeaderField | None = None
    carrier_code: HeaderField | None = None
    port_of_entry: HeaderField | None = None
    eta: HeaderField | None = None
    # CI
    invoice_number: HeaderField | None = None
    invoice_date: HeaderField | None = None
    vendor: HeaderField | None = None
    purchaser: HeaderField | None = None
    currency: HeaderField | None = None
    incoterms: HeaderField | None = None


class Line(Strict):
    """One invoice line. `hs_code` is kept as printed on the invoice (a string, so
    leading zeros and dots survive)."""

    line_no: int = Field(ge=1)
    description: str
    hs_code: str
    country_of_origin: str
    quantity: float
    unit: str
    unit_price: float
    amount: float


class ValidationIssue(Strict):
    field: str  # "header.gross_weight", "invoice_total", ...
    issue: str
    found: dict[str, Scalar | None] = {}  # the value on each document, or the figures compared
    accepted: bool = False  # a person accepted these exact values (see ReviewAccepted)


class Validation(Strict):
    """`passed` is true when every issue is accepted. Accepted issues stay listed."""

    passed: bool
    issues: list[ValidationIssue] = []

    @property
    def open_issues(self) -> list[ValidationIssue]:
        return [i for i in self.issues if not i.accepted]


class ReviewCorrected(Strict):
    field: str
    action: Literal["corrected"]
    old: Scalar | None = None
    new: Scalar
    by: Literal["human"] = "human"
    note: str = ""


class ReviewAccepted(Strict):
    field: str
    action: Literal["accepted"]
    values: dict[str, Scalar | None] = {}  # the values the acceptance holds for
    by: Literal["human"] = "human"
    note: str


ReviewRecord = Annotated[ReviewCorrected | ReviewAccepted, Field(discriminator="action")]


class Shipment(Strict):
    """The shipment JSON saved as output/<shipment>/shipment.json."""

    email: Email | None = None
    header: Header = Header()
    invoice_total: HeaderField | None = (
        None  # from the CI; used by validation, not typed into Candata
    )
    lines: list[Line] = []
    validation: Validation | None = None
    review: list[ReviewRecord] = []


class Correction(Strict):
    """One `[[correction]]` entry of corrections.toml.

    `write_review` fills `field`, `issue` and `found`. A person then sets `value`
    (the correct value) or `accept = true` with a `note` (the documents really
    differ and the extracted value stands). TOML has no null, so an entry whose
    `value` is missing or "" is still open.
    """

    field: str
    issue: str
    found: dict[str, Scalar | None] = {}
    value: Scalar | None = None
    accept: bool = False
    note: str = ""

    @model_validator(mode="after")
    def _one_decision(self) -> "Correction":
        if self.accept and self.has_value:
            raise ValueError("set either `value` or `accept = true`, not both")
        if self.accept and not self.note.strip():
            raise ValueError("an accepted issue needs a `note` saying why")
        return self

    @property
    def has_value(self) -> bool:
        return self.value is not None and self.value != ""

    @property
    def is_open(self) -> bool:
        return not self.accept and not self.has_value


class CorrectionsFile(Strict):
    """corrections.toml as a whole."""

    correction: list[Correction] = []

    def open_entries(self) -> list[Correction]:
        return [c for c in self.correction if c.is_open]


class ShipmentState(BaseModel):
    """CrewAI Flow state (`ShipmentFlow(Flow[ShipmentState])`). Needs defaults for
    every field, because Flow builds it empty. Not persisted: the two computers hand
    off through shipment.json."""

    shipment_dir: str = ""
    shipment: Shipment | None = None
    corrections: list[Correction] = []
