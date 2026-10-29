"""Printer catalogue and per-printer slicer project configs (built by tools/build_printers.py)."""
from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DIR = Path(__file__).with_name("printers")
SLICERS = {"bambu": "Bambu Studio", "orca": "OrcaSlicer", "prusa": "PrusaSlicer", "generic": "any slicer"}


@dataclass(frozen=True)
class Printer:
    id: str
    brand: str
    name: str
    slicer: str                 # bambu | orca | prusa | generic
    slots: int                  # colours it can change by itself (AMS, MMU, toolheads); 1 = none
    system: str                 # name of the colour system, e.g. "AMS"
    bed: tuple[float, float, float, float]  # x0, y0, x1, y1 of the printable area
    height: float
    extruders: int = 1
    layer_height: float = 0.2
    first_layer_height: float = 0.2
    tower_width: float = 35.0

    @property
    def size(self) -> tuple[float, float]:
        return self.bed[2] - self.bed[0], self.bed[3] - self.bed[1]

    @property
    def label(self) -> str:
        return f"{self.brand} {self.name}".strip()

    def public(self) -> dict:
        return {"id": self.id, "brand": self.brand, "name": self.name, "label": self.label,
                "slicer": self.slicer, "slicer_name": SLICERS[self.slicer], "slots": self.slots,
                "system": self.system, "bed": [round(v, 1) for v in self.size], "height": self.height}


@lru_cache(maxsize=1)
def catalog() -> dict[str, Printer]:
    keys = Printer.__dataclass_fields__
    out = {}
    for p in json.loads((DIR / "catalog.json").read_text()):
        out[p["id"]] = Printer(**{k: tuple(v) if k == "bed" else v for k, v in p.items() if k in keys})
    return out


def generic(bed_w: float = 220, bed_d: float = 220, slots: int = 1) -> Printer:
    """Any other printer: universal 3MF, the user picks the printer in their slicer."""
    return Printer("generic", "", "Other printer", "generic", max(1, int(slots)),
                   "multi-material" if slots > 1 else "", (0.0, 0.0, float(bed_w), float(bed_d)), 250.0)


def get(printer_id: str, bed_w: float = 220, bed_d: float = 220, slots: int = 1) -> Printer:
    if printer_id == "generic":
        return generic(bed_w, bed_d, slots)
    try:
        return catalog()[printer_id]
    except KeyError:
        raise ValueError(f"Unknown printer {printer_id!r}")


@lru_cache(maxsize=8)
def _config(printer_id: str) -> str:
    return gzip.decompress((DIR / f"{printer_id}.json.gz").read_bytes()).decode()


def project_config(printer_id: str) -> dict:
    """A fresh copy of the printer's single-filament project config."""
    return json.loads(_config(printer_id))
