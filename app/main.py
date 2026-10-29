"""Image -> multi-colour 3D print web app (FastAPI)."""
from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from pipeline.colors import BAMBU_PLA_BASIC, quantize, render_preview, rgb_to_hex, snap_to_filaments
from pipeline.export import bambu_3mf_bytes, stl_bytes
from pipeline.mesh import Mesh
from pipeline.model import BED_MM, ModelSettings, build_parts, center_parts

DATA = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
JOBS = DATA / "jobs"
JOBS.mkdir(parents=True, exist_ok=True)
STATIC = Path(__file__).resolve().parent / "static"
MAX_UPLOAD = 25 * 1024 * 1024
JOB_TTL_S = 24 * 3600

app = FastAPI(title="Image to 3D print")


def _cleanup_old_jobs() -> None:
    now = time.time()
    for d in JOBS.iterdir():
        if d.is_dir() and now - d.stat().st_mtime > JOB_TTL_S:
            shutil.rmtree(d, ignore_errors=True)


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", Path(name).stem).strip("_")[:40] or "model"


def _job_dir(job_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        raise HTTPException(404, "Unknown job")
    d = JOBS / job_id
    if not d.is_dir():
        raise HTTPException(404, "Job expired, upload the image again")
    return d


@app.get("/api/filaments")
def filaments():
    return [{"name": n, "hex": h} for n, h in BAMBU_PLA_BASIC.items()]


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    _cleanup_old_jobs()
    data = await file.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "Image too large (max 25 MB)")
    job_id = uuid.uuid4().hex[:12]
    d = JOBS / job_id
    d.mkdir()
    (d / "source").write_bytes(data)
    (d / "meta.json").write_text(json.dumps({"filename": file.filename or "image"}))
    return {"job_id": job_id}


@app.post("/api/generate/{job_id}")
def generate(
    job_id: str,
    n_colors: int = Form(4),
    width_mm: float = Form(100.0),
    pixel_mm: float = Form(0.3),
    mode: str = Form("relief"),
    base_thickness: float = Form(1.2),
    relief_height: float = Form(0.8),
    stepped: bool = Form(False),
    step_height: float = Form(0.4),
    base_color_index: int = Form(-1),
    base_color_custom: str = Form(""),
    border_mm: float = Form(0.0),
    remove_bg: bool = Form(False),
    bg_tolerance: float = Form(12.0),
    smoothing: int = Form(1),
    min_region_mm2: float = Form(1.0),
    palette: str = Form(""),          # JSON list of hex: map the image to exactly these colours
    colors: str = Form(""),           # JSON list of hex: output filament colour per palette entry
    names: str = Form(""),            # JSON list of display/filament names (same order as palette)
    snap_filaments: bool = Form(False),
):
    d = _job_dir(job_id)
    meta = json.loads((d / "meta.json").read_text())
    custom_palette = json.loads(palette) if palette else None
    out_colors = json.loads(colors) if colors else None
    for lst in (custom_palette, out_colors):
        if lst is not None and not all(re.fullmatch(r"#[0-9A-Fa-f]{6}", h) for h in lst):
            raise HTTPException(400, "Colours must be #RRGGBB")
    if base_color_custom and not re.fullmatch(r"#[0-9A-Fa-f]{6}", base_color_custom):
        raise HTTPException(400, "Backing colour must be #RRGGBB")
    if not (1 <= n_colors <= 16):
        raise HTTPException(400, "Colours must be between 1 and 16")
    width_mm = min(max(width_mm, 10.0), BED_MM)
    pixel_mm = min(max(pixel_mm, 0.1), 2.0)
    if width_mm / pixel_mm > 2000:
        raise HTTPException(400, "Resolution too fine for this width (max 2000 px across)")

    t0 = time.time()
    try:
        q = quantize((d / "source").read_bytes(), width_mm=width_mm, pixel_mm=pixel_mm,
                     n_colors=n_colors, remove_bg=remove_bg, bg_tolerance=bg_tolerance,
                     smoothing=smoothing, min_region_mm2=min_region_mm2, custom_palette=custom_palette)
    except ValueError as e:
        raise HTTPException(400, str(e))

    source_hexes = [rgb_to_hex(c) for c in q.palette]
    hexes = [h.upper() for h in out_colors] if out_colors and len(out_colors) == len(source_hexes) \
        else list(source_hexes)
    names_list = json.loads(names) if names else []
    if snap_filaments:
        snapped = snap_to_filaments(q.palette)
        names_list = [n for n, _ in snapped]
        hexes = [h for _, h in snapped]
    if len(names_list) != len(hexes):
        names_list = [f"Color {i + 1}" for i in range(len(hexes))]

    settings = ModelSettings(
        mode=mode if mode in ("flat", "relief") else "relief",
        base_thickness=min(max(base_thickness, 0.4), 20), relief_height=min(max(relief_height, 0.2), 20),
        stepped=stepped, step_height=min(max(step_height, 0.08), 5),
        base_color_index=base_color_index, base_color_custom=base_color_custom or None,
        border_mm=min(max(border_mm, 0), 20))
    parts = build_parts(q, hexes, names_list, settings)
    if not parts:
        raise HTTPException(400, "No printable geometry produced")
    size = center_parts(parts)
    filament_colors = hexes + ([settings.base_color_custom] if settings.base_color_custom else [])
    filament_names = names_list + (["Backing"] if settings.base_color_custom else [])

    # outputs
    out = d / "out"
    shutil.rmtree(out, ignore_errors=True)
    (out / "parts").mkdir(parents=True)
    base = _slug(meta["filename"])
    part_info = []
    for i, p in enumerate(parts):
        fname = f"{i + 1:02d}_{_slug(p.name)}.stl"
        (out / "parts" / fname).write_bytes(stl_bytes(p.mesh, p.name))
        part_info.append({"name": p.name, "color": p.color, "filament": p.filament,
                          "z0": round(p.z0, 3), "z1": round(p.z1, 3),
                          "triangles": int(len(p.mesh.faces)), "stl": f"parts/{fname}"})
    (out / f"{base}.stl").write_bytes(stl_bytes(Mesh.concat([p.mesh for p in parts]), base))
    (out / f"{base}_P2S.3mf").write_bytes(bambu_3mf_bytes(parts, filament_colors, base))
    with zipfile.ZipFile(out / f"{base}_parts.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted((out / "parts").iterdir()):
            z.write(f, f.name)
    (out / "preview.png").write_bytes(render_preview(q, hexes))

    total_px = int((q.labels >= 0).sum())
    palette_info = [{"index": i, "hex": h, "source_hex": source_hexes[i], "name": names_list[i],
                     "area_pct": round(100 * float((q.labels == i).sum()) / max(total_px, 1), 1)}
                    for i, h in enumerate(hexes)]
    warnings = []
    if size[0] > BED_MM or size[1] > BED_MM:
        warnings.append(f"Model is {size[0]:.0f} x {size[1]:.0f} mm, larger than the P2S bed (256 x 256 mm).")
    if len(filament_colors) > 4:
        warnings.append(f"{len(filament_colors)} filaments: needs more than one 4-slot AMS "
                        "(or manual filament changes).")
    tiny = [p["name"] for p in palette_info if 0 < p["area_pct"] < 0.5]
    if tiny:
        warnings.append("Very small colour areas: " + ", ".join(tiny) + ". Consider fewer colours.")

    return {
        "job_id": job_id,
        "size_mm": [round(s, 1) for s in size],
        "grid_px": [q.width_px, q.height_px],
        "palette": palette_info,
        "filaments": [{"slot": i + 1, "hex": h, "name": n}
                      for i, (h, n) in enumerate(zip(filament_colors, filament_names))],
        "parts": part_info,
        "files": {"3mf": f"{base}_P2S.3mf", "stl": f"{base}.stl", "zip": f"{base}_parts.zip",
                  "preview": "preview.png"},
        "warnings": warnings,
        "seconds": round(time.time() - t0, 2),
    }


@app.get("/api/jobs/{job_id}/source")
def source(job_id: str):
    return FileResponse(_job_dir(job_id) / "source")


@app.get("/api/jobs/{job_id}/files/{path:path}")
def job_file(job_id: str, path: str):
    out = (_job_dir(job_id) / "out").resolve()
    f = (out / path).resolve()
    if out not in f.parents or not f.is_file():
        raise HTTPException(404, "File not found")
    download = f.suffix in (".3mf", ".zip") or (f.suffix == ".stl" and f.parent == out)
    return FileResponse(f, filename=f.name if download else None,
                        headers={"Cache-Control": "no-store"})


@app.get("/api/health")
def health():
    return {"ok": True}


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
