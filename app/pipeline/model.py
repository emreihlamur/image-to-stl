"""Build the coloured parts of the print from a quantised image."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .colors import NO_LABEL, Quantized, remove_pinches
from .mesh import Mesh, extrude_mask
from .outline import Outlines


@dataclass
class Part:
    name: str
    color: str          # filament hex
    filament: int       # 1-based filament slot
    mesh: Mesh
    z0: float
    z1: float


@dataclass
class ModelSettings:
    mode: str = "relief"            # "flat" mosaic, "relief" (backing plate + raised colours) or
                                    # "layers" (colour by height, for filament swaps on one nozzle)
    base_thickness: float = 1.2     # relief backing plate / flat total thickness
    relief_height: float = 0.8      # height of raised colours above the backing (relief)
    stepped: bool = False           # relief: each colour one step higher than the previous
    step_height: float = 0.4
    base_color_index: int = -1      # relief: palette index of the backing, -1 = most used colour
    base_color_custom: str | None = None  # relief: extra filament used only for the backing
    border_mm: float = 0.0          # extra backing rim around the shape (relief only)
    edge_smoothing: int = 2         # 0 = pixel-exact outlines, 1..3 = increasingly smooth curves
    layer_height: float = 0.2       # layers: heights snap to the printer's layers so every
    first_layer_height: float = 0.2  # colour change lands exactly on a layer boundary

    def snap(self, z: float) -> float:
        """Nearest layer top at or above the first layer."""
        fl, lh = self.first_layer_height, self.layer_height
        return round(fl + max(0, round((z - fl) / lh)) * lh, 4)


def _dilate(mask: np.ndarray, px: int) -> np.ndarray:
    import cv2
    if px <= 0:
        return mask
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * px + 1, 2 * px + 1))
    padded = np.pad(mask.astype(np.uint8), px)
    return remove_pinches(cv2.dilate(padded, k)).astype(bool)


def build_parts(q: Quantized, colors: list[str], names: list[str], s: ModelSettings) -> list[Part]:
    labels = q.labels
    k = len(colors)
    footprint = labels != NO_LABEL
    pad = 0
    if s.mode == "relief" and s.border_mm > 0:
        pad = int(round(s.border_mm / q.pixel_mm))
        footprint = _dilate(footprint, pad)
        labels = np.pad(labels, pad, constant_values=NO_LABEL)
        labels = np.where(footprint & (labels == NO_LABEL), k, labels)  # border = pseudo colour k
        labels = remove_pinches(labels)
        footprint = labels != NO_LABEL

    # one shared smoothed outline graph for every region, so all parts keep meeting exactly
    outlines = Outlines(labels, s.edge_smoothing)

    parts: list[Part] = []
    if s.mode == "flat":
        for i in range(k):
            m = extrude_mask(labels == i, 0.0, s.base_thickness, q.pixel_mm, outlines)
            if len(m.faces):
                parts.append(Part(names[i], colors[i], i + 1, m, 0.0, s.base_thickness))
        return parts

    counts = np.array([(labels == i).sum() for i in range(k)])
    if s.base_color_custom:
        base_idx = None
        base_name, base_color, base_slot = "Backing", s.base_color_custom, k + 1
    else:
        base_idx = int(counts.argmax()) if s.base_color_index < 0 else min(s.base_color_index, k - 1)
        base_name, base_color, base_slot = names[base_idx], colors[base_idx], base_idx + 1

    base_top = s.snap(s.base_thickness) if s.mode == "layers" else s.base_thickness
    base = extrude_mask(footprint, 0.0, base_top, q.pixel_mm, outlines)
    parts.append(Part(f"{base_name} (base)", base_color, base_slot, base, 0.0, base_top))

    # raised colours, stepped ones ordered by area (biggest lowest) so details stand on top
    raised = [int(i) for i in np.argsort(-counts) if i != base_idx and counts[i] > 0]
    if s.mode == "layers":
        # one band of layers per colour: band k covers every region raised k or more steps, so
        # each region's top shows its own colour and each band is one filament between swaps
        step = max(s.snap(s.first_layer_height + s.step_height) - s.first_layer_height, s.layer_height)
        z0 = base_top
        for k, i in enumerate(raised):
            z1 = round(z0 + step, 4)
            mask = np.isin(labels, raised[k:])
            m = extrude_mask(mask, z0, z1, q.pixel_mm, outlines)
            if len(m.faces):
                parts.append(Part(names[i], colors[i], 1, m, z0, z1))
            z0 = z1
        parts[0].filament = 1
        return parts
    for step, i in enumerate(raised, start=1):
        h = s.step_height * step if s.stepped else s.relief_height
        z1 = s.base_thickness + h
        m = extrude_mask(labels == i, s.base_thickness, z1, q.pixel_mm, outlines)
        if len(m.faces):
            parts.append(Part(names[i], colors[i], i + 1, m, s.base_thickness, z1))
    return parts


def swap_plan(parts: list[Part], s: ModelSettings) -> list[tuple[float, int, str, str]]:
    """Layers mode: (print_z of the first layer in the new colour, its layer number, colour, name)."""
    out = []
    for p in parts[1:]:
        z = round(p.z0 + s.layer_height, 4)
        layer = 1 + int(round((z - s.first_layer_height) / s.layer_height))
        out.append((z, layer, p.color, p.name))
    return out


def center_parts(parts: list[Part]) -> tuple[float, float, float]:
    """Shift all parts so the model is centred on XY origin; returns (size_x, size_y, size_z)."""
    allv = np.concatenate([p.mesh.vertices for p in parts])
    lo, hi = allv.min(axis=0), allv.max(axis=0)
    shift = np.array([(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, 0.0])
    for p in parts:
        p.mesh.vertices = p.mesh.vertices - shift
    size = hi - lo
    return float(size[0]), float(size[1]), float(hi[2])
