"""Generate 3MFs for several printers and slice each with its real slicer CLI.

Usage: python tests/slice_check.py <image> <outdir> [printer_id ...] [--mode relief|flat|layers]
       [--universal]   (slice the universal 3MF with the printer's presets loaded; PrusaSlicer
                        printers only: Bambu Studio / OrcaSlicer ignore its per-part extruders)

Checks that the slicer accepts the file, uses the printer's bed, and that the G-code contains a
tool change per extra filament (multi-material) or a colour change per swap (layers mode).
Slicer apps: BAMBU_STUDIO, ORCA_SLICER, PRUSA_SLICER env vars (default /Applications/<Name>.app).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from pipeline import printers  # noqa: E402
from pipeline.colors import quantize, rgb_to_hex  # noqa: E402
from pipeline.export import Swap, project_3mf_bytes, prusa_3mf_bytes  # noqa: E402
from pipeline.model import ModelSettings, build_parts, center_parts, swap_plan  # noqa: E402

APPS = {
    "bambu": Path(os.environ.get("BAMBU_STUDIO", "/Applications/BambuStudio.app")) / "Contents/MacOS/BambuStudio",
    "orca": Path(os.environ.get("ORCA_SLICER", "/Applications/OrcaSlicer.app")) / "Contents/MacOS/OrcaSlicer",
    "prusa": Path(os.environ.get("PRUSA_SLICER", "/Applications/PrusaSlicer.app")) / "Contents/MacOS/PrusaSlicer",
}


def make(image: bytes, pr: printers.Printer, mode: str, n_colors: int = 4, width: float = 80):
    q = quantize(image, width_mm=width, pixel_mm=0.4, n_colors=n_colors, remove_bg=False,
                 bg_tolerance=12, smoothing=1, min_region_mm2=1)
    hexes = [rgb_to_hex(c) for c in q.palette]
    names = [f"Color {i + 1}" for i in range(len(hexes))]
    s = ModelSettings(mode=mode, stepped=mode == "relief", layer_height=pr.layer_height,
                      first_layer_height=pr.first_layer_height)
    parts = build_parts(q, hexes, names, s)
    center_parts(parts)
    if mode == "layers":
        return parts, [parts[0].color], [Swap(*t) for t in swap_plan(parts, s)]
    return parts, hexes, []


def printer_args(pr: printers.Printer, out: Path, n_filaments: int) -> list[str]:
    """CLI options that select the printer's presets (as a user would in the slicer), for the
    universal 3MF, which carries no printer of its own."""
    out.mkdir(parents=True, exist_ok=True)
    if pr.slicer == "prusa":
        ini = out / "printer.ini"
        ini.write_text("".join(f"{k} = {v}\n" for k, v in printers.project_config(pr.id).items()))
        return ["--load", str(ini)]
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    import build_printers as bp
    entry = json.loads((printers.DIR / "catalog.json").read_text())
    entry = next(e for e in entry if e["id"] == pr.id)
    prof = bp.JsonProfiles(bp.APPS[pr.slicer] / "Contents/Resources/profiles")
    vendor = "BBL" if pr.slicer == "bambu" else prof.vendor_of(entry["preset"])
    paths = {}
    for kind, name in (("machine", entry["preset"]), ("process", entry["process"]), ("filament", entry["filament"])):
        paths[kind] = out / f"{kind}.json"
        paths[kind].write_text(json.dumps(prof.resolve(name, vendor)))
    return ["--load-settings", f"{paths['machine']};{paths['process']}",
            "--load-filaments", ";".join([str(paths["filament"])] * n_filaments)]


def slice_file(slicer: str, f: Path, out: Path, extra: list[str] = ()) -> str:
    """Slice and return the G-code text."""
    out.mkdir(parents=True, exist_ok=True)
    if slicer == "prusa":
        g = out / "out.gcode"
        r = subprocess.run([str(APPS["prusa"]), *extra, "--export-gcode", "--binary-gcode=0", "--output", str(g), str(f)],
                           capture_output=True, text=True, timeout=600)
        if not g.exists():
            raise RuntimeError(r.stdout[-600:] + r.stderr[-600:])
        return g.read_text(errors="replace")
    try:
        r = subprocess.run([str(APPS[slicer]), "--datadir", str(out / "dd"), *extra, "--slice", "1",
                            "--outputdir", str(out), "--export-3mf", "sliced.3mf", str(f)],
                           capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        # Bambu Studio's CLI hangs after writing the G-code when the project has pauses
        g = out / "plate_1.gcode"
        if g.exists() and "; EXECUTABLE_BLOCK_END" in (text := g.read_text(errors="replace")):
            return text
        raise
    res = json.loads((out / "result.json").read_text()) if (out / "result.json").exists() else {}
    if res.get("return_code", 0) != 0 or not (out / "sliced.3mf").exists():
        raise RuntimeError(f"return_code {res.get('return_code')}: {res.get('error_string')} "
                           + r.stdout[-400:])
    with zipfile.ZipFile(out / "sliced.3mf") as z:
        return z.read("Metadata/plate_1.gcode").decode(errors="replace")


def check(gcode: str, slicer: str) -> dict:
    tools = sorted(set(re.findall(r"^(?:M620 S|T)(\d+)\b", gcode, re.M)) - {"255", "1000"}, key=int)
    m600 = len(re.findall(r"^(?:M600|M400 U1|PAUSE|M601|M25)\b", gcode, re.M))
    bed = re.search(r"^; (?:bed_shape|printable_area) = (.*)$", gcode, re.M)
    t = re.search(r"^; (?:estimated printing time \(normal mode\)|model printing time|total estimated time)[^=:]*[=:] ?(.*)$",
                  gcode, re.M)
    return {"tools": tools, "M600": m600, "bed": bed.group(1)[:40] if bed else "?",
            "time": t.group(1).strip()[:30] if t else "?"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("outdir")
    ap.add_argument("printers", nargs="*")
    ap.add_argument("--mode", default="relief")
    ap.add_argument("--universal", action="store_true")
    a = ap.parse_args()
    image = Path(a.image).read_bytes()
    out = Path(a.outdir)
    ids = a.printers or list(printers.catalog())
    ok = True
    for pid in ids:
        pr = printers.get(pid)
        parts, colors, swaps = make(image, pr, a.mode)
        if a.universal:
            data = prusa_3mf_bytes(parts, colors, "test", printers.generic(*pr.size, pr.slots), swaps)
        else:
            data = project_3mf_bytes(parts, colors, "test", pr, swaps)
        f = out / f"{pid}_{a.mode}{'_u' if a.universal else ''}.3mf"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)
        slicer = pr.slicer
        try:
            extra = printer_args(pr, out / f.stem, len(colors)) if a.universal else []
            info = check(slice_file(slicer, f, out / f.stem, extra), slicer)
        except Exception as e:
            ok = False
            print(f"FAIL {pid:22s} {slicer:6s} {str(e)[:300]}")
            continue
        n_fil = len({p.filament for p in parts})
        want_tools = n_fil if a.mode != "layers" else 1
        want_m600 = len(swaps)
        good = info["M600"] >= want_m600 if a.mode == "layers" else len(info["tools"]) >= want_tools
        ok &= good
        print(f"{'ok  ' if good else 'BAD '} {pid:22s} {slicer:6s} filaments={n_fil} tools={info['tools']} "
              f"M600={info['M600']}/{want_m600} bed={info['bed']} time={info['time']}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
