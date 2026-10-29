"""Rebuild app/pipeline/p2s_project_settings.json from the installed Bambu Studio system presets.

Usage: python tools/build_p2s_template.py <cli_exported_project_settings.config>
The CLI export supplies the full key set; the P2S machine/process/filament presets (with their
`inherits` chains resolved) are layered on top so every value matches the real P2S presets.
"""
import json
import sys
from pathlib import Path

PROFILES = Path("/Applications/BambuStudio.app/Contents/Resources/profiles/BBL")
PRESETS = {
    "machine": "Bambu Lab P2S 0.4 nozzle",
    "process": "0.20mm Standard @BBL P2S",
    "filament": "Bambu PLA Basic @BBL P2S",
}
# Per-filament settings that are not part of the filament preset chain itself.
EXTRA_PER_FILAMENT = [
    "default_filament_colour", "filament_colour", "filament_ids", "filament_settings_id",
    "filament_self_index", "filament_map", "filament_map_2", "filament_nozzle_map",
    "filament_volume_map", "filament_is_mixed", "filament_mixed_components",
    "filament_mixed_gradient", "filament_mixed_gradient_curve", "filament_mixed_gradient_per_part",
    "filament_mixed_gradient_range", "filament_mixed_sublayer_ratios", "filament_change_length_nc",
    "enable_pressure_advance", "pressure_advance", "ironing_fan_speed",
    "first_x_layer_part_fan_speed", "pre_start_fan_time", "overhang_threshold_participating_cooling",
    "enable_overhang_bridge_fan",
]
META = {"name", "inherits", "from", "setting_id", "instantiation", "type", "version",
        "filament_id", "description", "compatible_printers", "compatible_printers_condition",
        "compatible_prints", "compatible_prints_condition"}


def resolve(kind: str, name: str) -> dict:
    index = {}
    for f in (PROFILES / kind).rglob("*.json"):
        try:
            index[json.loads(f.read_text()).get("name")] = f
        except Exception:
            pass
    chain = []
    while name:
        d = json.loads(index[name].read_text())
        chain.append(d)
        name = d.get("inherits")
    merged = {}
    for d in reversed(chain):
        merged.update(d)
    return merged


def main() -> None:
    base = json.loads(Path(sys.argv[1]).read_text())
    changed = []
    for kind, name in PRESETS.items():
        for k, v in resolve(kind, name).items():
            if k in META or k not in base:
                continue
            # per-nozzle-variant lists are stored collapsed (one value per filament) in projects;
            # the CLI export already holds the collapsed values, so keep those
            if isinstance(v, list) and isinstance(base[k], list) and len(v) != len(base[k]):
                continue
            if base[k] != v:
                changed.append(k)
            base[k] = v
    base["printer_settings_id"] = PRESETS["machine"]
    base["print_settings_id"] = PRESETS["process"]
    base["filament_settings_id"] = [PRESETS["filament"]]
    per_filament = sorted(
        k for k in (set(resolve("filament", PRESETS["filament"])) - META) | set(EXTRA_PER_FILAMENT)
        if isinstance(base.get(k), list) and len(base[k]) == 1)
    base["_per_filament_keys"] = per_filament  # read (and removed) by the 3MF writer
    out = Path(__file__).resolve().parents[1] / "app/pipeline/p2s_project_settings.json"
    out.write_text(json.dumps(base, indent=1, sort_keys=True))
    print(len(per_filament), "per-filament keys")
    print(f"{len(changed)} keys updated from presets -> {out}")
    print({k: base[k] for k in ("printable_area", "printable_height", "printer_model", "layer_height")})


if __name__ == "__main__":
    main()
