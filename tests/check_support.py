import sys, pathlib; sys.path.insert(0, "app")
import shapely
from shapely.geometry import Polygon
from pipeline.colors import quantize, rgb_to_hex
from pipeline.model import ModelSettings, build_parts
q = quantize(pathlib.Path(sys.argv[1]).read_bytes(), width_mm=100, pixel_mm=0.3, n_colors=5, remove_bg=True, bg_tolerance=12, smoothing=1, min_region_mm2=1.0)
hexes = [rgb_to_hex(c) for c in q.palette]
def cap(p, z):
    v, f = p.mesh.vertices, p.mesh.faces
    return shapely.union_all([Polygon(v[t, :2]) for t in f[(abs(v[f][:, :, 2] - z) < 1e-6).all(axis=1)]])
for border in [0, 2]:
    for lvl in [0, 2, 3]:
        parts = build_parts(q, hexes, [""] * 5, ModelSettings(mode="relief", edge_smoothing=lvl, border_mm=border))
        base = cap(parts[0], parts[0].z1)
        unsupported = sum(cap(p, p.z0).difference(base).area for p in parts[1:])
        print(f"border={border} level={lvl} unsupported area={unsupported:.6f} mm2")
