"""
Motion engine: smooth S-curves for pan, tilt and zoom.

Every axis follows its target (from the joystick, keys or buttons) jerk-limited rather than
jumping to it: acceleration ramps up gently, holds, and ramps down gently again. The result is
an S-shaped velocity profile, even when the target changes half-way.

Per-axis settings (stored in curves.json):
  accel_s   time from standstill to full speed
  decel_s   time from full speed to standstill
  s_share   0 = straight ramp, 1 = S-shaped all the way
  max       top speed of this axis (0..1)
Plus: expo (joystick sensitivity, applied in the page) and rate_hz (send rate).
"""
import copy
import json
import math
import os
import threading
import time

AXES = ("pan", "tilt", "zoom")

# Measured speed levels of the camera (1080p image, wide zoom):
# [level, image speed in px/s]. The camera maps ONVIF velocities to level = ceil(|v| * 24).
N_LEVELS = 24
DEFAULT_LEVELS = {
    "pan": [[1, 105], [2, 240], [8, 890], [9, 1011], [16, 1337], [24, 1755]],
    "tilt": [[1, 107], [8, 400], [24, 1245]],
}


def level_table(points, n=N_LEVELS):
    """Speed per level 1..n, linear between the measured points, monotonically increasing."""
    try:
        pts = sorted((int(a), float(b)) for a, b in points if 1 <= int(a) <= n and float(b) > 0)
    except (TypeError, ValueError):
        pts = []
    if len(pts) < 2:
        return [float(k) for k in range(1, n + 1)]
    res = []
    for k in range(1, n + 1):
        lo = max((p for p in pts if p[0] <= k), default=pts[0])
        hi = min((p for p in pts if p[0] >= k), default=pts[-1])
        res.append(lo[1] if hi[0] == lo[0] else lo[1] + (hi[1] - lo[1]) * (k - lo[0]) / (hi[0] - lo[0]))
    for i in range(1, n):
        res[i] = max(res[i], res[i - 1])
    return res


def quantize(u, table):
    """Fraction of top speed (-1..1) -> nearest camera level (0 = stop, sign = direction)."""
    target = abs(u) * table[-1]
    if target < table[0] * 0.5:
        return 0
    k = min(range(len(table)), key=lambda i: abs(table[i] - target)) + 1
    return k if u > 0 else -k


W_ANGLE = 6.0      # weight of the direction error (rad²) relative to the relative speed error
HYST = 1.15        # keep the previous level pair while it is at most 15 % worse


def quantize_pair(pan, tilt, tp, tt, prev=None):
    """Map both axes to camera levels together.

    Rounding each axis on its own makes the direction jump at low speed (e.g. 18° wanted,
    45° driven). Here the pair that best matches direction and speed together wins; a slightly
    worse previous pair is kept so the output does not flicker at cell boundaries.
    """
    vx, vy = pan * tp[-1], tilt * tt[-1]
    kp0, kt0 = quantize(pan, tp), quantize(tilt, tt)
    if kp0 == 0 and kt0 == 0:
        return 0, 0
    tgt = math.hypot(vx, vy)
    ang = math.atan2(vy, vx)

    def speed(k, t):
        return 0.0 if k == 0 else math.copysign(t[abs(k) - 1], k)

    def cost(kp, kt):
        cx, cy = speed(kp, tp), speed(kt, tt)
        cv = math.hypot(cx, cy)
        if cv == 0:
            return float("inf")
        da = abs(math.atan2(cy, cx) - ang)
        da = min(da, 2 * math.pi - da)
        return W_ANGLE * da * da + ((cv - tgt) / tgt) ** 2

    sx, sy = (1 if vx >= 0 else -1), (1 if vy >= 0 else -1)
    cand = set()
    for dp in range(-3, 4):
        for dt in range(-3, 4):
            a, b = abs(kp0) + dp, abs(kt0) + dt
            if 0 <= a <= N_LEVELS and 0 <= b <= N_LEVELS:
                cand.add((a * sx, b * sy))
    best = min(cand, key=lambda c: cost(*c))
    if prev and prev in cand and prev != best and cost(*prev) <= cost(*best) * HYST:
        return prev
    return best


DEFAULTS = {
    "pan": {"accel_s": 1.2, "decel_s": 1.0, "s_share": 0.8, "max": 1.0},
    "tilt": {"accel_s": 1.2, "decel_s": 1.0, "s_share": 0.8, "max": 1.0},
    "zoom": {"accel_s": 0.8, "decel_s": 0.6, "s_share": 0.6, "max": 1.0},
    "expo": 0.5,
    "rate_hz": 20,
    "pt_path": "onvif",
    "levels_enabled": True,
    "levels": DEFAULT_LEVELS,
}

# Keys used by earlier (German) versions of curves.json
LEGACY_KEYS = {"anfahren_s": "accel_s", "bremsen_s": "decel_s", "s_anteil": "s_share",
               "pt_weg": "pt_path", "stufen_aktiv": "levels_enabled", "stufen": "levels"}

TICK_HZ = 50          # engine tick
DEADMAN_S = 0.7       # no new input for this long -> stop smoothly
PT_EPS = 0.004        # smallest speed change worth sending to the camera


def limits(ramp_s, s_share):
    """Acceleration A and jerk J for a run 0 -> 1 in ramp_s seconds with S share s_share."""
    ramp_s = max(0.05, float(ramp_s))
    s = min(1.0, max(0.0, float(s_share)))
    if s < 0.01:
        return 1.0 / ramp_s, math.inf
    a = 1.0 / (ramp_s * (1.0 - s / 2.0))
    return a, 2.0 * a / (s * ramp_s)


def _english_keys(raw):
    if not isinstance(raw, dict):
        return raw
    return {LEGACY_KEYS.get(k, k): _english_keys(v) for k, v in raw.items()}


def clean_config(raw):
    """Validate values, clamp them to sensible ranges, drop anything unknown."""
    cfg = copy.deepcopy(DEFAULTS)
    if not isinstance(raw, dict):
        return cfg
    raw = _english_keys(raw)
    for ax in AXES:
        src = raw.get(ax) or {}
        for key, lo, hi in (("accel_s", 0.05, 6.0), ("decel_s", 0.05, 6.0), ("s_share", 0.0, 1.0), ("max", 0.05, 1.0)):
            try:
                cfg[ax][key] = min(hi, max(lo, float(src.get(key, cfg[ax][key]))))
            except (TypeError, ValueError):
                pass
    try:
        cfg["expo"] = min(1.0, max(0.0, float(raw.get("expo", cfg["expo"]))))
    except (TypeError, ValueError):
        pass
    try:
        cfg["rate_hz"] = int(min(30, max(5, int(raw.get("rate_hz", cfg["rate_hz"])))))
    except (TypeError, ValueError):
        pass
    if raw.get("pt_path") in ("onvif", "web"):
        cfg["pt_path"] = raw["pt_path"]
    if isinstance(raw.get("levels_enabled"), bool):
        cfg["levels_enabled"] = raw["levels_enabled"]
    st = raw.get("levels")
    if isinstance(st, dict):
        for ax in ("pan", "tilt"):
            pts = st.get(ax)
            if isinstance(pts, list) and len(pts) >= 2 and all(isinstance(p, (list, tuple)) and len(p) == 2 for p in pts):
                try:
                    cfg["levels"][ax] = [[int(a), float(b)] for a, b in pts]
                except (TypeError, ValueError):
                    pass
    return cfg


class Axis:
    __slots__ = ("v", "a", "target")

    def __init__(self):
        self.v = self.a = self.target = 0.0

    def step(self, dt, c):
        e = self.target - self.v
        if abs(e) < 1e-4 and abs(self.a) < 1e-3:
            self.v, self.a = self.target, 0.0
            return
        braking = abs(self.target) < abs(self.v) - 1e-9 or self.target * self.v < 0
        a_max, jerk = limits(c["decel_s"] if braking else c["accel_s"], c["s_share"])
        # desired acceleration: such that easing out with jerk J ends exactly on the target
        a_des = math.copysign(a_max if jerk == math.inf else min(a_max, math.sqrt(2.0 * jerk * abs(e))), e)
        if jerk == math.inf:
            self.a = a_des
        else:
            self.a += max(-jerk * dt, min(jerk * dt, a_des - self.a))
        nv = self.v + self.a * dt
        if (self.target - nv) * e <= 0:  # target reached or overshot
            nv, self.a = self.target, 0.0
        self.v = nv


class Engine:
    """Computes the curves and forwards the values to the camera."""

    def __init__(self, path, out_pt, out_zoom, log):
        self.path, self.out_pt, self.out_zoom, self.log = path, out_pt, out_zoom, log
        self.cfg = self._load()
        self.tables = {ax: level_table(self.cfg["levels"][ax]) for ax in ("pan", "tilt")}
        self.cur = {"pan": None, "tilt": None}
        self.ax = {k: Axis() for k in AXES}
        self.lock = threading.Lock()
        self.last_input = 0.0
        self.sent = {"pt": (0.0, 0.0), "zoom": 0.0}
        self.last_send = 0.0
        self.error = ""
        self.wake = threading.Event()
        self.locked = False  # refuse input while a calibration is running
        threading.Thread(target=self._run, daemon=True).start()

    # --- settings
    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return clean_config(json.load(f))
        except (OSError, ValueError):
            return copy.deepcopy(DEFAULTS)

    def get_config(self):
        return copy.deepcopy(self.cfg)

    def set_config(self, raw):
        cfg = clean_config(raw)
        with self.lock:
            self.cfg = cfg
            self.tables = {ax: level_table(cfg["levels"][ax]) for ax in ("pan", "tilt")}
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, self.path)
        return copy.deepcopy(cfg)

    # --- input
    def set_target(self, x, y, z):
        if self.locked:
            return dict(self.state(), error="Calibration running – controls locked")
        with self.lock:
            for k, v in (("pan", x), ("tilt", y), ("zoom", z)):
                v = max(-1.0, min(1.0, float(v)))
                self.ax[k].target = v * self.cfg[k]["max"]
            self.last_input = time.monotonic()
        self.wake.set()
        return self.state()

    def release(self):
        with self.lock:
            for a in self.ax.values():
                a.target = 0.0
        self.wake.set()
        return self.state()

    def halt(self):
        """Stop immediately, without a curve (e.g. before recalling a preset)."""
        with self.lock:
            for a in self.ax.values():
                a.v = a.a = a.target = 0.0
            self.sent = {"pt": (None, None), "zoom": None}  # always send the stop
        self.wake.set()

    def moving(self):
        return any(abs(a.v) > 1e-4 or abs(a.target) > 1e-4 for a in self.ax.values())

    def state(self):
        return {"v": {k: round(a.v, 3) for k, a in self.ax.items()},
                "target": {k: round(a.target, 3) for k, a in self.ax.items()},
                "moving": self.moving(), "path": self.cfg["pt_path"], "error": self.error,
                "level": dict(self.cur), "levels_enabled": self.cfg.get("levels_enabled", True)}

    # --- tick
    def _run(self):
        last = time.monotonic()
        while True:
            if not self.moving() and self.sent["pt"] == (0.0, 0.0) and self.sent["zoom"] == 0.0:
                self.wake.wait(0.5)
                self.wake.clear()
                last = time.monotonic()
            time.sleep(1.0 / TICK_HZ)
            now = time.monotonic()
            dt, last = min(0.1, now - last), now
            with self.lock:
                if now - self.last_input > DEADMAN_S and any(abs(a.target) > 0 for a in self.ax.values()):
                    for a in self.ax.values():
                        a.target = 0.0
                for k, a in self.ax.items():
                    a.step(dt, self.cfg[k])
                pan, tilt, zoom = self.ax["pan"].v, self.ax["tilt"].v, self.ax["zoom"].v
                rate = self.cfg["rate_hz"]
            self._send(now, pan, tilt, zoom, rate)

    def _send(self, now, pan, tilt, zoom, rate):
        if self.cfg.get("levels_enabled", True):
            # Map onto the real camera levels and only send on a level change:
            # every new drive command would otherwise restart the camera's own short ramp.
            prev = (self.cur.get("pan"), self.cur.get("tilt"))
            prev = prev if None not in prev and prev != (0, 0) else None
            kp, kt = quantize_pair(pan, tilt, self.tables["pan"], self.tables["tilt"], prev)
            self.cur = {"pan": kp, "tilt": kt}
            pan = 0.0 if kp == 0 else math.copysign((abs(kp) - 0.5) / N_LEVELS, kp)
            tilt = 0.0 if kt == 0 else math.copysign((abs(kt) - 0.5) / N_LEVELS, kt)
            eps = 1e-9
        else:
            self.cur = {"pan": None, "tilt": None}
            pan = 0.0 if abs(pan) < 1e-3 else pan
            tilt = 0.0 if abs(tilt) < 1e-3 else tilt
            eps = PT_EPS
        zoom = 0.0 if abs(zoom) < 1e-3 else zoom
        sp, st = self.sent["pt"]
        pt_stop = pan == 0.0 and tilt == 0.0
        pt_changed = sp is None or (pt_stop and (sp, st) != (0.0, 0.0)) or \
            (not pt_stop and (abs(pan - sp) >= eps or abs(tilt - st) >= eps))
        sz = self.sent["zoom"]
        z_changed = sz is None or zoom != sz
        if not (pt_changed or z_changed):
            return
        # stops and forced sends go out immediately, intermediate values at most at the send rate
        urgent = sp is None or sz is None or (pt_stop and pt_changed) or (zoom == 0.0 and z_changed)
        if not urgent and now - self.last_send < 1.0 / rate:
            return
        self.last_send = now
        try:
            if pt_changed:
                self.out_pt(pan, tilt)
                self.sent["pt"] = (pan, tilt)
            if z_changed:
                self.out_zoom(zoom)
                self.sent["zoom"] = zoom
            self.error = ""
        except Exception as e:  # noqa
            if str(e) != self.error:
                self.log(f"Motion: {e}")
            self.error = str(e)
            self.sent = {"pt": (None, None), "zoom": None}
            time.sleep(0.2)
