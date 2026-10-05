# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Pesi (pesi3d.de)
"""Grading — fill the open ground between edges with smooth, organic terrain,
for IngeTrazo.

Select the **edges that bound the open ground** — the edge of a path, a
terrace, a platform, a ramp (loose edges and/or groups of edges) — and run
**Grading ▸ Fill from Edges…**. The edges may lie at different heights; the
ground between them is filled with a terrain that meets every edge exactly.

* **Smoothness** 100 % lets the ground run out level from every edge and
  rounds the top and the toe of each slope — it looks laid out by hand.
  0 % gives straight slopes with a crease at the edges.
* The **largest closed loop** (seen from above) is the outer contour;
  closed loops inside it are inner contours — **Holes** (a platform or a
  bed the ground stops at) or **Fixed lines** (the ground runs on past
  them). **Open edge chains** inside are fixed lines (a swale, a ridge),
  selected **guide points** (Tape Measure) are spot heights.

The result is ONE group that remembers its source and settings — select it
and run **Edit Grading…** to change it. Every run is one undo step; the Live
Preview shows the result in the model while the dialog stays open (the
viewport can still be orbited).

Install: copy this file into the plugins folder
(Extensions ▸ Open plugins folder; on Windows %APPDATA%\\ingetrazo\\plugins)
and restart IngeTrazo. Needs IngeTrazo ≥ 0.5 (extension API 2).
"""
from __future__ import annotations

import json
import math
import time

KEY = "grading_tool"
TITLE = "Grading"
VERSION = "1.0"
SETTINGS_KEY = "plugins/grading_tool/params"
MIN_DIV = 4
MAX_DIV = 250
MAX_TRIANGLES = 150_000        # pure-Python Delaunay + solver: keep it sane
WARN_TRIANGLES = 40_000
EPS = 1e-12


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------

def default_params() -> dict:
    return {
        "res_mode": "div",        # "div" | "size"
        "div": 60,                # divisions on the long side
        "size": 0.5,              # metres (mesh size)
        "smooth": 100.0,          # %  0 = straight slopes, 100 = rounded
        "inner": "holes",         # "holes" | "lines"
        "soft": True,             # soft edges (smooth look)
        "flip": False,            # flip the faces
        "follow": True,           # dialog: read the selection again on change
        "force": False,           # build over the safe limit (never saved)
    }


def normalize_params(p) -> dict:
    out = default_params()
    if isinstance(p, dict):
        for k, v in p.items():
            if k in out:
                out[k] = v
    try:
        out["div"] = int(min(MAX_DIV, max(MIN_DIV, int(out["div"]))))
        out["size"] = float(max(1e-4, float(out["size"])))
        out["smooth"] = float(min(100.0, max(0.0, float(out["smooth"]))))
    except (TypeError, ValueError):
        return default_params()
    if out["res_mode"] not in ("div", "size"):
        out["res_mode"] = "div"
    if out["inner"] not in ("holes", "lines"):
        out["inner"] = "holes"
    for k in ("soft", "flip", "force", "follow"):
        out[k] = bool(out[k])
    return out


def load_params() -> dict:
    try:
        from PySide6.QtCore import QSettings
        raw = QSettings().value(SETTINGS_KEY, "")
        if raw:
            return normalize_params(json.loads(raw))
    except Exception:  # noqa: BLE001
        pass
    return default_params()


def save_params(p: dict) -> None:
    try:
        from PySide6.QtCore import QSettings
        p = {k: v for k, v in p.items() if k != "force"}
        QSettings().setValue(SETTINGS_KEY, json.dumps(p))
    except Exception:  # noqa: BLE001
        pass


class GradingError(Exception):
    """A user-facing refusal."""


# ---------------------------------------------------------------------------
# Source data from the selection
# ---------------------------------------------------------------------------

def _xyz(p) -> tuple:
    return (float(p.x()), float(p.y()), float(p.z()))


def grading_data(group):
    data = (getattr(group, "ext", None) or {}).get(KEY)
    return data if isinstance(data, dict) and "segs" in data else None


def _world_edges(group):
    from core.group import world_mesh
    try:
        return list(world_mesh(group).edges)
    except Exception:  # noqa: BLE001
        return list(group.mesh.edges)


def gather(scene):
    """``{"segs": [(x0,y0,z0,x1,y1,z1), …], "points": [(x,y,z), …]}`` from
    the selection: loose edges, every edge inside selected groups (a
    terrain made by this plugin is skipped) and guide points."""
    from core.group import Group
    from core.mesh import Edge
    try:
        from core.guide import Guide
    except Exception:  # noqa: BLE001
        Guide = None
    segs, pts = [], []
    seen = set()
    for ent in scene.selection:
        if Guide is not None and isinstance(ent, Guide):
            if not getattr(ent, "is_line", False):
                pts.append(_xyz(ent.point))
            continue
        if isinstance(ent, Edge):
            edges = [ent]
        elif isinstance(ent, Group) and grading_data(ent) is None:
            edges = _world_edges(ent)
        else:
            continue
        for e in edges:
            a, b = _xyz(e.a), _xyz(e.b)
            k = (a, b) if a <= b else (b, a)
            if k in seen or a == b:
                continue
            seen.add(k)
            segs.append(a + b)
    return {"segs": segs, "points": pts}


# ---------------------------------------------------------------------------
# Edges → polylines, joined in PLAN (seen from above)
# ---------------------------------------------------------------------------

def chain_segments(segs, tol=1e-4):
    """Join segments into polylines (lists of (x, y, z)) by their plan
    position; a closed loop repeats its first point at the end. Vertical
    edges (no length in plan) are dropped — where two edges meet at the
    same plan point at different heights, the height is averaged.
    Returns ``(lines, steps)``: steps = how many such height jumps."""
    def key(x, y):
        return (round(x / tol), round(y / tol))

    adj: dict = {}
    zs: dict = {}
    xy: dict = {}
    edges = []
    for s in segs:
        ka, kb = key(s[0], s[1]), key(s[3], s[4])
        if ka == kb:
            continue
        for k, q in ((ka, s[:3]), (kb, s[3:])):
            xy[k] = (q[0], q[1])
            zs.setdefault(k, []).append(q[2])
        i = len(edges)
        edges.append((ka, kb))
        adj.setdefault(ka, []).append(i)
        adj.setdefault(kb, []).append(i)
    steps = 0
    pos = {}
    for k, zz in zs.items():
        if max(zz) - min(zz) > 1e-6:
            steps += 1
        pos[k] = (xy[k][0], xy[k][1], sum(zz) / len(zz))
    used = [False] * len(edges)

    def walk(start_k, first_e):
        line = [start_k]
        k, e = start_k, first_e
        while e is not None:
            used[e] = True
            a, b = edges[e]
            k = b if a == k else a
            line.append(k)
            if len(adj[k]) != 2:
                break
            nxt = [j for j in adj[k] if not used[j]]
            e = nxt[0] if nxt else None
        return line

    lines = []
    for k, es in adj.items():
        if len(es) != 2:
            for e in es:
                if not used[e]:
                    lines.append(walk(k, e))
    for e in range(len(edges)):
        if not used[e]:
            lines.append(walk(edges[e][0], e))
    return [[pos[k] for k in ln] for ln in lines], steps


# ---------------------------------------------------------------------------
# Geometry helpers (numpy)
# ---------------------------------------------------------------------------

def _incircle_ok(cx, cy, r2, px, py):
    return (cx - px) ** 2 + (cy - py) ** 2 < r2

# Delaunay (Bowyer–Watson with a neighbour walk)

def delaunay(xy):
    """Delaunay triangulation of (n, 2) points → (m, 3) CCW index array.

    Bowyer–Watson with a neighbour map: each point is found by walking from
    the last new triangle (points inserted in a snake order over a grid, so
    the walk is short) and only the triangles around it are tested — about
    linear time instead of quadratic."""
    import numpy as np
    xy = np.asarray(xy, dtype=float)
    n = len(xy)
    if n < 3:
        raise GradingError("At least three points are needed.")
    mn, mx = xy.min(0), xy.max(0)
    span = float(max(mx - mn)) or 1.0
    c = (mn + mx) / 2
    P = ((xy - c) / span).tolist()
    big = 1.0e4
    P += [[-big, -big], [big, -big], [0.0, big]]
    X = [q[0] for q in P]
    Y = [q[1] for q in P]

    tri = []          # [a, b, c] CCW, or None when deleted
    cc = []           # (cx, cy, r2)
    edge = {}         # directed edge (a, b) -> triangle id

    def circ(a, b, c3):
        ax, ay, bx, by, cx_, cy_ = X[a], Y[a], X[b], Y[b], X[c3], Y[c3]
        d = 2.0 * (ax * (by - cy_) + bx * (cy_ - ay) + cx_ * (ay - by))
        if abs(d) < 1e-300:
            return (0.0, 0.0, math.inf)
        a2, b2, c2 = ax * ax + ay * ay, bx * bx + by * by, cx_ * cx_ + cy_ * cy_
        ux = (a2 * (by - cy_) + b2 * (cy_ - ay) + c2 * (ay - by)) / d
        uy = (a2 * (cx_ - bx) + b2 * (ax - cx_) + c2 * (bx - ax)) / d
        return (ux, uy, (ax - ux) ** 2 + (ay - uy) ** 2)

    def add(a, b, c3):
        t = len(tri)
        tri.append([a, b, c3])
        cc.append(circ(a, b, c3))
        edge[(a, b)] = t
        edge[(b, c3)] = t
        edge[(c3, a)] = t
        return t

    last = add(n, n + 1, n + 2)

    # Insertion order: snake over a √n × √n grid of buckets (locality).
    k = max(1, int(math.sqrt(n / 4.0)))
    gx = np.minimum((((xy[:, 0] - mn[0]) / (span or 1)) * k).astype(int), k - 1)
    gy = np.minimum((((xy[:, 1] - mn[1]) / (span or 1)) * k).astype(int), k - 1)
    gx = np.where(gy % 2 == 1, k - 1 - gx, gx)
    order = np.lexsort((xy[:, 0], gx, gy)).tolist()

    def orient(a, b, px, py):
        return (X[b] - X[a]) * (py - Y[a]) - (Y[b] - Y[a]) * (px - X[a])

    for i in order:
        px, py = X[i], Y[i]
        # 1. Walk to the triangle that holds the point.
        t = last
        if tri[t] is None:
            t = next(j for j in range(len(tri) - 1, -1, -1) if tri[j] is not None)
        steps = 0
        while True:
            a, b, c3 = tri[t]
            if orient(a, b, px, py) < 0:
                nt = edge.get((b, a))
            elif orient(b, c3, px, py) < 0:
                nt = edge.get((c3, b))
            elif orient(c3, a, px, py) < 0:
                nt = edge.get((a, c3))
            else:
                break
            steps += 1
            if nt is None or steps > 4 * len(tri) + 10:
                # Fallback (degenerate walk): any triangle whose circle
                # holds the point.
                nt = next(j for j, tt in enumerate(tri) if tt is not None
                          and _incircle_ok(*cc[j], px, py))
                t = nt
                break
            t = nt
        # 2. The cavity: triangles around it whose circle holds the point.
        bad = {t}
        stack = [t]
        while stack:
            u = stack.pop()
            a, b, c3 = tri[u]
            for e0, e1 in ((a, b), (b, c3), (c3, a)):
                v = edge.get((e1, e0))
                if v is not None and v not in bad:
                    cx_, cy_, r2 = cc[v]
                    if (cx_ - px) ** 2 + (cy_ - py) ** 2 < r2:
                        bad.add(v)
                        stack.append(v)
        # 3. Its rim, then re-triangulate as a fan to the point.
        rim = []
        for u in bad:
            a, b, c3 = tri[u]
            for e0, e1 in ((a, b), (b, c3), (c3, a)):
                if edge.get((e1, e0)) not in bad:
                    rim.append((e0, e1))
        for u in bad:
            a, b, c3 = tri[u]
            for e in ((a, b), (b, c3), (c3, a)):
                if edge.get(e) == u:
                    del edge[e]
            tri[u] = None
        for e0, e1 in rim:
            last = add(e0, e1, i)

    T = np.asarray([tt for tt in tri if tt is not None], dtype=np.int64)
    T = T[(T < n).all(1)]
    A, B, C = xy[T[:, 0]], xy[T[:, 1]], xy[T[:, 2]]
    cr = (B[:, 0] - A[:, 0]) * (C[:, 1] - A[:, 1]) - (B[:, 1] - A[:, 1]) * (C[:, 0] - A[:, 0])
    T[cr < 0] = T[cr < 0][:, [0, 2, 1]]
    return T[np.abs(cr) > span * span * 1e-14]


def poly_area(xy):
    import numpy as np
    x, y = xy[:, 0], xy[:, 1]
    return 0.5 * float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))


def inside_poly(poly, X, Y):
    """Even-odd point-in-polygon, vectorised over arrays X, Y."""
    import numpy as np
    X = np.asarray(X, dtype=float)
    Y = np.asarray(Y, dtype=float)
    res = np.zeros(X.shape, dtype=bool)
    px, py = poly[:, 0], poly[:, 1]
    qx, qy = np.roll(px, -1), np.roll(py, -1)
    for x0, y0, x1, y1 in zip(px.tolist(), py.tolist(), qx.tolist(), qy.tolist()):
        if y0 == y1:
            continue
        cond = (y0 > Y) != (y1 > Y)
        xi = x0 + (Y - y0) * (x1 - x0) / (y1 - y0)
        res ^= cond & (X < xi)
    return res


def resample(poly, closed, h):
    """Subdivide a 3D polyline so no piece is longer than ``h``; the
    original vertices (corners) are kept. A closed loop is returned
    without the repeated end point."""
    import numpy as np
    P = np.asarray(poly, dtype=float)
    if closed and len(P) > 1 and np.allclose(P[0], P[-1]):
        P = P[:-1]
    out = []
    m = len(P) if closed else len(P) - 1
    for i in range(m):
        a, b = P[i], P[(i + 1) % len(P)]
        k = max(1, int(math.ceil(float(np.linalg.norm(b - a)) / h - 1e-9)))
        for j in range(k):
            out.append(a + (b - a) * (j / k))
    if not closed:
        out.append(P[-1])
    return np.asarray(out, dtype=float)


def _min_dist2(A, B, chunk=2048):
    """Squared distance from every row of A (n, 2) to the nearest row of B."""
    import numpy as np
    if len(B) == 0:
        return np.full(len(A), np.inf)
    out = np.empty(len(A))
    for s in range(0, len(A), chunk):
        a = A[s:s + chunk]
        d = ((a[:, None, :] - B[None, :, :]) ** 2).sum(-1)
        out[s:s + chunk] = d.min(1)
    return out

# ---------------------------------------------------------------------------
# What the selection means: outer contour, inner contours, lines, points
# ---------------------------------------------------------------------------

def classify(data, p):
    """Source → a description of the domain (in plan, XY)."""
    import numpy as np
    segs = data.get("segs") or []
    lines, steps = chain_segments(segs) if segs else ([], 0)
    closed, opened = [], []
    for ln in lines:
        if len(ln) >= 4 and np.allclose(ln[0][:2], ln[-1][:2], atol=1e-6):
            closed.append(np.asarray(ln[:-1], dtype=float))
        elif len(ln) >= 2:
            opened.append(np.asarray(ln, dtype=float))
    if not closed:
        raise GradingError(
            "No closed contour in the selection — select edges that "
            "enclose the open ground all the way round (seen from above).")
    allp = np.vstack(closed)
    c = allp.mean(0)
    e1 = np.array([1.0, 0.0, 0.0])
    e2 = np.array([0.0, 1.0, 0.0])
    n = np.array([0.0, 0.0, 1.0])

    def to2(P):
        Q = np.asarray(P, dtype=float)
        return Q[..., :2] - c[:2]

    areas = [abs(poly_area(to2(L))) for L in closed]
    io = int(np.argmax(areas))
    if areas[io] < 1e-12:
        raise GradingError(
            "The contour encloses no area seen from above — the ground is "
            "filled in plan, so the edges must not lie in a vertical plane.")
    outer = closed[io]
    o2 = to2(outer)
    inner, ignored = [], 0
    for i, L in enumerate(closed):
        if i == io:
            continue
        l2 = to2(L)
        if inside_poly(o2, l2[:, 0], l2[:, 1]).all():
            inner.append(L)
        else:
            ignored += 1
    lines_in, pts_in = [], []
    for L in opened:
        l2 = to2(L)
        ok = inside_poly(o2, l2[:, 0], l2[:, 1])
        if ok.any():
            lines_in.append(L)
        else:
            ignored += 1
    pts = np.asarray(data.get("points") or [], dtype=float).reshape(-1, 3)
    if len(pts):
        p2 = to2(pts)
        ok = inside_poly(o2, p2[:, 0], p2[:, 1])
        ignored += int((~ok).sum())
        pts_in = pts[ok]
    else:
        pts_in = pts
    span = o2.max(0) - o2.min(0)
    long_side = float(span.max())
    area = areas[io]
    if p["inner"] == "holes":
        area -= sum(abs(poly_area(to2(L))) for L in inner)
    h = long_side / p["div"] if p["res_mode"] == "div" else p["size"]
    h = max(h, long_side / (MAX_DIV * 4))
    zparts = [L[:, 2] for L in [outer] + inner + lines_in]
    if len(pts_in):
        zparts.append(pts_in[:, 2])
    zall = np.concatenate(zparts)
    return {
        "plane": (c, e1, e2, n), "outer": outer, "inner": inner,
        "lines": lines_in, "points": pts_in, "ignored": ignored,
        "steps": steps, "h": h, "long": long_side,
        "area": max(area, 1e-12), "drop": float(zall.max() - zall.min()),
        "to2": to2,
    }


def estimate_triangles(dom):
    """Expected triangle count — cheap, for the dialog."""
    h = dom["h"]
    n_in = dom["area"] / (h * h * math.sqrt(3) / 2)
    rim = sum(len(resample(L, True, h)) for L in [dom["outer"]] + dom["inner"])
    return int(2 * n_in + rim)


def check_size(n, force=False):
    if n > MAX_TRIANGLES and not force:
        raise GradingError(
            f"About {n:,} triangles — over the safe limit of "
            f"{MAX_TRIANGLES:,}. Use fewer divisions.")


# ---------------------------------------------------------------------------
# The mesh: a triangular lattice inside, the constraints resampled, Delaunay
# made conforming by splitting the boundary where an edge is missing
# ---------------------------------------------------------------------------

def build_mesh(dom, p, max_rounds=8):
    """→ ``(V3 fixed-or-start positions, V2, fixed mask, T)``."""
    import numpy as np
    to2 = dom["to2"]
    c, e1, e2, _n = dom["plane"]
    h = dom["h"]
    holes = p["inner"] == "holes"
    # Constraints: (polyline 3D, closed, cuts the domain)
    cons = [[resample(dom["outer"], True, h), True, True]]
    for L in dom["inner"]:
        cons.append([resample(L, True, h), True, holes])
    for L in dom["lines"]:
        cons.append([resample(L, False, h), False, False])
    o2 = to2(dom["outer"])
    hole2 = [to2(L) for L in dom["inner"]] if holes else []

    def in_domain(X, Y):
        ok = inside_poly(o2, X, Y)
        for hp in hole2:
            ok &= ~inside_poly(hp, X, Y)
        return ok

    # The lattice (equilateral triangles: no cocircular quads for Delaunay)
    mn, mx = o2.min(0), o2.max(0)
    dy = h * math.sqrt(3) / 2
    ys = np.arange(mn[1] + dy / 2, mx[1], dy)
    rows = []
    for r, y in enumerate(ys):
        x0 = mn[0] + (h / 2 if r % 2 else h / 4)
        xs = np.arange(x0, mx[0], h)
        rows.append(np.stack([xs, np.full(len(xs), y)], 1))
    lat = np.vstack(rows) if rows else np.zeros((0, 2))
    if len(lat):
        lat = lat[in_domain(lat[:, 0], lat[:, 1])]
    fixed_pts = np.asarray(dom["points"], dtype=float).reshape(-1, 3)
    if len(fixed_pts) and holes and hole2:
        fp2 = to2(fixed_pts)
        fixed_pts = fixed_pts[in_domain(fp2[:, 0], fp2[:, 1])]

    T = None
    for _round in range(max_rounds):
        # Fixed points: constraint vertices + guide points, deduplicated in 2D
        P3, P2, key_of = [], [], {}
        tol = max(h * 1e-4, 1e-9)
        idx_lists = []

        def add(q3):
            q2 = to2(q3[None, :])[0]
            k = (round(q2[0] / tol), round(q2[1] / tol))
            if k not in key_of:
                key_of[k] = len(P3)
                P3.append(q3)
                P2.append(q2)
            return key_of[k]

        for poly, closed_, cut in cons:
            if not cut:
                q2 = to2(poly)
                keep = in_domain(q2[:, 0], q2[:, 1]) if closed_ is False else \
                    np.ones(len(poly), bool)
                idx_lists.append([add(q) if k else None
                                  for q, k in zip(poly, keep)])
            else:
                idx_lists.append([add(q) for q in poly])
        for q in fixed_pts:
            add(q)
        nf = len(P3)
        F2 = np.asarray(P2)
        free2 = lat
        if len(free2):
            free2 = free2[_min_dist2(free2, F2) > (0.5 * h) ** 2]
        V2 = np.vstack([F2, free2]) if len(free2) else F2
        T = delaunay(V2)
        cen = V2[T].mean(1)
        T = T[in_domain(cen[:, 0], cen[:, 1])]
        # Conformity: every constraint segment must be a mesh edge
        es = set()
        for a, b, d in T.tolist():
            es.add((a, b) if a < b else (b, a))
            es.add((b, d) if b < d else (d, b))
            es.add((d, a) if d < a else (a, d))
        changed = False
        for ci, (poly, closed_, cut) in enumerate(cons):
            ids = idx_lists[ci]
            m = len(ids) if closed_ else len(ids) - 1
            new = []
            for k in range(m):
                a, b = ids[k], ids[(k + 1) % len(ids)]
                new.append(poly[k])
                if a is None or b is None or a == b:
                    continue
                if ((a, b) if a < b else (b, a)) not in es:
                    new.append((poly[k] + poly[(k + 1) % len(poly)]) / 2)
                    changed = True
            if not closed_:
                new.append(poly[-1])
            cons[ci][0] = np.asarray(new)
        if not changed:
            break
    V3 = np.empty((len(V2), 3))
    V3[:nf] = np.asarray(P3)
    if len(V2) > nf:
        V3[nf:] = c + np.outer(V2[nf:, 0], e1) + np.outer(V2[nf:, 1], e2)
    fixed = np.zeros(len(V2), dtype=bool)
    fixed[:nf] = True
    # Drop unused vertices (fixed points that fell into a hole, …)
    used = np.zeros(len(V2), dtype=bool)
    used[T.ravel()] = True
    remap = -np.ones(len(V2), dtype=np.int64)
    remap[used] = np.arange(int(used.sum()))
    return V3[used], V2[used], fixed[used], remap[T]

# ---------------------------------------------------------------------------
# The solver: a height field over the plan. Harmonic (Laplace) = straight
# slopes; biharmonic (thin plate, L·M⁻¹·L with the full cotangent Laplacian)
# = the ground runs out level from every edge (zero slope across it) and the
# top and the toe of each slope are rounded. Smoothness blends the two.
# Solved by Jacobi-preconditioned conjugate gradients (numpy only).
# ---------------------------------------------------------------------------

def _laplacian(V2, T):
    """Cotangent Laplacian of the flat plan mesh as COO arrays
    ``(rows, cols, vals)`` (positive diagonal) and the lumped vertex areas."""
    import numpy as np
    n = len(V2)
    A, B, C = V2[T[:, 0]], V2[T[:, 1]], V2[T[:, 2]]

    def cot(u, v):
        cr = u[:, 0] * v[:, 1] - u[:, 1] * v[:, 0]
        return (u * v).sum(1) / np.maximum(np.abs(cr), 1e-300)

    ct = np.concatenate([cot(B - A, C - A), cot(C - B, A - B),
                         cot(A - C, B - C)])
    opp = np.concatenate([T[:, [1, 2]], T[:, [2, 0]], T[:, [0, 1]]])
    key = np.sort(opp, 1)
    E, inv = np.unique(key, axis=0, return_inverse=True)
    w = 0.5 * np.bincount(inv.ravel(), weights=ct, minlength=len(E))
    floor = 1e-3 * float(np.abs(w).mean() + 1e-300)
    w = np.maximum(w, floor)
    I = np.concatenate([E[:, 0], E[:, 1]])
    J = np.concatenate([E[:, 1], E[:, 0]])
    W = np.concatenate([w, w])
    d = np.bincount(I, weights=W, minlength=n)
    rows = np.concatenate([I, np.arange(n)])
    cols = np.concatenate([J, np.arange(n)])
    vals = np.concatenate([-W, d])
    ar = np.abs((B[:, 0] - A[:, 0]) * (C[:, 1] - A[:, 1])
                - (B[:, 1] - A[:, 1]) * (C[:, 0] - A[:, 0])) / 2.0
    M = np.zeros(n)
    for j in range(3):
        M += np.bincount(T[:, j], weights=ar / 3.0, minlength=n)
    return rows, cols, vals, np.maximum(M, 1e-300)


def _pcg(matvec, rhs, x0, minv, tol, maxit, tick=None):
    import numpy as np
    x = x0.copy()
    r = rhs - matvec(x)
    z = minv * r
    d = z.copy()
    rz = float(r @ z)
    bn = float(np.linalg.norm(rhs)) + 1e-300
    k = 0
    for k in range(maxit):
        if float(np.linalg.norm(r)) <= tol * bn:
            break
        Ad = matvec(d)
        a = rz / (float(d @ Ad) + 1e-300)
        x += a * d
        r -= a * Ad
        z = minv * r
        rz2 = float(r @ z)
        d = z + (rz2 / (rz + 1e-300)) * d
        rz = rz2
        if tick is not None and k % 100 == 99:
            tick(k + 1)
    return x, k


def solve_heights(V2, T, fixed, zfix, smooth, progress=None):
    """Heights for every vertex: ``zfix`` where ``fixed``, solved elsewhere.
    ``smooth`` 0…1 blends harmonic → biharmonic."""
    import numpy as np
    n = len(V2)
    z = np.where(fixed, zfix, 0.0).astype(float)
    free = ~fixed
    m = int(free.sum())
    if m == 0:
        return z
    shift = float(zfix[fixed].mean())
    zc = np.where(fixed, zfix - shift, 0.0)
    rows, cols, vals, M = _laplacian(V2, T)

    def Lx(x):
        return np.bincount(rows, weights=vals * x[cols], minlength=n)

    def full(xf):
        x = np.zeros(n)
        x[free] = xf
        return x

    # 1. harmonic
    diag = rows == cols
    dh = np.bincount(rows[diag], weights=vals[diag], minlength=n)[free]
    zh, _k = _pcg(lambda xf: Lx(full(xf))[free], -Lx(zc)[free],
                  np.zeros(m), 1.0 / np.maximum(dh, 1e-300), 1e-9,
                  int(min(20000, 200 + 20 * math.sqrt(m))))
    if progress is not None:
        progress(10)
    res = zh
    if smooth > 1e-6:
        # 2. biharmonic, started from the harmonic solution
        Mi = 1.0 / M

        def mvb(xf):
            return Lx(Mi * Lx(full(xf)))[free]

        db = np.bincount(cols, weights=vals ** 2 * Mi[rows], minlength=n)[free]
        maxit = int(min(60000, 500 + 40 * m))

        def tick(k):
            if progress is not None:
                progress(min(99, 10 + int(90 * k / max(1, 6 * math.sqrt(m) * 3))))

        zb, _k = _pcg(mvb, -Lx(Mi * Lx(zc))[free], zh.copy(),
                      1.0 / np.maximum(db, 1e-300), 1e-10, maxit, tick)
        res = (1.0 - smooth) * zh + smooth * zb
    z[free] = res + shift
    return z


def slope_stats(V, T, fixed):
    """Steepest slope (degrees) of the triangles that are not entirely on
    the contours."""
    import numpy as np
    keep = ~fixed[T].all(1)
    if not keep.any():
        return 0.0
    A, B, C = V[T[keep, 0]], V[T[keep, 1]], V[T[keep, 2]]
    nrm = np.cross(B - A, C - A)
    nz = np.abs(nrm[:, 2])
    nl = np.linalg.norm(nrm, axis=1)
    ok = nl > 1e-300
    if not ok.any():
        return 0.0
    ang = np.degrees(np.arccos(np.clip(nz[ok] / nl[ok], 0.0, 1.0)))
    return float(ang.max())


def slope_text(deg):
    if deg < 0.05:
        return "flat"
    if deg >= 89.9:
        return f"{deg:.0f}°"
    return f"{deg:.0f}° (1 : {1.0 / math.tan(math.radians(deg)):.1f})"


# ---------------------------------------------------------------------------
# The group
# ---------------------------------------------------------------------------

def compute(data, p, progress=None):
    """Source + params → ``(V, T, info)`` — no IngeTrazo objects."""
    import numpy as np
    p = normalize_params(p)
    t0 = time.perf_counter()
    dom = classify(data, p)
    check_size(estimate_triangles(dom), p.get("force", False))
    V3, V2, fixed, T = build_mesh(dom, p)
    if len(T) == 0:
        raise GradingError("No mesh could be laid inside the contour.")
    check_size(len(T), p.get("force", False))
    V2 = V3[:, :2] - dom["plane"][0][:2]       # exact plan positions
    z = solve_heights(V2, T, fixed, V3[:, 2], p["smooth"] / 100.0, progress)
    V = V3.copy()
    V[:, 2] = z
    steep = slope_stats(V, T, fixed)
    if p["flip"]:
        T = T[:, [0, 2, 1]]
    info = {"triangles": int(len(T)), "seconds": time.perf_counter() - t0,
            "slope": steep, "ignored": dom["ignored"], "steps": dom["steps"],
            "inner": len(dom["inner"]), "lines": len(dom["lines"]),
            "points": int(len(dom["points"]))}
    return np.asarray(V), np.asarray(T), info


def build_group(V, T, p, rec, name="Grading"):
    import numpy as np
    from core.group import Group
    from core.mesh import Mesh
    pos = V[T].reshape(-1, 3)
    sizes = np.full(len(T), 3, dtype=np.int64)
    mesh = Mesh()
    mesh.add_faces_bulk(np.ascontiguousarray(pos, dtype=float), sizes,
                        np.ones(len(sizes), dtype=np.int64))
    if p["soft"]:
        for e in mesh.edges:
            if len(e.faces) >= 2:
                e.soft = True
    g = Group(mesh=mesh, name=name)
    g.ext = {KEY: rec}
    return g


def make_grading(data, p, name="Grading", progress=None):
    """→ ``(Group, info)``"""
    p = normalize_params(p)
    V, T, info = compute(data, p, progress)
    rec = {"version": VERSION,
           "params": {k: v for k, v in p.items() if k != "force"},
           "segs": [[round(float(v), 6) for v in s] for s in data["segs"]],
           "points": [[round(float(v), 6) for v in q]
                      for q in (data.get("points") or [])]}
    return build_group(V, T, p, rec, name), info


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------

def _make_command_class():
    from core.history import Command, InsertGroupCommand

    class GradingCommand(Command):
        """Insert a new terrain, or replace ``old`` by ``new`` in place.
        ``select=False`` (the preview) leaves the user's selection alone, so
        edges can still be added to it while the dialog is open."""

        def __init__(self, new, old=None, select=True):
            self.new = new
            self.old = old
            self.select = select
            self._saved = None
            self._insert = InsertGroupCommand(new) if old is None else None
            self._owner = None
            self._index = None

        def _owner_of(self, scene, g):
            if g in scene.groups:
                return scene.groups
            ctx = getattr(scene, "edit_group", None)
            kids = getattr(ctx, "children", None)
            if kids is not None and g in kids:
                return kids
            return None

        def do(self, scene):
            self._saved = set(scene.selection)
            if self._insert is not None:
                self._insert.do(scene)
            else:
                owner = self._owner_of(scene, self.old)
                if owner is None:
                    raise RuntimeError("The terrain is no longer in the model.")
                i = owner.index(self.old)
                owner[i] = self.new
                self._owner, self._index = owner, i
                scene.selection.discard(self.old)
            scene.selection.clear()
            if self.select:
                scene.selection.add(self.new)
            else:
                scene.selection.update(self._saved - {self.old})
            scene.version += 1

        def undo(self, scene):
            if self._insert is not None:
                self._insert.undo(scene)
            elif self._owner is not None and self.new in self._owner:
                self._owner[self._owner.index(self.new)] = self.old
            scene.selection.discard(self.new)
            if self.select:
                if self.old is not None:
                    scene.selection.add(self.old)
            elif self.old is not None and self._saved and self.old in self._saved:
                scene.selection.add(self.old)   # the rest is the user's now
            scene.version += 1

    return GradingCommand


_CMD = None



def run_grading(viewport, data, p, old=None, progress=None, select=True):
    """Build and insert (or replace ``old``) as one undo step. Returns the
    command (``cmd.info`` holds the statistics). ``select=False``: keep the
    current selection (preview)."""
    global _CMD
    if _CMD is None:
        _CMD = _make_command_class()
    name = old.name if old is not None else _next_name(viewport.scene)
    g, info = make_grading(data, p, name, progress)
    if old is not None:
        g.layer = old.layer
        g.material = getattr(old, "material", None)
    cmd = _CMD(g, old, select)
    cmd.info = info
    hist = viewport.history
    hist.execute(cmd)
    err = getattr(hist, "last_error", None)
    if err:
        raise RuntimeError(err)
    notify = getattr(viewport, "notify_scene_changed", None)
    if notify:
        notify()
    viewport.update()
    return cmd

def _next_name(scene):
    used = {g.name for g in scene.groups}
    if "Grading" not in used:
        return "Grading"
    n = 2
    while f"Grading {n}" in used:
        n += 1
    return f"Grading {n}"


def _unit():
    try:
        from core import units
        code = units.model_unit()
        short = {"in": "in", "in-frac": "in", "ft": "ft", "ft-in": "ft",
                 "ft-in-frac": "ft"}.get(code, code)
        return units.bare_number_scale(), short
    except Exception:  # noqa: BLE001
        return 1.0, "m"


def _fmt(v, scale, unit):
    return f"{v / scale:,.2f} {unit}"


# ---------------------------------------------------------------------------
# The dialog
# ---------------------------------------------------------------------------

def _make_dialog_class():
    from PySide6.QtCore import QEventLoop, Qt, QTimer
    from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox,
                                   QComboBox, QDialog, QDoubleSpinBox,
                                   QFormLayout, QGroupBox, QHBoxLayout,
                                   QLabel, QMessageBox, QPushButton,
                                   QRadioButton, QSlider, QSpinBox,
                                   QVBoxLayout)

    class GradingDialog(QDialog):
        _PV_ON = ("QPushButton { background: #2e9e4f; color: white; "
                  "font-weight: bold; border: 1px solid #1f7a3a; "
                  "border-radius: 3px; padding: 3px 8px; }")
        _BANNER = {
            "live": "background: #1f5f33; color: #e8ffe9;",
            "busy": "background: #7a5a12; color: #fff6e0;",
            "paused": "background: #5c4a1a; color: #ffe9b8;",
            "error": "background: #7a1f1f; color: #ffecec;",
        }

        def __init__(self, viewport, data, old=None, parent=None):
            super().__init__(parent)
            self.viewport = viewport
            self.data = data
            self.old = old
            self.scale_m, self.ulabel = _unit()
            self.p = (normalize_params(grading_data(old).get("params"))
                      if old is not None else load_params())
            self.dom = None
            self._preview_cmd = None
            self._block = None
            self._busy = False
            self._computing = False
            self._timer = QTimer(self)
            self._timer.setSingleShot(True)
            self._timer.setInterval(300)
            self._timer.timeout.connect(self._refresh_preview)
            what = "Edit Grading" if old is not None else "Fill from Edges"
            self.setWindowTitle(f"{TITLE} {VERSION} — {what}")
            self.setModal(False)
            self.setAttribute(Qt.WA_DeleteOnClose, True)
            self._sel_note = ""
            self._sig = self._selection_sig()
            self._sel_timer = QTimer(self)
            self._sel_timer.setInterval(400)
            self._sel_timer.timeout.connect(self._poll_selection)
            self._build_ui()
            self._load_into_ui()
            self._sel_timer.start()

        # ---- UI ------------------------------------------------------------
        def _dspin(self, lo, hi, dec, suffix=None):
            s = QDoubleSpinBox(self)
            s.setRange(lo, hi)
            s.setDecimals(dec)
            s.setSuffix(f" {self.ulabel}" if suffix is None else suffix)
            s.setKeyboardTracking(False)
            s.setMinimumWidth(110)
            s.valueChanged.connect(self._changed)
            return s

        def _check(self, text, tip=""):
            c = QCheckBox(text, self)
            c.setToolTip(tip)
            c.toggled.connect(self._changed)
            return c

        def _build_ui(self):
            lay = QVBoxLayout(self)

            src = QGroupBox("Source", self)
            sl = QFormLayout(src)
            self.src_lbl = QLabel(self)
            self.src_lbl.setWordWrap(True)
            self.src_lbl.setMinimumWidth(380)
            sl.addRow(self.src_lbl)
            self.inner = QComboBox(self)
            self.inner.addItem("Holes — the ground stops at them "
                               "(platform, bed)", "holes")
            self.inner.addItem("Fixed lines — the ground runs on past them",
                               "lines")
            self.inner.setToolTip(
                "Closed contours inside the outer contour: cut out (the "
                "ground meets their edge and stops), or only held at their "
                "height.")
            self.inner.currentIndexChanged.connect(self._changed)
            sl.addRow("Inner contours:", self.inner)
            srow = QHBoxLayout()
            self.follow = QCheckBox("Follow selection", self)
            self.follow.setToolTip(
                "While the dialog is open, a changed selection is read again "
                "by itself: add or remove edges, contours, lines or guide "
                "points in the viewport and the terrain follows.")
            self.follow.toggled.connect(self._follow_toggled)
            srow.addWidget(self.follow)
            srow.addStretch(1)
            self.b_read = QPushButton("↻ Read selection", self)
            self.b_read.setToolTip("Take the current selection as the source "
                                   "now.")
            self.b_read.clicked.connect(lambda: self._read_selection(True))
            srow.addWidget(self.b_read)
            sl.addRow(srow)
            lay.addWidget(src)

            mbox = QGroupBox("Mesh", self)
            ml = QFormLayout(mbox)
            self.r_div = QRadioButton("Divisions (long side)", self)
            self.r_size = QRadioButton("Mesh size", self)
            grp = QButtonGroup(self)
            grp.addButton(self.r_div)
            grp.addButton(self.r_size)
            self.r_div.toggled.connect(self._changed)
            self.div = QSpinBox(self)
            self.div.setRange(MIN_DIV, MAX_DIV)
            self.div.setKeyboardTracking(False)
            self.div.setToolTip("Triangles along the longest side of the "
                                "outer contour. 40–100 is plenty for most "
                                "sites.")
            self.div.valueChanged.connect(self._changed)
            self.size = self._dspin(0.001, 1e5, 3)
            self.size.setToolTip("Edge length of the triangles.")
            ml.addRow(self.r_div, self.div)
            ml.addRow(self.r_size, self.size)
            self.mesh_lbl = QLabel(self)
            self.mesh_lbl.setWordWrap(True)
            ml.addRow(self.mesh_lbl)
            self.warn_lbl = QLabel(self)
            self.warn_lbl.setWordWrap(True)
            ml.addRow(self.warn_lbl)
            self.force = self._check(
                "Build anyway — at my own risk",
                "Over the safe limit IngeTrazo may become very slow or run "
                "out of memory. Never remembered.")
            ml.addRow(self.force)
            lay.addWidget(mbox)

            sbox = QGroupBox("Shape", self)
            fl = QFormLayout(sbox)
            srow2 = QHBoxLayout()
            self.smooth_sl = QSlider(Qt.Horizontal, self)
            self.smooth_sl.setRange(0, 100)
            self.smooth_sl.setSingleStep(5)
            self.smooth_sl.setPageStep(10)
            self.smooth = self._dspin(0.0, 100.0, 0, " %")
            self.smooth.setSingleStep(10.0)
            self.smooth.setMinimumWidth(80)
            tip = ("100 % = the ground runs out level from every edge; the "
                   "top and the toe of each slope are rounded — it looks laid "
                   "out by hand.\n0 % = straight slopes with a crease at the "
                   "edges.")
            self.smooth.setToolTip(tip)
            self.smooth_sl.setToolTip(tip)
            self.smooth_sl.valueChanged.connect(self._slider_moved)
            self.smooth_sl.sliderReleased.connect(self._changed)
            srow2.addWidget(self.smooth_sl, 1)
            srow2.addWidget(self.smooth)
            fl.addRow("Smoothness:", srow2)
            lay.addWidget(sbox)

            obox = QGroupBox("Output", self)
            ol = QVBoxLayout(obox)
            self.soft = self._check("Soft edges (smooth look)",
                                    "The triangle edges are softened.")
            self.flip = self._check("Flip faces",
                                    "Turn the front side of the terrain "
                                    "around (normally it faces up).")
            ol.addWidget(self.soft)
            ol.addWidget(self.flip)
            lay.addWidget(obox)

            self.pv_lbl = QLabel(self)
            self.pv_lbl.setWordWrap(True)
            self.pv_lbl.setVisible(False)
            lay.addWidget(self.pv_lbl)

            row = QHBoxLayout()
            self.preview = QPushButton("Preview", self)
            self.preview.setCheckable(True)
            self.preview.toggled.connect(self._preview_toggled)
            reset = QPushButton("Reset", self)
            reset.setToolTip("Back to the default settings.")
            reset.clicked.connect(self._reset)
            self.ok_btn = QPushButton("OK", self)
            self.ok_btn.setDefault(True)
            self.ok_btn.clicked.connect(self.accept)
            cancel = QPushButton("Cancel", self)
            cancel.clicked.connect(self.reject)
            row.addWidget(self.preview)
            row.addWidget(reset)
            row.addStretch(1)
            row.addWidget(self.ok_btn)
            row.addWidget(cancel)
            lay.addLayout(row)
            self._style_preview(False)

        def _slider_moved(self, v):
            """Slider → spin box (no rebuild while dragging; on release)."""
            if self._busy:
                return
            self._busy = True
            self.smooth.setValue(float(v))
            self._busy = False
            if not self.smooth_sl.isSliderDown():
                self._changed()

        def _load_into_ui(self):
            self._busy = True
            p, s = self.p, self.scale_m
            self.r_div.setChecked(p["res_mode"] == "div")
            self.r_size.setChecked(p["res_mode"] == "size")
            self.div.setValue(p["div"])
            self.size.setValue(p["size"] / s)
            self.smooth.setValue(p["smooth"])
            self.smooth_sl.setValue(int(round(p["smooth"])))
            self.inner.setCurrentIndex(max(0, self.inner.findData(p["inner"])))
            self.soft.setChecked(p["soft"])
            self.flip.setChecked(p["flip"])
            self.follow.setChecked(p["follow"])
            self.force.setChecked(False)
            p["force"] = False
            self._busy = False
            self._sync()

        def _read_ui(self):
            p, s = self.p, self.scale_m
            p["res_mode"] = "size" if self.r_size.isChecked() else "div"
            p["div"] = self.div.value()
            p["size"] = self.size.value() * s
            p["smooth"] = self.smooth.value()
            p["inner"] = self.inner.currentData()
            p["soft"] = self.soft.isChecked()
            p["flip"] = self.flip.isChecked()
            p["follow"] = self.follow.isChecked()
            p["force"] = self.force.isChecked()
            self.p = normalize_params(p)
            if self.smooth_sl.value() != int(round(self.p["smooth"])):
                self.smooth_sl.blockSignals(True)
                self.smooth_sl.setValue(int(round(self.p["smooth"])))
                self.smooth_sl.blockSignals(False)

        def _sync(self):
            """Source text, mesh estimate, limits — no building."""
            p = self.p
            self.div.setEnabled(p["res_mode"] == "div")
            self.size.setEnabled(p["res_mode"] == "size")
            try:
                self.dom = classify(self.data, p)
            except GradingError as exc:
                self.dom = None
                self.src_lbl.setText(f"<b>{exc}</b>")
                self.mesh_lbl.setText("")
                self.warn_lbl.setVisible(False)
                self.force.setVisible(False)
                self.ok_btn.setEnabled(False)
                self.inner.setEnabled(False)
                self._block = str(exc)
                return
            d = self.dom
            parts = ["1 outer contour"]
            if d["inner"]:
                parts.append(f"{len(d['inner'])} inner contour"
                             f"{'s' if len(d['inner']) > 1 else ''}")
            if d["lines"]:
                parts.append(f"{len(d['lines'])} fixed line"
                             f"{'s' if len(d['lines']) > 1 else ''}")
            if len(d["points"]):
                parts.append(f"{len(d['points'])} spot height"
                             f"{'s' if len(d['points']) > 1 else ''}")
            txt = " · ".join(parts)
            txt += (f"<br>Size {_fmt(d['long'], self.scale_m, self.ulabel)} "
                    f"(long side) · height difference "
                    f"{_fmt(d['drop'], self.scale_m, self.ulabel)}")
            if d["steps"]:
                txt += (f"<br><span style='color:#e8912d'>{d['steps']} "
                        f"point(s) where edges meet at different heights — "
                        f"the height is averaged there.</span>")
            if d["ignored"]:
                txt += (f"<br><span style='color:#e8912d'>{d['ignored']} "
                        f"item(s) outside the outer contour — "
                        f"ignored.</span>")
            if self._sel_note:
                txt += f"<br><span style='color:#5fbf73'>{self._sel_note}</span>"
            elif self.old is not None:
                txt += "<br>Source stored with the terrain."
            self.src_lbl.setText(txt)
            self.inner.setEnabled(bool(d["inner"]))
            n = estimate_triangles(d)
            self.mesh_lbl.setText(
                f"Triangle size {_fmt(d['h'], self.scale_m, self.ulabel)} "
                f"(≈ {n:,} triangles)")
            too_big = n > MAX_TRIANGLES
            if too_big:
                warn = (f"<b>Too many triangles</b> — safe limit "
                        f"{MAX_TRIANGLES:,}. Use fewer divisions.")
                color = "#e0483e"
            elif n > WARN_TRIANGLES:
                warn, color = "Large: slow to build.", "#e8912d"
            else:
                warn, color = "", ""
            style = f"color: {color};" if color else ""
            self.mesh_lbl.setStyleSheet(style)
            self.warn_lbl.setStyleSheet(style)
            self.warn_lbl.setText(warn)
            self.warn_lbl.setVisible(bool(warn))
            if not too_big and self.force.isChecked():
                self.force.blockSignals(True)
                self.force.setChecked(False)
                self.force.blockSignals(False)
                p["force"] = False
            self.force.setVisible(too_big)
            allowed = (not too_big) or self.force.isChecked()
            self.ok_btn.setEnabled(allowed)
            self._block = None if allowed else (
                f"Over the safe limit of {MAX_TRIANGLES:,} triangles — use "
                f"fewer divisions or tick «Build anyway».")
            if self.isVisible():
                QTimer.singleShot(0, self.adjustSize)

        def _banner(self, kind, text):
            self.pv_lbl.setStyleSheet(
                self._BANNER[kind] + " padding: 6px 8px; border-radius: 3px;")
            self.pv_lbl.setText(text)
            self.pv_lbl.setVisible(True)

        def _style_preview(self, on):
            if on:
                self.preview.setText("● Live Preview ON")
                self.preview.setStyleSheet(self._PV_ON)
                self.preview.setToolTip("The preview follows every change by "
                                        "itself — click to switch it off.")
            else:
                self.preview.setText("Preview")
                self.preview.setStyleSheet("")
                self.preview.setToolTip("Show the terrain in the model; it "
                                        "then updates by itself on every "
                                        "change.")
                self.pv_lbl.setVisible(False)
            if self.isVisible():
                QTimer.singleShot(0, self.adjustSize)

        def _changed(self, *_a):
            if self._busy:
                return
            self._read_ui()
            self._sync()
            if self.preview.isChecked():
                if not self._block:
                    self._banner("busy", "⟳ Updating the preview…")
                self._timer.start()

        def _reset(self):
            self.p = default_params()
            self._load_into_ui()
            self._changed()

        # ---- building --------------------------------------------------------
        def _progress(self, pct):
            self._banner("busy", f"⟳ Shaping the ground… {pct} %")
            QApplication.processEvents(QEventLoop.ExcludeUserInputEvents)

        # ---- following the selection -----------------------------------------
        def _selection_sig(self):
            try:
                return frozenset(id(e) for e in self.viewport.scene.selection)
            except Exception:  # noqa: BLE001
                return frozenset()

        def _poll_selection(self):
            if self._computing or not self.follow.isChecked():
                return
            sig = self._selection_sig()
            if sig != self._sig:
                self._sig = sig
                self._read_selection(False)

        def _follow_toggled(self, on):
            if self._busy:
                return
            self.p["follow"] = bool(on)
            if on:                       # catch up with what changed meanwhile
                self._sig = self._selection_sig()
                self._read_selection(False)

        @staticmethod
        def _key(data):
            return (sorted(tuple(round(v, 6) for v in s) for s in data["segs"]),
                    sorted(tuple(round(v, 6) for v in q)
                           for q in (data.get("points") or [])))

        def _read_selection(self, manual):
            """Take the current selection as the new source (edges, groups,
            guide points). A selection without edges — nothing, or only the
            terrain — keeps the source as it is."""
            if self._computing:
                return
            data = gather(self.viewport.scene)
            if not data["segs"]:
                if manual:
                    self.viewport.flash_status(
                        f"{TITLE}: no edges in the selection — the source "
                        f"stays as it is.", 4000)
                return
            if not manual and self._key(data) == self._key(self.data):
                return
            self.data = data
            self._sel_note = (f"↻ Selection read again "
                              f"({time.strftime('%H:%M:%S')}): "
                              f"{len(data['segs'])} edges"
                              + (f", {len(data['points'])} points"
                                 if data["points"] else ""))
            self._sync()
            if self.preview.isChecked():
                if not self._block:
                    self._banner("busy", "⟳ Updating the preview…")
                self._timer.start()

        def _run(self, select=True):
            self._computing = True
            try:
                return run_grading(self.viewport, self.data, self.p, self.old,
                                   self._progress, select)
            finally:
                self._computing = False

        def _result_text(self, info):
            return (f"{info['triangles']:,} triangles "
                    f"({info['seconds']:.1f} s) · steepest "
                    f"{slope_text(info['slope'])}")

        def _undo_preview(self):
            cmd = self._preview_cmd
            self._preview_cmd = None
            if cmd is None:
                return
            stack = getattr(self.viewport.history, "undo_stack", [])
            if stack and stack[-1] is cmd:
                self.viewport.history.undo()
                self.viewport.update()

        def _refresh_preview(self):
            if self._computing:            # a change during a build: later
                self._timer.start()
                return
            self._undo_preview()
            if not self.preview.isChecked():
                return
            if self._block:
                self._banner("paused", f"⏸ <b>Preview paused</b> — "
                             f"{self._block} It comes back by itself.")
                return
            self._banner("busy", "⟳ Updating the preview…")
            QApplication.processEvents(QEventLoop.ExcludeUserInputEvents)
            try:
                self._preview_cmd = self._run(select=False)
                self._preview_cmd.key = self._state_key()
                self._banner("live", "● <b>LIVE PREVIEW</b> — "
                             + self._result_text(self._preview_cmd.info)
                             + " · changes apply instantly")
            except Exception as exc:  # noqa: BLE001
                self._preview_cmd = None
                self._banner("error", f"Preview failed: {exc}")

        def _preview_toggled(self, on):
            self._style_preview(on)
            if on:
                self._refresh_preview()
            else:
                self._timer.stop()
                self._undo_preview()

        # ---- close -------------------------------------------------------------
        def _state_key(self):
            """What the result depends on — source and settings."""
            p = {k: v for k, v in self.p.items() if k != "follow"}
            return (self._key(self.data), json.dumps(p, sort_keys=True))

        def _keep_preview(self):
            """The live preview already IS the result for these settings:
            keep it (it is one undo step already) instead of building again."""
            cmd = self._preview_cmd
            if cmd is None or getattr(cmd, "key", None) != self._state_key():
                return None
            stack = getattr(self.viewport.history, "undo_stack", [])
            if not stack or stack[-1] is not cmd:
                return None
            scene = self.viewport.scene
            cmd.select = True               # redo/undo now behave like OK
            scene.selection.clear()
            scene.selection.add(cmd.new)
            scene.version += 1
            self._preview_cmd = None
            notify = getattr(self.viewport, "notify_scene_changed", None)
            if notify:
                notify()
            self.viewport.update()
            return cmd

        def accept(self):
            if self._computing:
                return
            self._sel_timer.stop()
            self._read_ui()
            self._timer.stop()
            save_params(self.p)
            cmd = self._keep_preview()
            if cmd is None:
                self._undo_preview()
                try:
                    cmd = self._run()
                except Exception as exc:  # noqa: BLE001
                    QMessageBox.warning(self, TITLE, str(exc))
                    self._sel_timer.start()
                    return
            info = cmd.info
            self.viewport.flash_status(
                f"{TITLE}: «{cmd.new.name}» — {info['triangles']:,} triangles "
                f"in {info['seconds']:.1f} s, steepest "
                f"{slope_text(info['slope'])} (one undo step)", 6000)
            super().accept()

        def reject(self):
            if self._computing:
                return
            self._sel_timer.stop()
            self._timer.stop()
            self._undo_preview()
            self._read_ui()
            save_params(self.p)
            super().reject()

    return GradingDialog


_DIALOG = None
_OPEN = None


def _dialog(viewport, data, old, parent):
    """Non-modal: the viewport stays usable (orbit, pan, zoom) while the
    dialog is open. One dialog at a time."""
    global _DIALOG, _OPEN
    if _DIALOG is None:
        _DIALOG = _make_dialog_class()
    if _OPEN is not None:
        try:
            _OPEN.reject()
        except RuntimeError:
            pass
    dlg = _DIALOG(viewport, data, old, parent or viewport.window())
    _OPEN = dlg
    dlg.show()
    dlg.raise_()
    return dlg


def selected_grading(scene):
    from core.group import Group
    for ent in scene.selection:
        if isinstance(ent, Group) and grading_data(ent) is not None:
            return ent
    return None


def show_fill(viewport, parent=None):
    data = gather(viewport.scene)
    if not data["segs"]:
        viewport.flash_status(
            f"{TITLE}: select the edges around the open ground (a closed "
            f"contour, seen from above) first.", 5000)
        return
    _dialog(viewport, data, None, parent)


def show_edit(viewport, parent=None):
    g = selected_grading(viewport.scene)
    if g is None:
        viewport.flash_status(
            f"{TITLE}: select a terrain made with this plugin first.", 5000)
        return
    rec = grading_data(g)
    data = {"segs": [tuple(s) for s in rec.get("segs", [])],
            "points": [tuple(q) for q in rec.get("points", [])]}
    _dialog(viewport, data, g, parent)


def setup(app) -> None:
    from PySide6.QtCore import QTimer
    sub = app.add_menu(TITLE)
    sub.addAction("Fill from Edges…",
                  lambda: show_fill(app.viewport, app.window))
    sub.addSeparator()
    sub.addAction("Edit Grading…", lambda: show_edit(app.viewport, app.window))

    def later(fn):
        return lambda _c=False: QTimer.singleShot(
            0, lambda: fn(app.viewport, app.window))

    def context(menu, selection) -> None:
        scene = app.viewport.scene
        if not scene.selection:
            return
        menu.addSeparator()
        if selected_grading(scene) is not None:
            menu.addAction("Edit Grading…").triggered.connect(later(show_edit))
            return
        sm = menu.addMenu(TITLE)
        sm.addAction("Fill from Edges…").triggered.connect(later(show_fill))

    app.add_context_menu(context)
