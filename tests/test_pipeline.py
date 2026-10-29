import sys, pathlib, io, zipfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "app"))
import numpy as np, trimesh
from pipeline.colors import quantize, snap_to_filaments, rgb_to_hex
from pipeline.model import ModelSettings, build_parts, center_parts
from pipeline import printers
from pipeline.export import stl_bytes, project_3mf_bytes

data = pathlib.Path(sys.argv[1]).read_bytes()
out = pathlib.Path(sys.argv[2]); out.mkdir(parents=True, exist_ok=True)
for mode, rm_bg in [("flat", False), ("relief", True), ("layers", False)]:
    q = quantize(data, width_mm=120, pixel_mm=0.3, n_colors=5, remove_bg=rm_bg, bg_tolerance=12,
                 smoothing=1, min_region_mm2=1.0)
    hexes = [rgb_to_hex(c) for c in q.palette]
    names = [f"Color {i+1}" for i in range(len(hexes))]
    parts = build_parts(q, hexes, names, ModelSettings(mode=mode, border_mm=2 if rm_bg else 0))
    print(mode, q.labels.shape, hexes, snap_to_filaments(q.palette))
    print(" size", center_parts(parts))
    for p in parts:
        tm = trimesh.Trimesh(p.mesh.vertices, p.mesh.faces, process=True)
        print(f"  {p.name:18s} slot={p.filament} tris={len(p.mesh.faces):6d} watertight={tm.is_watertight} winding={tm.is_winding_consistent} vol={tm.volume:9.1f} euler={tm.euler_number}")
    (out / f"{mode}.3mf").write_bytes(project_3mf_bytes(parts, hexes, f"test_{mode}", printers.get("bambu-p2s")))
    for p in parts:
        (out / f"{mode}_{p.name.replace(' ', '_')}.stl").write_bytes(stl_bytes(p.mesh))
