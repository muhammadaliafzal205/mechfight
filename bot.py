"""mechfight bot: survive the zone, win fights with lead-aimed gun + lightning/laser, break line of sight when hurt."""
import math
import os
import random
import sys
import time

import mechfight as mf

try:
    from world import World
except Exception:  # keep playing without a map
    World = None

PI = math.pi
TUNE = {"smin": 0.2, "smax": 0.5, "pref_gun": 130.0, "flip": 0.7}
UPGRADE_PRIORITY = ["gun_mag", "gun_damage", "plating", "lightning_efficiency", "armor", "gun_rate", "nanites",
                    "laser_damage", "lightning_chain", "laser_charge", "gyros", "servos", "sensors", "gun_velocity",
                    "jets", "reactor", "capacitor", "lightning_range", "laser_pierce"]


def wrap(a):
    return (a + PI) % (2 * PI) - PI


def norm(x, y):
    d = math.hypot(x, y)
    return (x / d, y / d) if d > 1e-9 else (0.0, 0.0)


def lead_point(rel, vel, speed):
    """Where to aim so a bullet of `speed` from the origin meets a target at rel moving at vel."""
    a = vel[0] ** 2 + vel[1] ** 2 + vel[2] ** 2 - speed * speed
    b = 2 * (rel[0] * vel[0] + rel[1] * vel[1] + rel[2] * vel[2])
    c = rel[0] ** 2 + rel[1] ** 2 + rel[2] ** 2
    t = 0.0
    if abs(a) < 1e-6:
        t = -c / b if b < 0 else 0.0
    else:
        disc = b * b - 4 * a * c
        if disc >= 0:
            sq = math.sqrt(disc)
            ts = [x for x in ((-b - sq) / (2 * a), (-b + sq) / (2 * a)) if x > 0]
            t = min(ts) if ts else 0.0
    t = min(t, 1.5)
    return [rel[0] + vel[0] * t, rel[1] + vel[1] * t, rel[2] + vel[2] * t], t


def aim_of(v):
    return [math.atan2(v[1], v[0]), math.atan2(v[2], math.hypot(v[0], v[1]))]


class Bot:
    name = "kiter-v2"

    def __init__(self, hello):
        self.cfg = hello["config"]
        self.rng = random.Random(hello.get("seed", 0))
        self.world = None
        if World is not None:
            dirs = [hello.get("map", {}).get("dir", ""), "/arena", os.path.join(os.path.dirname(os.path.abspath(__file__)), "arena")]
            for d in dirs:
                try:
                    if d and os.path.exists(os.path.join(d, "v2", "world.json")):
                        self.world = World(d)
                        break
                except Exception as e:
                    mf.log("map load failed", d, e)
        self.slot = hello.get("slot", 0)
        self.strafe = 1
        self.strafe_until = 0.0
        self.hist = []
        self.unstick_until = 0.0
        self.unstick_dir = (1.0, 0.0)
        self.path = []
        self.path_goal = None
        self.path_t = -99.0
        self.pickups = {}
        self.cubes = {}
        self.crates = {}
        self.bad_pickups = set()
        self.last_damage_t = -99.0
        self.last_damage_bearing = None
        self.target_id = None
        self.cover = None
        self.cover_t = -99.0
        self.dodge_side = {}
        self.slow_until = 0.0
        self.last_aim = [0.0, 0.0]
        self.laser_hold = 0

    # ------------------------------------------------------------ helpers
    def los(self, a, b):
        if self.world is None:
            return True
        return self.world.los(a, b)

    def go(self, x, y, goal, t):
        """Unit direction from (x, y) toward goal, routed around buildings."""
        gx, gy = goal
        dx, dy = gx - x, gy - y
        d = math.hypot(dx, dy)
        if d < 1.5:
            return 0.0, 0.0
        w = self.world
        if w is None or d < 12 and w.line_free((x, y), goal):
            return dx / d, dy / d
        if w.line_free((x, y), goal) and d < 60:
            return dx / d, dy / d
        if (self.path_goal is None or math.hypot(self.path_goal[0] - gx, self.path_goal[1] - gy) > 10
                or t - self.path_t > 3.0 or not self.path) and t >= self.slow_until:
            self.path = w.path((x, y), goal)
            self.path_goal = goal
            self.path_t = t
        # drop waypoints behind us, then aim at the farthest one that is still in a straight line
        while len(self.path) > 1 and math.hypot(self.path[0][0] - x, self.path[0][1] - y) < 5:
            self.path.pop(0)
        tgt = None
        for wp in self.path[:12]:
            if math.hypot(wp[0] - x, wp[1] - y) > 40:
                break
            if w.line_free((x, y), wp):
                tgt = wp
        if tgt is None:
            tgt = self.path[0] if self.path else goal
        return norm(tgt[0] - x, tgt[1] - y)

    def steer(self, x, y, ux, uy):
        """Pick the heading closest to the desired one that still has open ground ahead (no walls, no map edge)."""
        w = self.world
        n = math.hypot(ux, uy)
        if w is None or n < 1e-6:
            return ux, uy
        base = math.atan2(uy, ux)
        best, bs = (ux, uy), -1e9
        for k in range(-8, 9):
            ang = base + k * PI / 8
            vx, vy = math.cos(ang), math.sin(ang)
            free = 0
            for r in (3, 6, 9, 12, 16, 20, 26):
                if w.blocked(x + vx * r, y + vy * r):
                    break
                free = r
            sc = math.cos(k * PI / 8) * 2.0 + free / 26.0 * 0.9 - (3.0 if free < 6 else 0.0)
            if sc > bs:
                bs, best = sc, (vx * n, vy * n)
        return best

    # ------------------------------------------------------------ zone
    def zone_target(self, obs):
        z = obs["zone"]
        t = obs["t"]
        c, r = z["center"], z["radius"]
        nc, nr = z["next_center"], z["next_radius"]
        if nr < r - 1 and t >= z["shrink_start"] - 25:
            c, r = nc, nr
        safe = r * 0.8 if r > 60 else r * 0.45
        return c, r, safe

    # ------------------------------------------------------------ main decision
    def act(self, obs):
        t0 = time.perf_counter()
        act = self._act(obs)
        dt = time.perf_counter() - t0
        if dt > 0.03:
            self.slow_until = obs["t"] + 1.0
        return act

    def _act(self, obs):
        t = obs["t"]
        me = obs["self"]
        x, y, z = me["pos"]
        vel = me["vel"]
        stats = me["stats"]
        size = 1 + 0.05 * me["cubes"]
        eye = [x, y, z + stats.get("eye_height", 8)]
        muzzle = [x, y, z + stats.get("muzzle_height", 7)]
        act = {"tick": obs["tick"]}

        # ---- offers and events
        if obs["offer"]:
            ids = [c["id"] for c in obs["offer"]["choices"]]
            best = min(range(len(ids)), key=lambda i: UPGRADE_PRIORITY.index(ids[i]) if ids[i] in UPGRADE_PRIORITY else 99)
            act["pick"] = best
        for e in obs["events"]:
            if e["type"] == "damaged":
                self.last_damage_t = t
                if e.get("bearing") is not None:
                    self.last_damage_bearing = e["bearing"]
            elif e["type"] == "take_failed":
                self.bad_pickups.add(e["id"])
                self.pickups.pop(e["id"], None)
            elif e["type"] == "took":
                self.pickups.pop(e["id"], None)

        # ---- memory of loot
        for p in obs["pickups"]:
            self.pickups[p["id"]] = (p["kind"], p["pos"])
        for c in obs["cubes"]:
            self.cubes[c["id"]] = c["pos"]
        for c in obs["crates"]:
            self.crates[c["id"]] = c["pos"]
        seen_p = {p["id"] for p in obs["pickups"]}
        seen_c = {c["id"] for c in obs["cubes"]}
        seen_k = {c["id"] for c in obs["crates"]}
        for mem, seen in ((self.pickups, seen_p), (self.cubes, seen_c), (self.crates, seen_k)):
            for i in list(mem):
                if i in seen:
                    continue
                pos = mem[i][1] if mem is self.pickups else mem[i]
                if math.hypot(pos[0] - x, pos[1] - y) < 30:
                    del mem[i]

        # ---- weapons
        slots = [(w["kind"] if w else None) for w in me["weapons"]]
        wobj = {w["kind"]: (i, w) for i, w in enumerate(me["weapons"]) if w}
        gun = wobj.get("gun")
        lightning = wobj.get("lightning")
        laser = wobj.get("laser")
        energy = me["energy"]

        # ---- enemies
        mechs = [m for m in obs["mechs"] if m["kind"] == "mech"]
        npcs = [m for m in obs["mechs"] if m["kind"] == "npc"]
        for m in mechs:
            m["_d"] = math.dist(m["pos"][:2], (x, y))
        mechs.sort(key=lambda m: m["_d"])
        near = [m for m in mechs if m["_d"] < 170]
        cands = []
        for m in near[:5]:
            c = m["pos"]
            sc = 1 + 0.05 * m.get("cubes", 0)
            tc = [c[0], c[1], c[2] + 4.5 * sc]
            if self.los(muzzle, tc):
                m["_c"] = tc
                m["_r"] = 2.5 * sc
                cands.append(m)
        target = None
        if cands:
            def key(m):
                hpf = m.get("hp", 1000) / max(m.get("max_hp", 1400), 1)
                return m["_d"] * (0.5 + hpf) * (0.75 if m["id"] == self.target_id else 1.0)
            target = min(cands, key=key)
            self.target_id = target["id"]
        hp_frac = me["hp"] / me["max_hp"]
        recently_hit = t - self.last_damage_t < 3.0

        # ---- movement intent
        mv = (0.0, 0.0)
        boost = False
        label = ""
        zc, zr, zsafe = self.zone_target(obs)
        zd = math.hypot(x - zc[0], y - zc[1])
        outside = not me["in_zone"]
        retreat = False  # v2: never run; every fight we leave is damage we do not deal

        if outside or zd > zsafe + 15:
            ux, uy = self.go(x, y, self.zone_point(zc, zsafe, x, y), t)
            mv = (ux, uy)
            boost = outside and energy > 8
            label = "zone"
            if target and not outside:
                mv = (mv[0] * 0.8, mv[1] * 0.8)
        elif retreat:
            mv = self.retreat_move(obs, x, y, z, near, zc, zsafe, t)
            boost = energy > 25
            label = "retreat"
        elif near and (target is not None or near[0]["_d"] < 70):
            mv = self.combat_move(obs, me, near, target, x, y, t, lightning, laser)
            label = "fight"
        else:
            mv = self.roam_move(obs, me, x, y, z, t, zc, zsafe, slots, act)
            label = "roam"

        # ---- bullets and beams
        dv = self.dodge(obs, x, y, z, size)
        if dv:
            mv = (mv[0] + 1.6 * dv[0], mv[1] + 1.6 * dv[1])

        # ---- stuck detection
        self.hist.append((t, x, y, math.hypot(*mv) > 0.3))
        self.hist = [h for h in self.hist if t - h[0] < 1.2]
        if t >= self.unstick_until and len(self.hist) >= 10 and all(h[3] for h in self.hist) and math.hypot(x - self.hist[0][1], y - self.hist[0][2]) < 1.5:
            self.unstick_until = t + 0.9
            a = self.rng.uniform(0, 2 * PI)
            self.unstick_dir = (math.cos(a), math.sin(a))
            self.path = []
            self.hist = []
        jump = False
        if t < self.unstick_until:
            mv = self.unstick_dir
            jump = True
            boost = True
        n = math.hypot(*mv)
        if n > 1:
            mv = (mv[0] / n, mv[1] / n)
        mv = self.steer(x, y, *mv) if t >= self.unstick_until else mv
        n2 = math.hypot(*mv)
        act["move"] = [mv[0], mv[1]]
        act["boost"] = bool(boost and n2 > 0.3 and energy > 5)
        if jump:
            act["jump"] = True

        # ---- aim and fire
        fire = [False, False]
        aim = None
        if target is not None:
            c = target["_c"]
            rel = [c[0] - muzzle[0], c[1] - muzzle[1], c[2] - muzzle[2]]
            tv = [target["vel"][0], target["vel"][1], 0.0]
            gspeed = gun[1]["stats"]["speed"] if gun else 252.0
            lp, tof = lead_point(rel, tv, gspeed)
            aim = aim_of(lp)
            d = target["_d"]
            ang_r = math.atan2(target["_r"] * 0.8, max(d, 1.0))
            cur = me["aim"]
            err = math.hypot(wrap(cur[0] - aim[0]) * math.cos(aim[1]), cur[1] - aim[1])
            if gun:
                i, gw_ = gun
                if gw_.get("ammo", 1) > 0 and gw_.get("reload", 0) <= 0 and d < gw_["stats"]["range"] - 10 and err < max(ang_r, 0.02):
                    fire[i] = True
            if lightning:
                i, lw = lightning
                rng = lw["stats"]["range"]
                cone = math.radians(lw["stats"].get("cone_deg", 30)) * 0.7
                dyaw = abs(wrap(me["aim"][0] - math.atan2(rel[1], rel[0])))
                if d < rng - 3 and dyaw < cone and energy >= 5:
                    fire[i] = True
            if laser:
                i, lz = laser
                direct = aim_of([rel[0] + tv[0] * 0.1, rel[1] + tv[1] * 0.1, rel[2]])
                lerr = math.hypot(wrap(cur[0] - direct[0]) * math.cos(direct[1]), cur[1] - direct[1])
                charge = lz.get("charge", 0.0)
                full = lz["stats"].get("charge", 0.75)
                if charge > 0:
                    if charge >= full * 0.9 and lerr < max(ang_r * 0.9, 0.012):
                        fire[i] = False  # release: the beam leaves along the current aim
                    elif charge < full * 0.9 or lerr >= 0.012:
                        fire[i] = d < lz["stats"]["range"] - 10
                        if charge >= full + 1.2:
                            fire[i] = False
                    aim = direct
                elif lz["cooldown"] <= 0.12 and d > 25 and d < lz["stats"]["range"] - 20 and hp_frac > 0.25 and lerr < 0.4:
                    fire[i] = True
                    aim = direct
        else:
            # look toward trouble: last damage, loud sounds, or where we are heading
            bearing = None
            if t - self.last_damage_t < 2.5 and self.last_damage_bearing is not None:
                bearing = self.last_damage_bearing
            else:
                loud = [s for s in obs["sounds"] if s["kind"] in ("gun", "laser", "mortar", "lightning", "explosion", "laser_charge") and s["age"] < 1.0]
                if loud:
                    bearing = loud[0]["bearing"]
            if bearing is None and near:
                m = near[0]
                bearing = math.atan2(m["pos"][1] - y, m["pos"][0] - x)
            if bearing is None and math.hypot(*mv) > 0.3:
                bearing = math.atan2(mv[1], mv[0])
            if bearing is not None:
                aim = [bearing, 0.0]
            if gun and gun[1].get("ammo", 12) < 4 and not near:
                act["reload"] = True
        if target is None and label == "roam":
            fx = self.farm_fire(obs, me, x, y, z, muzzle, near, gun)
            if fx is not None:
                aim, fire = fx
        if aim is not None:
            act["aim"] = [aim[0], max(-0.7, min(1.3, aim[1]))]
        act["fire"] = fire
        mf.draw.label(f"{label} hp{me['hp']:.0f} e{len(near)}")
        return act

    # ------------------------------------------------------------ movement modes
    def zone_point(self, zc, zsafe, x, y):
        d = math.hypot(x - zc[0], y - zc[1])
        if d < 1:
            return zc
        k = min(zsafe * 0.6, d) / d
        return (zc[0] + (x - zc[0]) * k, zc[1] + (y - zc[1]) * k)

    def combat_move(self, obs, me, near, target, x, y, t, lightning, laser):
        main = target or near[0]
        mx, my = main["pos"][0], main["pos"][1]
        d = max(math.hypot(mx - x, my - y), 1e-6)
        ux, uy = (mx - x) / d, (my - y) / d
        enemy_w = set(main.get("weapons", []))
        energy = me["energy"]
        if lightning and energy > 25:
            pref = 27
        elif laser:
            pref = 110
        else:
            pref = TUNE["pref_gun"]
        if "lightning" in enemy_w and not lightning:
            pref = max(pref, 65)
        if "laser" in enemy_w and lightning:
            pref = min(pref, 25)
        if "mortar" in enemy_w and not laser:
            pref = min(pref, 40)
        if t >= self.strafe_until:
            self.strafe = -self.strafe if self.rng.random() < TUNE["flip"] else self.strafe
            self.strafe_until = t + self.rng.uniform(TUNE["smin"], TUNE["smax"])
        k = max(-1.0, min(1.0, (d - pref) / 20.0))
        tx, ty = -uy * self.strafe, ux * self.strafe
        mvx, mvy = ux * k + tx * 0.9, uy * k + ty * 0.9
        # keep clear of a second enemy that is close
        if len(near) >= 2 and near[1]["_d"] < 45:
            ox, oy = norm(x - near[1]["pos"][0], y - near[1]["pos"][1])
            mvx += ox * 0.6
            mvy += oy * 0.6
        # stay inside the safe circle
        zc, zr, zsafe = self.zone_target(obs)
        dz = math.hypot(x - zc[0], y - zc[1])
        if dz > zsafe:
            cx, cy = norm(zc[0] - x, zc[1] - y)
            mvx += cx * 1.2
            mvy += cy * 1.2
        return (mvx, mvy)

    def retreat_move(self, obs, x, y, z, near, zc, zsafe, t):
        w = self.world
        if w is None:
            ox, oy = norm(x - near[0]["pos"][0], y - near[0]["pos"][1])
            return (ox, oy)
        if self.cover is None or t - self.cover_t > 0.6 or math.hypot(self.cover[0] - x, self.cover[1] - y) < 3:
            best, bs = None, -1e9
            eyes = [(m["pos"][0], m["pos"][1], m["pos"][2] + 8) for m in near[:2]]
            for r in (10, 18, 28):
                for k in range(12):
                    a = k * PI / 6
                    cx, cy = x + r * math.cos(a), y + r * math.sin(a)
                    if w.blocked(cx, cy) or math.hypot(cx - zc[0], cy - zc[1]) > zsafe + 20:
                        continue
                    if not w.line_free((x, y), (cx, cy)):
                        continue
                    hidden = sum(0 if w.los(e, (cx, cy, w.ground(cx, cy) + 5)) else 1 for e in eyes)
                    away = sum(math.hypot(cx - e[0], cy - e[1]) - math.hypot(x - e[0], y - e[1]) for e in eyes)
                    sc = hidden * 40 + away * 0.5 - r * 0.8
                    if sc > bs:
                        best, bs = (cx, cy), sc
            self.cover = best
            self.cover_t = t
        if self.cover is None:
            return norm(x - near[0]["pos"][0], y - near[0]["pos"][1])
        return self.go(x, y, self.cover, t)

    def roam_move(self, obs, me, x, y, z, t, zc, zsafe, slots, act):
        w = self.world
        reach = self.cfg["loot"]["pickup_radius"]
        held = slots[1] if len(slots) > 1 else "gun"
        best, bs, bid = None, 0.0, None
        grd = (lambda px, py: w.ground(px, py)) if w else (lambda px, py: z)

        def ok(pos):
            if math.hypot(pos[0] - zc[0], pos[1] - zc[1]) > zsafe:
                return False
            if pos[2] - grd(pos[0], pos[1]) > 3.5:
                return False
            return not (w and w.blocked(pos[0], pos[1]))

        cand = []
        for i, (kind, pos) in self.pickups.items():
            if i in self.bad_pickups or kind == "mortar" or kind == held or not ok(pos):
                continue
            if kind == "laser" or (kind == "lightning" and held is None):
                cand.append((9.0 if held is None else 7.0, pos, i))
        if me["hp"] > 0.5 * me["max_hp"] and t < 260:
            for m in obs["mechs"]:
                if m["kind"] == "npc" and ok(m["pos"]):
                    cand.append((3.0, m["pos"], None))
        for i, pos in self.cubes.items():
            if ok(pos):
                cand.append((4.0, pos, None))
        if t < 200:
            for i, pos in self.crates.items():
                if ok(pos):
                    cand.append((2.5, pos, None))
        for dp in obs["drops"]:
            if dp["lands_in"] < 12 and math.hypot(dp["pos"][0] - zc[0], dp["pos"][1] - zc[1]) < zsafe + 20:
                if dp["holder"] in (None, me["id"]):
                    cand.append((12.0, dp["pos"], None))
        for pos_v, pos, i in cand:
            d = math.hypot(pos[0] - x, pos[1] - y)
            if d > 160:
                continue
            sc = pos_v / (d + 25)
            if sc > bs:
                best, bs, bid = pos, sc, i
        if best is not None:
            d = math.hypot(best[0] - x, best[1] - y)
            if bid is not None and d < reach - 0.5 and abs(best[2] - z) < 3.5:
                act["take"] = bid
                if held is not None:
                    act["drop"] = 1
            if bid is None and d < 38 and any(m["kind"] == "npc" and math.dist(m["pos"][:2], best[:2]) < 3 for m in obs["mechs"]):
                return (0.0, 0.0)  # in range of a drone: stand and shoot it
            return self.go(x, y, best[:2], t)
        # nothing to do: stage inside the next circle
        dz = math.hypot(x - zc[0], y - zc[1])
        z_ = obs["zone"]
        if t >= z_["shrink_start"] - 25 or dz > zsafe * 0.5:
            return self.go(x, y, self.zone_point(zc, zsafe, x, y), t)
        return (0.0, 0.0)

    def farm_fire(self, obs, me, x, y, z, muzzle, near, gun):
        """Shoot visible crates and drones when no mech is around (cubes and weapons)."""
        if near or gun is None:
            return None
        best, bd = None, 70.0
        for m in obs["mechs"]:
            if m["kind"] == "npc":
                d = math.dist(m["pos"][:2], (x, y))
                if d < bd:
                    best, bd = [m["pos"][0], m["pos"][1], m["pos"][2] + 1.7], d
        for c in obs["crates"]:
            d = math.dist(c["pos"][:2], (x, y))
            if d < min(bd, 50.0) and abs(c["pos"][2] - z) < 6:
                best, bd = [c["pos"][0], c["pos"][1], c["pos"][2] + 1.2], d
        if best is None or not self.los(muzzle, best):
            return None
        rel = [best[0] - muzzle[0], best[1] - muzzle[1], best[2] - muzzle[2]]
        aim = aim_of(rel)
        cur = me["aim"]
        i, g = gun
        f = [False, False]
        if g.get("ammo", 1) > 0 and g.get("reload", 0) <= 0 and abs(wrap(cur[0] - aim[0])) < 0.05 and abs(cur[1] - aim[1]) < 0.08:
            f[i] = True
        return aim, f

    # ------------------------------------------------------------ incoming fire
    def dodge(self, obs, x, y, z, size):
        cx, cy, cz = x, y, z + 4.5 * size
        rad = 2.5 * size + 1.5
        sx = sy = 0.0
        hit = False
        for p in obs["projectiles"]:
            px, py, pz = p["pos"]
            vx, vy, vz = p["vel"]
            sp2 = vx * vx + vy * vy + vz * vz
            if sp2 < 1:
                continue
            if p["kind"] == "mortar":
                g = self.cfg["weapons"]["mortar"]["gravity"]
                # time to fall to our height
                a, b, c = -0.5 * g, vz, pz - cz
                disc = b * b - 4 * a * c
                if disc < 0:
                    continue
                tt = (-b - math.sqrt(disc)) / (2 * a)
                if tt <= 0 or tt > 2.5:
                    continue
                ix, iy = px + vx * tt, py + vy * tt
                dd = math.hypot(ix - x, iy - y)
                if dd < 11:
                    ax, ay = norm(x - ix, y - iy)
                    if ax == 0 and ay == 0:
                        ax, ay = 1.0, 0.0
                    sx += ax * 1.5
                    sy += ay * 1.5
                    hit = True
                continue
            tt = ((cx - px) * vx + (cy - py) * vy + (cz - pz) * vz) / sp2
            if tt < 0 or tt > 0.9:
                continue
            mx, my, mz = px + vx * tt - cx, py + vy * tt - cy, pz + vz * tt - cz
            miss = math.sqrt(mx * mx + my * my + mz * mz)
            if miss > rad + 2.0:
                continue
            nx, ny = norm(-vy, vx)  # perpendicular to the bullet path
            side = 1.0 if (mx * nx + my * ny) <= 0 else -1.0  # step away from the line through us
            if abs(mx * nx + my * ny) < 0.3:
                side = self.dodge_side.setdefault(p["id"], self.rng.choice((-1.0, 1.0)))
            sx += nx * side
            sy += ny * side
            hit = True
        for m in obs["mechs"]:
            if m["kind"] == "mech" and m.get("charging"):
                bx, by = x - m["pos"][0], y - m["pos"][1]
                dist = math.hypot(bx, by)
                if dist > 5 and abs(wrap(m["aim"][0] - math.atan2(by, bx))) < 0.12:
                    nx, ny = norm(-by, bx)
                    side = self.dodge_side.setdefault(("l", m["id"]), self.rng.choice((-1.0, 1.0)))
                    sx += nx * side * 1.2
                    sy += ny * side * 1.2
                    hit = True
        if len(self.dodge_side) > 200:
            self.dodge_side.clear()
        return (sx, sy) if hit else None


if __name__ == "__main__":
    mf.run(Bot)
