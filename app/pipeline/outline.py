"""Smooth, gap-free outlines for all colour regions at once.

All colour borders form one planar graph on the pixel-corner grid. The graph's vertices are
smoothed together: every vertex relaxes towards its neighbours (Laplacian), junctions where
three or more regions meet a bit less, and no vertex may drift more than `max_shift` pixels
from its original corner. The chains are then simplified (Douglas-Peucker) with the same keep
decision on both sides. Every region outline is assembled from these shared vertices, so
neighbouring parts still meet exactly: the staircase becomes a curve, with no gaps or overlaps.
"""
from __future__ import annotations

import numpy as np

# (iterations, max shift in px) per edge-smoothing level; level 0 = pixel-exact
LEVELS = {0: (0, 0.0), 1: (8, 0.5), 2: (30, 1.0), 3: (60, 1.5)}
# a turn between two straight pixel runs at least this long is a real corner and stays sharp
CORNER_RUN_PX = 3


def _straight_run(hz: np.ndarray, vt: np.ndarray, i: np.ndarray, j: np.ndarray,
                  di: np.ndarray, dj: np.ndarray, n: int) -> np.ndarray:
    """True where n consecutive boundary edges continue from corner (i, j) in direction (di, dj)."""
    hzp = np.pad(hz, n + 1)
    vtp = np.pad(vt, n + 1)
    ok = np.ones(len(i), bool)
    for k in range(n):
        ci, cj = i + k * di + n + 1, j + k * dj + n + 1  # current corner in padded coords
        horiz = di == 0
        e_h = hzp[ci, np.where(dj > 0, cj, cj - 1)]
        e_v = vtp[np.where(di > 0, ci, ci - 1), cj]
        ok &= np.where(horiz, e_h, e_v)
    return ok


class Outlines:
    def __init__(self, labels: np.ndarray, level: int = 2, simplify_tol: float = 0.08):
        self.h, self.w = labels.shape
        h, w = self.h, self.w
        cw = w + 1
        lp = np.pad(labels, 1, constant_values=np.iinfo(np.int32).min)
        hz = lp[0:h + 1, 1:w + 1] != lp[1:h + 2, 1:w + 1]   # edge (i,j)-(i,j+1), shape (h+1, w)
        vt = lp[1:h + 1, 0:w + 1] != lp[1:h + 1, 1:w + 2]   # edge (i,j)-(i+1,j), shape (h, w+1)
        hi, hj = np.nonzero(hz)
        vi, vj = np.nonzero(vt)
        u = np.concatenate([hi * cw + hj, vi * cw + vj])
        v = np.concatenate([hi * cw + hj + 1, (vi + 1) * cw + vj])

        n_corners = (h + 1) * cw
        ci, cj = np.divmod(np.arange(n_corners), cw)
        self.pos = np.stack([cj, h - ci], axis=1).astype(np.float64)
        self.keep = np.ones(n_corners, bool)

        iters, max_shift = LEVELS.get(int(level), LEVELS[2])
        if iters == 0 or len(u) == 0:
            return
        deg = np.bincount(np.concatenate([u, v]), minlength=n_corners)
        src = np.concatenate([u, v])
        dst = np.concatenate([v, u])
        order = np.argsort(src, kind="stable")
        src, dst = src[order], dst[order]
        start = np.searchsorted(src, np.arange(n_corners))
        # real corners: deg-2 turns between two long straight runs (text, rectangles)
        chain = np.nonzero(deg == 2)[0]
        a, b = dst[start[chain]], dst[start[chain] + 1]
        ii, jj = ci[chain], cj[chain]
        dai, daj = ci[a] - ii, cj[a] - jj
        dbi, dbj = ci[b] - ii, cj[b] - jj
        turn = (dai + dbi != 0) | (daj + dbj != 0)
        sharp = np.zeros(n_corners, bool)
        sharp[chain] = turn & _straight_run(hz, vt, ii, jj, dai, daj, CORNER_RUN_PX) \
            & _straight_run(hz, vt, ii, jj, dbi, dbj, CORNER_RUN_PX)

        verts = np.nonzero(deg >= 2)[0]
        seg = start[verts]
        # junctions (3+ regions meet) move less so small features keep their shape; corners stay
        limit = np.where(deg[verts] == 2, max_shift, 0.5 * max_shift)
        limit = np.where(sharp[verts], 0.0, limit)[:, None]
        p0 = self.pos[verts].copy()
        pos = self.pos
        for _ in range(iters):
            target = np.add.reduceat(pos[dst], seg) / deg[verts][:, None]
            d = 0.5 * (pos[verts] + target) - p0
            dist = np.linalg.norm(d, axis=1, keepdims=True)
            pos[verts] = p0 + d * np.minimum(1.0, limit / np.maximum(dist, 1e-12))
        self._simplify(deg, sharp, start, dst, simplify_tol)

    # ---------- simplification ----------
    def _simplify(self, deg, sharp, start, dst, tol):
        """Drop chain vertices that Douglas-Peucker deems redundant; decided once per chain."""
        anchor = (deg != 2) | sharp
        self.keep = anchor.copy()  # junctions, corners (and unused grid corners) always kept
        visited = np.zeros(len(deg), bool)
        nb = lambda x: (int(dst[start[x]]), int(dst[start[x] + 1]))  # noqa: E731  (deg-2 only)

        def walk(prev, cur):
            path = [prev]
            while not anchor[cur] and not visited[cur]:
                visited[cur] = True
                path.append(cur)
                a, b = nb(cur)
                prev, cur = cur, (b if a == prev else a)
            path.append(cur)
            return path

        for j in np.nonzero(anchor & (deg > 0))[0]:
            for k in range(int(deg[j])):
                n = int(dst[start[j] + k])
                if not anchor[n] and not visited[n]:
                    self._dp(walk(int(j), n), tol)
        # closed loops with no junction
        for c in np.nonzero(~anchor & ~visited)[0]:
            if visited[c]:
                continue
            visited[c] = True
            a, _ = nb(c)
            path = walk(int(c), a)  # ends back at c
            self.keep[c] = True
            if len(path) > 4:
                far = 1 + int(np.argmax(np.linalg.norm(self.pos[path[1:-1]] - self.pos[c], axis=1)))
                self.keep[path[far]] = True
                self._dp(path[:far + 1], tol)
                self._dp(path[far:], tol)

    def _dp(self, path, tol):
        pts = self.pos[path]
        keep = np.zeros(len(path), bool)
        keep[0] = keep[-1] = True
        stack = [(0, len(path) - 1)]
        while stack:
            a, b = stack.pop()
            if b - a < 2:
                continue
            seg = pts[b] - pts[a]
            rel = pts[a + 1:b] - pts[a]
            ln = np.hypot(*seg)
            d = np.abs(seg[0] * rel[:, 1] - seg[1] * rel[:, 0]) / ln if ln > 1e-12 \
                else np.hypot(rel[:, 0], rel[:, 1])
            m = int(np.argmax(d))
            if d[m] > tol:
                keep[a + 1 + m] = True
                stack += [(a, a + 1 + m), (a + 1 + m, b)]
        idx = np.asarray(path)
        self.keep[idx[1:-1]] = keep[1:-1]

    # ---------- per-region output ----------
    def ring(self, corner_ids: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Smoothed coordinates of a traced loop (all corners), and the same loop pixel-exact."""
        ids = corner_ids[self.keep[corner_ids]]
        if len(ids) < 3:
            ids = corner_ids
        i, j = np.divmod(corner_ids, self.w + 1)
        raw = np.stack([j, self.h - i], axis=1).astype(np.float64)
        return self.pos[ids], raw
