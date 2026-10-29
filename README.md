# Image → 3D Print

Upload an image and get print-ready multi-colour files for **almost any FDM printer**:

- **Project 3MF for your printer's slicer**: one object with one part per colour. Each part is
  pre-assigned to its filament/extruder, and the printer, a 0.20 mm process, PLA and the filament
  colours are all set in the project. Open it and slice.
  - **Bambu Studio**: A1 mini, A1, P1P, P1S, P2S, X1 Carbon, X1E, H2S, H2D
  - **PrusaSlicer**: MK4S, CORE One, XL (1/2/5 toolheads), MK3.5, MK3S+, MINI+, with and without MMU3
  - **OrcaSlicer**: Creality K1C/K1 Max (CFS), K2 Plus, Hi, Ender-3 V3/KE/SE/S1/V2, Elegoo Centauri
    Carbon 1/2, Neptune 4 Pro, Anycubic Kobra S1/3/2 Neo, Snapmaker U1, Flashforge AD5X,
    Qidi X-Plus 4/Q1 Pro, Sovol SV08/SV06, Voron 2.4/Trident
- **Universal 3MF** for any other printer: no printer settings inside, you pick the printer in the
  slicer. PrusaSlicer keeps each part on its extruder; other slicers import it as plain geometry
  (use the per-colour STLs there, or pick a listed printer for Bambu Studio / OrcaSlicer).
- **STL** of the whole model, plus a **zip with one STL per colour**.

**Single-nozzle printers** (no AMS/MMU/CFS…) print in colour with **Layer swaps**: every colour is its
own band of layers, and the 3MF carries a pause (Bambu) or colour change (M600) at each band, so the
slicer stops for each filament swap. The app lists the swaps (layer, height, colour).

Everything prints **without supports**: parts are vertical extrusions standing on the plate or on the base.

## Run

```bash
docker compose up -d --build     # http://localhost:8000
```

Local development without Docker:

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r app/requirements.txt
cd app && ../.venv/bin/uvicorn main:app --reload
```

## How it works

1. **Resample**: the image is resized so 1 pixel = *Detail* mm of the print (default 0.3 mm).
2. **Background**: transparent pixels are dropped. Optionally, the region connected to the border
   that matches the border colour is removed too.
3. **Quantise**: k-means in CIE Lab with N colours, followed by a mode filter (*Smoothing*),
   removal of features thinner than the nozzle (0.45 mm, e.g. anti-aliasing slivers), merging of
   specks below a minimum area, and removal of diagonal-only pixel contacts (these would otherwise
   become non-manifold edges).
4. **Smooth outlines** (`outline.py`): all colour borders form one shared graph on the pixel-corner
   grid. It is smoothed as a whole, so neighbouring parts still meet exactly with no gaps or overlaps.
   Each vertex may move at most 0.5–1.5 px, depending on the *Edges* setting. Real corners, where
   two straight runs of at least 3 px meet, stay sharp, so text and rectangles keep their corners
   while staircases become curves. The chains are then simplified with Douglas-Peucker.
5. **Mesh**: the regions are assembled from those shared outlines, and the caps are triangulated
   with GEOS constrained Delaunay. Every part is watertight.
6. **Shape modes**
   - *Raised relief*: a backing plate (the most-used colour, any palette colour or an extra filament)
     with the other colours raised on top. They can have equal heights or be stepped. Colour changes
     happen only in the raised layers.
   - *Flat mosaic*: every colour is a full-thickness tile. Good for coasters and signs.
   - *Layer swaps*: the base, then one band of layers per colour. Band *k* covers every region that is
     raised *k* or more bands, so each region's top shows its own colour, and every layer has a single
     colour. Heights snap to the printer's layer grid so the swaps land exactly between bands.
7. **Export** (`app/pipeline/export.py`): binary STL, Bambu Studio / OrcaSlicer project 3MF,
   PrusaSlicer 3MF (with its project config for Prusa printers, without one for the universal file).

## Palette editing

- **Number of colours** (1–16). The UI warns when you need more than one 4-slot AMS.
- Change a swatch's colour to change that region's **filament colour**. This doesn't move any pixels.
- **×** removes a colour; its pixels merge into the nearest remaining colour.
- **Snap to Bambu PLA** swaps each colour for the closest distinct Bambu PLA Basic filament.
- Names you give the colours become the part names in Bambu Studio.

## Printer profiles

`app/pipeline/printers/` holds the catalogue (`catalog.json`) and one gzipped project config per
printer. `tools/build_printers.py` builds them from the slicers' own system presets:

- Bambu Studio / OrcaSlicer: the machine preset, a 0.20 mm process and a PLA preset (with their
  `inherits` chains resolved) are loaded through the slicer CLI, which exports the project config.
  A second export with two filaments shows which settings repeat per filament.
- PrusaSlicer: its CLI doesn't apply printer presets, so the PrusaResearch bundle is resolved on top
  of PrusaSlicer's defaults.

```bash
.venv/bin/python tools/build_printers.py            # all printers in its CATALOG list
.venv/bin/python tools/build_printers.py --only bambu-p2s,prusa-xl-5t
```

Adding a printer = one line in `CATALOG` (id, brand, name, slicer, machine preset, colour slots, system).
Slicer apps default to `/Applications/<Name>.app`; override with `BAMBU_STUDIO`, `ORCA_SLICER`, `PRUSA_SLICER`.

## Tests

```bash
.venv/bin/python tests/make_sample.py /tmp/sample.png
.venv/bin/python tests/test_pipeline.py /tmp/sample.png /tmp/out   # prints watertightness per part
# slice the generated 3MFs with the real slicers (tool changes / filament swaps in the G-code)
.venv/bin/python tests/slice_check.py /tmp/sample.png /tmp/sc bambu-p2s prusa-xl-5t creality-k2plus
.venv/bin/python tests/slice_check.py /tmp/sample.png /tmp/sc prusa-mk4s creality-ender3v3 --mode layers
```
