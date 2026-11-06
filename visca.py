"""
VISCA over IP for the PTZ camera.

The camera answers on three paths: TCP 52381 (raw VISCA), UDP 52381 (Sony "VISCA over IP"
header) and UDP 1259 (raw VISCA). This module speaks all three.

Message format: command 81 .. FF; replies 90 4y FF (ack), 90 5y FF (completion),
90 50 … FF (inquiry result), 90 6y zz FF (error: 02 syntax, 03 buffer full, 04 cancelled,
05 no socket, 41 not executable now).
"""
import socket
import threading
import time

MODES = ("tcp52381", "udp52381", "udp1259")
PT_INQ = bytes.fromhex("81090612FF")      # pan/tilt position inquiry
ZOOM_INQ = bytes.fromhex("81090447FF")    # zoom position inquiry
FOCUS_INQ = bytes.fromhex("81090448FF")   # focus position inquiry
AF_INQ = bytes.fromhex("81090438FF")      # focus mode inquiry
ERRORS = {0x02: "syntax error", 0x03: "command buffer full", 0x04: "cancelled", 0x05: "no socket",
          0x41: "not executable now"}


class ViscaError(Exception):
    pass


def nibbles(v, n):
    v &= (1 << (4 * n)) - 1
    return bytes((v >> (4 * (n - 1 - i))) & 0xF for i in range(n))


def from_nibbles(bs, signed=True):
    v = 0
    for b in bs:
        v = (v << 4) | (b & 0xF)
    if signed and v & (1 << (4 * len(bs) - 1)):
        v -= 1 << (4 * len(bs))
    return v


def hexs(b):
    return b.hex(" ")


class Link:
    def __init__(self, ip, mode="tcp52381", timeout=1.5):
        if mode not in MODES:
            raise ValueError(mode)
        self.ip, self.mode, self.timeout = ip, mode, timeout
        self.sock, self.seq, self.buf = None, 0, b""
        self.lock = threading.Lock()

    # --- transport
    def _connect(self):
        if self.sock:
            return
        if self.mode == "tcp52381":
            self.sock = socket.create_connection((self.ip, 52381), timeout=self.timeout)
        else:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            if self.mode == "udp52381":  # reset the sequence number
                try:
                    self.sock.settimeout(0.5)
                    self.sock.sendto(b"\x02\x00\x00\x01\x00\x00\x00\x00\x01", (self.ip, 52381))
                    self.sock.recvfrom(64)
                except OSError:
                    pass
        self.sock.settimeout(self.timeout)

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None

    def _send(self, payload, inquiry):
        if self.mode == "udp52381":
            self.seq = (self.seq + 1) & 0xFFFFFFFF
            head = (b"\x01\x10" if inquiry else b"\x01\x00") + len(payload).to_bytes(2, "big") + self.seq.to_bytes(4, "big")
            self.sock.sendto(head + payload, (self.ip, 52381))
        elif self.mode == "udp1259":
            self.sock.sendto(payload, (self.ip, 1259))
        else:
            self.sock.sendall(payload)

    def _messages(self, deadline):
        while True:
            i = self.buf.find(b"\xff")
            if i >= 0:
                m, self.buf = self.buf[:i + 1], self.buf[i + 1:]
                yield m
                continue
            left = deadline - time.monotonic()
            if left <= 0:
                return
            self.sock.settimeout(left)
            try:
                data = self.sock.recv(512) if self.mode == "tcp52381" else self.sock.recvfrom(512)[0]
            except socket.timeout:
                return
            if not data:
                self.close()
                raise ViscaError("connection closed by the camera")
            if self.mode == "udp52381":
                if data[:1] == b"\x02":
                    continue  # reply to a control command
                if len(data) >= 8 and data[:1] == b"\x01":
                    data = data[8:]
            self.buf += data

    def xfer(self, payload, inquiry=False, wait_done=False, timeout=None):
        """Sends a command and returns the replies (list of bytes)."""
        with self.lock:
            try:
                self._connect()
                self.buf = b""
                if self.mode == "tcp52381":  # drop stale replies (e.g. completions of earlier moves)
                    self.sock.setblocking(False)
                    try:
                        while self.sock.recv(512):
                            pass
                    except (BlockingIOError, OSError):
                        pass
                    self.sock.setblocking(True)
                self._send(payload, inquiry)
                msgs = []
                for m in self._messages(time.monotonic() + (timeout or self.timeout)):
                    msgs.append(m)
                    if len(m) >= 3 and (m[1] & 0xF0) == 0x60:
                        break
                    if inquiry and m[1] == 0x50 and len(m) > 3:
                        break
                    if not inquiry and len(m) == 3 and (m[1] & 0xF0) == 0x50:
                        break
                    if not inquiry and not wait_done and len(m) == 3 and (m[1] & 0xF0) == 0x40:
                        break
                return msgs
            except OSError as e:
                self.close()
                raise ViscaError(f"{self.mode}: {e}")

    # --- parsing
    @staticmethod
    def error_of(msgs):
        for m in msgs:
            if len(m) >= 4 and (m[1] & 0xF0) == 0x60:
                return ERRORS.get(m[2], f"error {m[2]:02x}")
        return None

    def pantilt(self):
        """(pan, tilt, pan digits, tilt digits, raw reply)"""
        msgs = self.xfer(PT_INQ, inquiry=True)
        err = self.error_of(msgs)
        rep = next((m for m in msgs if m[1] == 0x50 and len(m) > 3), None)
        if err or not rep:
            raise ViscaError(err or "no position reply")
        body = rep[2:-1]
        split = {8: (4, 4), 10: (5, 5), 9: (5, 4)}.get(len(body))
        if not split:
            raise ViscaError(f"unknown position format: {hexs(rep)}")
        np_, nt = split
        return from_nibbles(body[:np_]), from_nibbles(body[np_:]), np_, nt, hexs(rep)

    def inquiry_raw(self, payload):
        """Any inquiry – returns the raw reply as text (or the error)."""
        msgs = self.xfer(payload, inquiry=True)
        return self.error_of(msgs) or " | ".join(hexs(m) for m in msgs) or "no reply"

    def zoom(self):
        msgs = self.xfer(ZOOM_INQ, inquiry=True)
        err = self.error_of(msgs)
        rep = next((m for m in msgs if m[1] == 0x50 and len(m) > 3), None)
        if err or not rep:
            raise ViscaError(err or "no zoom reply")
        return from_nibbles(rep[2:-1], signed=False), hexs(rep)

    def _inq_value(self, payload, what):
        msgs = self.xfer(payload, inquiry=True)
        err = self.error_of(msgs)
        rep = next((m for m in msgs if m[1] == 0x50 and len(m) > 3), None)
        if err or not rep:
            raise ViscaError(err or f"no reply ({what})")
        return from_nibbles(rep[2:-1], signed=False)

    def focus_pos(self):
        return self._inq_value(FOCUS_INQ, "focus")

    def af_mode(self):
        """2 = autofocus, 3 = manual"""
        return self._inq_value(AF_INQ, "focus mode")

    # --- exposure (Sony command set)
    AE_MODES = {"auto": 0x00, "manual": 0x03, "shutter": 0x0A, "iris": 0x0B, "bright": 0x0D}
    STEP_CMDS = {"iris": 0x0B, "gain": 0x0C, "shutter": 0x0A, "expcomp": 0x0E}
    INQ = {"mode": 0x39, "iris": 0x4B, "gain": 0x4C, "shutter": 0x4A, "expcomp": 0x4E, "expcomp_on": 0x3E}

    def inquire(self, key):
        return self._inq_value(bytes([0x81, 0x09, 0x04, self.INQ[key], 0xFF]), key)

    def ae_mode(self, mode):
        return self.xfer(bytes([0x81, 0x01, 0x04, 0x39, self.AE_MODES[mode], 0xFF]))

    def step(self, what, up):
        return self.xfer(bytes([0x81, 0x01, 0x04, self.STEP_CMDS[what], 0x02 if up else 0x03, 0xFF]))

    def expcomp_on(self, on):
        return self.xfer(bytes([0x81, 0x01, 0x04, 0x3E, 0x02 if on else 0x03, 0xFF]))

    def zoom_direct(self, z):
        return self.xfer(b"\x81\x01\x04\x47" + nibbles(int(z), 4) + b"\xff")

    def focus_direct(self, f):
        return self.xfer(b"\x81\x01\x04\x48" + nibbles(int(f), 4) + b"\xff")

    def drive(self, vp, vt, pan_dir, tilt_dir):
        """pan_dir: -1 left, 0 stop, 1 right · tilt_dir: 1 up, 0 stop, -1 down · speed 1..24"""
        xp = {-1: 1, 0: 3, 1: 2}[pan_dir]
        yt = {1: 1, 0: 3, -1: 2}[tilt_dir]
        return self.xfer(bytes([0x81, 0x01, 0x06, 0x01, max(1, min(0x18, vp)), max(1, min(0x18, vt)), xp, yt, 0xFF]))

    def stop(self):
        return self.drive(1, 1, 0, 0)

    def absolute(self, vp, vt, pan, tilt, np_=4, nt=4, wait_done=False, timeout=None):
        cmd = bytes([0x81, 0x01, 0x06, 0x02, max(1, min(0x18, vp)), max(1, min(0x18, vt))]) + nibbles(pan, np_) + nibbles(tilt, nt) + b"\xff"
        return self.xfer(cmd, wait_done=wait_done, timeout=timeout)
