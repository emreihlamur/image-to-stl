"""STL and 3MF writers.

- Bambu Studio / OrcaSlicer project 3MF: one object with one part per colour, each part on its
  filament, with the printer/process/filament presets and the filament colours in the project.
- PrusaSlicer 3MF: one object with one volume per colour on its extruder, plus the PrusaSlicer
  project config when a Prusa printer is chosen. Without a config it is the universal 3MF: any
  slicer keeps its current printer. PrusaSlicer keeps the extruder of every part; Bambu Studio and
  OrcaSlicer ignore PrusaSlicer's part data, so their users pick a printer from the catalogue.
- Filament swaps (single-nozzle colour by layer) are stored as colour changes in the 3MF, so the
  slicer adds the pause (M600) at the right layer.
"""
from __future__ import annotations

import colorsys
import io
import json
import time
import uuid
import zipfile
from dataclasses import dataclass
from xml.sax.saxutils import quoteattr

import numpy as np

from .colors import hex_to_rgb
from .mesh import Mesh
from .model import Part
from .printers import Printer, project_config


@dataclass
class Swap:
    z: float        # print_z of the first layer printed in the new colour
    layer: int      # 1-based layer number of that layer
    color: str
    name: str


def stl_bytes(mesh: Mesh, name: str = "model") -> bytes:
    v, f = mesh.vertices.astype(np.float32), mesh.faces
    tri = v[f]
    normals = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    normals /= np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1e-12)
    rec = np.zeros(len(f), dtype=[("n", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")])
    rec["n"], rec["v"] = normals, tri
    header = name.encode("ascii", "replace")[:80].ljust(80, b" ")
    return header + np.uint32(len(f)).tobytes() + rec.tobytes()


def _verts_xml(v: np.ndarray) -> str:
    return "".join(f'<vertex x="{x:.4f}" y="{y:.4f}" z="{z:.4f}"/>' for x, y, z in v.tolist())


def _mesh_xml(mesh: Mesh) -> str:
    tris = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in mesh.faces.tolist())
    return f"<mesh><vertices>{_verts_xml(mesh.vertices)}</vertices><triangles>{tris}</triangles></mesh>"


CORE_NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
BBL_NS = (f'unit="millimeter" xml:lang="en-US" xmlns="{CORE_NS}" '
          'xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" '
          'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" '
          'requiredextensions="p"')
REL_3DMODEL = "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"
IDENTITY_4X4 = "1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"
CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
    '<Default Extension="png" ContentType="image/png"/></Types>')


def _rels(target: str) -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Target="{target}" Id="rel-1" Type="{REL_3DMODEL}"/></Relationships>')


def _zip(files: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


# ---------------------------------------------------------------- plate layout
TOWER_DEPTH_MAX, MARGIN = 70.0, 12.0


def plate_layout(parts: list[Part], printer: Printer) -> tuple[tuple[float, float], tuple[float, float]]:
    """Model centre and prime-tower corner on the bed. Parts are centred on the origin.
    The tower goes beside the model when there is room (right, else in front), else in a corner."""
    v = np.concatenate([p.mesh.vertices for p in parts])
    w, h = v[:, 0].max() - v[:, 0].min(), v[:, 1].max() - v[:, 1].min()
    x0, y0, x1, y1 = printer.bed
    bw, bd = x1 - x0, y1 - y0
    tw = printer.tower_width
    cx, cy = x0 + bw / 2, y0 + bd / 2
    if w + tw + 3 * MARGIN <= bw:  # model left, tower right
        mx = x0 + max(w / 2 + MARGIN, (bw - tw - MARGIN) / 2)
        tx = min(x1 - tw - MARGIN, mx + w / 2 + MARGIN)
        return (mx, cy), (tx, cy - TOWER_DEPTH_MAX / 2)
    if h + TOWER_DEPTH_MAX + 3 * MARGIN <= bd:  # model back, tower front
        return (cx, y1 - MARGIN - h / 2), (cx - tw / 2, y0 + MARGIN)
    return (cx, cy), (x1 - tw - MARGIN, y0 + MARGIN)


def flush_volume(src: str, dst: str) -> int:
    """Purge volume (mm^3) for a colour change, roughly following Bambu Studio's auto-calc:
    going to a lighter colour costs far more than going darker, hue changes add some."""
    if src == dst:
        return 0
    (r1, g1, b1), (r2, g2, b2) = (np.array(hex_to_rgb(c)) / 255.0 for c in (src, dst))
    h1, l1, s1 = colorsys.rgb_to_hls(r1, g1, b1)
    h2, l2, s2 = colorsys.rgb_to_hls(r2, g2, b2)
    lumi = (l2 - l1) ** 0.7 * 560 if l2 >= l1 else (l1 - l2) * 80
    dh = min(abs(h1 - h2), 1 - abs(h1 - h2)) * 2  # 0..1
    hue = dh * min(s1, s2) * 250 + abs(s1 - s2) * 120
    vol = float(np.hypot(lumi, hue))
    return int(min(max(vol + 107, 107), 800))


# ---------------------------------------------------------------- Bambu Studio / OrcaSlicer
APP_VERSION = {"bambu": ("BambuStudio-02.08.02.61", None),
               "orca": ("BambuStudio-02.06.00.51", "2.4.2")}


def bbl_project_settings(printer: Printer, filament_colors: list[str], multi: bool,
                         tower: tuple[float, float]) -> dict:
    cfg = project_config(printer.id)
    per_filament = cfg.pop("_per_filament_keys")
    n = len(filament_colors)
    for k in per_filament:
        cfg[k] = cfg[k] * n
    cfg["filament_colour"] = [c.upper() for c in filament_colors]
    if "filament_self_index" in cfg:
        cfg["filament_self_index"] = [str(i + 1) for i in range(n)]
    for k in ("inherits_group", "different_settings_to_system"):  # [print, filament..., printer]
        v = cfg.get(k)
        if isinstance(v, list) and len(v) == 3:
            cfg[k] = [v[0], *[v[1]] * n, v[2]]
    nozzles = printer.extruders if printer.slicer == "bambu" else 1
    cfg["flush_volumes_matrix"] = [str(flush_volume(a, b)) for a in filament_colors
                                   for b in filament_colors] * nozzles
    if isinstance(cfg.get("flush_multiplier"), list):
        cfg["flush_multiplier"] = cfg["flush_multiplier"][:1] * nozzles
    cfg["flush_volumes_vector"] = ["140", "140"] * n
    cfg["wipe_tower_x"] = [f"{tower[0]:.1f}"]
    cfg["wipe_tower_y"] = [f"{tower[1]:.1f}"]
    cfg["enable_prime_tower"] = "1" if multi else "0"
    if cfg.get("extruder_nozzle_stats") == [] and "nozzle_volume_type" in cfg:
        # the installed nozzle per extruder, normally read from the printer (H2D needs it to slice)
        cfg["extruder_nozzle_stats"] = [f"{t}#1" for t in cfg["nozzle_volume_type"]]
    return cfg


def _bbl_custom_gcode(swaps: list[Swap], printer: Printer, cfg: dict) -> str:
    # a pause (type 1) running the printer's own pause G-code: Bambu Studio and OrcaSlicer emit no
    # command for a colour change on most of their single-nozzle printer profiles
    gcode = cfg.get("machine_pause_gcode", "")
    layers = "".join(f'<layer top_z="{s.z:.3f}" type="1" extruder="1" color="{s.color}" extra="" '
                     f'gcode={quoteattr(gcode)}/>' for s in swaps)
    mode = "SingleExtruder" if printer.extruders == 1 else "MultiAsSingle"
    return ('<?xml version="1.0" encoding="utf-8"?>\n<custom_gcodes_per_layer><plate>'
            f'<plate_info id="1"/>{layers}<mode value="{mode}"/></plate></custom_gcodes_per_layer>')


def bbl_3mf_bytes(parts: list[Part], filament_colors: list[str], object_name: str,
                  printer: Printer, swaps: list[Swap] = ()) -> bytes:
    """Bambu Studio / OrcaSlicer project: one object made of one part per colour, each part
    pre-assigned to its filament slot, with the printer presets and filament colours."""
    n = len(parts)
    obj_id = n + 1
    (cx, cy), tower = plate_layout(parts, printer)
    app, orca = APP_VERSION[printer.slicer]

    objects = "".join(
        f'<object id="{i + 1}" p:UUID="{uuid.uuid4()}" type="model">{_mesh_xml(p.mesh)}</object>'
        for i, p in enumerate(parts))
    sub_model = (f'<?xml version="1.0" encoding="UTF-8"?>\n<model {BBL_NS}>'
                 f'<metadata name="BambuStudio:3mfVersion">1</metadata>'
                 f"<resources>{objects}</resources><build/></model>")
    comps = "".join(
        f'<component p:path="/3D/Objects/object_1.model" objectid="{i + 1}" '
        f'p:UUID="{uuid.uuid4()}" transform="1 0 0 0 1 0 0 0 1 0 0 0"/>' for i in range(n))
    main_model = (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<model {BBL_NS}>'
        f'<metadata name="Application">{app}</metadata>'
        + (f'<metadata name="OrcaSlicer">{orca}</metadata>' if orca else "") +
        '<metadata name="BambuStudio:3mfVersion">1</metadata>'
        f'<metadata name="Title">{object_name}</metadata>'
        '<metadata name="Designer">image-to-stl</metadata>'
        f'<resources><object id="{obj_id}" p:UUID="{uuid.uuid4()}" type="model">'
        f"<components>{comps}</components></object></resources>"
        f'<build p:UUID="{uuid.uuid4()}"><item objectid="{obj_id}" p:UUID="{uuid.uuid4()}" '
        f'transform="1 0 0 0 1 0 0 0 1 {cx:.3f} {cy:.3f} 0" printable="1"/></build></model>')

    part_cfg = "".join(
        f'<part id="{i + 1}" subtype="normal_part">'
        f'<metadata key="name" value={quoteattr(p.name)}/>'
        f'<metadata key="matrix" value="{IDENTITY_4X4}"/>'
        f'<metadata key="extruder" value="{p.filament}"/>'
        f'<mesh_stat edges_fixed="0" degenerate_facets="0" facets_removed="0" '
        f'facets_reversed="0" backwards_edges="0"/></part>' for i, p in enumerate(parts))
    model_settings = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<config>'
        f'<object id="{obj_id}"><metadata key="name" value={quoteattr(object_name)}/>'
        f'<metadata key="extruder" value="{parts[0].filament}"/>{part_cfg}</object>'
        '<plate><metadata key="plater_id" value="1"/><metadata key="plater_name" value=""/>'
        '<metadata key="locked" value="false"/>'
        f'<model_instance><metadata key="object_id" value="{obj_id}"/>'
        '<metadata key="instance_id" value="0"/><metadata key="identify_id" value="1"/>'
        '</model_instance></plate>'
        f'<assemble><assemble_item object_id="{obj_id}" instance_id="0" '
        f'transform="1 0 0 0 1 0 0 0 1 {cx:.3f} {cy:.3f} 0" offset="0 0 0"/></assemble></config>')

    multi = len({p.filament for p in parts}) > 1
    project = bbl_project_settings(printer, filament_colors, multi, tower)
    files = {
        "[Content_Types].xml": CONTENT_TYPES,
        "_rels/.rels": _rels("/3D/3dmodel.model"),
        "3D/3dmodel.model": main_model,
        "3D/_rels/3dmodel.model.rels": _rels("/3D/Objects/object_1.model"),
        "3D/Objects/object_1.model": sub_model,
        "Metadata/model_settings.config": model_settings,
        "Metadata/project_settings.config": json.dumps(project, indent=4, sort_keys=True),
    }
    if swaps:
        files["Metadata/custom_gcode_per_layer.xml"] = _bbl_custom_gcode(swaps, printer, project)
    return _zip(files)


# ---------------------------------------------------------------- PrusaSlicer / universal
PRUSA_VERSION = "2.9.6"


def _prusa_config(printer: Printer, filament_colors: list[str], multi: bool,
                  tower: tuple[float, float]) -> str:
    cfg = project_config(printer.id)
    n = max(printer.extruders, len(filament_colors))
    colors = (filament_colors + [filament_colors[-1]] * n)[:printer.extruders]
    cfg["extruder_colour"] = ";".join(colors)
    cfg["filament_colour"] = ";".join(colors)
    cfg["filament_settings_id"] = ";".join([cfg["filament_settings_id"]] * printer.extruders)
    cfg["wipe_tower"] = "1" if multi else "0"
    cfg["wipe_tower_x"], cfg["wipe_tower_y"] = f"{tower[0]:.1f}", f"{tower[1]:.1f}"
    stamp = time.strftime("%Y-%m-%d at %H:%M:%S UTC", time.gmtime())
    return (f"; generated by PrusaSlicer {PRUSA_VERSION} on {stamp}\n\n"
            + "".join(f"; {k} = {v}\n" for k, v in sorted(cfg.items())))


def _prusa_custom_gcode(swaps: list[Swap], extruders: int) -> str:
    codes = "".join(f'<code print_z="{s.z:.3f}" type="0" extruder="1" color="{s.color}" extra="" '
                    f'gcode="M600"/>' for s in swaps)
    mode = "SingleExtruder" if extruders == 1 else "MultiAsSingle"
    return ('<?xml version="1.0" encoding="utf-8"?>\n<custom_gcodes_per_print_z>'
            f'{codes}<mode value="{mode}"/></custom_gcodes_per_print_z>')


def prusa_3mf_bytes(parts: list[Part], filament_colors: list[str], object_name: str,
                    printer: Printer, swaps: list[Swap] = ()) -> bytes:
    """PrusaSlicer-style 3MF: one object, one volume per colour. With a Prusa printer the full
    PrusaSlicer project config is included, otherwise it is the printer-neutral universal 3MF."""
    (cx, cy), tower = plate_layout(parts, printer)
    verts, tris, volumes, offset, first = [], [], [], 0, 0
    for i, p in enumerate(parts):
        verts.append(p.mesh.vertices)
        tris.append("".join(f'<triangle v1="{a}" v2="{b}" v3="{c}" pid="1" p1="{i}"/>'
                            for a, b, c in (p.mesh.faces + offset).tolist()))
        offset += len(p.mesh.vertices)
        volumes.append((first, first + len(p.mesh.faces) - 1, p))
        first += len(p.mesh.faces)
    materials = "".join(f'<base name={quoteattr(p.name)} displaycolor="{p.color.upper()}FF"/>' for p in parts)
    model = (
        f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" '
        f'xmlns="{CORE_NS}" xmlns:slic3rpe="http://schemas.slic3r.org/3mf/2017/06">'
        '<metadata name="slic3rpe:Version3mf">1</metadata>'
        f'<metadata name="Title">{object_name}</metadata>'
        '<metadata name="Designer">image-to-stl</metadata>'
        f'<resources><basematerials id="1">{materials}</basematerials>'
        f'<object id="2" type="model" pid="1" pindex="0"><mesh>'
        f'<vertices>{_verts_xml(np.concatenate(verts))}</vertices>'
        f'<triangles>{"".join(tris)}</triangles></mesh></object></resources>'
        f'<build><item objectid="2" transform="1 0 0 0 1 0 0 0 1 {cx:.3f} {cy:.3f} 0" printable="1"/>'
        '</build></model>')
    vol_cfg = "".join(
        f'<volume firstid="{a}" lastid="{b}">'
        f'<metadata type="volume" key="name" value={quoteattr(p.name)}/>'
        '<metadata type="volume" key="volume_type" value="ModelPart"/>'
        f'<metadata type="volume" key="matrix" value="{IDENTITY_4X4}"/>'
        f'<metadata type="volume" key="extruder" value="{p.filament}"/>'
        '<mesh edges_fixed="0" degenerate_facets="0" facets_removed="0" facets_reversed="0" '
        'backwards_edges="0"/></volume>' for a, b, p in volumes)
    model_cfg = ('<?xml version="1.0" encoding="UTF-8"?>\n<config>'
                 f'<object id="2" instances_count="1">'
                 f'<metadata type="object" key="name" value={quoteattr(object_name)}/>'
                 f'{vol_cfg}</object></config>')
    multi = len({p.filament for p in parts}) > 1
    files = {
        "[Content_Types].xml": CONTENT_TYPES,
        "_rels/.rels": _rels("/3D/3dmodel.model"),
        "3D/3dmodel.model": model,
        "Metadata/Slic3r_PE_model.config": model_cfg,
    }
    if printer.slicer == "prusa":
        files["Metadata/Slic3r_PE.config"] = _prusa_config(printer, filament_colors, multi, tower)
        files["Metadata/Prusa_Slicer_wipe_tower_information.xml"] = (
            '<?xml version="1.0" encoding="utf-8"?>\n<wipe_tower_information bed_idx="0" '
            f'position_x="{tower[0]:.1f}" position_y="{tower[1]:.1f}" rotation_deg="0"/>')
    if swaps:
        files["Metadata/Prusa_Slicer_custom_gcode_per_print_z.xml"] = _prusa_custom_gcode(
            swaps, printer.extruders)
    return _zip(files)


def project_3mf_bytes(parts: list[Part], filament_colors: list[str], object_name: str,
                      printer: Printer, swaps: list[Swap] = ()) -> bytes:
    """The 3MF for the chosen printer's slicer."""
    if printer.slicer in ("bambu", "orca"):
        return bbl_3mf_bytes(parts, filament_colors, object_name, printer, swaps)
    return prusa_3mf_bytes(parts, filament_colors, object_name, printer, swaps)
