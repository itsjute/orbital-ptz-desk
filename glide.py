"""
Straight preset moves over VISCA, and the image-scale model for double-click aiming.

On its own the camera recalls presets on a bent path: its absolute-position command ignores
the tilt speed, so tilt finishes early. Here the server drives the move itself: it commands an
S-curve with the VISCA drive command (separate speed per axis), reads the position ten times a
second and corrects. At the end a slow absolute command sets the last few units exactly.

Speeds: the camera knows 24 levels per axis. The table level → units/s starts from values
measured during the VISCA check and can be measured precisely with "Calibrate speeds".
"""
import json
import math
import os
import threading
import time

# Measured during the VISCA check (units per second, start-up ramp excluded)
DEFAULT_SPEEDS = {
    "pan": {1: 300, 2: 590, 4: 1170, 8: 2130, 12: 2900},
    "tilt": {1: 170, 2: 340, 4: 580, 8: 1010, 12: 1500},
}
DEFAULT_CFG = {"mode": "straight", "max_level": 8, "min_duration": 1.5}
TICK = 0.1          # control interval
K_POS = 1.6         # position controller gain (1/s)
REST = 6            # leave this many units of remainder without an absolute command
LEAD = 0.15         # look-ahead: dead time of inquiry, command and the camera's start-up ramp

LEGACY_CFG = {"modus": "mode", "stufe_max": "max_level", "dauer_min": "min_duration"}
LEGACY_MODE = {"gerade": "straight", "kamera": "camera"}


def smooth(u):
    """Quintic S-curve: position and velocity (per unit time). Jerk-free at both ends."""
    u = max(0.0, min(1.0, u))
    return u ** 3 * (10 - 15 * u + 6 * u * u), 30 * u * u * (1 - u) ** 2


def _read_json(*paths):
    """First readable JSON among the given paths (new name first, then legacy name)."""
    for p in paths:
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            continue
    return None


class Speeds:
    def __init__(self, table=None):
        self.set(table or DEFAULT_SPEEDS)

    def set(self, table):
        self.raw = {ax: {int(k): float(v) for k, v in table[ax].items()} for ax in ("pan", "tilt")}
        self.full = {}
        for ax, pts in self.raw.items():
            ks = sorted(pts)
            row = []
            for lv in range(1, 25):
                if lv in pts:
                    row.append(pts[lv])
                    continue
                lo = max([k for k in ks if k < lv], default=None)
                hi = min([k for k in ks if k > lv], default=None)
                if lo is None:
                    row.append(pts[hi] * lv / hi)
                elif hi is None:  # continue with the slope of the last two points
                    a, b = ks[-2], ks[-1]
                    row.append(pts[b] + (pts[b] - pts[a]) / (b - a) * (lv - b))
                else:
                    row.append(pts[lo] + (pts[hi] - pts[lo]) * (lv - lo) / (hi - lo))
            self.full[ax] = row

    def speed(self, ax, lv):
        return self.full[ax][max(1, min(24, lv)) - 1]

    def level(self, ax, v):
        """Nearest level for |v| units/s; 0 = stand still."""
        v = abs(v)
        row = self.full[ax]
        if v < row[0] * 0.5:
            return 0
        return min(range(24), key=lambda i: abs(row[i] - v)) + 1


class Glide:
    """One move at a time. Any manual input cancels it."""

    def __init__(self, base_dir, log):
        self.log = log
        self.cfg_path = os.path.join(base_dir, "glide.json")
        self.speeds_path = os.path.join(base_dir, "visca_speeds.json")
        self.cfg = dict(DEFAULT_CFG)
        raw = _read_json(self.cfg_path, os.path.join(base_dir, "fahrten.json")) or {}
        raw = {LEGACY_CFG.get(k, k): v for k, v in raw.items()}
        if raw.get("mode") in LEGACY_MODE:
            raw["mode"] = LEGACY_MODE[raw["mode"]]
        self.cfg.update({k: v for k, v in raw.items() if k in DEFAULT_CFG})
        self.speeds = Speeds()
        self.measured = False
        raw = _read_json(self.speeds_path, os.path.join(base_dir, "visca_tempi.json")) or {}
        table = raw.get("speeds") or raw.get("tempi")
        if table:
            try:
                self.speeds.set(table)
                self.measured = True
            except (KeyError, ValueError, TypeError, ZeroDivisionError):
                self.speeds = Speeds()
        self.lock = threading.Lock()
        self.thread, self.abort = None, threading.Event()
        self.state = {"active": False}
        self.cal = {"running": False, "step": "", "error": ""}

    # --- settings
    def get_config(self):
        return dict(self.cfg, calibrated=self.measured)

    def set_config(self, data):
        c = dict(self.cfg)
        if data.get("mode") in ("straight", "camera"):
            c["mode"] = data["mode"]
        if "max_level" in data:
            c["max_level"] = int(max(1, min(24, int(data["max_level"]))))
        if "min_duration" in data:
            c["min_duration"] = round(max(0.5, min(10.0, float(data["min_duration"]))), 1)
        self.cfg = c
        tmp = self.cfg_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(c, f, indent=1)
        os.replace(tmp, self.cfg_path)
        return self.get_config()

    # --- planning
    def duration(self, dp, dt):
        vp = self.speeds.speed("pan", self.cfg["max_level"])
        vt = self.speeds.speed("tilt", self.cfg["max_level"])
        # the peak speed of the S-curve is 1.875 × the mean speed
        need = 1.875 * max(abs(dp) / vp, abs(dt) / vt)
        return max(self.cfg["min_duration"], need)

    # --- execution
    def cancel(self, wait=1.0):
        th = self.thread
        if th and th.is_alive():
            self.abort.set()
            th.join(wait)

    def start(self, link, start, target, on_done=None):
        """start/target: (pan, tilt). Runs in the background; returns the plan."""
        self.cancel()
        dp, dt = target[0] - start[0], target[1] - start[1]
        T = self.duration(dp, dt)
        self.abort = threading.Event()
        self.state = {"active": True, "start": list(start), "target": list(target), "duration_s": round(T, 2), "t0": time.time()}
        self.thread = threading.Thread(target=self._run, args=(link, start, target, T, self.abort, on_done), daemon=True)
        self.thread.start()
        return {"duration_s": round(T, 2), "delta": [dp, dt]}

    def _run(self, link, start, target, T, abort, on_done):
        sent = None
        dev_max, ok = 0.0, False
        D = (target[0] - start[0], target[1] - start[1])
        L = math.hypot(*D) or 1.0
        t0 = time.monotonic()
        try:
            while not abort.is_set():
                t = time.monotonic() - t0
                p, ti, *_ = link.pantilt()
                if t >= T:
                    break
                tl = t + LEAD
                s, ds = smooth(tl / T)
                want = []
                for i, (ax, cur) in enumerate((("pan", p), ("tilt", ti))):
                    lv0, d0 = sent[i] if sent else (0, 0)
                    pred = cur + (d0 * self.speeds.speed(ax, lv0) * LEAD if lv0 else 0)
                    des = start[i] + s * D[i]
                    v = ds / T * D[i] + K_POS * (des - pred)
                    if (target[i] - pred) * v <= 0 and tl >= T:   # never past the target
                        v = 0
                    lv = self.speeds.level(ax, v)
                    want.append((lv, (1 if v > 0 else -1) if lv else 0))
                # deviation across the line (for the log only)
                dev = abs((p - start[0]) * D[1] - (ti - start[1]) * D[0]) / L
                dev_max = max(dev_max, dev)
                if tl >= T and not any(lv for lv, _d in want):
                    break
                if want != sent:
                    (lp, dp_), (lt, dt_) = want
                    link.drive(lp or 1, lt or 1, dp_, dt_)
                    sent = want
                abort.wait(max(0.0, TICK - (time.monotonic() - t0 - t)))
            link.stop()
            if abort.is_set():
                self.log("Move cancelled.")
                return
            time.sleep(0.25)
            p, ti, np_, nt, _r = link.pantilt()
            if max(abs(p - target[0]), abs(ti - target[1])) > REST:
                link.absolute(2, 2, int(target[0]), int(target[1]), np_, nt)
                time.sleep(0.3)
            ok = True
            self.log(f"Move done ({T:.1f} s, largest deviation from the line {dev_max:.0f} units).")
        except Exception as e:  # noqa
            self.log(f"Move interrupted: {e}")
            try:
                link.stop()
            except Exception:  # noqa
                pass
        finally:
            self.state = dict(self.state, active=False, deviation=round(dev_max), ok=ok)
            if on_done and not abort.is_set():
                try:
                    on_done(ok)
                except Exception as e:  # noqa
                    self.log(f"After the move: {e}")

    # --- speed calibration
    LEVELS = (1, 2, 3, 4, 5, 6, 8, 10, 12, 14, 16, 18, 20, 24)

    def calibrate(self, link, before=None, after=None):
        if self.cal["running"]:
            return self.cal
        self.cancel()
        self.cal = {"running": True, "step": "starting …", "error": "", "result": None}
        threading.Thread(target=self._cal_run, args=(link, before, after), daemon=True).start()
        return self.cal

    def _cal_run(self, link, before, after):
        res = {"pan": {}, "tilt": {}}
        try:
            if before:
                before()
            for ax in ("pan", "tilt"):
                prev = 0.0
                for lv in self.LEVELS:
                    est = self.speeds.speed(ax, lv)
                    run = max(0.6, min(1.0, 0.25 + 1200 / est))   # at most ~1200 units of travel
                    vs = []
                    for d in (1, -1):
                        self.cal["step"] = f"{ax.capitalize()} level {lv} {'+' if d > 0 else '−'} …"
                        args = (lv, 1, d, 0) if ax == "pan" else (1, lv, 0, d)
                        link.drive(*args)
                        time.sleep(0.25)                      # let the ramp settle
                        a = link.pantilt()
                        ta = time.monotonic()
                        time.sleep(run - 0.25)
                        b = link.pantilt()
                        tb = time.monotonic()
                        link.stop()
                        time.sleep(0.35)
                        i = 0 if ax == "pan" else 1
                        vs.append(abs(b[i] - a[i]) / (tb - ta))
                    v = sum(vs) / 2
                    if lv > 1 and v < prev * 0.9:
                        self.log(f"Speed calibration: {ax} level {lv} slower than the one before ({v:.0f} < {prev:.0f}) – end stop? Axis finished.")
                        break
                    res[ax][lv] = round(v)
                    prev = v
            if len(res["pan"]) < 2 or len(res["tilt"]) < 2:
                raise RuntimeError("too few measurements")
            self.speeds.set(res)
            self.measured = True
            with open(self.speeds_path, "w", encoding="utf-8") as f:
                json.dump({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "speeds": res}, f, indent=1)
            self.cal.update(step="done", result=res)
            self.log("Speeds calibrated: " + " · ".join(f"{ax} " + ", ".join(f"{k}:{v}" for k, v in r.items()) for ax, r in res.items()))
        except Exception as e:  # noqa
            self.cal.update(step="error", error=str(e))
            self.log(f"Speed calibration failed: {e}")
            try:
                link.stop()
            except Exception:  # noqa
                pass
        finally:
            self.cal["running"] = False
            if after:
                after()


# --------------------------------------------------------------------------- double-click aiming
# Scale: camera units per full image width (pan) and image height (tilt), depending on zoom.
# Initial values from the motion calibration (level 4: ~1110 units/s ≈ 445 px/s at 1080p);
# after that the server learns from every click (the page measures where the point ended up).

DEFAULT_SCALE = (4800.0, 1350.0)


class ClickModel:
    def __init__(self, base_dir, log):
        self.log = log
        self.path = os.path.join(base_dir, "click_scale.json")
        self.samples = {}  # zoom bucket → [kp, kt, count]
        raw = _read_json(self.path, os.path.join(base_dir, "klick.json")) or {}
        try:
            raw = {int(k): v for k, v in (raw.get("scale") or raw.get("maßstab") or {}).items()}
            # drop nonsensical values (wrong sign or order of magnitude)
            self.samples = {k: v for k, v in raw.items()
                            if 0.25 < v[0] / DEFAULT_SCALE[0] < 4 and 0.25 < v[1] / DEFAULT_SCALE[1] < 4}
            if len(self.samples) < len(raw):
                log(f"Click scale: dropped {len(raw) - len(self.samples)} unusable measurement(s).")
        except (ValueError, AttributeError, TypeError, IndexError):
            self.samples = {}

    @staticmethod
    def bucket(zoom):
        return int(round((zoom or 0) / 128.0)) * 128

    def scale(self, zoom):
        """(kp, kt) for this zoom position; log-linear between measured points."""
        if not self.samples:
            return DEFAULT_SCALE
        z = zoom or 0
        ks = sorted(self.samples)
        lo = max([k for k in ks if k <= z], default=None)
        hi = min([k for k in ks if k >= z], default=None)
        if lo is None or hi is None or lo == hi:
            k = lo if lo is not None else hi
            return tuple(self.samples[k][:2])
        f = (z - lo) / (hi - lo)
        out = []
        for i in (0, 1):
            a, b = self.samples[lo][i], self.samples[hi][i]
            if a * b > 0:
                out.append(math.copysign(math.exp(math.log(abs(a)) * (1 - f) + math.log(abs(b)) * f), a))
            else:
                out.append(a if f < 0.5 else b)
        return tuple(out)

    def learn(self, zoom, du, dv, dx, dy, rx, ry):
        """du/dv: units driven · dx/dy: target in the image (fraction, right/down positive) · rx/ry: remainder after.

        Measurements that do not fit the current estimate (off by more than 2.5×, flipped sign,
        point barely moved) are dropped – usually the image was still moving.
        """
        b = self.bucket(zoom)
        ref = self.scale(zoom)
        n = self.samples.get(b, [0, 0, 0])[2]
        cur = list(self.samples[b][:2]) if b in self.samples else list(ref)
        meas = [None, None]
        shift = (dx - rx, dy - ry)
        moved = (du, -dv)
        want = (dx, dy)
        notes = []
        for i in (0, 1):
            if abs(moved[i]) <= 5 or abs(want[i]) < 0.04:
                continue  # barely moved on this axis: nothing to learn
            if abs(shift[i]) < max(0.03, 0.3 * abs(want[i])):
                notes.append(f"{'pan' if i == 0 else 'tilt'}: point barely moved")
                continue
            k = moved[i] / shift[i]
            ratio = k / ref[i]
            if not 0.4 < ratio < 2.5:
                notes.append(f"{'pan' if i == 0 else 'tilt'}: {k:.0f} does not fit {ref[i]:.0f}")
                continue
            meas[i] = k
        if notes:
            self.log("Click measurement dropped (" + "; ".join(notes) + ").")
        if meas == [None, None]:
            return cur + [n]
        w = 0.5 if n else 1.0
        for i in (0, 1):
            if meas[i] is not None:
                cur[i] = cur[i] * (1 - w) + meas[i] * w
        self.samples[b] = [round(cur[0], 1), round(cur[1], 1), n + 1]
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"scale": self.samples}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)
        return self.samples[b]
