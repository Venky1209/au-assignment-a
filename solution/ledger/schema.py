"""Loads workflow lead schemas and the disposition catalogue. The code never hardcodes field names:
a new workflow is a new JSON file, not a code change."""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

from . import normalize

DEFAULT_SCHEMA_DIR = Path(__file__).resolve().parents[2] / "assignment-a-data" / "schemas"


@dataclass
class Field:
    name: str
    type: str
    required: bool = False
    pii: bool = False
    reference: bool = False
    aliases: list[str] = field(default_factory=list)
    values: list[str] = field(default_factory=list)
    default: str | None = None


@dataclass
class Workflow:
    workflow_id: str
    fields: list[Field]

    @property
    def phone_field(self) -> Field:
        return next(f for f in self.fields if f.type == "phone")

    @property
    def ref_field(self) -> Field:
        return next(f for f in self.fields if f.reference)

    def header_map(self) -> dict[str, Field]:
        """normalised header/alias -> field"""
        m: dict[str, Field] = {}
        for f in self.fields:
            for name in [f.name, *f.aliases]:
                m.setdefault(normalize.header(name), f)
        return m


@dataclass
class Disposition:
    is_terminal: bool
    is_success: bool


@dataclass
class Catalog:
    workflows: dict[str, Workflow]
    dispositions: dict[tuple[str, str], Disposition]  # (workflow_id, code) -> flags


def load(schema_dir: Path = DEFAULT_SCHEMA_DIR) -> Catalog:
    workflows = {}
    for p in sorted(schema_dir.glob("*.json")):
        raw = json.loads(p.read_text(encoding="utf-8"))
        fields = [Field(**{k: v for k, v in f.items() if k in Field.__dataclass_fields__})
                  for f in raw["fields"]]
        workflows[raw["workflow_id"]] = Workflow(raw["workflow_id"], fields)
    disps = {}
    with open(schema_dir / "dispositions.csv", newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            disps[(row["workflow_id"], row["code"])] = Disposition(
                row["is_terminal"].strip().lower() == "true", row["is_success"].strip().lower() == "true")
    return Catalog(workflows, disps)
