"""Static map model: terrain height, building prisms, line of sight, and a coarse walk grid with A*."""
import array
import heapq
import json
import math
import os
import time

CELL = 3.0


def _pip(x, y, xs, ys):
    inside = False
    j = len(xs) - 1
    for i in range(len(xs)):
        if (ys[i] > y) != (ys[j] > y) and x < (xs[j] - xs[i]) * (y - ys[i]) / (ys[j] - ys[i]) + xs[i]:
            inside = not inside
        j = i
    return inside


class World:
    def __init__(self, mapdir):
        d = os.path.join(mapdir, "v2")
        w = json.load(open(os.path.join(d, "world.json")))
        t = w["terrain"]
        self.nx, self.ny = t["nx"], t["ny"]
        self.minX, self.minY, self.step = t["minX"], t["minY"], t["step"]
        self.hts = array.array("h")
        with open(os.path.join(d, "terrain.i16"), "rb") as f:
            self.hts.frombytes(f.read())
        b = w["bounds"]
        self.bx0, self.bx1, self.by0, self.by1 = b["minX"], b["maxX"], b["minY"], b["maxY"]
        self.prisms = []
        self.pgrid = {}
        for p in w["prisms"]:
            ring = p["ring"]
            xs, ys = ring[0::2], ring[1::2]
            holes = [(h[0::2], h[1::2]) for h in p["holes"]]
            pr = (p["kind"], p["base"], p["top"], xs, ys, holes, min(xs), max(xs), min(ys), max(ys))
            idx = len(self.prisms)
            self.prisms.append(pr)
            for ci in range(int(pr[6] // 32), int(pr[7] // 32) + 1):
                for cj in range(int(pr[8] // 32), int(pr[9] // 32) + 1):
                    self.pgrid.setdefault((ci, cj), []).append(idx)
        self._build_grid()

    # ---- terrain
    def ground(self, x, y):
        fx = (x - self.minX) / self.step
        fy = (y - self.minY) / self.step
        i = min(max(int(fx), 0), self.nx - 2)
        j = min(max(int(fy), 0), self.ny - 2)
        ax = min(max(fx - i, 0.0), 1.0)
        ay = min(max(fy - j, 0.0), 1.0)
        h = self.hts
        o = j * self.nx + i
        return 0.01 * (h[o] * (1 - ax) * (1 - ay) + h[o + 1] * ax * (1 - ay) + h[o + self.nx] * (1 - ax) * ay + h[o + self.nx + 1] * ax * ay)

    # ---- solids
    def solid(self, x, y, z):
        for idx in self.pgrid.get((int(x // 32), int(y // 32)), ()):
            kind, base, top, xs, ys, holes, x0, x1, y0, y1 = self.prisms[idx]
            if x < x0 or x > x1 or y < y0 or y > y1 or z < base or z > top:
                continue
            if _pip(x, y, xs, ys) and not any(_pip(x, y, hx, hy) for hx, hy in holes):
                return True
        return False

    def los(self, a, b, step=4.0):
        dx, dy, dz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        d = math.sqrt(dx * dx + dy * dy + dz * dz)
        n = int(d / step)
        for k in range(1, n + 1):
            f = k / (n + 1.0)
            x, y, z = a[0] + dx * f, a[1] + dy * f, a[2] + dz * f
            if z < self.ground(x, y) - 0.2 or self.solid(x, y, z):
                return False
        return True

    # ---- walk grid
    def _build_grid(self):
        self.gx0, self.gy0 = self.bx0, self.by0
        self.gw = int((self.bx1 - self.bx0) / CELL) + 1
        self.gh = int((self.by1 - self.by0) / CELL) + 1
        raw = bytearray(self.gw * self.gh)
        gw, gh = self.gw, self.gh

        def mark(x, y):
            i, j = int((x - self.gx0) / CELL), int((y - self.gy0) / CELL)
            if 0 <= i < gw and 0 <= j < gh:
                raw[j * gw + i] = 1

        for kind, base, top, xs, ys, holes, x0, x1, y0, y1 in self.prisms:
            if kind == "bridge":
                continue
            for i in range(max(int((x0 - self.gx0) / CELL), 0), min(int((x1 - self.gx0) / CELL) + 1, gw)):
                cx = self.gx0 + (i + 0.5) * CELL
                for j in range(max(int((y0 - self.gy0) / CELL), 0), min(int((y1 - self.gy0) / CELL) + 1, gh)):
                    cy = self.gy0 + (j + 0.5) * CELL
                    if raw[j * gw + i]:
                        continue
                    if _pip(cx, cy, xs, ys) and not any(_pip(cx, cy, hx, hy) for hx, hy in holes):
                        if top > self.ground(cx, cy) + 1.2:
                            raw[j * gw + i] = 1
            rings = [(xs, ys)] + holes
            for rx, ry in rings:
                n = len(rx)
                for k in range(n):
                    ax, ay, bx, by = rx[k], ry[k], rx[(k + 1) % n], ry[(k + 1) % n]
                    L = math.hypot(bx - ax, by - ay)
                    m = max(int(L / 1.5), 1)
                    for s in range(m + 1):
                        mark(ax + (bx - ax) * s / m, ay + (by - ay) * s / m)
        # steep ground and map edge
        for j in range(gh):
            for i in range(gw):
                x, y = self.gx0 + (i + 0.5) * CELL, self.gy0 + (j + 0.5) * CELL
                if x < self.bx0 + 6 or x > self.bx1 - 6 or y < self.by0 + 6 or y > self.by1 - 6:
                    raw[j * gw + i] = 1
        self.raw = raw
        dil = bytearray(raw)
        for j in range(1, gh - 1):
            row = j * gw
            for i in range(1, gw - 1):
                if raw[row + i]:
                    for o in (-gw - 1, -gw, -gw + 1, -1, 1, gw - 1, gw, gw + 1):
                        dil[row + i + o] = 1
        self.dil = dil

    def cell(self, x, y):
        return int((x - self.gx0) / CELL), int((y - self.gy0) / CELL)

    def blocked(self, x, y):
        i, j = self.cell(x, y)
        if i < 0 or j < 0 or i >= self.gw or j >= self.gh:
            return True
        return bool(self.raw[j * self.gw + i])

    def line_free(self, a, b, grid=None):
        grid = grid or self.raw
        dx, dy = b[0] - a[0], b[1] - a[1]
        n = int(math.hypot(dx, dy) / (CELL * 0.7)) + 1
        for k in range(n + 1):
            f = k / n
            i, j = self.cell(a[0] + dx * f, a[1] + dy * f)
            if i < 0 or j < 0 or i >= self.gw or j >= self.gh or grid[j * self.gw + i]:
                return False
        return True

    def _nearest_free(self, i, j):
        gw, gh, dil = self.gw, self.gh, self.dil
        i = min(max(i, 0), gw - 1)
        j = min(max(j, 0), gh - 1)
        if not dil[j * gw + i]:
            return i, j
        for r in range(1, 30):
            for di in range(-r, r + 1):
                for dj in (-r, r):
                    for ii, jj in ((i + di, j + dj), (i + dj, j + di)):
                        if 0 <= ii < gw and 0 <= jj < gh and not dil[jj * gw + ii]:
                            return ii, jj
        return None

    def path(self, a, b, budget_s=0.012):
        """Weighted A* on the dilated grid. Returns world-space waypoints (maybe partial if out of budget)."""
        t0 = time.perf_counter()
        si, sj = self.cell(*a)
        gi, gj = self.cell(*b)
        gw, gh, dil = self.gw, self.gh, self.dil
        s = (min(max(si, 0), gw - 1), min(max(sj, 0), gh - 1))
        g = self._nearest_free(gi, gj)
        if g is None:
            return []
        W = 1.6
        openh = [(0.0, s)]
        cost = {s: 0.0}
        came = {}
        best, besth = s, 1e18
        nodes = 0
        nbrs = ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0), (1, 1, 1.414), (1, -1, 1.414), (-1, 1, 1.414), (-1, -1, 1.414))
        while openh:
            _, cur = heapq.heappop(openh)
            ci, cj = cur
            h = math.hypot(ci - g[0], cj - g[1])
            if h < besth:
                besth, best = h, cur
            if cur == g:
                break
            nodes += 1
            if nodes & 127 == 0 and time.perf_counter() - t0 > budget_s:
                break
            cc = cost[cur]
            for di, dj, w in nbrs:
                ni, nj = ci + di, cj + dj
                if ni < 0 or nj < 0 or ni >= gw or nj >= gh or dil[nj * gw + ni]:
                    continue
                if di and dj and (dil[cj * gw + ni] or dil[nj * gw + ci]):
                    continue
                nc = cc + w
                n = (ni, nj)
                if nc < cost.get(n, 1e18):
                    cost[n] = nc
                    came[n] = cur
                    heapq.heappush(openh, (nc + W * math.hypot(ni - g[0], nj - g[1]), n))
        out = []
        cur = best
        while cur in came:
            out.append((self.gx0 + (cur[0] + 0.5) * CELL, self.gy0 + (cur[1] + 0.5) * CELL))
            cur = came[cur]
        out.reverse()
        return out
