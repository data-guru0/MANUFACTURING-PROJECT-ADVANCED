"""What S4 extracts from an RFQ (Pydantic). Every field is a Fact: a value, WHERE it came from and how sure we are.

Sources, most exact first: step (the 3D model) > dxf (CAD) > pdf (vector text layer) > ocr / vision (scans)
> odoo (a repeat part Apex already has) > email (the customer's words, read by the LLM) > estimate.
"""

from typing import Any

from pydantic import BaseModel, Field

SOURCE_RANK = {"step": 0, "dxf": 1, "pdf": 2, "vision": 3, "ocr": 4, "odoo": 5, "email": 6, "estimate": 7}


class Fact(BaseModel):
    value: Any
    source: str  # step | dxf | pdf | ocr | vision | odoo | email | estimate, plus the file name
    confidence: float = Field(ge=0, le=1)
    needs_human: bool = False
    note: str = ""


PART_FIELDS = [
    "drawing_no", "rev", "title", "customer_on_drawing", "units", "material_text", "material_code", "envelope_mm",
    "holes_count", "hole_dia_mm", "threads_count", "thread_size", "pockets", "bends", "tightest_tolerance_mm",
    "general_tolerance", "ra_um", "gdt", "outside_processes", "step_volume_cm3",
]  # fmt: skip
CRITICAL = ["material_code", "envelope_mm"]  # without these nobody can quote: ask the customer


class PartSpec(BaseModel):
    part_code: str  # drawing number + rev, e.g. D-20016-A
    files: list[str] = []
    fields: dict[str, Fact] = {}
    quantities: list[int] = []

    def value(self, name: str) -> Any:
        f = self.fields.get(name)
        return f.value if f else None


class RfqRequirements(BaseModel):
    """From the email text (any language), read by the LLM; every value with its quote from the email."""

    requested_date: Fact | None = None  # ISO date
    certificates: Fact | None = None  # list of strings
    fai: Fact | None = None
    ppap: Fact | None = None
    packaging: Fact | None = None
    incoterms: Fact | None = None
    co2_requested: Fact | None = None
    export_control: Fact | None = None  # e.g. "ITAR (USML Category XI)": OPA escalates to a senior
    language: str = "en"  # detected (lingua)
    reply_language: str = "en"


class Extraction(BaseModel):
    parts: list[PartSpec] = []
    requirements: RfqRequirements = RfqRequirements()
    needs_human: list[str] = []  # "<part>.<field>: why"
    clarification: list[str] = []  # questions for the customer (an email that needs approval)
    route: str = "estimator"  # OPA: estimator | senior
    llm_calls: int = 0
