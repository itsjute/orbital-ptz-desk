#!/usr/bin/env python3
"""
Orbital server.

Connects the control page in the browser with the PTZ camera on the network:
  * pan, tilt, zoom, focus, exposure, home and presets (camera web API, ONVIF, VISCA)
  * video via go2rtc (camera RTSP -> WebRTC in the browser)
  * recording, straight preset moves, double-click aiming, post stabilization

Runs on Python 3.8+ with the standard library only; stabilization additionally needs
numpy and opencv-python-headless (installable from the page).
Run from source, settings and state live next to this file (config.ini, *.json). The installed
app keeps them in %APPDATA%\\Orbital instead (or next to the program if a config.ini is there).
`python server.py` serves the page for any browser; `python app.py` opens it in its own window.
"""
import base64
import collections
import configparser
import datetime
import hashlib
import ipaddress
import json
import re
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET

import glide
import motion
import stabilize
import visca
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse, urlunparse
from xml.sax.saxutils import escape

FROZEN = bool(getattr(sys, "frozen", False))          # packaged app (PyInstaller)
HERE = os.path.dirname(os.path.abspath(__file__))
RES_DIR = getattr(sys, "_MEIPASS", HERE)               # bundled files: index.html, version.txt
APP_DIR = os.path.dirname(os.path.abspath(sys.executable)) if FROZEN else HERE


def _data_dir():
    """Where settings, state files and the log live."""
    env = (os.environ.get("ORBITAL_DATA") or os.environ.get("PTZ_DESK_DATA") or "").strip()
    if env:
        return os.path.abspath(os.path.expanduser(env))
    if not FROZEN or any(os.path.exists(os.path.join(APP_DIR, n)) for n in ("config.ini", "portable.txt")):
        return APP_DIR                                 # from source, or a portable copy of the app
    home = os.path.expanduser("~")
    if os.name == "nt":
        base, names = os.environ.get("APPDATA") or home, ("Orbital", "PTZ Desk")
    elif sys.platform == "darwin":
        base, names = os.path.join(home, "Library", "Application Support"), ("Orbital", "PTZ Desk")
    else:
        base, names = os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"), ("orbital", "ptz-desk")
    new, old = (os.path.join(base, n) for n in names)
    if not os.path.exists(new) and os.path.isdir(old):
        try:
            os.replace(old, new)                       # the app was called "PTZ Desk" before 1.1
        except OSError:
            return old
    return new


DATA_DIR = _data_dir()
os.makedirs(DATA_DIR, exist_ok=True)
INI = os.path.join(DATA_DIR, "config.ini")
LEGACY_INI = os.path.join(DATA_DIR, "kamera.ini")
PAGE = os.path.join(RES_DIR, "index.html")
LOG_PATH = os.path.join(DATA_DIR, "orbital.log")
GO2RTC_API = "http://127.0.0.1:1984"


def _read_version():
    for d in (RES_DIR, APP_DIR):
        try:
            with open(os.path.join(d, "version.txt"), encoding="utf-8") as f:
                v = f.read().strip()
                if v:
                    return v
        except OSError:
            pass
    return "source"


VERSION = _read_version()


def find_tool(name):
    """go2rtc / ffmpeg: next to the program, in the bundle, in the data folder, then on PATH."""
    exe = name + (".exe" if os.name == "nt" else "")
    for d in dict.fromkeys((APP_DIR, RES_DIR, os.path.join(APP_DIR, "bin"), DATA_DIR)):
        p = os.path.join(d, exe)
        if os.path.isfile(p):
            return p
    return shutil.which(name)


# --------------------------------------------------------------------------- log
# Everything the server reports goes to the console (if there is one), into a ring buffer that
# the page shows under Settings -> Log, and into orbital.log in the data folder.

_LOG = collections.deque(maxlen=4000)
_LOG_LOCK = threading.Lock()
_LOG_STATE = {"seq": 0, "file": None, "console": sys.stdout}
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def log(msg):
    stamp = time.strftime("%H:%M:%S")
    lines = [ln.rstrip() for ln in _ANSI.sub("", str(msg)).splitlines()] or [""]
    with _LOG_LOCK:
        for ln in lines:
            _LOG_STATE["seq"] += 1
            text_ = f"{stamp} {ln}"
            _LOG.append((_LOG_STATE["seq"], text_))
            f = _LOG_STATE["file"]
            if f:
                try:
                    f.write(text_ + "\n")
                    f.flush()
                except Exception:  # noqa
                    _LOG_STATE["file"] = None
            con = _LOG_STATE["console"]
            if con:
                try:
                    con.write(text_ + "\n")
                    con.flush()
                except Exception:  # noqa
                    _LOG_STATE["console"] = None


def log_since(seq):
    with _LOG_LOCK:
        return {"lines": [[n, t] for n, t in _LOG if n > seq], "next": _LOG_STATE["seq"]}


class _LogStream:
    """Stands in for stdout/stderr in the windowed app, so prints and tracebacks reach the log."""
    def __init__(self):
        self.buf = ""

    def write(self, s):
        self.buf += str(s)
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            if line.strip():
                log(line)
        return len(s)

    def flush(self):
        pass

    def isatty(self):
        return False


def setup_logging():
    if _LOG_STATE["file"] is None:
        try:
            if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 2 * 1024 ** 2:
                os.replace(LOG_PATH, LOG_PATH + ".old")
            _LOG_STATE["file"] = open(LOG_PATH, "a", encoding="utf-8")
            _LOG_STATE["file"].write("\n")
        except OSError:
            pass
    if sys.stdout is None:
        sys.stdout = _LogStream()
    if sys.stderr is None:
        sys.stderr = _LogStream()

    def thread_hook(a):
        import traceback
        log(f"Error in {a.thread.name if a.thread else 'thread'}:\n" +
            "".join(traceback.format_exception(a.exc_type, a.exc_value, a.exc_traceback)))
    threading.excepthook = thread_hook


# --------------------------------------------------------------------------- settings

DEFAULT_CONFIG = {
    "camera": {"ip": "192.168.1.100", "onvif_port": "8080", "web_port": "80", "control": "auto",
               "user": "admin", "password": "", "rtsp_main": "", "rtsp_sub": ""},
    "server": {"port": "8765", "local_only": "yes", "open_browser": "yes"},
    "recording": {"folder": "", "split_minutes": "30", "stream": "main"},
}

# kamera.ini of earlier (German) versions: section/key -> new section/key
LEGACY_INI_MAP = {
    ("kamera", "ip"): ("camera", "ip"), ("kamera", "onvif_port"): ("camera", "onvif_port"),
    ("kamera", "web_port"): ("camera", "web_port"), ("kamera", "steuerung"): ("camera", "control"),
    ("kamera", "benutzer"): ("camera", "user"), ("kamera", "passwort"): ("camera", "password"),
    ("kamera", "rtsp_haupt"): ("camera", "rtsp_main"), ("kamera", "rtsp_neben"): ("camera", "rtsp_sub"),
    ("dienst", "port"): ("server", "port"), ("dienst", "nur_lokal"): ("server", "local_only"),
    ("dienst", "browser_oeffnen"): ("server", "open_browser"),
    ("aufnahme", "ordner"): ("recording", "folder"), ("aufnahme", "teilen_minuten"): ("recording", "split_minutes"),
    ("aufnahme", "stream"): ("recording", "stream"),
}
LEGACY_VALUES = {"ja": "yes", "nein": "no", "haupt": "main", "neben": "sub"}

# State files of earlier versions: old name -> new name (contents are read in both formats)
LEGACY_FILES = {"kurven.json": "curves.json", "positionen.json": "presets.json", "fahrten.json": "glide.json",
                "visca_tempi.json": "visca_speeds.json", "klick.json": "click_scale.json"}


def migrate_legacy():
    """Carry settings of earlier versions over once: kamera.ini -> config.ini, old JSON names -> new."""
    if not os.path.exists(INI) and os.path.exists(LEGACY_INI):
        old = configparser.ConfigParser(interpolation=None)
        old.read(LEGACY_INI, encoding="utf-8-sig")
        new = configparser.ConfigParser(interpolation=None)
        new.read_dict(DEFAULT_CONFIG)
        for (s, k), (ns, nk) in LEGACY_INI_MAP.items():
            if old.has_option(s, k):
                v = old.get(s, k)
                new.set(ns, nk, LEGACY_VALUES.get(v.strip().lower(), v) if nk != "password" else v)
        if not new.get("recording", "folder").strip():
            legacy_dir = os.path.join(os.path.expanduser("~"), "Videos", "Kamera")
            if os.path.isdir(legacy_dir):
                new.set("recording", "folder", legacy_dir)
        with open(INI, "w", encoding="utf-8") as f:
            f.write("; Migrated from kamera.ini. See config.example.ini for all options.\n")
            new.write(f)
        log("Settings taken over from kamera.ini into config.ini (kamera.ini can be deleted).")
    for old_name, new_name in LEGACY_FILES.items():
        o, n = os.path.join(DATA_DIR, old_name), os.path.join(DATA_DIR, new_name)
        if os.path.exists(o) and not os.path.exists(n):
            try:
                os.replace(o, n)
                log(f"Renamed {old_name} -> {new_name}.")
            except OSError:
                pass


def read_ini():
    cp = configparser.ConfigParser(interpolation=None)
    cp.read_dict(DEFAULT_CONFIG)
    if os.path.exists(INI):
        cp.read(INI, encoding="utf-8-sig")
    return cp


def default_rtsp(ip, which):
    """RTSP address used when none is set (path of the TONGVEO and related cameras)."""
    return f"rtsp://{ip}:554/live/{'av0' if which == 'main' else 'av1'}" if ip else ""


def default_rec_dir():
    videos = os.path.join(os.path.expanduser("~"), "Videos")
    new, old = os.path.join(videos, "Orbital"), os.path.join(videos, "PTZ Desk")
    return old if os.path.isdir(old) and not os.path.isdir(new) else new   # keep using it after the rename


def load_config(quiet=False):
    cp = read_ini()
    if not os.path.exists(INI) and not quiet:
        log("No camera set up yet – enter it under Settings → Camera.")
    c, s, r = cp["camera"], cp["server"], cp["recording"]
    yes = lambda v: v.strip().lower() in ("yes", "true", "1", "on", "ja")
    stream = LEGACY_VALUES.get(r["stream"].strip().lower(), r["stream"].strip().lower()) or "main"
    return {
        "ip": c["ip"].strip(), "onvif_port": int(c["onvif_port"]), "user": c["user"].strip(),
        "web_port": int(c["web_port"]), "control": c["control"].strip().lower(),
        "password": c["password"],
        "rtsp_main": c["rtsp_main"].strip() or default_rtsp(c["ip"].strip(), "main"),
        "rtsp_sub": c["rtsp_sub"].strip() or default_rtsp(c["ip"].strip(), "sub"),
        "port": int(s["port"]), "local_only": yes(s["local_only"]), "open_browser": yes(s["open_browser"]),
        "rec_dir": os.path.expandvars(os.path.expanduser(r["folder"].strip())) or default_rec_dir(),
        "rec_minutes": int(r["split_minutes"] or 0), "rec_stream": stream,
        "configured": os.path.exists(INI),
    }


# --- editing the settings from the page (Settings -> Camera)

MASK = "••••"


def _mask_url(url):
    """Hide a password inside rtsp://user:password@host/… ."""
    try:
        u = urlparse(url)
        if u.password:
            host = u.netloc.rsplit("@", 1)[1]
            return urlunparse(u._replace(netloc=f"{u.username}:{MASK}@{host}"))
    except ValueError:
        pass
    return url


def _unmask_url(new, old):
    if MASK not in new:
        return new
    try:
        pw = urlparse(old).password or ""
    except ValueError:
        pw = ""
    return new.replace(MASK, pw, 1)


def config_view():
    cp = read_ini()
    c, sv, r = cp["camera"], cp["server"], cp["recording"]
    return {
        "camera": {"ip": c["ip"], "onvif_port": int(c["onvif_port"]), "web_port": int(c["web_port"]),
                   "control": c["control"], "user": c["user"], "password_set": bool(c["password"]),
                   "rtsp_main": _mask_url(c["rtsp_main"]), "rtsp_sub": _mask_url(c["rtsp_sub"])},
        "server": {"port": int(sv["port"]), "local_only": sv["local_only"].strip().lower() in ("yes", "true", "1", "on", "ja")},
        "recording": {"folder": r["folder"], "split_minutes": int(r["split_minutes"] or 0),
                      "stream": LEGACY_VALUES.get(r["stream"].strip().lower(), r["stream"].strip().lower()) or "main"},
        "defaults": {"rec_dir": default_rec_dir()},
        "configured": os.path.exists(INI), "file": INI, "data_dir": DATA_DIR,
    }


def save_config(data):
    """Validate, write config.ini and apply what can be applied without a restart."""
    cp = read_ini()
    cam, srv, rec = data.get("camera") or {}, data.get("server") or {}, data.get("recording") or {}

    def port(v, what):
        n = int(v)
        if not 1 <= n <= 65535:
            raise ValueError(f"{what} must be between 1 and 65535")
        return str(n)

    if "ip" in cam:
        ip = str(cam["ip"]).strip()
        if not ip or any(ch in ip for ch in " /@?#"):
            raise ValueError("enter the camera's IP address or host name")
        cp.set("camera", "ip", ip)
    for k, what in (("onvif_port", "ONVIF port"), ("web_port", "web port")):
        if k in cam:
            cp.set("camera", k, port(cam[k], what))
    if "control" in cam:
        v = str(cam["control"]).strip().lower()
        if v not in ("auto", "web", "onvif"):
            raise ValueError("control must be auto, web or onvif")
        cp.set("camera", "control", v)
    if "user" in cam:
        cp.set("camera", "user", str(cam["user"]).strip())
    if cam.get("password") is not None:          # null/missing = keep the stored password
        cp.set("camera", "password", str(cam["password"]))
    for k in ("rtsp_main", "rtsp_sub"):
        if k in cam:
            v = _unmask_url(str(cam[k]).strip(), cp.get("camera", k))
            if v and not v.lower().startswith(("rtsp://", "rtsps://")):
                raise ValueError("RTSP addresses start with rtsp://")
            cp.set("camera", k, v)
    if "port" in srv:
        cp.set("server", "port", port(srv["port"], "page port"))
    if "local_only" in srv:
        cp.set("server", "local_only", "yes" if srv["local_only"] else "no")
    if "folder" in rec:
        cp.set("recording", "folder", str(rec["folder"]).strip())
    if "split_minutes" in rec:
        n = int(rec["split_minutes"])
        if not 0 <= n <= 1440:
            raise ValueError("split minutes must be between 0 and 1440")
        cp.set("recording", "split_minutes", str(n))
    if "stream" in rec:
        v = str(rec["stream"]).strip().lower()
        if v not in ("main", "sub"):
            raise ValueError("recorded stream must be main or sub")
        cp.set("recording", "stream", v)

    tmp = INI + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("; Orbital settings – edited in the app (Settings → Camera) or by hand.\n"
                "; See config.example.ini for an explanation of every option.\n")
        cp.write(f)
    os.replace(tmp, INI)
    old, new = App.cfg, load_config(quiet=True)
    restart = apply_config(old, new)
    log("Settings saved" + (" – restart Orbital for the new page port/network setting." if restart else "."))
    return {"ok": True, "restart_required": restart, "config": config_view()}


CAMERA_KEYS = ("ip", "onvif_port", "web_port", "control", "user", "password")


def apply_config(old, new):
    """Bring the running objects in line with new settings. Returns True if a restart is needed."""
    # page port and network binding stay as they were until the next start
    for k in ("port", "local_only", "open_browser"):
        new[k] = old[k]
    App.cfg = new
    if any(old[k] != new[k] for k in CAMERA_KEYS):
        try:
            App.moves.cancel()
            App.motion.engine.halt()
        except Exception:  # noqa
            pass
        old_ptz, old_link = App.ptz, App.moves.link

        def let_go():
            # stopping the old camera may take a while if it is unreachable – not on the request
            for fn in (old_ptz.stop, old_link.close if old_link else None):
                try:
                    fn and fn()
                except Exception:  # noqa
                    pass
        threading.Thread(target=let_go, name="old-camera", daemon=True).start()
        App.moves.link = None
        App.moves.zoom_direct_ok = App.moves.focus_direct_ok = None
        App.expo.iris_max = None
        App.cam = Onvif(new)
        App.web = WebApi(new)
        App.ptz = Control(new, App.cam, App.web)
        App.dog.cam = App.ptz
        App.connecting = True
        threading.Thread(target=connect_camera, name="connect", daemon=True).start()
    if (old["rtsp_main"], old["rtsp_sub"]) != (new["rtsp_main"], new["rtsp_sub"]) or (not old["configured"] and new["configured"]):
        App.video.rtsp = {k: v for k, v in (("main", new["rtsp_main"]), ("sub", new["rtsp_sub"])) if v}

        def restart_video():
            try:
                App.video.restart()
            except Exception as e:  # noqa
                App.video.state = str(e)
            log(f"Video: {App.video.state}")
        threading.Thread(target=restart_video, name="video", daemon=True).start()
    App.rec.dir, App.rec.seg, App.rec.src = new["rec_dir"], max(0, new["rec_minutes"]) * 60, new["rec_stream"]
    cp = read_ini()
    want_local = cp.get("server", "local_only").strip().lower() in ("yes", "true", "1", "on", "ja")
    return int(cp.get("server", "port")) != old["port"] or want_local != old["local_only"]


# --------------------------------------------------------------------------- ONVIF

def lname(tag):
    return tag.rsplit("}", 1)[-1]


def first(el, name):
    if el is None:
        return None
    for e in el.iter():
        if lname(e.tag) == name:
            return e
    return None


def all_of(el, name):
    return [e for e in el.iter() if lname(e.tag) == name] if el is not None else []


def text(el, name, default=""):
    e = first(el, name)
    return (e.text or "").strip() if e is not None and e.text else default


def fnum(v):
    v = max(-1.0, min(1.0, float(v)))
    return f"{v:.3f}"


class OnvifError(Exception):
    pass


ENV_NS = ('xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
          'xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
          'xmlns:trt="http://www.onvif.org/ver10/media/wsdl" '
          'xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl" '
          'xmlns:tt="http://www.onvif.org/ver10/schema"')
WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
PW_DIGEST = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
B64 = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary"


class Onvif:
    def __init__(self, cfg):
        self.cfg = cfg
        self.base = f"http://{cfg['ip']}:{cfg['onvif_port']}"
        self.device_url = self.base + "/onvif/device_service"
        self.media_url = self.base + "/onvif/media_service"
        self.ptz_url = self.base + "/onvif/ptz_service"
        self.offset = 0.0
        self.profile = None
        self.profiles = []
        self.info = {}
        self.node = {}
        self.connected = False
        self.last_error = ""
        self.lock = threading.Lock()
        pm = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        pm.add_password(None, self.base, cfg["user"], cfg["password"])
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),  # never talk to the camera on the LAN through a proxy
            urllib.request.HTTPDigestAuthHandler(pm),
            urllib.request.HTTPBasicAuthHandler(pm),
        )

    # --- transport
    def _security(self):
        if not self.cfg["user"]:
            return ""
        nonce = secrets.token_bytes(16)
        now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=self.offset)
        created = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + self.cfg["password"].encode("utf-8")).digest()).decode()
        return (f'<wsse:Security s:mustUnderstand="1" xmlns:wsse="{WSSE}" xmlns:wsu="{WSU}"><wsse:UsernameToken>'
                f'<wsse:Username>{escape(self.cfg["user"])}</wsse:Username>'
                f'<wsse:Password Type="{PW_DIGEST}">{digest}</wsse:Password>'
                f'<wsse:Nonce EncodingType="{B64}">{base64.b64encode(nonce).decode()}</wsse:Nonce>'
                f'<wsu:Created>{created}</wsu:Created></wsse:UsernameToken></wsse:Security>')

    def call(self, url, action, body, auth=True, timeout=6):
        env = (f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope {ENV_NS}><s:Header>'
               f'{self._security() if auth else ""}</s:Header><s:Body>{body}</s:Body></s:Envelope>')
        req = urllib.request.Request(url, data=env.encode("utf-8"), method="POST", headers={
            "Content-Type": f'application/soap+xml; charset=utf-8; action="{action}"'})
        try:
            with self.opener.open(req, timeout=timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raw = e.read() or b""
            msg = self._fault(raw)
            if e.code in (401, 403) or self._is_auth(msg):
                raise OnvifError("Login rejected – check user/password in config.ini"
                                 + (f" ({msg})" if msg else ""))
            raise OnvifError(msg or f"HTTP {e.code} from the camera")
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            raise OnvifError(f"Camera not reachable at {url} ({reason})")
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            raise OnvifError("Camera reply is not valid XML")
        if first(root, "Fault") is not None:
            msg = self._fault(raw)
            if self._is_auth(msg):
                raise OnvifError("Login rejected – check user/password in config.ini (" + msg + ")")
            raise OnvifError(msg or "Camera reports an error")
        return root

    @staticmethod
    def _is_auth(msg):
        m = (msg or "").lower()
        return "notauthorized" in m or "not authorized" in m or "authority" in m or "unauthorized" in m

    @staticmethod
    def _fault(raw):
        try:
            root = ET.fromstring(raw)
        except Exception:
            return ""
        parts = [e.text.strip() for e in all_of(root, "Text") if e.text and e.text.strip()]
        parts += [e.text.strip() for e in all_of(root, "Value") if e.text and e.text.strip() and ":" in e.text]
        return " · ".join(dict.fromkeys(parts))

    def _fix(self, xaddr, fallback):
        """Cameras often report their old factory IP in XAddr. Keep the path, use the address from config.ini."""
        if not xaddr:
            return fallback
        u = urlparse(xaddr.strip().split()[0])
        return urlunparse(("http", f"{self.cfg['ip']}:{self.cfg['onvif_port']}", u.path or "/", "", u.query, ""))

    # --- connecting
    def connect(self):
        with self.lock:
            self.connected = False
            # 1. camera clock (no login), so that login does not fail on clock skew
            try:
                r = self.call(self.device_url, "http://www.onvif.org/ver10/device/wsdl/GetSystemDateAndTime",
                              "<tds:GetSystemDateAndTime/>", auth=False)
                utc = first(r, "UTCDateTime")
                if utc is not None:
                    cam = datetime.datetime(int(text(utc, "Year")), int(text(utc, "Month")), int(text(utc, "Day")),
                                            int(text(utc, "Hour")), int(text(utc, "Minute")), int(text(utc, "Second")),
                                            tzinfo=datetime.timezone.utc)
                    self.offset = (cam - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
                    if abs(self.offset) > 5:
                        log(f"Camera clock is off by {self.offset:+.0f} s – compensating.")
            except OnvifError as e:
                if "not reachable" in str(e):
                    self.last_error = str(e)
                    raise
                self.offset = 0.0
            except (ValueError, TypeError):
                self.offset = 0.0

            # 2. device info
            r = self.call(self.device_url, "http://www.onvif.org/ver10/device/wsdl/GetDeviceInformation",
                          "<tds:GetDeviceInformation/>")
            self.info = {k: text(r, k) for k in ("Manufacturer", "Model", "FirmwareVersion", "SerialNumber", "HardwareId")}

            # 3. service addresses
            try:
                r = self.call(self.device_url, "http://www.onvif.org/ver10/device/wsdl/GetCapabilities",
                              "<tds:GetCapabilities><tds:Category>All</tds:Category></tds:GetCapabilities>")
                media, ptz = first(r, "Media"), first(r, "PTZ")
                self.media_url = self._fix(text(media, "XAddr"), self.media_url)
                self.ptz_url = self._fix(text(ptz, "XAddr"), self.ptz_url)
            except OnvifError as e:
                log(f"GetCapabilities failed ({e}), using default paths.")

            # 4. profiles
            r = self.call(self.media_url, "http://www.onvif.org/ver10/media/wsdl/GetProfiles", "<trt:GetProfiles/>")
            self.profiles = []
            for p in all_of(r, "Profiles"):
                enc = first(p, "VideoEncoderConfiguration")
                self.profiles.append({
                    "token": p.get("token", ""),
                    "name": text(p, "Name"),
                    "ptz": first(p, "PTZConfiguration") is not None,
                    "ptz_conf": (first(p, "PTZConfiguration").get("token", "") if first(p, "PTZConfiguration") is not None else ""),
                    "codec": text(enc, "Encoding"),
                    "width": text(first(enc, "Resolution"), "Width"),
                    "height": text(first(enc, "Resolution"), "Height"),
                })
            if not self.profiles:
                raise OnvifError("Camera reports no media profiles")
            self.profile = next((p for p in self.profiles if p["ptz"]), self.profiles[0])

            # 5. PTZ node (optional)
            self.node = {}
            try:
                r = self.call(self.ptz_url, "http://www.onvif.org/ver20/ptz/wsdl/GetNodes", "<tptz:GetNodes/>")
                n = first(r, "PTZNode")
                if n is not None:
                    self.node = {"name": text(n, "Name"), "presets_max": text(n, "MaximumNumberOfPresets"),
                                 "home": text(n, "HomeSupported")}
            except OnvifError:
                pass
            self.connected = True
            self.last_error = ""
            return True

    def ensure(self):
        if not self.connected:
            self.connect()

    def _ptz(self, op, inner):
        self.ensure()
        with self.lock:
            return self.call(self.ptz_url, f"http://www.onvif.org/ver20/ptz/wsdl/{op}",
                             f"<tptz:{op}><tptz:ProfileToken>{escape(self.profile['token'])}</tptz:ProfileToken>{inner}</tptz:{op}>")

    # --- PTZ
    def move(self, x, y, z):
        vel = ""
        if x or y:
            vel += f'<tt:PanTilt x="{fnum(x)}" y="{fnum(y)}"/>'
        if z:
            vel += f'<tt:Zoom x="{fnum(z)}"/>'
        if not vel:
            return self.stop()
        self._ptz("ContinuousMove", f"<tptz:Velocity>{vel}</tptz:Velocity>")

    def zoom_step(self, dz):
        self._ptz("RelativeMove", f'<tptz:Translation><tt:Zoom x="{fnum(dz)}"/></tptz:Translation>')

    def diagnose(self):
        """Raw PTZ replies of the camera, to make zoom and position behaviour traceable."""
        self.ensure()
        tok = escape(self.profile["token"])
        conf = escape(self.profile.get("ptz_conf") or "")
        ops = [
            ("GetNodes", "<tptz:GetNodes/>"),
            ("GetConfigurations", "<tptz:GetConfigurations/>"),
            ("GetConfigurationOptions", f"<tptz:GetConfigurationOptions><tptz:ConfigurationToken>{conf}</tptz:ConfigurationToken></tptz:GetConfigurationOptions>"),
            ("GetServiceCapabilities", "<tptz:GetServiceCapabilities/>"),
            ("GetStatus", f"<tptz:GetStatus><tptz:ProfileToken>{tok}</tptz:ProfileToken></tptz:GetStatus>"),
        ]
        out = {}
        for op, body in ops:
            try:
                with self.lock:
                    r = self.call(self.ptz_url, f"http://www.onvif.org/ver20/ptz/wsdl/{op}", body)
                b = first(r, "Body")
                out[op] = ET.tostring(b if b is not None else r, encoding="unicode")[:8000]
            except OnvifError as e:
                out[op] = f"ERROR: {e}"
        return out

    def stop(self):
        # Some cameras ignore "Stop" while zooming and run to the end stop.
        # Velocity 0 stops them reliably; Stop is sent afterwards for good measure.
        try:
            self._ptz("ContinuousMove", '<tptz:Velocity><tt:PanTilt x="0" y="0"/><tt:Zoom x="0"/></tptz:Velocity>')
        except OnvifError:
            pass
        self._ptz("Stop", "<tptz:PanTilt>true</tptz:PanTilt><tptz:Zoom>true</tptz:Zoom>")

    def home(self):
        self._ptz("GotoHomePosition", "")

    def set_home(self):
        self._ptz("SetHomePosition", "")

    def presets(self):
        r = self._ptz("GetPresets", "")
        out = []
        for p in all_of(r, "Preset"):
            out.append({"token": p.get("token", ""), "name": text(p, "Name")})
        return out

    def goto(self, token):
        self._ptz("GotoPreset", f"<tptz:PresetToken>{escape(str(token))}</tptz:PresetToken>")

    def set_preset(self, token, name):
        inner = f"<tptz:PresetName>{escape(name)}</tptz:PresetName>"
        if token:
            inner += f"<tptz:PresetToken>{escape(str(token))}</tptz:PresetToken>"
        r = self._ptz("SetPreset", inner)
        return text(r, "PresetToken", str(token or ""))

    def status(self):
        r = self._ptz("GetStatus", "")
        pos = first(r, "Position")
        pt, zm = first(pos, "PanTilt"), first(pos, "Zoom")
        res = {}
        if pt is not None:
            res["pan"], res["tilt"] = pt.get("x"), pt.get("y")
        if zm is not None:
            res["zoom"] = zm.get("x")
        ms = first(r, "MoveStatus")
        if ms is not None:
            res["move_status"] = {lname(c.tag): (c.text or "").strip() for c in ms}
        return res


# --------------------------------------------------------------------------- camera web API
# The same calls the camera's own web interface uses (taken from its JavaScript):
#   POST /api/setLogin      {Username, Password, lg, VerifyID, ID}         -> result.ID (session)
#   POST /api/setPTZCmd     {Channel, PtzCmd, param1 (speed), param2, ID}
#        PtzCmd: 0 up, 1 down, 2 left, 3 right, 14 stop moving,
#                12 zoom in, 13 zoom out, 16 zoom stop, 15 home, 9 focus near, 8 focus far, 17 focus stop
#   POST /api/getPTZPreset  {ID}                                            -> result.PresetList
#   POST /api/setPTZPreset  {setPreset: set|recall|delete, Channel, PresetIndex, PresetName, ID}
#   POST /api/AjaxGetCfgVImg {ID, Channel, Profile}                         -> result.ability / result.PTZ
#   POST /api/AjaxSetCfgVImg {saveAllChannel, setChannel, cmd, param, profile}  e.g. cmd FocusMode 2 auto / 3 manual
#   GET  /api/getAiControl, POST /api/setAiControl {aiCmd, value}           -> auto tracking

class WebApiError(OnvifError):
    pass


class WebApi:
    UP, DOWN, LEFT, RIGHT, STOP_PT, ZOOM_IN, ZOOM_OUT, HOME, ZOOM_STOP = 0, 1, 2, 3, 14, 12, 13, 15, 16

    def __init__(self, cfg):
        self.cfg = cfg
        self.base = f"http://{cfg['ip']}:{cfg['web_port']}/api"
        self.id = None
        self.connected = False
        self.last_error = ""
        self.ability = {}
        self.speeds = {}
        self.preset_max = None
        self.pt_active = False
        self.z_active = False
        self.lock = threading.Lock()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _post(self, path, payload, timeout=5):
        hdr = {"Content-Type": "application/json;charset=utf-8"}
        if self.id:
            hdr["Cookie"] = f"Username={self.cfg['user']}; id={self.id}"
        req = urllib.request.Request(self.base + path, data=json.dumps(payload).encode("utf-8"), headers=hdr, method="POST")
        try:
            with self.opener.open(req, timeout=timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise WebApiError(f"Web API: HTTP {e.code} on {path}")
        except (urllib.error.URLError, OSError) as e:
            raise WebApiError(f"Web API not reachable ({getattr(e, 'reason', e)})")
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            raise WebApiError(f"Web API: unexpected reply on {path}")

    @staticmethod
    def _err(js):
        try:
            c = int(js.get("errorCode") or 0)
        except (TypeError, ValueError):
            c = 0
        return c if c >= 1000 else 0

    def _login(self):
        self.id = None
        js = self._post("/setLogin", {"Username": self.cfg["user"], "Password": self.cfg["password"],
                                      "lg": "1", "VerifyID": "", "ID": ""})
        if self._err(js):
            self.connected = False
            raise WebApiError(f"Web interface login rejected ({js.get('errorMessage') or js.get('errorCode')})"
                              " – check user/password in config.ini")
        rid = (js.get("result") or {}).get("ID")
        if not rid:
            raise WebApiError("Web interface login: no session ID received")
        self.id, self.connected, self.last_error = rid, True, ""

    def call(self, path, payload):
        with self.lock:
            if not self.id:
                self._login()
            js = self._post(path, dict(payload, ID=self.id))
            if self._err(js):  # session expired or similar: log in again once and retry
                self._login()
                js = self._post(path, dict(payload, ID=self.id))
                if self._err(js):
                    raise WebApiError(f"Camera reports an error on {path}: {js.get('errorMessage') or js.get('errorCode')}")
            return js

    def connect(self):
        try:
            with self.lock:
                self._login()
        except WebApiError as e:
            self.last_error = str(e)
            raise
        try:
            res = self.call("/AjaxGetCfgVImg", {"Channel": 0, "Profile": 0}).get("result") or {}
            ab, ptz = res.get("ability") or {}, res.get("PTZ") or {}

            def rng(k, dflt):
                try:
                    return int(str((ab.get(k) or {})["min"])), int(str((ab.get(k) or {})["max"]))
                except (KeyError, TypeError, ValueError):
                    return dflt
            self.ability = {"ptz": rng("PTZSpeed", (1, 24)), "zoom": rng("ZoomSpeed", (0, 7))}
            self.speeds = {k: ptz.get(k) for k in ("PTZSpeed", "PTZPanSpeed", "PTZTiltSpeed", "ZoomSpeed") if k in ptz}
        except WebApiError as e:
            log(f"Speed ranges not readable ({e}), using defaults.")
            self.ability = {"ptz": (1, 24), "zoom": (0, 7)}

    def _spd(self, frac, kind):
        lo, hi = self.ability.get(kind) or ((1, 24) if kind == "ptz" else (0, 7))
        frac = max(0.0, min(1.0, abs(frac)))
        return max(lo, min(hi, round(lo + frac * (hi - lo))))

    NAMES = {0: "up", 1: "down", 2: "left", 3: "right", 14: "stop", 12: "zoom in", 13: "zoom out",
             16: "zoom stop", 15: "home", 9: "focus near", 8: "focus far", 17: "focus stop"}

    def cmd(self, code, p1):
        self.call("/setPTZCmd", {"Channel": 0, "PtzCmd": code, "param1": p1, "param2": 0})
        if code != getattr(self, "_last_code", None):
            log(f"→ camera: {self.NAMES.get(code, code)}" + (f" (speed {p1})" if code not in (14, 16, 17) else ""))
        self._last_code = code

    def get_ai(self):
        """Auto-tracking state as in the web interface: GET /api/getAiControl."""
        hdr = {}
        if self.id:
            hdr["Cookie"] = f"Username={self.cfg['user']}; id={self.id}"
        req = urllib.request.Request(self.base + "/getAiControl", headers=hdr, method="GET")
        try:
            with self.opener.open(req, timeout=5) as r:
                js = json.loads(r.read() or b"{}")
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise WebApiError(f"Tracking state not readable ({getattr(e, 'reason', e)})")
        return js.get("result") or {}

    def set_ai(self, cmd, value):
        if cmd not in ("ai_enable", "ai_auto_zoom", "ai_auto_tilt"):
            raise ValueError("unknown tracking setting")
        if not self.id:
            with self.lock:
                self._login()
        hdr = {"Content-Type": "application/json;charset=utf-8", "Cookie": f"Username={self.cfg['user']}; id={self.id}"}
        req = urllib.request.Request(self.base + "/setAiControl", data=json.dumps({"aiCmd": cmd, "value": str(value)}).encode(),
                                     headers=hdr, method="POST")
        try:
            with self.opener.open(req, timeout=5) as r:
                js = json.loads(r.read() or b"{}")
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise WebApiError(f"Tracking could not be switched ({getattr(e, 'reason', e)})")
        if js.get("status") == "freeze":
            raise WebApiError("Camera does not accept the tracking setting right now (status \"freeze\"), wait a moment")
        log(f"→ camera: tracking setting {cmd} = {value}")
        return self.get_ai()

    def move(self, x, y, z):
        if x or y:
            if abs(x) >= abs(y):
                self.cmd(self.RIGHT if x > 0 else self.LEFT, self._spd(x, "ptz"))
            else:
                self.cmd(self.UP if y > 0 else self.DOWN, self._spd(y, "ptz"))
            self.pt_active = True
        elif self.pt_active:
            self.cmd(self.STOP_PT, 0)
            self.pt_active = False
        if z:
            self.cmd(self.ZOOM_IN if z > 0 else self.ZOOM_OUT, self._spd(z, "zoom"))
            self.z_active = True
        elif self.z_active:
            self.cmd(self.ZOOM_STOP, 0)
            self.z_active = False

    def stop(self):
        self.pt_active = self.z_active = False
        self.cmd(self.STOP_PT, 0)
        self.cmd(self.ZOOM_STOP, 0)

    def zoom_step(self, dz):
        self.cmd(self.ZOOM_IN if dz > 0 else self.ZOOM_OUT, self._spd(min(1.0, abs(dz) / 0.15), "zoom"))
        time.sleep(0.15)
        self.cmd(self.ZOOM_STOP, 0)

    def home(self):
        self.cmd(self.HOME, self._spd(0.7, "ptz"))

    # --- focus (as in the web interface: FocusMode 2 = auto, 3 = manual; PtzCmd 9 near, 8 far, 17 stop)
    FOCUS_NEAR, FOCUS_FAR, FOCUS_STOP = 9, 8, 17

    def set_cfg(self, cmd, param):
        return self.call("/AjaxSetCfgVImg", {"saveAllChannel": 0, "setChannel": 0, "cmd": str(cmd), "param": str(param), "profile": 0})

    def focus_mode(self):
        res = self.call("/AjaxGetCfgVImg", {"Channel": 0, "Profile": 0}).get("result") or {}
        m = str((res.get("PTZ") or {}).get("FocusMode", ""))
        return {"2": "auto", "3": "manual"}.get(m, m or None)

    def set_focus_mode(self, mode):
        self.set_cfg("FocusMode", "2" if mode == "auto" else "3")
        log(f"→ camera: focus {'auto' if mode == 'auto' else 'manual (locked)'}")

    def presets(self):
        res = self.call("/getPTZPreset", {}).get("result") or {}
        self.preset_max = res.get("PresetMaxCount")
        out = []
        for p in res.get("PresetList") or []:
            out.append({"token": str(p.get("PresetIndex")), "name": p.get("PresetName") or ""})
        return out

    def goto(self, token):
        # Like the camera's web interface: preset index as a string, otherwise it reports "set Failed!"
        self.call("/setPTZPreset", {"Channel": 0, "PresetIndex": str(token), "PresetName": "", "setPreset": "recall"})
        log(f"→ camera: recall preset {token}")

    def set_preset(self, token, name):
        # The web interface only allows letters and digits, at most 30 characters
        name = re.sub(r"[^A-Za-z0-9一-龥]", "", str(name))[:30] or f"Preset{token}"
        self.call("/setPTZPreset", {"setPreset": "set", "Channel": "0", "PresetIndex": str(token), "PresetName": name})
        return str(token)


class Control:
    """Picks the control path: the camera's web API (preferred) or ONVIF."""

    def __init__(self, cfg, onvif, web):
        self.cfg, self.onvif, self.web = cfg, onvif, web

    def mode(self):
        m = self.cfg["control"]
        if m in ("web", "onvif"):
            return m
        return "web" if self.web.connected else "onvif"

    def _b(self):
        return self.web if self.mode() == "web" else self.onvif

    def move(self, x, y, z): return self._b().move(x, y, z)
    def stop(self): return self._b().stop()
    def zoom_step(self, dz): return self._b().zoom_step(dz)
    def home(self): return self._b().home()
    def set_home(self): return self.onvif.set_home()
    def presets(self): return self._b().presets()
    def goto(self, token): return self._b().goto(token)
    def set_preset(self, token, name): return self._b().set_preset(token, name)


# --------------------------------------------------------------------------- smooth motion
# The motion engine (motion.py) computes the S-curves; this hands its output to the camera:
# pan/tilt together over ONVIF (or, as a fallback, one axis at a time over the web API),
# zoom over the web API (where it stops reliably).

class Motion:
    def __init__(self):
        self.zoom_state = (0, 0)
        self.pt_on = False
        self.engine = motion.Engine(os.path.join(DATA_DIR, "curves.json"), self.out_pt, self.out_zoom, log)

    def out_pt(self, x, y):
        cam, web = App.cam, App.web
        if self.engine.cfg["pt_path"] == "onvif" and cam.connected and cam.profile and cam.profile.get("ptz"):
            if x == 0 and y == 0:
                cam._ptz("ContinuousMove", '<tptz:Velocity><tt:PanTilt x="0" y="0"/></tptz:Velocity>')
                cam._ptz("Stop", "<tptz:PanTilt>true</tptz:PanTilt><tptz:Zoom>false</tptz:Zoom>")
                if self.pt_on:
                    log("→ camera: pan/tilt stopped")
                self.pt_on = False
            else:
                cam._ptz("ContinuousMove", f'<tptz:Velocity><tt:PanTilt x="{fnum(x)}" y="{fnum(y)}"/></tptz:Velocity>')
                if not self.pt_on:
                    log("→ camera: pan/tilt starting (ONVIF)")
                self.pt_on = True
            return
        if not web.connected:
            if x == 0 and y == 0:
                return
            raise OnvifError("No control path connected")
        if x == 0 and y == 0:
            web.cmd(web.STOP_PT, 0)
            web.pt_active = False
        elif abs(x) >= abs(y):
            web.cmd(web.RIGHT if x > 0 else web.LEFT, web._spd(x, "ptz"))
            web.pt_active = True
        else:
            web.cmd(web.UP if y > 0 else web.DOWN, web._spd(y, "ptz"))
            web.pt_active = True

    def out_zoom(self, z):
        web = App.web
        if not web.connected:
            if z == 0:
                return
            raise OnvifError("Zoom needs the camera's web API")
        if z == 0:
            if self.zoom_state[0] != 0:
                web.cmd(web.ZOOM_STOP, 0)
            web.z_active = False
            self.zoom_state = (0, 0)
            return
        d, sp = (1 if z > 0 else -1), web._spd(z, "zoom")
        if (d, sp) != self.zoom_state:
            web.cmd(web.ZOOM_IN if d > 0 else web.ZOOM_OUT, sp)
            web.z_active = True
            self.zoom_state = (d, sp)


# --------------------------------------------------------------------------- focus pulling
# Near/far with a speed, as long as the page keeps reporting in (dead man's switch like the joystick).

class FocusCtl:
    def __init__(self):
        self.dir, self.speed, self.last = 0, 0, 0.0
        self.mode = None
        self.lock = threading.Lock()
        threading.Thread(target=self._watch, daemon=True).start()

    def state(self):
        return {"mode": self.mode, "direction": self.dir, "speed": self.speed}

    def read_mode(self):
        try:
            self.mode = App.web.focus_mode()
        except Exception:  # noqa
            pass
        return self.state()

    def set_mode(self, mode):
        if mode not in ("auto", "manual"):
            raise ValueError("mode must be auto or manual")
        App.web.set_focus_mode(mode)
        self.mode = mode
        return self.state()

    def once(self, seconds):
        """One-push focus: autofocus briefly, then lock."""
        App.web.set_focus_mode("auto")
        self.mode = "auto"
        time.sleep(max(0.5, min(5.0, seconds)))
        App.web.set_focus_mode("manual")
        self.mode = "manual"
        return self.state()

    def drive(self, direction, speed):
        direction = 1 if direction > 0 else (-1 if direction < 0 else 0)
        speed = int(max(1, min(24, speed)))
        with self.lock:
            self.last = time.monotonic()
            if direction == self.dir and (speed == self.speed or not direction):
                return self.state()
            if direction and self.mode != "manual":
                App.web.set_focus_mode("manual")
                self.mode = "manual"
            web = App.web
            if direction == 0:
                web.cmd(web.FOCUS_STOP, 0)
            else:
                web.cmd(web.FOCUS_NEAR if direction > 0 else web.FOCUS_FAR, speed)
            self.dir, self.speed = direction, (speed if direction else 0)
        return self.state()

    def stop(self):
        with self.lock:
            if self.dir:
                try:
                    App.web.cmd(App.web.FOCUS_STOP, 0)
                finally:
                    self.dir, self.speed = 0, 0
        return self.state()

    def _watch(self):
        while True:
            time.sleep(0.2)
            if self.dir and time.monotonic() - self.last > 0.8:
                try:
                    self.stop()
                    log("No word from the page – focus pull stopped.")
                except Exception:  # noqa
                    pass


# --------------------------------------------------------------------------- preset names and targets
# The camera only allows letters and digits; display names (with spaces, accents) live here,
# together with the VISCA coordinates used for straight moves.

ICON_NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


class PresetNames:
    def __init__(self):
        self.path = os.path.join(DATA_DIR, "presets.json")
        self.names, self.targets, self.durations, self.icons = {}, {}, {}, {}
        self.lock = threading.Lock()
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
            names = raw.get("names", raw.get("namen"))
            if isinstance(names, dict):
                self.names = {str(k): str(v) for k, v in names.items()}
                targets = raw.get("targets", raw.get("ziele")) or {}
                for k, v in targets.items():
                    if isinstance(v, dict):
                        if "fokus" in v:  # earlier versions
                            v["focus"] = v.pop("fokus")
                        if "zeit" in v:
                            v["time"] = v.pop("zeit")
                        self.targets[str(k)] = v
                self.icons = {str(k): str(v) for k, v in (raw.get("icons") or {}).items() if ICON_NAME.match(str(v))}
                for k, v in (raw.get("durations") or {}).items():
                    try:
                        self.durations[str(k)] = max(0.0, min(120.0, float(v)))
                    except (TypeError, ValueError):
                        pass
            else:  # oldest format: names only
                self.names = {str(k): str(v) for k, v in raw.items()}
        except (OSError, ValueError, AttributeError):
            pass

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"names": self.names, "targets": self.targets, "durations": self.durations, "icons": self.icons},
                      f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def set(self, token, name):
        token, name = str(token), str(name or "").strip()[:40]
        with self.lock:
            if name:
                self.names[token] = name
            else:
                self.names.pop(token, None)
            self._save()
        return self.names

    def set_icon(self, token, icon):
        """Lucide icon name for this preset ("" = none)."""
        token, icon = str(token), str(icon or "").strip()
        if icon and not ICON_NAME.match(icon):
            raise ValueError("not an icon name")
        with self.lock:
            if icon:
                self.icons[token] = icon
            else:
                self.icons.pop(token, None)
            self._save()
        return self.icons

    def set_duration(self, token, value):
        """Move time for this preset in seconds; 0 = as fast as possible; None/"" = general settings."""
        token = str(token)
        with self.lock:
            if value is None or value == "":
                self.durations.pop(token, None)
            else:
                self.durations[token] = max(0.0, min(120.0, float(value)))
            self._save()
        return self.durations

    def set_target(self, token, target):
        with self.lock:
            self.targets[str(token)] = target
            self._save()

    def merge(self, presets):
        out = []
        for p in presets:
            q = dict(p)
            q["label"] = self.names.get(str(p.get("token")), "") or p.get("name") or ""
            q["straight"] = str(p.get("token")) in self.targets
            q["duration"] = self.durations.get(str(p.get("token")))
            q["icon"] = self.icons.get(str(p.get("token")))
            out.append(q)
        return {"presets": out, "names": self.names, "durations": self.durations, "icons": self.icons,
                "active": App.moves.active}


# --------------------------------------------------------------------------- recalling presets
# With stored VISCA coordinates the server drives a straight, smooth line itself (glide.py).
# Without coordinates the camera moves on its own; afterwards the server learns the target.

class PresetMoves:
    def __init__(self):
        self.glide = glide.Glide(DATA_DIR, log)
        self.clicks = glide.ClickModel(DATA_DIR, log)
        self.gen = 0
        self.link = None
        self.zoom_direct_ok = None
        self.focus_direct_ok = None
        # which preset the camera is at: state "moving" (on its way), "at", "left" (moved away by hand)
        self.active = {"token": None, "state": None}

    def _set_active(self, token, state):
        self.active = {"token": str(token) if token is not None else None, "state": state}

    def manual(self):
        """Pan, tilt or zoom by hand (joystick, keys, wheel, double-click, home): away from the preset."""
        if self.active["token"] and self.active["state"] != "left":
            self._set_active(self.active["token"], "left")

    def visca(self):
        if self.link is None:
            self.link = visca.Link(App.cfg["ip"], "tcp52381", timeout=1.0)
        return self.link

    def busy(self):
        return App.cal.running or App.vcheck.running or self.glide.cal["running"]

    def cancel(self):
        """Any manual input: cancel the running move and any learning."""
        self.gen += 1
        if self.active["state"] == "moving":       # interrupted on the way
            self._set_active(self.active["token"], "left")
        if self.glide.state.get("active"):
            self.glide.cancel()

    def snapshot(self, token):
        link = self.visca()
        p, t, *_ = link.pantilt()
        d = {"pan": p, "tilt": t, "time": time.strftime("%Y-%m-%d %H:%M:%S")}
        for key, fn in (("zoom", lambda: link.zoom()[0]), ("focus", link.focus_pos)):
            try:
                d[key] = fn()
            except Exception:  # noqa
                pass
        App.names.set_target(str(token), d)
        return d

    def _learn(self, token, gen, learn=True):
        """After a camera-driven move, wait until the camera stands still; then the preset counts as
        reached and (learn=True) its VISCA target is remembered for straight moves."""
        def run():
            try:
                link = self.visca()
                time.sleep(0.6)
                last, stable, t0 = None, 0, time.monotonic()
                while time.monotonic() - t0 < 15:
                    if gen != self.gen:
                        return
                    p, t, *_ = link.pantilt()
                    try:
                        z = link.zoom()[0]
                    except Exception:  # noqa
                        z = None
                    cur = (p, t, z)
                    stable = stable + 1 if cur == last else 0
                    last = cur
                    if stable >= 3:
                        if gen != self.gen:
                            return
                        self._set_active(token, "at")
                        if not learn:
                            return
                        d = self.snapshot(token)
                        log(f"Preset {token} learned (VISCA {d['pan']}/{d['tilt']}) – straight moves from now on.")
                        return
                    time.sleep(0.25)
            except Exception as e:  # noqa
                if gen == self.gen:
                    self._set_active(token, "at")
                if learn:
                    log(f"Preset {token} could not be learned: {e}")
        threading.Thread(target=run, daemon=True).start()

    def goto(self, token):
        token = str(token)
        if self.busy():
            raise OnvifError("A calibration is running")
        self.cancel()
        gen = self.gen
        self._set_active(token, "moving")
        App.motion.engine.halt()
        time.sleep(0.15)
        z = App.names.targets.get(token)
        if self.glide.cfg["mode"] == "straight" and z:
            try:
                link = self.visca()
                p, t, *_ = link.pantilt()
            except Exception as e:  # noqa
                log(f"VISCA not reachable ({e}) – the camera moves on its own.")
            else:
                zoom_sent = self._zoom_to(link, z)
                plan = self.glide.start(link, (p, t), (int(z["pan"]), int(z["tilt"])),
                                        on_done=lambda ok: self._after(token, z, zoom_sent, gen),
                                        duration=App.names.durations.get(token))
                return dict(plan, route="straight", token=token)
        App.ptz.goto(token)
        self._learn(token, gen, learn=self.glide.cfg["mode"] == "straight")
        return {"route": "camera", "token": token}

    def _zoom_to(self, link, z):
        """Start the zoom together with the move. True: running, False: not possible, None: not needed."""
        if z.get("zoom") is None or self.zoom_direct_ok is False:
            return False if self.zoom_direct_ok is False else None
        try:
            cur = link.zoom()[0]
            if abs(cur - int(z["zoom"])) <= 8:
                return None
            err = link.error_of(link.zoom_direct(int(z["zoom"])))
        except Exception as e:  # noqa
            err = str(e)
        if err:
            if self.zoom_direct_ok is None:
                log(f"Direct zoom not supported ({err}) – the camera sets the zoom after the move.")
            self.zoom_direct_ok = False
            return False
        self.zoom_direct_ok = True
        return True

    def _after(self, token, z, zoom_sent, gen):
        if gen != self.gen:
            return
        self._set_active(token, "at")
        link = self.visca()
        if zoom_sent is False and z.get("zoom") is not None:
            # pan/tilt are already there; the camera only adjusts zoom and focus.
            try:
                if abs(link.zoom()[0] - int(z["zoom"])) > 8:
                    App.ptz.goto(token)
                    return
            except Exception:  # noqa
                pass
        if z.get("focus") is None or self.focus_direct_ok is False:
            return
        try:
            if link.af_mode() != 3:
                return  # autofocus takes care of itself
            if zoom_sent:  # wait until the zoom has settled
                last, t0 = None, time.monotonic()
                while time.monotonic() - t0 < 4:
                    cur = link.zoom()[0]
                    if cur == last:
                        break
                    last = cur
                    time.sleep(0.2)
            err = link.error_of(link.focus_direct(int(z["focus"])))
            if err:
                self.focus_direct_ok = False
                log(f"Direct focus not supported ({err}).")
            else:
                self.focus_direct_ok = True
        except Exception as e:  # noqa
            log(f"Focus after the move: {e}")

    def click(self, dx, dy):
        """Bring an image point (fraction of image width/height from the centre, right/down positive) to the centre."""
        dx, dy = max(-0.5, min(0.5, float(dx))), max(-0.5, min(0.5, float(dy)))
        if self.busy():
            raise OnvifError("A calibration is running")
        self.cancel()
        self.manual()
        App.motion.engine.halt()
        link = self.visca()
        try:
            p, t, *_ = link.pantilt()
        except Exception as e:  # noqa
            raise OnvifError(f"Click aiming needs VISCA ({e})")
        try:
            zoom = link.zoom()[0]
        except Exception:  # noqa
            zoom = None
        kp, kt = self.clicks.scale(zoom)
        du, dv = int(round(kp * dx)), int(round(-kt * dy))
        plan = self.glide.start(link, (p, t), (p + du, t + dv))
        return dict(plan, du=du, dv=dv, zoom=zoom, scale=[round(kp), round(kt)])

    def click_learn(self, d):
        k = self.clicks.learn(d.get("zoom"), float(d["du"]), float(d["dv"]), float(d["dx"]), float(d["dy"]),
                              float(d["rx"]), float(d["ry"]))
        log(f"Click scale at zoom {d.get('zoom')}: {k[0]:.0f} / {k[1]:.0f} units per image width/height (sample {k[2]}).")
        return {"scale": k}

    def calibrate(self):
        if App.cal.running or App.vcheck.running:
            raise OnvifError("Wait for the running calibration to finish first")
        self.cancel()
        self.manual()
        eng = App.motion.engine

        def before():
            eng.halt()
            eng.locked = True

        def after():
            eng.locked = False
        return self.glide.calibrate(self.visca(), before, after)

    def status(self):
        return {"settings": self.glide.get_config(), "move": self.glide.state, "calibration": self.glide.cal,
                "active": self.active,
                "speeds": {ax: [round(v) for v in row] for ax, row in self.glide.speeds.full.items()}}


# --------------------------------------------------------------------------- exposure
# Over VISCA (Sony command set): mode, iris, shutter, gain, exposure compensation.
# The inquiry shows which commands the camera knows; only those are offered in the page.

class Exposure:
    MODES = {0x00: "auto", 0x03: "manual", 0x0A: "shutter", 0x0B: "iris", 0x0D: "bright"}

    def __init__(self):
        self.lock = threading.Lock()
        self.iris_max = None
        self.dumped = False

    def _link(self):
        return App.moves.visca()

    def _dump_web(self):
        """Store the web API's image settings once (for later analysis)."""
        if self.dumped or not App.web.connected:
            return
        self.dumped = True
        try:
            res = App.web.call("/AjaxGetCfgVImg", {"Channel": 0, "Profile": 0}).get("result") or {}
            with open(os.path.join(DATA_DIR, "image_settings.json"), "w", encoding="utf-8") as f:
                json.dump(res, f, ensure_ascii=False, indent=1)
        except Exception as e:  # noqa
            log(f"Image settings not readable: {e}")

    def state(self):
        self._dump_web()
        link, out, err = self._link(), {}, {}
        with self.lock:
            for key in ("mode", "iris", "gain", "shutter", "expcomp", "expcomp_on"):
                try:
                    out[key] = link.inquire(key)
                except Exception as e:  # noqa
                    err[key] = str(e)
        if "mode" in out:
            out["mode"] = self.MODES.get(out["mode"], f"0x{out['mode']:02x}")
        if "expcomp_on" in out:
            out["expcomp_on"] = out["expcomp_on"] == 2
        out["iris_max"] = self.iris_max
        out["errors"] = err
        return out

    def set_mode(self, mode):
        if mode not in visca.Link.AE_MODES:
            raise ValueError("unknown exposure mode")
        link = self._link()
        with self.lock:
            e = link.error_of(link.ae_mode(mode))
        if e:
            raise OnvifError(f"Camera rejects the exposure mode: {e}")
        log(f"→ camera: exposure {mode}")
        time.sleep(0.15)
        return self.state()

    def step(self, what, up):
        if what not in visca.Link.STEP_CMDS:
            raise ValueError("unknown setting")
        link = self._link()
        with self.lock:
            if what == "expcomp":
                try:
                    if link.inquire("expcomp_on") != 2:
                        link.expcomp_on(True)
                except Exception:  # noqa
                    pass
            e = link.error_of(link.step(what, up))
        if e:
            raise OnvifError(f"Camera rejects {what}: {e} – not adjustable in the current exposure mode?")
        time.sleep(0.12)
        return self.state()

    def iris_open(self):
        """Open the iris fully: keep opening until the value no longer changes."""
        link = self._link()
        with self.lock:
            last = None
            for _ in range(30):
                e = link.error_of(link.step("iris", True))
                if e:
                    raise OnvifError(f"Camera rejects iris: {e} – choose mode \"Iris\" or \"Manual\"")
                time.sleep(0.1)
                cur = link.inquire("iris")
                if cur == last:
                    break
                last = cur
            self.iris_max = last
        log(f"→ camera: iris fully open (value {last})")
        return self.state()


# --------------------------------------------------------------------------- audio source
# The camera itself has no microphone (only a line input). Alternatively an audio input of this
# computer (e.g. a USB audio interface) is added to the camera streams in go2rtc through ffmpeg,
# so that it is part of the live view and of the recordings.

class Audio:
    def __init__(self):
        self.path = os.path.join(DATA_DIR, "audio.json")
        self.cfg = {"source": "camera", "device": ""}
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
            if raw.get("source") in ("camera", "device"):
                self.cfg = {"source": raw["source"], "device": str(raw.get("device") or "")}
        except (OSError, ValueError, AttributeError):
            pass
        self.devices, self.info, self.listed = [], "", False

    def go2rtc_source(self):
        if self.cfg["source"] != "device" or not self.cfg["device"]:
            return None
        # AAC for the MP4 recording, Opus for WebRTC in the browser
        return f"ffmpeg:device?audio={urllib.request.quote(self.cfg['device'], safe='')}#audio=aac#audio=opus"

    def list_devices(self):
        self.listed = True
        ff = App.smooth.ff or App.smooth.find()
        if not ff:
            self.devices, self.info = [], "ffmpeg not found – it is needed for audio devices (see Recordings)."
            return self.devices
        if os.name != "nt":
            self.devices, self.info = [], "Listing audio devices is implemented for Windows (DirectShow) only."
            return self.devices
        try:
            r = subprocess.run([ff, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                               capture_output=True, timeout=20, creationflags=NOWIN)
            text_ = r.stderr.decode("utf-8", "replace")
        except Exception as e:  # noqa
            self.devices, self.info = [], f"ffmpeg could not list devices ({e})"
            return self.devices
        devs, section = [], None
        for line in text_.splitlines():
            if "DirectShow video devices" in line:
                section = "video"
                continue
            if "DirectShow audio devices" in line:
                section = "audio"
                continue
            if "Alternative name" in line:
                continue
            m = re.search(r'"([^"]+)"\s*(?:\((audio|video)\))?', line)
            if m and (m.group(2) or section) == "audio" and m.group(1) not in devs:
                devs.append(m.group(1))
        self.devices = devs
        self.info = f"{len(devs)} audio input(s) found." if devs else "No audio inputs found."
        return devs

    def state(self):
        if not self.listed:
            self.list_devices()
        return dict(self.cfg, devices=self.devices, info=self.info)

    def set(self, source, device):
        if source not in ("camera", "device"):
            raise ValueError("source must be camera or device")
        if source == "device" and not device:
            raise ValueError("no device given")
        if App.rec.active:
            raise OnvifError("Stop the recording before changing the audio source")
        self.cfg = {"source": source, "device": str(device or "") if source == "device" else ""}
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.cfg, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)
        log(f"Audio source: {'camera stream' if source == 'camera' else device} – restarting video.")
        App.video.restart()
        return self.state()


# --------------------------------------------------------------------------- motion watchdog
# While a key is held, the page reports in every 400 ms. If that stops
# (tab closed, network gone), the server stops the camera after 1.5 s.

class Watchdog:
    def __init__(self, cam):
        self.cam, self.moving, self.last = cam, False, 0.0
        self.lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True).start()

    def moved(self):
        with self.lock:
            self.moving, self.last = True, time.monotonic()

    def alive(self):
        with self.lock:
            self.last = time.monotonic()

    def stopped(self):
        with self.lock:
            self.moving = False

    def _run(self):
        while True:
            time.sleep(0.3)
            with self.lock:
                due = self.moving and time.monotonic() - self.last > 1.5
                if due:
                    self.moving = False
            if due:
                try:
                    self.cam.stop()
                    log("No word from the page – motion stopped.")
                except Exception as e:
                    log(f"Stopping failed: {e}")


# --------------------------------------------------------------------------- go2rtc

class Video:
    def __init__(self, cfg):
        self.cfg = cfg
        self.proc = None
        self.state = "off"
        self.exe = find_tool("go2rtc") or os.path.join(APP_DIR, "go2rtc.exe" if os.name == "nt" else "go2rtc")
        self.rtsp = {k: v for k, v in (("main", cfg["rtsp_main"]), ("sub", cfg["rtsp_sub"])) if v}
        self.streams = {}
        self.foreign = False

    def _sources(self):
        """Sources per stream name: the camera's RTSP plus, if chosen, an audio device of this computer."""
        extra = App.audio.go2rtc_source() if App.audio else None
        return {k: [v] + ([extra] if extra else []) for k, v in self.rtsp.items()}

    def restart(self):
        """Apply new stream sources (e.g. a different audio source)."""
        if self.foreign:
            self.streams = self._sources()
            bad = self._add_streams()
            if bad:
                raise OnvifError("The go2rtc that is running did not accept the new sources – close it in Task Manager and restart")
            return
        self.stop()
        self.proc = None
        for _ in range(25):
            if not self._api_ok():
                break
            time.sleep(0.2)
        self.start()

    def start(self):
        self.streams = self._sources()
        if not self.streams:
            self.state = "no RTSP address set"
            return
        yml = os.path.join(DATA_DIR, "go2rtc.yaml")
        self.foreign = False
        if self._api_ok():
            info = self._api_info()
            if os.path.normcase(os.path.abspath(info.get("config_path") or "")) == os.path.normcase(yml):
                log("An old go2rtc from an earlier start is still running – stopping it.")
                self._exit_running()
            elif self._ours(info.get("config_path")):
                # left over from another Orbital copy (source folder, portable, installed) – only
                # one of them can run at a time (same page port), so that one is not in use
                log(f"go2rtc of another Orbital copy is still running ({info.get('config_path')}) – stopping it.")
                self._exit_running()
            if self._api_ok():
                # Another go2rtc (e.g. left over from an earlier start, or an older version that does not
                # report its config path): register our streams there instead of failing with "stream not found".
                self.foreign = True
                bad = self._add_streams()
                if bad:
                    self.state = ("running, but as a foreign go2rtc on port 1984 that did not accept the streams "
                                  f"({', '.join(bad)}) – close go2rtc in Task Manager and restart")
                else:
                    self.state = "running (go2rtc that was already running on port 1984, streams registered)"
                    log("go2rtc was already running – registered our streams there.")
                return
        if not os.path.exists(self.exe):
            self.state = f"{os.path.basename(self.exe)} is missing (put it next to the program)"
            return
        q = lambda s: '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
        lines = ["# Rewritten by Orbital on every start. Make changes in config.ini.",
                 "api:", '  listen: "127.0.0.1:1984"',
                 # go2rtc's ffmpeg sources (audio devices) hand their output back over RTSP, so the
                 # RTSP server must run – but only on this computer
                 "rtsp:", '  listen: "127.0.0.1:18554"', "webrtc:", '  listen: ":8555"',
                 "log:", "  level: warn", "  format: text", "streams:"]
        for k, srcs in self.streams.items():
            lines.append(f"  {k}:")
            lines += [f"    - {q(v)}" for v in srcs]
        ff = App.smooth.ff if App.smooth else None
        if ff:  # go2rtc needs ffmpeg for audio devices
            lines += ["ffmpeg:", f"  bin: {q(ff)}"]
        with open(yml, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.proc = subprocess.Popen([self.exe, "-config", yml], cwd=DATA_DIR, creationflags=flags,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self._job = bind_to_this_process(self.proc)
        threading.Thread(target=self._pump, args=(self.proc,), name="go2rtc-log", daemon=True).start()
        for _ in range(30):
            time.sleep(0.2)
            if self._api_ok():
                self.state = "running"
                return
            if self.proc.poll() is not None:
                self.state = f"go2rtc exited (code {self.proc.returncode})"
                return
        self.state = "go2rtc does not respond"

    @staticmethod
    def _pump(proc):
        """go2rtc's own messages into our log."""
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if line:
                log("go2rtc: " + line)
        proc.stdout.close()

    @staticmethod
    def _ours(path):
        """Was this go2rtc config written by Orbital (any version)?"""
        try:
            if not path or os.path.basename(path).lower() != "go2rtc.yaml":
                return False
            with open(path, encoding="utf-8", errors="replace") as f:
                head = f.read(400)
            return "Orbital" in head or "Orbital" in head or "kamera.ini" in head or "config.ini" in head
        except OSError:
            return False

    def _api_info(self):
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(GO2RTC_API + "/api", timeout=1) as r:
                return json.loads(r.read() or b"{}")
        except Exception:
            return {}

    def _add_streams(self):
        """Register our streams in a running go2rtc via its API. Returns the names that failed."""
        bad = []
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for name, srcs in self.streams.items():
            q = "&".join(f"src={urllib.request.quote(x, safe='')}" for x in srcs) + f"&name={urllib.request.quote(name)}"
            for method in ("PATCH", "PUT"):   # update an existing stream, otherwise create it
                try:
                    op.open(urllib.request.Request(f"{GO2RTC_API}/api/streams?{q}", data=b"", method=method), timeout=3)
                    break
                except Exception:  # noqa
                    continue
            else:
                bad.append(name)
        return bad

    def _exit_running(self):
        try:
            req = urllib.request.Request(GO2RTC_API + "/api/exit?code=0", data=b"", method="POST")
            urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=2)
        except Exception:
            pass  # the process often exits before the reply arrives
        for _ in range(25):
            if not self._api_ok():
                return
            time.sleep(0.2)

    def _api_ok(self):
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(GO2RTC_API + "/api", timeout=1):
                return True
        except Exception:
            return False

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except Exception:
                pass


def bind_to_this_process(proc):
    """Windows: put go2rtc into a job object that is closed together with this process –
    even when the console window is simply closed."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

        class Io(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                         "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", Io), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        job = k32.CreateJobObjectW(None, None)
        info = Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))  # 9 = ExtendedLimitInformation
        ok = ok and k32.AssignProcessToJobObject(job, wintypes.HANDLE(int(proc._handle)))
        if not ok:
            log("Note: go2rtc could not be tied to the server (close it in Task Manager if it keeps running).")
        return job  # the handle must stay open while the server runs
    except Exception as e:  # noqa
        log(f"Note: could not tie go2rtc to the server ({e}).")
        return None


def on_window_close(callback):
    """Windows: still stop the camera and go2rtc when the console window is closed."""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
        HANDLER = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

        def handler(event):
            if event in (0, 1, 2, 5, 6):  # Ctrl+C, Ctrl+Break, window closed, logoff, shutdown
                try:
                    callback()
                except Exception:
                    pass
                if event == 2:
                    os._exit(0)
            return False

        on_window_close._ref = HANDLER(handler)  # keep a reference, otherwise Python collects the callback
        ctypes.windll.kernel32.SetConsoleCtrlHandler(on_window_close._ref, True)
    except Exception:
        pass


# --------------------------------------------------------------------------- recording
# The server pulls the camera stream from go2rtc as continuous MP4 (no re-encoding, original
# quality) and writes it straight to disk. Fragmented MP4 stays playable even if cut off.

class Recorder:
    MIN_FREE = 2 * 1024 ** 3  # stop below 2 GB of free space

    def __init__(self, cfg):
        self.dir = cfg["rec_dir"]
        self.seg = max(0, cfg["rec_minutes"]) * 60
        self.src = cfg["rec_stream"]
        self.active = False
        self.thread = None
        self.file = None
        self.files = []
        self.started = None
        self.bytes = 0
        self.file_bytes = 0
        self.error = ""
        self.lock = threading.Lock()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def free(self):
        try:
            return shutil.disk_usage(self.dir if os.path.isdir(self.dir) else os.path.dirname(self.dir) or ".").free
        except OSError:
            return None

    def status(self):
        f = self.free()
        return {"active": self.active, "file": os.path.basename(self.file) if self.file else None,
                "duration_s": round(time.time() - self.started) if self.active and self.started else 0,
                "bytes": self.bytes, "file_bytes": self.file_bytes, "files": [os.path.basename(x) for x in self.files[-10:]],
                "folder": self.dir, "free_gb": round(f / 1024 ** 3, 1) if f is not None else None,
                "stream": self.src, "split_minutes": self.seg // 60, "error": self.error}

    def start(self, video):
        with self.lock:
            if self.active:
                return self.status()
            if self.src not in video.streams:
                raise ValueError(f"Stream \"{self.src}\" is not set in config.ini")
            if not video._api_ok():
                raise OnvifError(f"Video service is not running ({video.state})")
            try:
                os.makedirs(self.dir, exist_ok=True)
            except OSError as e:
                raise OnvifError(f"Cannot create the recording folder: {self.dir} ({e})")
            f = self.free()
            if f is not None and f < self.MIN_FREE:
                raise OnvifError(f"Not enough free space in the recording folder ({f / 1024 ** 3:.1f} GB)")
            self.active, self.started, self.files, self.bytes, self.error = True, time.time(), [], 0, ""
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()
            log(f"Recording started ({self.src}) → {self.dir}")
            return self.status()

    def stop(self, wait=0.0):
        was = self.active
        self.active = False
        if wait and self.thread:
            self.thread.join(wait)
        if was:
            log(f"Recording stopped: {len(self.files)} file(s), {self.bytes / 1024 ** 2:.0f} MB")
        return self.status()

    def _run(self):
        fails = 0
        while self.active:
            name = os.path.join(self.dir, "rec_" + time.strftime("%Y-%m-%d_%H-%M-%S") + ".mp4")
            got = 0
            try:
                url = f"{GO2RTC_API}/api/stream.mp4?src={urllib.request.quote(self.src)}&mp4=all"
                with self.opener.open(url, timeout=10) as resp, open(name, "wb") as out:
                    self.file, self.file_bytes = name, 0
                    self.files.append(name)
                    log(f"Recording to: {os.path.basename(name)}")
                    seg_start = time.monotonic()
                    last_check = seg_start
                    while self.active:
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            raise OSError("video stream ended")
                        out.write(chunk)
                        got += len(chunk)
                        self.bytes += len(chunk)
                        self.file_bytes = got
                        now = time.monotonic()
                        if now - last_check > 30:
                            last_check = now
                            f = self.free()
                            if f is not None and f < self.MIN_FREE:
                                self.error = "Recording stopped: less than 2 GB free"
                                log(self.error)
                                self.active = False
                        if self.seg and now - seg_start >= self.seg:
                            break  # next file
                fails = 0
            except Exception as e:  # noqa
                if not self.active:
                    break
                fails += 1
                self.error = f"Recording interrupted ({e}), retrying …"
                log(self.error)
                time.sleep(min(10, 2 * fails))
            finally:
                if got == 0 and os.path.exists(name):
                    try:
                        os.remove(name)  # empty file of a failed attempt
                        if name in self.files:
                            self.files.remove(name)
                    except OSError:
                        pass
        self.file = None


# --------------------------------------------------------------------------- motion calibration
# Drives a fixed sequence of short moves over ONVIF and the web API, each at different speeds,
# and records meanwhile. Recording + log show which control path really applies the speed.
# At the end the usual VISCA ports are only queried.

class _Cancelled(Exception):
    pass


def visca_probe(ip):
    """Queries the VISCA version (moves nothing) – over the usual TCP and UDP ports."""
    res, inq = {}, b"\x81\x09\x00\x02\xff"
    for port in (1259, 5678, 52380, 52381):
        try:
            with socket.create_connection((ip, port), timeout=1.5) as s:
                s.settimeout(1.5)
                s.sendall(inq)
                try:
                    r = s.recv(64)
                    res[f"tcp_{port}"] = ("reply " + r.hex(" ")) if r else "open, connection closed"
                except socket.timeout:
                    res[f"tcp_{port}"] = "open, no reply"
        except OSError as e:
            res[f"tcp_{port}"] = f"closed ({type(e).__name__})"
    for port, pkt in ((52381, b"\x01\x10\x00\x05\x00\x00\x00\x01" + inq), (1259, inq)):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.settimeout(1.5)
            s.sendto(pkt, (ip, port))
            r, _ = s.recvfrom(64)
            res[f"udp_{port}"] = "reply " + r.hex(" ")
        except socket.timeout:
            res[f"udp_{port}"] = "no reply"
        except OSError as e:
            res[f"udp_{port}"] = f"error ({e})"
        finally:
            s.close()
    return res


class Calibration:
    PAUSE = 1.5

    def __init__(self):
        self.running = self.abort = False
        self.step, self.error, self.progress, self.result = "", "", 0.0, None

    def status(self):
        return {"running": self.running, "step": self.step, "progress": round(self.progress, 3),
                "result": self.result, "error": self.error}

    def start(self):
        if self.running:
            return self.status()
        if not (App.web.connected or (App.cam.connected and App.cam.profile and App.cam.profile.get("ptz"))):
            raise OnvifError("No control path connected")
        if not App.rec.active and not App.video._api_ok():
            raise OnvifError(f"Video service is not running ({App.video.state}) – the calibration is useless without a recording")
        self.running, self.abort, self.error, self.result, self.progress = True, False, "", None, 0.0
        self.step = "starting …"
        threading.Thread(target=self._run, daemon=True).start()
        return self.status()

    def stop(self):
        self.abort = True
        return self.status()

    def _wait(self, sec):
        end = time.time() + sec
        while time.time() < end:
            if self.abort:
                raise _Cancelled()
            time.sleep(0.05)

    def _plan(self):
        web, cam = App.web, App.cam
        plan = []
        if cam.connected and cam.profile and cam.profile.get("ptz"):
            for x in (0.05, -0.3, 1.0, -0.05):
                plan.append(("onvif", (x, 0.0), 2.0, f"ONVIF pan {x:+.2f}"))
            plan.append(("onvif", (0.3, 0.3), 2.0, "ONVIF diagonal +0.30/+0.30"))
            plan.append(("onvif", (-0.3, -0.3), 2.0, "ONVIF diagonal −0.30/−0.30"))
        if web.connected:
            lo, hi = web.ability.get("ptz") or (1, 24)
            zlo, zhi = web.ability.get("zoom") or (0, 7)
            q = lambda f: int(round(lo + f * (hi - lo)))
            for code, f in ((web.RIGHT, 0.0), (web.LEFT, 1 / 3), (web.RIGHT, 2 / 3), (web.LEFT, 1.0)):
                plan.append(("web", (code, q(f), 0, web.STOP_PT), 2.0, f"Web {'right' if code == web.RIGHT else 'left'} speed {q(f)}"))
            plan.append(("web", (web.UP, lo, 0, web.STOP_PT), 2.0, f"Web up speed {lo}"))
            plan.append(("web", (web.DOWN, lo, 0, web.STOP_PT), 2.0, f"Web down speed {lo}"))
            plan.append(("web", (web.UP, hi, 0, web.STOP_PT), 1.0, f"Web up speed {hi}"))
            plan.append(("web", (web.DOWN, hi, 0, web.STOP_PT), 1.0, f"Web down speed {hi}"))
            plan.append(("web", (web.ZOOM_IN, zlo, 0, web.ZOOM_STOP), 1.5, f"Zoom in speed {zlo}"))
            plan.append(("web", (web.ZOOM_OUT, zlo, 0, web.ZOOM_STOP), 1.5, f"Zoom out speed {zlo}"))
            plan.append(("web", (web.ZOOM_IN, zhi, 0, web.ZOOM_STOP), 1.0, f"Zoom in speed {zhi}"))
            plan.append(("web", (web.ZOOM_OUT, zhi, 0, web.ZOOM_STOP), 1.0, f"Zoom out speed {zhi}"))
        return plan

    def _start_move(self, kind, args):
        if kind == "onvif":
            x, y = args
            App.cam._ptz("ContinuousMove", f'<tptz:Velocity><tt:PanTilt x="{fnum(x)}" y="{fnum(y)}"/></tptz:Velocity>')
            return None
        code, p1, p2, _ = args
        return App.web.call("/setPTZCmd", {"Channel": 0, "PtzCmd": code, "param1": p1, "param2": p2})

    def _stop_move(self, kind, args):
        if kind == "onvif":
            App.cam._ptz("ContinuousMove", '<tptz:Velocity><tt:PanTilt x="0" y="0"/></tptz:Velocity>')
            App.cam._ptz("Stop", "<tptz:PanTilt>true</tptz:PanTilt><tptz:Zoom>true</tptz:Zoom>")
        else:
            App.web.call("/setPTZCmd", {"Channel": 0, "PtzCmd": args[3], "param1": 0, "param2": 0})

    def _all_stop(self):
        for f in (lambda: App.cam._ptz("Stop", "<tptz:PanTilt>true</tptz:PanTilt><tptz:Zoom>true</tptz:Zoom>"),
                  lambda: App.web.cmd(App.web.STOP_PT, 0), lambda: App.web.cmd(App.web.ZOOM_STOP, 0)):
            try:
                f()
            except Exception:
                pass

    def _run(self):
        eng = App.motion.engine
        entries, started_rec, rec_file = [], False, None
        log("Motion calibration starting – the camera now moves on its own.")
        try:
            eng.halt()
            eng.locked = True
            if not App.rec.active:
                App.rec.start(App.video)
                started_rec = True
            self.step = "recording is starting …"
            self._wait(3.0)
            rec_file = App.rec.file
            entries.append({"t": round(time.time(), 3), "kind": "begin", "recording": os.path.basename(rec_file) if rec_file else None,
                            "recording_start": App.rec.started, "web_speed": App.web.ability, "camera": App.cam.info})
            plan = self._plan()
            for i, (kind, args, dur, label) in enumerate(plan):
                self.progress, self.step = i / max(1, len(plan)), f"{i + 1}/{len(plan)}: {label}"
                e = {"t": round(time.time(), 3), "kind": kind, "command": label, "values": list(args), "duration_s": dur}
                entries.append(e)
                try:
                    r = self._start_move(kind, args)
                    if r is not None:
                        e["reply"] = {k: r.get(k) for k in ("errorCode", "errorMessage", "status") if k in r}
                except Exception as ex:  # noqa
                    e["error"] = str(ex)
                self._wait(dur)
                e["t_stop"] = round(time.time(), 3)
                try:
                    self._stop_move(kind, args)
                except Exception as ex:  # noqa
                    e["stop_error"] = str(ex)
                self._wait(self.PAUSE)
            self.step = "checking VISCA ports …"
            self.progress = 0.97
            entries.append({"t": round(time.time(), 3), "kind": "visca", "result": visca_probe(App.cfg["ip"])})
            self.progress = 1.0
            self.step = "done"
        except _Cancelled:
            self.error, self.step = "cancelled", "cancelled"
            entries.append({"t": round(time.time(), 3), "kind": "cancelled"})
        except Exception as ex:  # noqa
            self.error, self.step = str(ex), "error"
            entries.append({"t": round(time.time(), 3), "kind": "error", "text": str(ex)})
        finally:
            self._all_stop()
            eng.locked = False
            time.sleep(1.0)
            if started_rec:
                App.rec.stop(wait=3)
            base = os.path.splitext(rec_file)[0] if rec_file else os.path.join(App.rec.dir, "rec_" + time.strftime("%Y-%m-%d_%H-%M-%S"))
            proto = base + "_calibration.json"
            try:
                with open(proto, "w", encoding="utf-8") as f:
                    json.dump(entries, f, ensure_ascii=False, indent=1)
            except OSError as ex:
                self.error = f"Log could not be saved ({ex})"
            self.result = {"recording": os.path.basename(rec_file) if rec_file else None, "log": os.path.basename(proto),
                           "folder": App.rec.dir}
            self.running = False
            log(f"Motion calibration {self.step}: {self.result['recording']} + {self.result['log']}")


# --------------------------------------------------------------------------- VISCA check
# Queries the position over all three VISCA paths, then moves briefly: pan/tilt at two speed
# levels, absolute moves with a position log and diagonal drives. Result: visca_check.json.

class ViscaCheck:
    def __init__(self):
        self.running, self.step, self.result, self.error = False, "", None, ""

    def status(self):
        return {"running": self.running, "step": self.step, "result": self.result, "error": self.error}

    def start(self):
        if self.running:
            return self.status()
        if App.cal.running:
            raise OnvifError("Wait for the motion calibration to finish first")
        self.running, self.result, self.error, self.step = True, None, "", "starting …"
        threading.Thread(target=self._run, daemon=True).start()
        return self.status()

    def _run(self):
        ip = App.cfg["ip"]
        res = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "paths": {}, "motion": None}
        best = None
        log("VISCA check starting.")
        try:
            for mode in visca.MODES:
                self.step = f"position inquiry over {mode} …"
                r, link = {}, visca.Link(ip, mode)
                try:
                    p, t, n1, n2, raw = link.pantilt()
                    r.update(pan=p, tilt=t, digits=[n1, n2], raw=raw)
                    try:
                        z, zr = link.zoom()
                        r.update(zoom=z, zoom_raw=zr)
                    except Exception as e:  # noqa
                        r["zoom_error"] = str(e)
                    for label, q in (("focus", "81090448FF"), ("af_mode", "81090438FF")):
                        try:
                            r[label + "_raw"] = link.inquiry_raw(bytes.fromhex(q))
                        except Exception as e:  # noqa
                            r[label + "_raw"] = f"error: {e}"
                    if best is None:
                        best = (mode, n1, n2)
                except Exception as e:  # noqa
                    r["error"] = str(e)
                finally:
                    link.close()
                res["paths"][mode] = r
            if best:
                res["motion"] = self._move_test(ip, *best)
            self.step = "done"
        except Exception as e:  # noqa
            self.error, self.step = str(e), "error"
            res["error"] = str(e)
        finally:
            self.result = res
            try:
                with open(os.path.join(DATA_DIR, "visca_check.json"), "w", encoding="utf-8") as f:
                    json.dump(res, f, ensure_ascii=False, indent=1)
            except OSError:
                pass
            self.running = False
            ok = [m for m, r in res["paths"].items() if "pan" in r]
            log(f"VISCA check {self.step}: position readable over {', '.join(ok) if ok else 'no path'}")

    def _move_test(self, ip, mode, np_, nt):
        eng = App.motion.engine
        eng.halt()
        eng.locked = True
        link = visca.Link(ip, mode)
        out = {"path": mode, "steps": []}

        def pos():
            p, t, _a, _b, _r = link.pantilt()
            return [p, t]

        def rec(label, **kw):
            e = {"step": label, "t": round(time.time(), 3)}
            e.update(kw)
            out["steps"].append(e)
            return e

        def track(limit=8.0):
            pts, t0, stable, last = [], time.time(), 0, None
            while time.time() - t0 < limit:
                pp = pos()
                pts.append([round(time.time() - t0, 2)] + pp)
                stable = stable + 1 if pp == last else 0
                last = pp
                if stable >= 4 and time.time() - t0 > 0.8:
                    break
                time.sleep(0.08)
            return pts

        try:
            time.sleep(0.4)
            rec("start", pos=pos())
            moved = {}
            for lv, dur in ((4, 1.0), (12, 0.6)):
                for axis, d in (("pan", 1), ("pan", -1), ("tilt", 1), ("tilt", -1)):
                    self.step = f"VISCA {axis} {'+' if d > 0 else '−'} level {lv} …"
                    a = pos()
                    msgs = link.drive(lv, lv, d if axis == "pan" else 0, d if axis == "tilt" else 0)
                    time.sleep(dur)
                    link.stop()
                    time.sleep(0.6)
                    b = pos()
                    moved[(axis, lv, d)] = (b[0] - a[0], b[1] - a[1])
                    rec(f"{axis} {'+' if d > 0 else '-'} level {lv}", before=a, after=b, duration_s=dur,
                        reply=[visca.hexs(m) for m in msgs])
            back = pos()
            dp = max(40, abs(moved[("pan", 4, 1)][0]) * 2)
            dt = max(20, abs(moved[("tilt", 4, 1)][1]))
            self.step = "VISCA absolute move (same speed) …"
            msgs = link.absolute(8, 8, back[0] + dp, back[1] + dt, np_, nt)
            rec("absolute out, speed 8/8", start=back, target=[back[0] + dp, back[1] + dt],
                reply=[visca.hexs(m) for m in msgs], track=track())
            self.step = "VISCA absolute move back (slower tilt) …"
            msgs = link.absolute(8, 2, back[0], back[1], np_, nt)
            rec("absolute back, speed 8/2", target=back, reply=[visca.hexs(m) for m in msgs], track=track())
            for vp, vt, dd in ((8, 2, 1), (8, 2, -1), (2, 8, 1), (2, 8, -1)):
                self.step = f"VISCA diagonal drive, speed {vp}/{vt} …"
                a = pos()
                msgs = link.drive(vp, vt, dd, dd)
                time.sleep(1.0)
                link.stop()
                time.sleep(0.6)
                b = pos()
                rec(f"diagonal {'+' if dd > 0 else '-'} speed {vp}/{vt}", before=a, after=b, duration_s=1.0,
                    reply=[visca.hexs(m) for m in msgs])
            rec("end", pos=pos())
        except Exception as e:  # noqa
            rec("error", text=str(e))
        finally:
            try:
                link.stop()
            except Exception:  # noqa
                pass
            link.close()
            eng.locked = False
        return out


# --------------------------------------------------------------------------- stabilization (post)
# The actual computation lives in stabilize.py (measure motion, smooth the path, shift frames).
# Result: <recording>_smooth.mp4 next to the original.

NOWIN = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
SMOOTH_SUFFIXES = ("_smooth.mp4", "_glatt.mp4")   # the second one from earlier versions


class Smoother:
    PRESETS = stabilize.PRESETS

    def __init__(self, rec):
        self.rec = rec
        self.ff, self.ff_info = None, "not checked yet"
        self.deps_ok, self.deps_info = False, "not checked yet"
        self.pip = {"running": False, "log": "", "error": ""}
        self.running = self.abort = False
        self.file, self.phase, self.progress, self.result, self.error = None, "", 0.0, None, ""
        self._cache = {}

    def find(self):
        exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
        cands = [find_tool("ffmpeg")]
        la = os.environ.get("LOCALAPPDATA")
        if la:
            cands.append(os.path.join(la, "Microsoft", "WinGet", "Links", exe))
            pk = os.path.join(la, "Microsoft", "WinGet", "Packages")
            if os.path.isdir(pk):
                for dname in os.listdir(pk):
                    if dname.lower().startswith("gyan.ffmpeg"):
                        for root, _dirs, files in os.walk(os.path.join(pk, dname)):
                            if exe in files:
                                cands.append(os.path.join(root, exe))
                                break
        return next((c for c in cands if c and os.path.isfile(c)), None)

    def check(self):
        ff = self.find()
        if not ff:
            self.ff, self.ff_info = None, "ffmpeg not found"
        else:
            try:
                subprocess.run([ff, "-hide_banner", "-version"], capture_output=True, timeout=20, creationflags=NOWIN)
                self.ff, self.ff_info = ff, ff
            except Exception as e:  # noqa
                self.ff, self.ff_info = None, f"ffmpeg does not start ({e})"
        self.deps_ok, self.deps_info = stabilize.deps()

    def install_deps(self):
        if self.pip["running"]:
            return self.status()
        if FROZEN:
            self.pip = {"running": False, "log": "",
                        "error": "This copy was installed without the stabilization component – run the Orbital "
                                 "installer again and tick “Stabilization”."}
            return self.status()
        self.pip = {"running": True, "log": "", "error": ""}

        def work():
            cmd = [sys.executable, "-m", "pip", "install", "--user", "--disable-pip-version-check", "numpy", "opencv-python-headless"]
            log("Installing numpy and OpenCV (pip) …")
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=900, creationflags=NOWIN)
                out = (r.stdout + r.stderr).decode("utf-8", "replace").strip().splitlines()
                self.pip["log"] = "\n".join(out[-8:])
                if r.returncode != 0:
                    self.pip["error"] = out[-1] if out else f"pip exit code {r.returncode}"
            except Exception as e:  # noqa
                self.pip["error"] = str(e)
            import importlib
            importlib.invalidate_caches()
            self.deps_ok, self.deps_info = stabilize.deps()
            if not self.deps_ok and not self.pip["error"]:
                self.pip["error"] = "Installed, but only found after restarting the server"
            log(f"Packages: {'ready' if self.deps_ok else self.pip['error']}")
            self.pip["running"] = False

        threading.Thread(target=work, daemon=True).start()
        return self.status()

    def info(self, path):
        st = os.stat(path)
        key = (path, st.st_size)
        if key in self._cache:
            return self._cache[key]
        res = {}
        if self.ff:
            try:
                err = subprocess.run([self.ff, "-hide_banner", "-i", path], capture_output=True, timeout=20,
                                     creationflags=NOWIN).stderr.decode("utf-8", "replace")
                m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
                if m:
                    res["duration_s"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                m = re.search(r"Audio: ([^\n,]+)", err)
                res["audio"] = m.group(1).strip() if m else None
            except Exception:  # noqa
                pass
        self._cache[key] = res
        return res

    def files(self):
        d, out = self.rec.dir, []
        if not os.path.isdir(d):
            return out
        names = sorted((n for n in os.listdir(d) if n.lower().endswith(".mp4")
                        and not n.lower().endswith(SMOOTH_SUFFIXES + (".part.mp4",))),
                       key=lambda n: os.path.getmtime(os.path.join(d, n)), reverse=True)[:30]
        cur = os.path.basename(self.rec.file) if self.rec.active and self.rec.file else None
        for i, n in enumerate(names):
            p = os.path.join(d, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            smooth = next((n[:-4] + s for s in SMOOTH_SUFFIXES if os.path.exists(os.path.join(d, n[:-4] + s))), None)
            e = {"name": n, "mb": round(st.st_size / 1024 ** 2, 1),
                 "time": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
                 "recording": n == cur, "smooth": smooth}
            if not e["recording"] and i < 15:
                e.update(self.info(p))
            out.append(e)
        return out

    def status(self):
        return {"ffmpeg": {"ok": bool(self.ff), "info": self.ff_info}, "packages": {"ok": self.deps_ok, "info": self.deps_info, **self.pip},
                "ready": bool(self.ff) and self.deps_ok, "running": self.running, "file": self.file,
                "phase": self.phase, "progress": round(self.progress, 3), "result": self.result, "error": self.error,
                "folder": self.rec.dir}

    def start(self, name, preset):
        if self.running:
            raise OnvifError("A recording is already being stabilized")
        if not (self.ff and self.deps_ok):
            self.check()
            if not self.ff:
                raise OnvifError(f"Stabilizing not possible: {self.ff_info}")
            if not self.deps_ok:
                raise OnvifError("Stabilizing needs numpy and OpenCV – button \"Install packages\"")
        if preset not in self.PRESETS:
            raise ValueError("unknown strength")
        if os.path.basename(name) != name or not name.lower().endswith(".mp4"):
            raise ValueError("invalid file name")
        p = os.path.join(self.rec.dir, name)
        if not os.path.isfile(p):
            raise OnvifError(f"{name} not found")
        if self.rec.active and self.rec.file and os.path.basename(self.rec.file) == name:
            raise OnvifError("This recording is still running")
        self.running, self.abort, self.file, self.result, self.error = True, False, name, None, ""
        self.progress, self.phase = 0.0, "starting …"
        threading.Thread(target=self._run, args=(name, preset), daemon=True).start()
        return self.status()

    def stop(self):
        self.abort = True
        return self.status()

    def _run(self, name, preset):
        d = self.rec.dir
        src = os.path.join(d, name)
        tmp, dst = os.path.join(d, name[:-4] + "_smooth.part.mp4"), os.path.join(d, name[:-4] + "_smooth.mp4")
        log(f"Stabilizing: {name} ({preset})")

        def prog(phase, frac):
            self.phase, self.progress = phase, frac

        try:
            res = stabilize.run(self.ff, src, tmp, preset, prog, lambda: self.abort)
            os.replace(tmp, dst)
            self.result, self.progress, self.phase = os.path.basename(dst), 1.0, "done"
            log(f"Stabilized: {os.path.basename(dst)} (max correction {res['max_correction_px']} px, margin {res['margin_px']} px)")
        except stabilize.Cancelled:
            self.error, self.phase = "cancelled", "cancelled"
        except Exception as e:  # noqa
            self.error, self.phase = str(e), "error"
            log(f"Stabilizing: {e}")
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
            self.running = False


# --------------------------------------------------------------------------- HTTP

class App:
    cfg = cam = web = ptz = dog = video = rec = motion = cal = smooth = vcheck = focus = names = moves = expo = audio = None
    booting = False
    connecting = False
    last_request = 0.0      # monotonic time of the page's last request (app.py watches it)


def open_path(path):
    if os.name == "nt":
        os.startfile(path)  # noqa
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])


APP_WINDOW = {"kind": "browser"}   # set by app.py: "window" when shown in its own window


def is_safe_host(host):
    name = (host or "").rsplit(":", 1)[0].strip("[]")
    if name == "localhost":
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


class Handler(BaseHTTPRequestHandler):
    server_version = "Orbital/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except (ConnectionError, OSError):
            pass   # the page went away (window closed or reloaded) before the answer

    def _guard(self, post):
        # DNS-rebinding protection: only answer to localhost/IP host names,
        # and accept POSTs only from our own page (custom header => no simple cross-site form posts).
        if not is_safe_host(self.headers.get("Host")):
            self._send(403, {"error": "Host not allowed"})
            return False
        App.last_request = time.monotonic()
        if post and self.headers.get("X-Orbital") != "1":
            self._send(403, {"error": "Request not from the control page"})
            return False
        return True

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        if not self._guard(False):
            return
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            try:
                with open(PAGE, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                self._send(404, {"error": "index.html is missing in the folder"})
            return
        if path == "/lucide-icons.json":      # icon set for the presets (Lucide, ISC licence)
            try:
                with open(os.path.join(RES_DIR, "lucide-icons.json"), "rb") as f:
                    return self._send(200, f.read())
            except OSError:
                return self._send(404, {"error": "lucide-icons.json is missing"})
        if path == "/api/status":
            return self._status()
        if path == "/api/ping":
            return self._send(200, {"ok": True})
        if path == "/api/ptz/active":
            return self._send(200, App.moves.active)
        if path == "/api/log":
            try:
                since = int(parse_qs(urlparse(self.path).query).get("since", ["0"])[0])
            except ValueError:
                since = 0
            return self._send(200, log_since(since))
        if path == "/api/config":
            return self._run(config_view)
        if path == "/api/ptz/presets":
            return self._run(lambda: App.names.merge(App.ptz.presets()))
        if path == "/api/focus":
            return self._run(App.focus.read_mode)
        if path == "/api/glide":
            return self._send(200, App.moves.status())
        if path == "/api/exposure":
            return self._run(App.expo.state)
        if path == "/api/audio":
            return self._run(App.audio.state)
        if path == "/api/ptz/position":
            return self._run(App.cam.status)
        if path == "/api/diagnostics":
            return self._run(App.cam.diagnose)
        if path == "/api/ai":
            return self._run(lambda: {"ai": App.web.get_ai()})
        if path == "/api/recording":
            return self._send(200, App.rec.status())
        if path == "/api/joy":
            return self._send(200, App.motion.engine.state())
        if path == "/api/curves":
            return self._send(200, {"curves": App.motion.engine.get_config(), "defaults": motion.DEFAULTS})
        if path == "/api/calibration":
            return self._send(200, App.cal.status())
        if path == "/api/visca":
            return self._send(200, App.vcheck.status())
        if path == "/api/stabilize":
            return self._send(200, App.smooth.status())
        if path == "/api/recordings":
            return self._run(lambda: {"folder": App.rec.dir, "files": App.smooth.files()})
        self._send(404, {"error": "Unknown path"})

    def do_POST(self):
        if not self._guard(True):
            return
        u = urlparse(self.path)
        path = u.path
        if path == "/api/webrtc":
            return self._webrtc(parse_qs(u.query).get("src", [""])[0])
        if path == "/api/still":
            return self._run(self._still)
        try:
            data = json.loads(self._body() or b"{}")
        except ValueError:
            return self._send(400, {"error": "Invalid data"})
        cam, dog = App.ptz, App.dog
        # any manual input cancels a running straight move
        moving_input = (path in ("/api/ptz/home", "/api/ptz/zoomstep", "/api/calibration/start", "/api/visca/start")
                        or (path in ("/api/joy", "/api/ptz/move") and any(float(data.get(k, 0) or 0) for k in ("x", "y", "z"))))
        if moving_input or path in ("/api/ptz/stop", "/api/joy/halt"):
            App.moves.cancel()
        if moving_input:
            App.moves.manual()
        routes = {
            "/api/connect": self._reconnect,
            "/api/config": lambda: save_config(data),
            "/api/log/open": lambda: (open_path(DATA_DIR), {"ok": True, "folder": DATA_DIR})[1],
            "/api/ptz/move": lambda: (cam.move(float(data.get("x", 0)), float(data.get("y", 0)), float(data.get("z", 0))),
                                      dog.moved(), {"ok": True})[2],
            "/api/ptz/alive": lambda: (dog.alive(), {"ok": True})[1],
            "/api/ptz/stop": lambda: (dog.stopped(), cam.stop(), {"ok": True})[2],
            "/api/ptz/zoomstep": lambda: (cam.zoom_step(float(data["dz"])), {"ok": True})[1],
            "/api/ptz/home": lambda: (App.motion.engine.halt(), time.sleep(0.15), cam.home(), {"ok": True})[3],
            "/api/ptz/sethome": lambda: (cam.set_home(), {"ok": True})[1],
            "/api/ptz/goto": lambda: App.moves.goto(data["token"]),
            "/api/ptz/setpreset": lambda: self._set_preset(data),
            "/api/ptz/name": lambda: {"names": App.names.set(data["token"], data.get("name", ""))},
            "/api/ptz/icon": lambda: {"icons": App.names.set_icon(data["token"], data.get("icon", ""))},
            "/api/ptz/duration": lambda: {"durations": App.names.set_duration(data["token"], data.get("duration"))},
            "/api/ptz/click": lambda: App.moves.click(data["dx"], data["dy"]),
            "/api/ptz/click/learn": lambda: App.moves.click_learn(data),
            "/api/ai": lambda: {"ai": App.web.set_ai(str(data["cmd"]), str(data["value"]))},
            "/api/joy": lambda: App.motion.engine.set_target(float(data.get("x", 0)), float(data.get("y", 0)), float(data.get("z", 0))),
            "/api/joy/release": App.motion.engine.release,
            "/api/joy/halt": lambda: (App.motion.engine.halt(), App.motion.engine.state())[1],
            "/api/curves": lambda: {"curves": App.motion.engine.set_config(data)},
            "/api/glide": lambda: (App.moves.glide.set_config(data), App.moves.status())[1],
            "/api/glide/calibrate": App.moves.calibrate,
            "/api/audio": lambda: App.audio.set(str(data.get("source", "")), str(data.get("device", ""))),
            "/api/audio/devices": lambda: (App.audio.list_devices(), App.audio.state())[1],
            "/api/exposure": lambda: self._exposure(data),
            "/api/focus/mode": lambda: App.focus.set_mode(str(data.get("mode", ""))),
            "/api/focus/once": lambda: App.focus.once(float(data.get("seconds", 2.0))),
            "/api/focus/drive": lambda: App.focus.drive(int(data.get("direction", 0)), int(data.get("speed", 3))),
            "/api/focus/stop": App.focus.stop,
            "/api/recording/start": lambda: App.rec.start(App.video),
            "/api/recording/stop": lambda: App.rec.stop(wait=3),
            "/api/recording/folder": self._open_folder,
            "/api/recordings/reveal": lambda: self._reveal(data),
            "/api/stabilize/start": lambda: App.smooth.start(str(data.get("file", "")), str(data.get("strength", "medium"))),
            "/api/stabilize/stop": App.smooth.stop,
            "/api/stabilize/install": App.smooth.install_deps,
            "/api/stabilize/check": lambda: (App.smooth.check(), App.smooth.status())[1],
            "/api/calibration/start": App.cal.start,
            "/api/calibration/stop": App.cal.stop,
            "/api/visca/start": App.vcheck.start,
        }
        fn = routes.get(path)
        if fn:
            return self._run(fn)
        self._send(404, {"error": "Unknown path"})

    # --- POST helpers
    def _reconnect(self):
        if App.cfg["control"] != "onvif":
            try:
                App.web.connect()
            except OnvifError:
                pass
        try:
            App.cam.connect()
        except OnvifError:
            if App.ptz.mode() == "onvif":
                raise
        return self._status_dict()

    def _set_preset(self, data):
        tok = str(data.get("token"))
        name = str(data.get("name") or App.names.names.get(tok) or f"Preset{tok}")
        App.moves.cancel()
        res = {"token": App.ptz.set_preset(tok, name)}
        App.moves._set_active(tok, "at")
        try:
            res["visca"] = App.moves.snapshot(tok)
        except Exception as e:  # noqa
            log(f"VISCA coordinates for preset {tok} not read: {e}")
        return res

    def _exposure(self, data):
        if "mode" in data:
            return App.expo.set_mode(str(data["mode"]))
        if data.get("iris_open"):
            return App.expo.iris_open()
        return App.expo.step(str(data["step"]), int(data.get("direction", 1)) > 0)

    def _still(self):
        """Still image from the page (JPEG) -> recordings folder."""
        n = int(self.headers.get("Content-Length") or 0)
        if not 0 < n <= 40 * 1024 ** 2:
            raise ValueError("no image")
        data = self.rfile.read(n)
        if data[:3] != b"\xff\xd8\xff":
            raise ValueError("not a JPEG image")
        os.makedirs(App.rec.dir, exist_ok=True)
        name = time.strftime("still_%Y-%m-%d_%H-%M-%S.jpg")
        path = os.path.join(App.rec.dir, name)
        k = 1
        while os.path.exists(path):
            k += 1
            path = os.path.join(App.rec.dir, name[:-4] + f"_{k}.jpg")
        with open(path, "wb") as f:
            f.write(data)
        return {"ok": True, "file": os.path.basename(path), "folder": App.rec.dir}

    def _open_folder(self):
        os.makedirs(App.rec.dir, exist_ok=True)
        open_path(App.rec.dir)
        return {"ok": True, "folder": App.rec.dir}

    def _reveal(self, data):
        n = os.path.basename(str(data.get("file", "")))
        p = os.path.join(App.rec.dir, n)
        if not n or not os.path.isfile(p):
            raise ValueError("file not found")
        if os.name == "nt":
            subprocess.Popen(f'explorer /select,"{p}"')
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", p])
        else:
            open_path(os.path.dirname(p))
        return {"ok": True}

    def _run(self, fn):
        try:
            self._send(200, fn())
        except OnvifError as e:
            (App.web if isinstance(e, WebApiError) else App.cam).last_error = str(e)
            self._send(502, {"error": str(e)})
        except (KeyError, ValueError, TypeError) as e:
            self._send(400, {"error": f"Invalid request ({e})"})
        except Exception as e:  # noqa
            self._send(500, {"error": f"{type(e).__name__}: {e}"})

    def _status_dict(self):
        c = App.cam
        return {
            "app": {"version": VERSION, "configured": App.cfg["configured"], "booting": App.booting,
                    "frozen": FROZEN, "data_dir": DATA_DIR, "window": APP_WINDOW["kind"]},
            "camera": {"ip": App.cfg["ip"], "onvif_port": App.cfg["onvif_port"], "user": App.cfg["user"],
                       "password_set": bool(App.cfg["password"])},
            "onvif": {"connected": c.connected, "error": c.last_error, "info": c.info, "profile": c.profile,
                      "profiles": c.profiles, "ptz_node": c.node, "services": {"media": c.media_url, "ptz": c.ptz_url},
                      "clock_offset_s": round(c.offset)},
            "video": {"state": App.video.state, "streams": list(App.video.streams.keys())},
            "control": {"kind": App.ptz.mode(), "setting": App.cfg["control"],
                        "diagonal": App.ptz.mode() == "onvif" or (App.motion.engine.cfg["pt_path"] == "onvif" and App.cam.connected),
                        "motion": App.motion.engine.state(),
                        "move": App.moves.glide.state,
                        "web": {"connected": App.web.connected, "error": App.web.last_error,
                                "speed_range": App.web.ability, "camera_speeds": App.web.speeds,
                                "presets_max": App.web.preset_max}},
        }

    def _status(self):
        if App.booting or App.connecting or not App.cfg["configured"]:
            return self._send(200, self._status_dict())
        if App.cfg["control"] != "onvif" and not App.web.connected:
            try:
                App.web.connect()
            except OnvifError:
                pass
        c = App.cam
        if not c.connected:
            try:
                c.connect()
            except OnvifError as e:
                c.last_error = str(e)
            except Exception as e:  # noqa
                c.last_error = f"{type(e).__name__}: {e}"
        self._send(200, self._status_dict())

    def _webrtc(self, src):
        body = self._body()
        req = urllib.request.Request(f"{GO2RTC_API}/api/webrtc?src={urllib.request.quote(src)}", data=body,
                                     method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=10) as r:
                self._send(r.status, r.read(), r.headers.get("Content-Type", "application/json"))
        except urllib.error.HTTPError as e:
            detail = (e.read() or b"").decode("utf-8", "replace").strip()
            self._send(502, {"error": f"Video service reports {e.code}: {detail or 'no details'}"})
        except Exception:
            self._send(503, {"error": f"Video service not reachable ({App.video.state})"})


def connect_camera():
    """Connect ONVIF and the web API and report what was found (also after settings changes)."""
    try:
        _connect_camera()
    finally:
        App.connecting = False


def _connect_camera():
    cfg = App.cfg
    if not cfg["configured"]:
        return
    if not cfg["password"]:
        log("Note: no camera password set.")
    try:
        App.cam.connect()
        i = App.cam.info
        log(f"Camera: {i.get('Manufacturer', '')} {i.get('Model', '')} (firmware {i.get('FirmwareVersion', '?')})")
        log(f"Profile: {App.cam.profile['name']} – PTZ {'yes' if App.cam.profile['ptz'] else 'no'}")
    except Exception as e:  # noqa
        App.cam.last_error = str(e)
        log(f"ONVIF: {e}")
    if cfg["control"] != "onvif":
        try:
            App.web.connect()
            log(f"Control: camera web API (PTZ speed {App.web.ability.get('ptz')}, zoom {App.web.ability.get('zoom')})")
        except Exception as e:  # noqa
            log(f"Web API: {e}")
    if App.ptz.mode() == "onvif":
        log("Control: ONVIF")


class PortInUse(Exception):
    pass


def boot():
    """Create everything and open the HTTP server (not serving yet). Video and camera connect in
    the background so that the page is there at once. Returns (server, url)."""
    setup_logging()
    log(f"Orbital {VERSION} – data folder {DATA_DIR}")
    migrate_legacy()
    App.cfg = cfg = load_config()
    App.cam = Onvif(cfg)
    App.web = WebApi(cfg)
    App.ptz = Control(cfg, App.cam, App.web)
    App.dog = Watchdog(App.ptz)
    App.video = Video(cfg)
    App.rec = Recorder(cfg)
    App.motion = Motion()
    App.cal = Calibration()
    App.vcheck = ViscaCheck()
    App.focus = FocusCtl()
    App.names = PresetNames()
    App.moves = PresetMoves()
    App.expo = Exposure()
    App.audio = Audio()
    App.smooth = Smoother(App.rec)

    bind = "127.0.0.1" if cfg["local_only"] else "0.0.0.0"
    try:
        srv = ThreadingHTTPServer((bind, cfg["port"]), Handler)
    except OSError as e:
        raise PortInUse(f"Port {cfg['port']} is in use ({e}). Is Orbital already running? "
                        "Otherwise pick another page port in config.ini.")
    url = f"http://localhost:{cfg['port']}/"
    App.booting = True

    def startup():
        try:
            App.smooth.check()
            log(f"Stabilizing: ffmpeg {App.smooth.ff or '– ' + App.smooth.ff_info} · packages {App.smooth.deps_info}")
            if cfg["configured"]:
                App.video.start()
            else:
                App.video.state = "no camera set up yet"
            log(f"Video: {App.video.state}")
            connect_camera()
        finally:
            App.booting = False
        log(f"Recordings: {App.rec.dir} (stream {App.rec.src}, "
            f"{'every ' + str(App.rec.seg // 60) + ' min' if App.rec.seg else 'one file'})")

    threading.Thread(target=startup, name="startup", daemon=True).start()
    log(f"Control page: {url}" + ("" if cfg["local_only"] else f" (also reachable from the network on port {cfg['port']})"))
    return srv, url


def shutdown():
    """Stop moves, recording, camera and go2rtc. Safe to call more than once."""
    if getattr(shutdown, "done", False):
        return
    shutdown.done = True
    for fn in (App.moves.cancel, App.motion.engine.halt, App.focus.stop, lambda: App.rec.stop(wait=3),
               App.ptz.stop, App.video.stop):
        try:
            fn()
        except Exception:  # noqa
            pass
    log("Stopped.")


def main():
    try:
        srv, url = boot()
    except PortInUse as e:
        log(str(e))
        return 1
    log("Keep this window open. Quit with Ctrl+C.")
    on_window_close(shutdown)
    if App.cfg["open_browser"]:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
