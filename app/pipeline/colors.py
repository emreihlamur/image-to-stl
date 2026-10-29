"""Image loading, background removal, colour quantisation and region clean-up.

Everything works on a pixel grid where one pixel = `pixel_mm` millimetres of the final print,
so all clean-up thresholds are expressed in real-world units.
"""
from __future__ import annotations

import io
from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image, ImageOps

# Bambu Lab PLA Basic colours (approximate hex values) used by "snap to filament".
BAMBU_PLA_BASIC = {
    "Jade White": "#FFFFFF",
    "Black": "#000000",
    "Gray": "#8E9089",
    "Silver": "#A6A9AA",
    "Red": "#C12E1F",
    "Magenta": "#EC008C",
    "Pink": "#F55A74",
    "Orange": "#FF6A13",
    "Yellow": "#F4EE2A",
    "Gold": "#E4BD68",
    "Bambu Green": "#00AE42",
    "Mistletoe Green": "#3F8E43",
    "Cyan": "#0086D6",
    "Blue": "#0A2989",
    "Purple": "#5E43B7",
    "Brown": "#9D432C",
    "Beige": "#F7E6DE",
    "Cocoa Brown": "#6F5034",
}

NO_LABEL = -1  # background / transparent pixels


def hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def rgb_to_hex(rgb) -> str:
    r, g, b = (int(round(float(c))) for c in rgb)
    return f"#{r:02X}{g:02X}{b:02X}"


def to_lab(rgb: np.ndarray) -> np.ndarray:
    """uint8 RGB (..., 3) -> float32 CIE Lab (..., 3)."""
    shape = rgb.shape
    flat = rgb.reshape(-1, 1, 3).astype(np.float32) / 255.0
    return cv2.cvtColor(flat, cv2.COLOR_RGB2LAB).reshape(shape)


@dataclass
class Quantized:
    labels: np.ndarray        # (H, W) int, NO_LABEL outside the shape
    palette: np.ndarray       # (K, 3) uint8 RGB
    pixel_mm: float

    @property
    def height_px(self) -> int:
        return self.labels.shape[0]

    @property
    def width_px(self) -> int:
        return self.labels.shape[1]


def load_image(data: bytes, width_mm: float, pixel_mm: float) -> tuple[np.ndarray, np.ndarray]:
    """Decode, fix orientation and resample so 1 pixel == pixel_mm. Returns (rgb, alpha_mask)."""
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img).convert("RGBA")
    w_px = max(8, int(round(width_mm / pixel_mm)))
    h_px = max(8, int(round(w_px * img.height / img.width)))
    img = img.resize((w_px, h_px), Image.LANCZOS)
    arr = np.asarray(img)
    return arr[..., :3].copy(), arr[..., 3] > 127


def remove_background(rgb: np.ndarray, mask: np.ndarray, tolerance: float) -> np.ndarray:
    """Drop the region connected to the image border whose colour matches the border colour."""
    border = np.concatenate([rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]])
    bg_lab = to_lab(np.median(border, axis=0).astype(np.uint8)[None, None])[0, 0]
    dist = np.linalg.norm(to_lab(rgb) - bg_lab, axis=-1)
    candidate = ((dist < tolerance) & mask).astype(np.uint8)
    n, comp = cv2.connectedComponents(candidate, connectivity=4)
    edge_ids = np.unique(np.concatenate([comp[0], comp[-1], comp[:, 0], comp[:, -1]]))
    edge_ids = edge_ids[edge_ids != 0]
    return mask & ~np.isin(comp, edge_ids)


def kmeans_palette(rgb: np.ndarray, mask: np.ndarray, k: int) -> np.ndarray:
    """K-means in Lab space on the visible pixels. Returns (K, 3) uint8 RGB."""
    lab = to_lab(rgb)[mask]
    if len(lab) > 60000:  # subsample for speed, the palette barely changes
        lab = lab[np.random.default_rng(0).choice(len(lab), 60000, replace=False)]
    k = max(1, min(k, len(np.unique(lab.round(1), axis=0))))
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 50, 0.2)
    cv2.setRNGSeed(42)
    _, _, centers = cv2.kmeans(lab.astype(np.float32), k, None, criteria, 4, cv2.KMEANS_PP_CENTERS)
    centers_rgb = cv2.cvtColor(centers.reshape(-1, 1, 3), cv2.COLOR_LAB2RGB).reshape(-1, 3)
    return np.clip(centers_rgb * 255.0 + 0.5, 0, 255).astype(np.uint8)


def assign_labels(rgb: np.ndarray, mask: np.ndarray, palette: np.ndarray) -> np.ndarray:
    lab = to_lab(rgb)
    pal_lab = to_lab(palette[None])[0]
    d = np.linalg.norm(lab[:, :, None, :] - pal_lab[None, None], axis=-1)
    labels = d.argmin(axis=-1).astype(np.int32)
    labels[~mask] = NO_LABEL
    return labels


def majority_smooth(labels: np.ndarray, k: int, radius: int) -> np.ndarray:
    """Mode filter: each pixel takes the most common label in its neighbourhood (shape kept)."""
    if radius <= 0:
        return labels
    size = 2 * radius + 1
    votes = np.stack([cv2.blur((labels == i).astype(np.float32), (size, size)) for i in range(k)])
    out = votes.argmax(axis=0).astype(np.int32)
    out[labels == NO_LABEL] = NO_LABEL
    return out


def remove_small_regions(labels: np.ndarray, min_px: int, max_passes: int = 4) -> np.ndarray:
    """Merge connected regions smaller than min_px into their most common neighbouring colour."""
    if min_px <= 1:
        return labels
    labels = labels.copy()
    kernel = np.ones((3, 3), np.uint8)
    for _ in range(max_passes):
        changed = False
        for lab in np.unique(labels):
            if lab == NO_LABEL:
                continue
            n, comp, stats, _ = cv2.connectedComponentsWithStats(
                (labels == lab).astype(np.uint8), connectivity=4)
            for c in np.nonzero(stats[1:, cv2.CC_STAT_AREA] < min_px)[0] + 1:
                x, y, w, h = stats[c, :4]
                y0, y1 = max(0, y - 1), min(labels.shape[0], y + h + 1)
                x0, x1 = max(0, x - 1), min(labels.shape[1], x + w + 1)
                region = comp[y0:y1, x0:x1] == c
                ring = cv2.dilate(region.astype(np.uint8), kernel).astype(bool) & ~region
                neigh = labels[y0:y1, x0:x1][ring]
                neigh = neigh[neigh != NO_LABEL]
                if len(neigh):
                    labels[y0:y1, x0:x1][region] = np.bincount(neigh).argmax()
                    changed = True
        if not changed:
            break
    return labels


def remove_thin(labels: np.ndarray, min_width_px: int) -> np.ndarray:
    """Remove features narrower than min_width_px (e.g. anti-aliasing slivers along edges, lines
    thinner than the nozzle): each label is morphologically opened, and pixels that no label
    keeps are given to the nearest kept label."""
    if min_width_px < 2:
        return labels
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (min_width_px, min_width_px))
    kept = np.zeros(labels.shape, bool)
    for lab in np.unique(labels):
        if lab == NO_LABEL:
            continue
        m = (labels == lab).astype(np.uint8)
        kept |= cv2.morphologyEx(m, cv2.MORPH_OPEN, kernel).astype(bool)
    kept |= labels == NO_LABEL  # background is never removed here
    if kept.all():
        return labels
    # nearest kept pixel for every orphan
    _, nearest = cv2.distanceTransformWithLabels((~kept).astype(np.uint8), cv2.DIST_L2, 5,
                                                 labelType=cv2.DIST_LABEL_PIXEL)
    ys, xs = np.nonzero(kept)
    lut = np.empty(nearest.max() + 1, np.int64)
    lut[nearest[ys, xs]] = labels[ys, xs]
    out = labels.copy()
    out[~kept] = lut[nearest[~kept]]
    return out


def remove_pinches(labels: np.ndarray, max_iter: int = 50) -> np.ndarray:
    """Remove 2x2 checkerboard patterns where a colour touches itself only diagonally.

    Those corner contacts become non-manifold edges in the mesh; growing one side by a pixel
    gives every part a clean, manifold outline. Works for label maps and boolean masks.
    """
    labels = labels.copy()
    for _ in range(max_iter):
        a, b = labels[:-1, :-1], labels[:-1, 1:]
        c, d = labels[1:, :-1], labels[1:, 1:]
        p1 = (a == d) & (b != a) & (c != a)
        if p1.any():
            i, j = np.nonzero(p1)
            labels[i, j + 1] = labels[i, j]
            continue
        p2 = (b == c) & (a != b) & (d != b)
        if not p2.any():
            break
        i, j = np.nonzero(p2)
        labels[i, j] = labels[i, j + 1]
    return labels


def drop_unused(labels: np.ndarray, palette: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Remove palette entries with no pixels left and sort the palette dark -> light."""
    used = [i for i in range(len(palette)) if np.any(labels == i)]
    lum = [0.2126 * palette[i][0] + 0.7152 * palette[i][1] + 0.0722 * palette[i][2] for i in used]
    order = [used[i] for i in np.argsort(lum)]
    remap = np.full(len(palette) + 1, NO_LABEL, np.int32)
    for new, old in enumerate(order):
        remap[old] = new
    out = np.where(labels == NO_LABEL, NO_LABEL, remap[labels])
    return out.astype(np.int32), palette[order]


def snap_to_filaments(palette: np.ndarray) -> list[tuple[str, str]]:
    """Closest distinct Bambu PLA Basic colour for each palette entry -> [(name, hex)]."""
    names = list(BAMBU_PLA_BASIC)
    fil_lab = to_lab(np.array([hex_to_rgb(BAMBU_PLA_BASIC[n]) for n in names], np.uint8)[None])[0]
    pal_lab = to_lab(palette[None])[0]
    d = np.linalg.norm(pal_lab[:, None] - fil_lab[None], axis=-1)
    taken: set[int] = set()
    result: list[tuple[str, str] | None] = [None] * len(palette)
    # Greedy: most confident matches first, every filament used at most once.
    for flat in np.argsort(d, axis=None):
        p, f = divmod(int(flat), len(names))
        if result[p] is None and f not in taken:
            result[p] = (names[f], BAMBU_PLA_BASIC[names[f]])
            taken.add(f)
    return [r if r else ("Custom", rgb_to_hex(palette[i])) for i, r in enumerate(result)]


def quantize(data: bytes, *, width_mm: float, pixel_mm: float, n_colors: int,
             remove_bg: bool, bg_tolerance: float, smoothing: int, min_region_mm2: float,
             min_width_mm: float = 0.45,
             custom_palette: list[str] | None = None) -> Quantized:
    rgb, mask = load_image(data, width_mm, pixel_mm)
    if remove_bg:
        mask = remove_background(rgb, mask, bg_tolerance)
    if not mask.any():
        raise ValueError("Nothing left after background removal; lower the tolerance or turn it off.")
    if smoothing > 0:  # edge-preserving denoise before clustering
        rgb = cv2.bilateralFilter(rgb, d=5, sigmaColor=20 + 15 * smoothing, sigmaSpace=5)

    if custom_palette:
        palette = np.array([hex_to_rgb(h) for h in custom_palette], np.uint8)
    else:
        palette = kmeans_palette(rgb, mask, n_colors)
    labels = assign_labels(rgb, mask, palette)
    labels = majority_smooth(labels, len(palette), smoothing)
    labels = remove_thin(labels, int(np.ceil(min_width_mm / pixel_mm - 1e-6)))
    labels = remove_small_regions(labels, int(round(min_region_mm2 / (pixel_mm ** 2))))
    labels = remove_pinches(labels)
    if custom_palette:  # keep the user's order and colours exactly
        return Quantized(labels, palette, pixel_mm)
    labels, palette = drop_unused(labels, palette)
    return Quantized(labels, palette, pixel_mm)


def render_preview(q: Quantized, hexes: list[str], scale: int = 1) -> bytes:
    h, w = q.labels.shape
    img = np.zeros((h, w, 4), np.uint8)
    for i, hx in enumerate(hexes):
        img[q.labels == i] = (*hex_to_rgb(hx), 255)
    pil = Image.fromarray(img, "RGBA")
    if scale > 1:
        pil = pil.resize((w * scale, h * scale), Image.NEAREST)
    buf = io.BytesIO()
    pil.save(buf, "PNG")
    return buf.getvalue()
