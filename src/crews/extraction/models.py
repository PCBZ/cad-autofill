"""What each extraction task returns (`output_pydantic`).

The specialists report what one document says, field by field, with the page. The
reconciler only picks a value for the fields that several documents give. Code then
builds the `Shipment` from these (crew.build_shipment), so `source` and `found` come
from what the specialists read, not from what the reconciler says they read.
"""

from typing import Literal

from pydantic import Field

from src.schema import Line, Scalar, Strict

SpecialistDocument = Literal["bl", "ci", "emf"]


class Reading(Strict):
    """One value as printed on one document. "" means the document does not give it."""

    value: Scalar = ""
    page: int | None = Field(default=None, ge=1)


class BLReading(Strict):
    bl_number: Reading = Reading()
    shipper: Reading = Reading()
    consignee: Reading = Reading()
    port_of_loading: Reading = Reading()
    port_of_discharge: Reading = Reading()
    packages: Reading = Reading()
    package_type: Reading = Reading()
    gross_weight: Reading = Reading()
    vessel_voyage: Reading = Reading()
    container: Reading = Reading()


class CIReading(Strict):
    invoice_number: Reading = Reading()
    invoice_date: Reading = Reading()
    vendor: Reading = Reading()
    purchaser: Reading = Reading()
    currency: Reading = Reading()
    incoterms: Reading = Reading()
    invoice_total: Reading = Reading()
    # For cross-checking against the B/L, when the invoice prints them.
    bl_number: Reading = Reading()
    consignee: Reading = Reading()


class CILines(Strict):
    """The invoice table, read by its own task: one purpose, one output."""

    lines: list[Line] = []


class EMFReading(Strict):
    cargo_control_number: Reading = Reading()
    carrier_code: Reading = Reading()
    port_of_entry: Reading = Reading()
    eta: Reading = Reading()
    # For cross-checking against the B/L.
    bl_number: Reading = Reading()
    packages: Reading = Reading()
    gross_weight: Reading = Reading()


class Choice(Strict):
    value: Scalar
    source: SpecialistDocument


class Reconciled(Strict):
    """The reconciler's pick for each field several documents give. None: no document gave it."""

    bl_number: Choice | None = None
    consignee: Choice | None = None
    packages: Choice | None = None
    gross_weight: Choice | None = None
    notes: list[str] = []  # conflicts it saw, for the person reviewing


# Which documents give each field the reconciler decides, in the order used when the
# reconciler gives no pick (the B/L is the reference document for all four).
SHARED_FIELDS: dict[str, tuple[SpecialistDocument, ...]] = {
    "bl_number": ("bl", "emf", "ci"),
    "consignee": ("bl", "ci"),
    "packages": ("bl", "emf"),
    "gross_weight": ("bl", "emf"),
}
