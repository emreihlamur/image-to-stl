# Image → 3D Print

Upload an image and get print-ready multi-colour files for a **Bambu Lab P2S**:

- **Bambu 3MF**: one object with one part per colour. Each part is pre-assigned to its filament slot,
  and the filament colours, the P2S printer, the 0.20 mm Standard process, PLA Basic and the flush
  volumes are all set in the project. Open it in Bambu Studio and slice.
- **STL** of the whole model, plus a **zip with one STL per colour**.

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
   merging of specks below a minimum area, and removal of diagonal-only pixel contacts (these would
   otherwise become non-manifold edges).
4. **Mesh**: each colour's outline is traced along exact pixel edges, collinear points are collapsed,
   and the caps are triangulated with GEOS constrained Delaunay. Neighbouring parts share their walls
   exactly, every part is watertight, and triangle counts stay low.
5. **Shape modes**
   - *Raised relief*: a backing plate (the most-used colour, any palette colour or an extra filament)
     with the other colours raised on top. They can have equal heights or be stepped. Colour changes
     happen only in the raised layers.
   - *Flat mosaic*: every colour is a full-thickness tile. Good for coasters and signs.
6. **Export**: binary STL writer, and a Bambu Studio 3MF writer (`app/pipeline/export.py`).

## Palette editing

- **Number of colours** (1–16). The UI warns when you need more than one 4-slot AMS.
- Change a swatch's colour to change that region's **filament colour**. This doesn't move any pixels.
- **×** removes a colour; its pixels merge into the nearest remaining colour.
- **Snap to Bambu PLA** swaps each colour for the closest distinct Bambu PLA Basic filament.
- Names you give the colours become the part names in Bambu Studio.

## P2S project template

`app/pipeline/p2s_project_settings.json` is the complete project config Bambu Studio expects. It is
built from the installed Bambu Studio's system presets:

```bash
# export any STL to 3MF with the P2S presets via the Bambu Studio CLI, then:
.venv/bin/python tools/build_p2s_template.py <exported>/Metadata/project_settings.config
```

## Tests

```bash
.venv/bin/python tests/make_sample.py /tmp/sample.png
.venv/bin/python tests/test_pipeline.py /tmp/sample.png /tmp/out   # prints watertightness per part
```
