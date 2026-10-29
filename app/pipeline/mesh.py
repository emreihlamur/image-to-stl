"""Turn label masks into watertight extruded meshes.

Each colour region's outline is traced along exact pixel edges, then (optionally) replaced by the
shared smoothed outline from `outline.Outlines`, so neighbouring parts still meet exactly (no gaps,
no overlaps). The caps are triangulated with GEOS constrained Delaunay, which keeps the triangle
count low compared to one quad per pixel.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import shapely
from shapely.geometry import Point, Polygon

from .outline import Outlines

# Directions in model XY space.
PX, PY, NX, NY = 0, 1, 2, 3


@dataclass
class Mesh:
    vertices: np.ndarray = field(default_factory=lambda: np.zeros((0, 3)))
    faces: np.ndarray = field(default_factory=lambda: np.zeros((0, 3), np.int64))

    @staticmethod
    def concat(meshes: list["Mesh"]) -> "Mesh":
        meshes = [m for m in meshes if len(m.faces)]
        if not meshes:
            return Mesh()
        verts, faces, off = [], [], 0
        for m in meshes:
            verts.append(m.vertices)
            faces.append(m.faces + off)
            off += len(m.vertices)
        return Mesh(np.concatenate(verts), np.concatenate(faces))


def _boundary_edges(mask: np.ndarray):
    """Directed pixel-edge boundary segments with the region on the left (outer CCW, holes CW).

    Corner (i, j) of the pixel grid maps to model coordinates x = j, y = H - i.
    Returns arrays start_corner, end_corner, direction.
    """
    h, w = mask.shape
    p = np.pad(mask, 1)
    cw = w + 1
    cid = lambda i, j: i * cw + j  # noqa: E731
    starts, ends, dirs = [], [], []

    def add(sel, s, e, d):
        i, j = np.nonzero(sel)
        starts.append(cid(*s(i, j)))
        ends.append(cid(*e(i, j)))
        dirs.append(np.full(len(i), d))

    # pixel (i,j) corners: a=(i,j) b=(i,j+1) c=(i+1,j+1) d=(i+1,j)
    add(mask & ~p[:-2, 1:-1], lambda i, j: (i, j + 1), lambda i, j: (i, j), NX)          # top    b->a
    add(mask & ~p[2:, 1:-1], lambda i, j: (i + 1, j), lambda i, j: (i + 1, j + 1), PX)   # bottom d->c
    add(mask & ~p[1:-1, 2:], lambda i, j: (i + 1, j + 1), lambda i, j: (i, j + 1), PY)   # right  c->b
    add(mask & ~p[1:-1, :-2], lambda i, j: (i, j), lambda i, j: (i + 1, j), NY)          # left   a->d
    return np.concatenate(starts), np.concatenate(ends), np.concatenate(dirs)


def _trace_loops(mask: np.ndarray, collapse: bool = True) -> list[np.ndarray]:
    """Chain boundary edges into closed loops of corner ids (collinear points removed if collapse)."""
    starts, ends, dirs = _boundary_edges(mask)
    out_edges: dict[int, list[int]] = {}
    for k, s in enumerate(starts.tolist()):
        out_edges.setdefault(s, []).append(k)
    used = np.zeros(len(starts), bool)
    ends_l, dirs_l = ends.tolist(), dirs.tolist()
    loops = []
    for first in range(len(starts)):
        if used[first]:
            continue
        loop_pts, loop_dirs = [], []
        k = first
        while True:
            used[k] = True
            loop_pts.append(int(starts[k]))
            loop_dirs.append(dirs_l[k])
            v, d = ends_l[k], dirs_l[k]
            cands = [e for e in out_edges[v] if not used[e]]
            if not cands:
                break
            if len(cands) > 1:  # pinch vertex (diagonal pixels): take the left turn -> simple loops
                pref = {(d + 1) % 4: 0, d: 1, (d + 3) % 4: 2}
                cands.sort(key=lambda e: pref.get(dirs_l[e], 3))
            k = cands[0]
        # keep only corners where the direction changes
        n = len(loop_pts)
        keep = [loop_pts[i] for i in range(n) if not collapse or loop_dirs[i] != loop_dirs[i - 1]]
        if len(keep) >= 3:
            loops.append(np.array(keep, np.int64))
    return loops


def _loop_xy(loop: np.ndarray, h: int, w: int) -> np.ndarray:
    i, j = np.divmod(loop, w + 1)
    return np.stack([j, h - i], axis=1).astype(np.float64)


def _signed_area(xy: np.ndarray) -> float:
    x, y = xy[:, 0], xy[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def _collapse(xy: np.ndarray) -> np.ndarray:
    """Drop collinear points of an axis-aligned pixel loop."""
    d = np.roll(xy, -1, axis=0) - xy
    return xy[np.any(d != np.roll(d, 1, axis=0), axis=1)]


def mask_to_polygons(mask: np.ndarray, outlines: Outlines | None = None
                     ) -> list[tuple[np.ndarray, list[np.ndarray]]]:
    """Return [(outer_xy, [hole_xy, ...])] in pixel units; outer CCW, holes CW.

    With `outlines`, rings use the shared smoothed vertices; a polygon whose smoothed rings would
    be invalid (rare, very thin features) falls back to its pixel-exact outline."""
    h, w = mask.shape
    outers, holes = [], []
    for loop in _trace_loops(mask, collapse=outlines is None):
        if outlines is None:
            raw = smooth = _loop_xy(loop, h, w)
        else:
            smooth, raw = outlines.ring(loop)
            raw = _collapse(raw)
        area = _signed_area(raw)
        (outers if area > 0 else holes).append((raw, smooth, abs(area)))
    shapes = [Polygon(raw) for raw, _, _ in outers]
    result = [(o, []) for o in outers]
    for hole in holes:
        pt = Polygon(hole[0]).representative_point()
        best, best_area = None, np.inf
        for idx, ((_, _, oarea), shp) in enumerate(zip(outers, shapes)):
            if hole[2] < oarea < best_area and shp.contains(Point(pt.x, pt.y)):
                best, best_area = idx, oarea
        if best is not None:
            result[best][1].append(hole)
    polys = []
    for outer, hs in result:
        smooth = (outer[1], [hh[1] for hh in hs])
        if outlines is not None and not Polygon(*smooth).is_valid:
            polys.append((outer[0], [hh[0] for hh in hs]))
        else:
            polys.append(smooth)
    return polys


def extrude_polygon(outer: np.ndarray, holes: list[np.ndarray], z0: float, z1: float,
                    scale: float, offset: tuple[float, float]) -> Mesh:
    rings = [outer] + holes
    verts2d = np.concatenate(rings)
    # GEOS returns the input coordinates unchanged, so triangle corners map back to ring indices.
    index = {pt: k for k, pt in enumerate(map(tuple, verts2d.tolist()))}
    cdt = shapely.get_parts(shapely.constrained_delaunay_triangles(Polygon(outer, holes)))
    if len(cdt) == 0:
        return Mesh()
    corners = shapely.get_coordinates(shapely.get_exterior_ring(cdt)).reshape(len(cdt), 4, 2)[:, :3]
    tris = np.array([index[pt] for pt in map(tuple, corners.reshape(-1, 2).tolist())],
                    np.int64).reshape(-1, 3)
    # orient caps: top CCW (+z), bottom CW (-z)
    a, b, c = verts2d[tris[:, 0]], verts2d[tris[:, 1]], verts2d[tris[:, 2]]
    cross = (b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])
    tris[cross < 0] = tris[cross < 0][:, ::-1]
    n = len(verts2d)
    xy = verts2d * scale + np.asarray(offset)
    vertices = np.concatenate([np.column_stack([xy, np.full(n, z0)]),
                               np.column_stack([xy, np.full(n, z1)])])
    faces = [tris + n, tris[:, ::-1]]
    # side walls: region is left of each ring edge, so the outward side is on the right
    start = 0
    for r in rings:
        idx = np.arange(start, start + len(r))
        nxt = np.roll(idx, -1)
        faces.append(np.column_stack([idx, nxt, nxt + n]))
        faces.append(np.column_stack([idx, nxt + n, idx + n]))
        start += len(r)
    return Mesh(vertices, np.concatenate(faces))


def extrude_mask(mask: np.ndarray, z0: float, z1: float, pixel_mm: float,
                 outlines: Outlines | None = None) -> Mesh:
    """Extrude a boolean mask between z0 and z1; model origin at the image's bottom-left."""
    if not mask.any() or z1 <= z0:
        return Mesh()
    return Mesh.concat([extrude_polygon(o, hs, z0, z1, pixel_mm, (0.0, 0.0))
                        for o, hs in mask_to_polygons(mask, outlines)])
