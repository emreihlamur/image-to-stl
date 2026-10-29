"""Build the printer catalogue and per-printer project configs in app/pipeline/printers/.

Usage: python tools/build_printers.py [--only <id>,<id>]

Each printer's project config is produced by the slicer itself, so it matches what the slicer
would write for that printer, process and filament:
- Bambu Studio / OrcaSlicer: the system presets (with their `inherits` chains resolved) are loaded
  through the slicer CLI, which exports a 3MF. Its project_settings.config is the template.
  A second export with two filaments shows which settings repeat per filament.
- PrusaSlicer: its CLI does not apply printer presets, so the PrusaResearch bundle is resolved
  here (inherits, in order) on top of PrusaSlicer's defaults.

Slicer locations: BAMBU_STUDIO, ORCA_SLICER and PRUSA_SLICER environment variables (paths to the
.app bundles); defaults are /Applications/<Name>.app.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "app/pipeline/printers"
APPS = {
    "bambu": Path(os.environ.get("BAMBU_STUDIO", "/Applications/BambuStudio.app")),
    "orca": Path(os.environ.get("ORCA_SLICER", "/Applications/OrcaSlicer.app")),
    "prusa": Path(os.environ.get("PRUSA_SLICER", "/Applications/PrusaSlicer.app")),
}
EXE = {"bambu": "BambuStudio", "orca": "OrcaSlicer", "prusa": "PrusaSlicer"}

# id, brand, display name, slicer, machine preset, colour slots, colour system
# slots = how many colours the printer can swap by itself (1 = single nozzle, no changer)
CATALOG = [
    ("bambu-a1mini", "Bambu Lab", "A1 mini", "bambu", "Bambu Lab A1 mini 0.4 nozzle", 4, "AMS lite"),
    ("bambu-a1", "Bambu Lab", "A1", "bambu", "Bambu Lab A1 0.4 nozzle", 4, "AMS lite"),
    ("bambu-p1p", "Bambu Lab", "P1P", "bambu", "Bambu Lab P1P 0.4 nozzle", 16, "AMS"),
    ("bambu-p1s", "Bambu Lab", "P1S", "bambu", "Bambu Lab P1S 0.4 nozzle", 16, "AMS"),
    ("bambu-p2s", "Bambu Lab", "P2S", "bambu", "Bambu Lab P2S 0.4 nozzle", 16, "AMS"),
    ("bambu-x1c", "Bambu Lab", "X1 Carbon", "bambu", "Bambu Lab X1 Carbon 0.4 nozzle", 16, "AMS"),
    ("bambu-x1e", "Bambu Lab", "X1E", "bambu", "Bambu Lab X1E 0.4 nozzle", 16, "AMS"),
    ("bambu-h2s", "Bambu Lab", "H2S", "bambu", "Bambu Lab H2S 0.4 nozzle", 16, "AMS"),
    ("bambu-h2d", "Bambu Lab", "H2D", "bambu", "Bambu Lab H2D 0.4 nozzle", 16, "AMS"),

    ("prusa-mk4s", "Prusa", "MK4S", "prusa", "Original Prusa MK4S 0.4 nozzle", 1, ""),
    ("prusa-mk4s-mmu3", "Prusa", "MK4S + MMU3", "prusa", "Original Prusa MK4S MMU3 0.4 nozzle", 5, "MMU3"),
    ("prusa-coreone", "Prusa", "CORE One", "prusa", "Prusa CORE One 0.4 nozzle", 1, ""),
    ("prusa-coreone-mmu3", "Prusa", "CORE One + MMU3", "prusa", "Prusa CORE One MMU3 0.4 nozzle", 5, "MMU3"),
    ("prusa-xl", "Prusa", "XL (1 toolhead)", "prusa", "Original Prusa XL 0.4 nozzle", 1, ""),
    ("prusa-xl-2t", "Prusa", "XL 2 toolheads", "prusa", "Original Prusa XL - 2T 0.4 nozzle", 2, "toolheads"),
    ("prusa-xl-5t", "Prusa", "XL 5 toolheads", "prusa", "Original Prusa XL - 5T 0.4 nozzle", 5, "toolheads"),
    ("prusa-mk35", "Prusa", "MK3.5 / MK3.5S", "prusa", "Original Prusa MK3.5 & MK3.5S 0.4 nozzle", 1, ""),
    ("prusa-mk3s", "Prusa", "MK3S / MK3S+", "prusa", "Original Prusa i3 MK3S & MK3S+", 1, ""),
    ("prusa-mk3s-mmu3", "Prusa", "MK3S+ + MMU3", "prusa", "Original Prusa i3 MK3S & MK3S+ MMU3", 5, "MMU3"),
    ("prusa-mini", "Prusa", "MINI / MINI+", "prusa", "Original Prusa MINI & MINI+", 1, ""),

    ("creality-k1c", "Creality", "K1C", "orca", "Creality K1C 0.4 nozzle", 1, ""),
    ("creality-k1c-cfs", "Creality", "K1C + CFS", "orca", "Creality K1C_CFS-C 0.4 nozzle", 4, "CFS"),
    ("creality-k1max-cfs", "Creality", "K1 Max + CFS", "orca", "Creality K1 Max_CFS-C 0.4 nozzle", 4, "CFS"),
    ("creality-k2plus", "Creality", "K2 Plus", "orca", "Creality K2 Plus 0.4 nozzle", 16, "CFS"),
    ("creality-hi", "Creality", "Hi", "orca", "Creality Hi 0.4 nozzle", 4, "CFS"),
    ("creality-ender3v3", "Creality", "Ender-3 V3", "orca", "Creality Ender-3 V3 0.4 nozzle", 1, ""),
    ("creality-ender3v3ke", "Creality", "Ender-3 V3 KE", "orca", "Creality Ender-3 V3 KE 0.4 nozzle", 1, ""),
    ("creality-ender3v3se", "Creality", "Ender-3 V3 SE", "orca", "Creality Ender-3 V3 SE 0.4 nozzle", 1, ""),
    ("creality-ender3s1", "Creality", "Ender-3 S1", "orca", "Creality Ender-3 S1 0.4 nozzle", 1, ""),
    ("creality-ender3v2", "Creality", "Ender-3 V2", "orca", "Creality Ender-3 V2 0.4 nozzle", 1, ""),
    ("elegoo-cc", "Elegoo", "Centauri Carbon", "orca", "Elegoo Centauri Carbon 0.4 nozzle", 1, ""),
    ("elegoo-cc2", "Elegoo", "Centauri Carbon 2", "orca", "Elegoo Centauri Carbon 2 0.4 nozzle", 4, "CANVAS"),
    ("elegoo-neptune4pro", "Elegoo", "Neptune 4 Pro", "orca", "Elegoo Neptune 4 Pro 0.4 nozzle", 1, ""),
    ("anycubic-kobras1", "Anycubic", "Kobra S1", "orca", "Anycubic Kobra S1 0.4 nozzle", 4, "ACE Pro"),
    ("anycubic-kobra3", "Anycubic", "Kobra 3", "orca", "Anycubic Kobra 3 0.4 nozzle", 4, "ACE Pro"),
    ("anycubic-kobra2neo", "Anycubic", "Kobra 2 Neo", "orca", "Anycubic Kobra 2 Neo 0.4 nozzle", 1, ""),
    ("snapmaker-u1", "Snapmaker", "U1", "orca", "Snapmaker U1 (0.4 nozzle)", 4, "toolheads"),
    ("flashforge-ad5x", "Flashforge", "AD5X", "orca", "Flashforge AD5X 0.4 nozzle", 4, "IFS"),
    ("qidi-xplus4", "Qidi", "X-Plus 4", "orca", "Qidi X-Plus 4 0.4 nozzle", 4, "QIDI Box"),
    ("qidi-q1pro", "Qidi", "Q1 Pro", "orca", "Qidi Q1 Pro 0.4 nozzle", 1, ""),
    ("sovol-sv08", "Sovol", "SV08", "orca", "Sovol SV08 0.4 nozzle", 1, ""),
    ("sovol-sv06", "Sovol", "SV06", "orca", "Sovol SV06 0.4 nozzle", 1, ""),
    ("voron-24-350", "Voron", "2.4 350", "orca", "Voron 2.4 350 0.4 nozzle", 1, ""),
    ("voron-trident-300", "Voron", "Trident 300", "orca", "Voron Trident 300 0.4 nozzle", 1, ""),
]

# per-filament settings that the two-filament CLI export does not grow but projects store per filament
EXTRA_PER_FILAMENT = [
    "default_filament_colour", "filament_colour", "filament_ids", "filament_settings_id",
    "filament_self_index", "filament_map", "filament_map_2", "filament_nozzle_map",
    "filament_volume_map", "filament_is_mixed", "filament_mixed_components",
    "filament_mixed_gradient", "filament_mixed_gradient_curve", "filament_mixed_gradient_per_part",
    "filament_mixed_gradient_range", "filament_mixed_sublayer_ratios", "filament_change_length_nc",
    "extruder_colour", "filament_extruder_variant", "filament_multi_colour", "filament_colour_type",
]
META = {"name", "inherits", "from", "setting_id", "instantiation", "type", "version", "filament_id",
        "description", "compatible_printers", "compatible_printers_condition", "compatible_prints",
        "compatible_prints_condition", "renamed_from", "alias"}


def run(cmd: list[str], seconds: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=seconds)


# ---------------------------------------------------------------- Bambu Studio / OrcaSlicer
class JsonProfiles:
    def __init__(self, root: Path):
        self.root = root
        self.by_vendor: dict[str, dict[str, Path]] = {}
        for f in root.rglob("*.json"):
            rel = f.relative_to(root).parts
            if len(rel) < 2:
                continue
            try:
                d = json.loads(f.read_text())
            except Exception:
                continue
            if isinstance(d, dict) and "name" in d:
                self.by_vendor.setdefault(rel[0], {}).setdefault(d["name"], f)

    def find(self, name: str, vendor: str | None) -> Path:
        for v in [vendor, "OrcaFilamentLibrary", *self.by_vendor]:
            if v in self.by_vendor and name in self.by_vendor[v]:
                return self.by_vendor[v][name]
        raise KeyError(name)

    def resolve(self, name: str, vendor: str | None = None) -> dict:
        chain, n = [], name
        while n:
            f = self.find(n, vendor)
            vendor = f.relative_to(self.root).parts[0]
            d = json.loads(f.read_text())
            chain.append(d)
            n = d.get("inherits")
        merged: dict = {}
        for d in reversed(chain):
            merged.update(d)
        merged.pop("inherits", None)
        merged["name"], merged["from"] = name, "system"
        return merged

    def vendor_of(self, name: str) -> str:
        for v, names in self.by_vendor.items():
            if name in names and v != "OrcaFilamentLibrary":
                return v
        raise KeyError(name)


def export_project(slicer: str, tmp: Path, presets: dict, n_filaments: int) -> dict:
    exe = str(APPS[slicer] / "Contents/MacOS" / EXE[slicer])
    paths = {}
    for kind, d in presets.items():
        paths[kind] = tmp / f"{kind}.json"
        paths[kind].write_text(json.dumps(d))
    out = tmp / f"out{n_filaments}"
    out.mkdir(exist_ok=True)
    cube = tmp / "cube.stl"
    if not cube.exists():
        import trimesh
        trimesh.creation.box((20, 20, 5)).export(cube)
    r = run([exe, "--datadir", str(tmp / "dd"),
             "--load-settings", f"{paths['machine']};{paths['process']}",
             "--load-filaments", ";".join([str(paths["filament"])] * n_filaments),
             "--outputdir", str(out), "--export-3mf", "p.3mf", str(cube)])
    f = out / "p.3mf"
    if not f.exists():
        raise RuntimeError(f"{slicer} export failed: {r.stdout[-400:]} {r.stderr[-400:]}")
    with zipfile.ZipFile(f) as z:
        return json.loads(z.read("Metadata/project_settings.config"))


def pick_preset(profiles: JsonProfiles, vendor: str, kind: str, machine: str, prefer: tuple,
                ok) -> str | None:
    """A visible `kind` preset of the vendor that lists the machine as compatible and passes `ok`,
    choosing by the first name fragment in `prefer` that matches."""
    found = []
    for name, f in profiles.by_vendor.get(vendor, {}).items():
        if f.relative_to(profiles.root).parts[1] != kind:
            continue
        d = profiles.resolve(name, vendor)
        if d.get("instantiation") == "false" or machine not in d.get("compatible_printers", []):
            continue
        if ok(d):
            found.append(name)
    for frag in prefer:
        for name in sorted(found):
            if frag in name:
                return name
    return None


def build_json_printer(slicer: str, profiles: JsonProfiles, preset: str) -> tuple[dict, dict]:
    vendor = "BBL" if slicer == "bambu" else profiles.vendor_of(preset)
    machine = profiles.resolve(preset, vendor)
    process_name = pick_preset(profiles, vendor, "process", preset, ("Standard", "Optimal", "Quality", ""),
                               lambda d: str(d.get("layer_height")) == "0.2") or machine["default_print_profile"]
    filament_name = machine["default_filament_profile"][0]
    if "PLA" not in filament_name:
        filament_name = pick_preset(profiles, vendor, "filament", preset, ("Generic PLA", "PLA"),
                                    lambda d: d.get("filament_type") == ["PLA"]) or filament_name
    presets = {"machine": machine, "process": profiles.resolve(process_name, vendor),
               "filament": profiles.resolve(filament_name, vendor)}
    with tempfile.TemporaryDirectory() as td:
        one = export_project(slicer, Path(td), presets, 1)
        two = export_project(slicer, Path(td), presets, 2)
    per = sorted({k for k, v in one.items() if isinstance(v, list) and isinstance(two.get(k), list)
                  and len(two[k]) == 2 * len(v) and len(v) > 0}
                 | {k for k in EXTRA_PER_FILAMENT if isinstance(one.get(k), list) and len(one[k]) == 1})
    one["_per_filament_keys"] = per
    return one, {"process": process_name, "filament": filament_name}


def json_info(cfg: dict) -> dict:
    pts = [tuple(float(c) for c in p.lower().split("x")) for p in cfg["printable_area"]]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return {
        "bed": [min(xs), min(ys), max(xs), max(ys)],
        "height": float(cfg.get("printable_height", 250)),
        "extruders": len(cfg.get("nozzle_diameter", ["0.4"])),
        "layer_height": float(cfg.get("layer_height", 0.2)),
        "first_layer_height": float(cfg.get("initial_layer_print_height", 0.2)),
        "tower_width": float(cfg.get("prime_tower_width", 35)),
    }


# ---------------------------------------------------------------- PrusaSlicer
class IniBundle:
    def __init__(self, path: Path):
        parts = re.split(r"^\[([^\]]+)\]\s*$", path.read_text(), flags=re.M)
        self.sections: dict[str, dict[str, str]] = {}
        for name, body in zip(parts[1::2], parts[2::2]):
            kv = {}
            for line in body.splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.split("=", 1)
                    kv[k.strip()] = v.strip()
            self.sections[name] = kv

    def resolve(self, kind: str, name: str) -> dict:
        sec = self.sections[f"{kind}:{name}"]
        out: dict = {}
        for parent in filter(None, (p.strip() for p in sec.get("inherits", "").split(";"))):
            out.update(self.resolve(kind, parent))
        out.update(sec)
        out.pop("inherits", None)
        return out


def prusa_query(datadir: Path, printer: str) -> dict:
    exe = str(APPS["prusa"] / "Contents/MacOS/PrusaSlicer")
    out = datadir / "q.json"
    run([exe, "--datadir", str(datadir), "--printer-profile", printer,
         "--query-print-filament-profiles", "--output", str(out)])
    return json.loads(out.read_text())


def prusa_datadir(tmp: Path) -> Path:
    """A PrusaSlicer data dir with every PrusaResearch printer and filament installed."""
    src = APPS["prusa"] / "Contents/Resources/profiles"
    dd = tmp / "pdd"
    (dd / "vendor").mkdir(parents=True, exist_ok=True)
    for ext in ("ini", "idx"):
        shutil.copy(src / f"PrusaResearch.{ext}", dd / "vendor")
    bundle = IniBundle(src / "PrusaResearch.ini")
    models, fil = [], []
    for name, kv in bundle.sections.items():
        if name.startswith("printer_model:") and "variants" in kv:
            models.append(f"{name.split(':', 1)[1]} = {kv['variants']}")
        if name.startswith("filament:") and not name.startswith("filament:*"):
            fil.append(f"{name.split(':', 1)[1]} = 1")
    (dd / "PrusaSlicer.ini").write_text(
        "[vendor:PrusaResearch]\n" + "\n".join(models) + "\n\n[filaments]\n" + "\n".join(fil) + "\n")
    return dd


def prusa_defaults(tmp: Path) -> dict:
    exe = str(APPS["prusa"] / "Contents/MacOS/PrusaSlicer")
    ini = tmp / "defaults.ini"
    run([exe, "--datadir", str(tmp / "empty"), "--save", str(ini)])
    return {k.strip(): v.strip() for k, v in
            (line.split("=", 1) for line in ini.read_text().splitlines() if "=" in line and not line.startswith("#"))}


def build_prusa_printer(bundle: IniBundle, datadir: Path, defaults: dict, preset: str) -> tuple[dict, dict]:
    q = prusa_query(datadir, preset)
    prints = [p["name"] for p in q["print_profiles"]]
    process = next(p for pat in ("0.20mm SPEED @", "0.20mm QUALITY @", "0.20mm NORMAL @", "0.20mm")
                   for p in prints if p.startswith(pat))
    fils = next(p["filament_profiles"] for p in q["print_profiles"] if p["name"] == process)
    filament = next(f for pat in ("Prusament PLA @", "Prusament PLA", "Generic PLA @", "Generic PLA")
                    for f in fils if f.startswith(pat) and "Blend" not in f)
    cfg = dict(defaults)
    for kind, name in (("printer", preset), ("print", process), ("filament", filament)):
        for k, v in bundle.resolve(kind, name).items():
            if k not in META:
                cfg[k] = v
    cfg["printer_settings_id"], cfg["print_settings_id"] = preset, process
    cfg["filament_settings_id"] = f'"{filament}"'
    return cfg, {"process": process, "filament": filament}


def prusa_info(cfg: dict) -> dict:
    pts = [tuple(float(c) for c in p.split("x")) for p in cfg["bed_shape"].split(",")]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return {
        "bed": [min(xs), min(ys), max(xs), max(ys)],
        "height": float(cfg.get("max_print_height", 200)),
        "extruders": len(cfg["nozzle_diameter"].split(",")),
        "layer_height": float(cfg.get("layer_height", 0.2)),
        "first_layer_height": float(cfg.get("first_layer_height", 0.2)),
        "tower_width": float(cfg.get("wipe_tower_width", 60)),
    }


# ---------------------------------------------------------------- main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    only = set(filter(None, args.only.split(",")))
    OUT.mkdir(parents=True, exist_ok=True)
    cat_path = OUT / "catalog.json"
    catalog = {p["id"]: p for p in json.loads(cat_path.read_text())} if cat_path.exists() else {}

    json_profiles = {s: JsonProfiles(APPS[s] / "Contents/Resources/profiles") for s in ("bambu", "orca")}
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        bundle = IniBundle(APPS["prusa"] / "Contents/Resources/profiles/PrusaResearch.ini")
        datadir = prusa_datadir(tmp)
        defaults = prusa_defaults(tmp)
        for pid, brand, name, slicer, preset, slots, system in CATALOG:
            if only and pid not in only:
                continue
            try:
                if slicer == "prusa":
                    cfg, chosen = build_prusa_printer(bundle, datadir, defaults, preset)
                    info = prusa_info(cfg)
                else:
                    cfg, chosen = build_json_printer(slicer, json_profiles[slicer], preset)
                    info = json_info(cfg)
            except Exception as e:  # keep going; report at the end
                print(f"FAILED {pid}: {e}")
                continue
            (OUT / f"{pid}.json.gz").write_bytes(gzip.compress(json.dumps(cfg, sort_keys=True).encode(), 9))
            catalog[pid] = {"id": pid, "brand": brand, "name": name, "slicer": slicer, "preset": preset,
                            "slots": slots, "system": system, **chosen, **info}
            print(f"{pid:22s} {info['bed']} h={info['height']:.0f} ext={info['extruders']} "
                  f"lh={info['layer_height']}/{info['first_layer_height']} | {chosen['process']} | {chosen['filament']}")
    order = {c[0]: i for i, c in enumerate(CATALOG)}
    cat = sorted(catalog.values(), key=lambda p: order.get(p["id"], 999))
    cat_path.write_text(json.dumps(cat, indent=1))


if __name__ == "__main__":
    main()
