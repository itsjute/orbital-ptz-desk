"""
Stabilize recordings from the PTZ camera.

The camera moves in fixed speed levels; every level change shows up as a small jolt in the
image. This module measures the image shift from frame to frame (phase correlation), smooths
the camera path with a short Gaussian window and shifts every frame accordingly. A slight zoom
(crop) hides the borders; if it is not enough, the correction is softly limited.

Needs: ffmpeg (any version) and the Python packages numpy and opencv-python-headless.
Manual use:  python stabilize.py <recording.mp4> [light|medium|strong]
"""
import os
import re
import subprocess
import sys

NOWIN = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# Smoothing window (Gaussian sigma in seconds) and crop (fraction of the image size)
PRESETS = {"light": (0.12, 0.04), "medium": (0.20, 0.06), "strong": (0.35, 0.10)}


class Cancelled(Exception):
    pass


def deps():
    """(ok, text) – are numpy and OpenCV available?"""
    try:
        import numpy
        import cv2
        return True, f"numpy {numpy.__version__}, OpenCV {cv2.__version__}"
    except Exception as e:  # noqa
        return False, f"{type(e).__name__}: {e}"


def probe(ff, src):
    err = subprocess.run([ff, "-hide_banner", "-i", src], capture_output=True, timeout=30,
                         creationflags=NOWIN).stderr.decode("utf-8", "replace")
    m = re.search(r"Video: [^\n]*?(\d{3,5})x(\d{3,5})", err)
    if not m:
        raise RuntimeError("no video track found")
    w, h = int(m.group(1)), int(m.group(2))
    m = re.search(r"([\d.]+) fps", err)
    fps = float(m.group(1)) if m else 30.0
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0
    return {"w": w, "h": h, "fps": fps, "duration_s": dur, "audio": "Audio:" in err}


def _gauss(x, sigma):
    import numpy as np
    if sigma <= 0:
        return x.copy()
    r = int(3 * sigma) + 1
    k = np.exp(-0.5 * (np.arange(-r, r + 1) / sigma) ** 2)
    k /= k.sum()
    return np.convolve(np.pad(x, (r, r), mode="edge"), k, "valid")


def run(ff, src, dst, preset="medium", progress=None, abort=None, crf=18, x264_preset="medium"):
    """Stabilizes src into dst. progress(phase, fraction) is called as it goes; abort() -> True cancels."""
    import numpy as np
    import cv2

    progress = progress or (lambda p, f: None)
    abort = abort or (lambda: False)
    sigma_s, crop = PRESETS[preset]
    info = probe(ff, src)
    w, h, fps = info["w"], info["h"], info["fps"]
    total = max(1, int(info["duration_s"] * fps))
    procs = []

    def stop_all():
        for p in procs:
            if p.poll() is None:
                p.kill()

    try:
        # 1. measure motion (downscaled, grayscale)
        sw = 960 if w > 960 else w
        sh = int(round(h * sw / w / 2) * 2)
        scale = w / sw
        dec = subprocess.Popen([ff, "-v", "error", "-nostdin", "-i", src, "-vf", f"scale={sw}:{sh}",
                                "-f", "rawvideo", "-pix_fmt", "gray", "-"], stdout=subprocess.PIPE, creationflags=NOWIN)
        procs.append(dec)
        win = cv2.createHanningWindow((sw, sh), cv2.CV_32F)
        prev, d, n = None, [(0.0, 0.0)], sw * sh
        while True:
            b = dec.stdout.read(n)
            if len(b) < n:
                break
            f = np.frombuffer(b, np.uint8).reshape(sh, sw).astype(np.float32)
            if prev is not None:
                (dx, dy), resp = cv2.phaseCorrelate(prev, f, win)
                d.append((dx * scale, dy * scale) if resp > 0.05 else d[-1])
                if len(d) % 15 == 0:
                    progress("1/2: measuring motion", 0.3 * min(1.0, len(d) / total))
                    if abort():
                        raise Cancelled()
            prev = f
        dec.wait()

        # 2. smooth the path, softly limit the correction to the crop margin
        d = np.array(d)
        P = np.cumsum(d, axis=0)
        S = np.column_stack([_gauss(P[:, 0], sigma_s * fps), _gauss(P[:, 1], sigma_s * fps)])
        C = S - P
        mx, my = w * crop / 2 * 0.9, h * crop / 2 * 0.9
        C[:, 0] = mx * np.tanh(C[:, 0] / mx)
        C[:, 1] = my * np.tanh(C[:, 1] / my)

        # 3. shift the frames and re-encode (audio is copied over)
        z = 1.0 / (1.0 - crop)
        dec = subprocess.Popen([ff, "-v", "error", "-nostdin", "-i", src, "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                               stdout=subprocess.PIPE, creationflags=NOWIN)
        enc = subprocess.Popen([ff, "-v", "error", "-nostdin", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}",
                                "-r", f"{fps}", "-i", "-", "-i", src, "-map", "0:v", "-map", "1:a?", "-c:v", "libx264",
                                "-crf", str(crf), "-preset", x264_preset, "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                                "-shortest", "-movflags", "+faststart", dst],
                               stdin=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NOWIN)
        procs += [dec, enc]
        n, i = w * h * 3, 0
        while True:
            b = dec.stdout.read(n)
            if len(b) < n:
                break
            f = np.frombuffer(b, np.uint8).reshape(h, w, 3)
            cx, cy = C[min(i, len(C) - 1)]
            M = np.array([[z, 0, (1 - z) * w / 2 + z * cx], [0, z, (1 - z) * h / 2 + z * cy]], np.float32)
            try:
                enc.stdin.write(cv2.warpAffine(f, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE).tobytes())
            except (BrokenPipeError, OSError):
                break
            i += 1
            if i % 15 == 0:
                progress("2/2: shifting and writing frames", 0.3 + 0.7 * min(1.0, i / total))
                if abort():
                    raise Cancelled()
        enc.stdin.close()
        err = enc.stderr.read().decode("utf-8", "replace").strip()
        if enc.wait() != 0:
            raise RuntimeError("ffmpeg: " + (err.splitlines()[-1] if err else "encoding failed"))
        dec.wait()
        progress("done", 1.0)
        return {"frames": i, "max_correction_px": [round(float(abs(C[:, 0]).max()), 1), round(float(abs(C[:, 1]).max()), 1)],
                "margin_px": [round(w * crop / 2), round(h * crop / 2)]}
    finally:
        stop_all()


if __name__ == "__main__":
    import shutil
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    src = sys.argv[1]
    pre = sys.argv[2] if len(sys.argv) > 2 else "medium"
    ff = shutil.which("ffmpeg") or "ffmpeg"
    out = os.path.splitext(src)[0] + "_smooth.mp4"
    res = run(ff, src, out, pre, lambda p, f: print(f"\r{p} {f * 100:5.1f} %", end="", flush=True))
    print("\n", out, res)
