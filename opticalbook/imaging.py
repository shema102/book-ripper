"""Camera ownership, perspective correction and inexpensive image quality checks."""
from __future__ import annotations
import math
import os
import sys
import threading
import time
import copy
import numpy as np
from PIL import Image


# Calibrated camera-image quadrilaterals: project key -> (saved test crop, smallest crop side in pixels).
# All are clicked top-left, top-right, bottom-right, bottom-left in reading order.
REGIONS = {"corners": ("crop.png", 50), "screen_corners": ("screen.png", 50), "indicator_corners": ("indicator.png", 10)}
UNIT = [(0, 0), (1, 0), (1, 1), (0, 1)]


def quad(corners, size=None):
    """Validated 4x2 array of a convex, perimeter-ordered quadrilateral (inside `size` when given)."""
    if len(corners) != 4:
        raise ValueError("Select four corners: top-left, top-right, bottom-right, bottom-left in reading order.")
    points = np.asarray(corners, dtype=float)
    if points.shape != (4, 2) or not np.isfinite(points).all():
        raise ValueError("Invalid corner coordinates.")
    if size and any(not (0 <= x <= size[0] and 0 <= y <= size[1]) for x, y in points):
        raise ValueError("Calibration corners lie outside the camera image.")
    edges = np.roll(points, -1, axis=0) - points
    crosses = [np.linalg.det(np.array([edges[i], edges[(i+1) % 4]])) for i in range(4)]
    if not (all(x > 0 for x in crosses) or all(x < 0 for x in crosses)):
        raise ValueError("Corners cross or form a flat shape. Select them again in perimeter order.")
    return points


def rotate_point(point, size, turns):
    """Where `point` of an image of `size` lands after rotating the image `turns` quarter turns clockwise.
    The inverse is rotate_point(rotated, rotated_size, -turns)."""
    (x, y), (width, height) = point, size
    for _ in range(turns % 4):
        x, y, width, height = height - y, x, height, width
    return [x, y]


def homography(source, target):
    """3x3 perspective matrix mapping four source points onto four target points."""
    rows, rhs = [], []
    for (x, y), (u, v) in zip(source, target):
        rows.extend([[x, y, 1, 0, 0, 0, -u*x, -u*y], [0, 0, 0, x, y, 1, -v*x, -v*y]])
        rhs.extend([u, v])
    return np.append(np.linalg.solve(rows, rhs), 1).reshape(3, 3)


def project(matrix, point):
    x, y, w = matrix @ [point[0], point[1], 1]
    return [float(x/w), float(y/w)]


def to_screen(screen, point):
    """Camera-image pixel -> phone screen fractions (0–1 inside the calibrated screen)."""
    return project(homography(quad(screen), UNIT), point)


def from_screen(screen, fraction):
    """Phone screen fractions -> camera-image pixel, for drawing gestures on the preview."""
    return project(homography(UNIT, quad(screen)), fraction)


def gesture_from(screen, points, kind):
    """Gesture settings from camera-image clicks: one point for a tap, start and end for a swipe."""
    if len(screen) != 4:
        raise ValueError("Calibrate the four phone screen corners first.")
    if kind not in ("tap", "swipe") or len(points) != (1 if kind == "tap" else 2):
        raise ValueError("A tap needs one point; a swipe needs a start and an end point.")
    fractions = [[round(value, 4) for value in to_screen(screen, point)] for point in points]
    if any(not 0 <= value <= 1 for fraction in fractions for value in fraction):
        raise ValueError("Gesture points must lie inside the calibrated phone screen.")
    return {"kind": "tap", "end": fractions[0]} if kind == "tap" else {"kind": "swipe", "start": fractions[0], "end": fractions[1]}


def rectify(image, corners, minimum=50):
    points = quad(corners, image.size)
    a, b, c, d = points
    width = round((np.linalg.norm(b-a) + np.linalg.norm(c-d)) / 2)
    height = round((np.linalg.norm(d-a) + np.linalg.norm(c-b)) / 2)
    if min(width, height) < minimum or width * height > 60_000_000:
        raise ValueError("Calibrated crop is too small or too large.")
    coefficients = homography([(0, 0), (width, 0), (width, height), (0, height)], points).flatten()[:8]
    return image.transform((width, height), Image.Transform.PERSPECTIVE, coefficients, Image.Resampling.BICUBIC)


def sharpness(image):
    gray = np.asarray(image.convert("L"), dtype=np.float32)
    if min(gray.shape) < 3:
        return 0.0
    return float((4*gray[1:-1, 1:-1] - gray[:-2, 1:-1] - gray[2:, 1:-1]
                  - gray[1:-1, :-2] - gray[1:-1, 2:]).var())


# Absolute |Laplacian| thresholds on 0-255 grey: a blurred image shows no peaking at all.
PEAKING = {"Low": 110, "Mid": 70, "High": 40}


def peaking(image, size, level="Mid", colour=(255, 32, 32)):
    """Preview at `size` with in-focus edges painted, like camera focus peaking. Edges are found at
    full resolution (fine text detail is lost by downscaling) and kept when any pixel of a block peaks."""
    gray = np.asarray(image.convert("L"), dtype=np.int16)
    edges = np.zeros(gray.shape, dtype=np.uint8)
    if min(gray.shape) >= 3:
        laplacian = 4*gray[1:-1, 1:-1] - gray[:-2, 1:-1] - gray[2:, 1:-1] - gray[1:-1, :-2] - gray[1:-1, 2:]
        edges[1:-1, 1:-1] = (np.abs(laplacian) >= PEAKING[level]) * np.uint8(255)
    mask = Image.fromarray(edges).resize(size, Image.Resampling.BOX).point(lambda value: 255 if value else 0)
    result = image.convert("RGB").resize(size)
    result.paste(colour, mask=mask)
    return result


def difference(left, right):
    def thumb(image):
        return np.asarray(image.convert("L").resize((192, 256)), dtype=float)
    return float(np.abs(thumb(left)-thumb(right)).mean())


# Controls an open camera accepts; everything else (source, backend, size, FPS, FourCC) needs a reopen.
# Order matters: automatic modes are switched before the manual values they would override.
LIVE = [("autofocus", "CAP_PROP_AUTOFOCUS"), ("focus", "CAP_PROP_FOCUS"),
        ("auto_exposure", "CAP_PROP_AUTO_EXPOSURE"), ("exposure", "CAP_PROP_EXPOSURE"),
        ("brightness", "CAP_PROP_BRIGHTNESS"), ("auto_white_balance", "CAP_PROP_AUTO_WB"),
        ("white_balance", "CAP_PROP_WB_TEMPERATURE")]
# Manual value -> its mode switch. None = camera default (not sent), True = automatic, False = manual value.
MANUAL = {"focus": "autofocus", "exposure": "auto_exposure", "white_balance": "auto_white_balance"}


def controls(config, backend=""):
    """Ordered (setting, OpenCV property name, raw value) writes for the live controls."""
    for value, mode in MANUAL.items():
        if config.get(mode) is False and config.get(value) is None:
            raise ValueError(f"Manual {value} needs a value, or set {mode} to automatic.")
        if config.get(value) is not None and (config.get(mode) is None or config.get(mode) is True):
            raise ValueError(f"A {value} value only applies in manual mode; set {mode} to False or clear {value}.")
    writes = []
    for key, name in LIVE:
        value = config.get(key)
        if value is None:
            continue
        if isinstance(value, bool):
            # V4L2 exposure modes are 3 = automatic, 1 = manual; other backends use nonzero = automatic.
            value = (3 if value else 1) if key == "auto_exposure" and backend == "v4l2" else int(value)
        writes.append((key, name, float(value)))
    return writes


def exposure_seconds(raw, backend):
    """Shutter time of a raw CAP_PROP_EXPOSURE value: 0.1 ms units on V4L2, log2 seconds elsewhere."""
    return raw * 1e-4 if backend == "v4l2" else 2.0 ** raw


def exposure_raw(seconds, backend):
    """Nearest raw CAP_PROP_EXPOSURE value; Windows backends only take whole powers of two."""
    return float(max(1, round(seconds * 1e4))) if backend == "v4l2" else float(round(math.log2(seconds)))


def shutter_text(seconds):
    return f"1/{round(1/seconds):g}" if seconds < 1 else f"{seconds:g}s"


def parse_shutter(text):
    """Seconds from '1/60', '60' (camera style, = 1/60), '0.5' or '2s'."""
    value = text.strip().lower()
    try:
        if value.startswith("1/"): seconds = 1/float(value[2:].rstrip("s"))
        elif value.endswith("s"): seconds = float(value[:-1])
        else: seconds = float(value); seconds = 1/seconds if seconds > 1 else seconds
    except (ValueError, ZeroDivisionError):
        seconds = 0
    if not seconds > 0:
        raise ValueError(f"Exposure '{text}' is not a shutter time such as 1/60, 60 or 2s.")
    return seconds


def resolve_backend(name):
    """'auto' on Windows means DirectShow: it opens in about a second with the format negotiated up front,
    delivers MJPG at full frame rate and exposes white balance. Media Foundation does neither."""
    return "dshow" if name == "auto" and sys.platform == "win32" else name


class Camera:
    """Continuously drain a camera so captures do not use stale queued frames."""
    def __init__(self, config, log=lambda text: None):
        # MSMF hardware colour conversion makes each open and each format change take about 6 s.
        os.environ.setdefault("OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS", "0")
        try:
            import cv2
        except ImportError as error:
            raise RuntimeError('Install webcam support with: pip install ".[capture]"') from error
        if config.get("fourcc") and len(config["fourcc"]) != 4:
            raise ValueError("FourCC must contain four characters, or be blank.")
        controls(config)
        self.settings = copy.deepcopy(config)
        self.cv2 = cv2
        backends = {"auto": cv2.CAP_ANY, "dshow": cv2.CAP_DSHOW, "msmf": cv2.CAP_MSMF,
                    "v4l2": cv2.CAP_V4L2, "avfoundation": cv2.CAP_AVFOUNDATION}
        backend = resolve_backend(config["backend"])
        source = str(config["source"])
        source = int(source) if source.isdecimal() else source
        formats = [("width", cv2.CAP_PROP_FRAME_WIDTH), ("height", cv2.CAP_PROP_FRAME_HEIGHT), ("fps", cv2.CAP_PROP_FPS)]
        # Negotiating the format while opening avoids one stream restart per property. MSMF rejects FourCC here.
        params = [cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*config["fourcc"])] if config.get("fourcc") and backend != "msmf" else []
        params += [value for key, prop in formats if config.get(key) for value in (prop, int(config[key]))]
        started = time.monotonic()
        self.cap = cv2.VideoCapture(source, backends[backend], params)
        separate = not self.cap.isOpened()
        if separate:
            self.cap.release()
            log("Camera refused the requested format while opening; retrying with separate settings (slower).")
            self.cap = cv2.VideoCapture(source, backends[backend])
        if not self.cap.isOpened():
            self.cap.release()
            raise RuntimeError("Cannot open camera. Check its index/backend, OS permissions and other camera apps.")
        try:
            try:
                self.backend = self.cap.getBackendName().lower()
            except Exception:
                self.backend = backend
            writes = controls(config, self.backend)
            properties = formats + [(key, getattr(cv2, name)) for key, name in LIVE]
            if separate:
                if config.get("fourcc"):
                    self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*config["fourcc"]))
                for key, prop in formats:
                    if config.get(key) is not None and not self.cap.set(prop, float(config[key])):
                        log(f"Camera did not accept {key}={config[key]}; this control may be unsupported.")
            self.rejected = self._write(writes)
            self._report(writes, self.rejected, log)
            self.actual = {key: self.cap.get(prop) for key, prop in properties}
            code = int(self.cap.get(cv2.CAP_PROP_FOURCC)).to_bytes(4, "little")
            self.actual["fourcc"] = code.decode() if code.isascii() and code.decode().isprintable() else ""
            log(f"Opened camera {source} with {self.backend.upper()} in {time.monotonic()-started:.1f} s: " + str(self.actual))
            self.condition = threading.Condition()
            self.sequence, self.latest, self.error, self.pending = 0, None, None, None
            self.stop = threading.Event()
            self.thread = threading.Thread(target=self._read, daemon=True)
            self.thread.start()
        except BaseException:
            self.cap.release()
            raise

    def _read(self):
        try:
            while not self.stop.is_set():
                job = self.pending
                if job:
                    self.rejected = self._write(job[0])
                ok, frame = self.cap.read()
                if not ok:
                    raise RuntimeError("Camera stopped producing images.")
                image = Image.fromarray(self.cv2.cvtColor(frame, self.cv2.COLOR_BGR2RGB))
                self.actual['width'], self.actual['height'] = image.size
                if job:
                    # Read back after a frame: some drivers (C920 white balance) report stale values right after a set.
                    self.actual.update({key: self.cap.get(getattr(self.cv2, name)) for key, name in LIVE})
                    self.pending = None
                    job[1].set()
                with self.condition:
                    self.latest = image
                    self.sequence += 1
                    self.condition.notify_all()
        except Exception as error:
            with self.condition:
                self.error = error
                self.condition.notify_all()
                if self.pending:
                    self.pending[1].set()

    def _write(self, writes):
        """Send control values; returns the settings the driver refused."""
        return [key for key, name, value in writes if not self.cap.set(getattr(self.cv2, name), value)]

    @staticmethod
    def _report(writes, rejected, log):
        for key, _, value in writes:
            if key in rejected:
                log(f"Camera did not accept {key}={value:g}; this control may be unsupported or out of range.")

    def apply(self, config, log=lambda text: None, timeout=15):
        """Set focus/exposure/brightness/white balance without reopening. False when a reopen is needed.
        Settings the driver refused are listed in `self.rejected`."""
        live = {key for key, _ in LIVE}
        if any(config.get(key) != self.settings.get(key) for key in set(config) | set(self.settings) if key not in live):
            return False
        # "Camera default" means not sent; an open camera cannot return a control to its driver default.
        # Switching a manual value back to automatic is fine: the mode stays set.
        if any(config.get(key) is None and self.settings.get(key) is not None and config.get(MANUAL.get(key)) is None
               for key in live):
            return False
        writes = controls(config, self.backend)
        done = threading.Event()
        with self.condition:
            if self.error:
                raise self.error
            # The reader thread only touches the camera: `log` may update a UI owned by this thread.
            self.pending = (writes, done)
        if not done.wait(timeout):
            raise RuntimeError("Timed out applying camera settings.")
        if self.error:
            raise self.error
        self.settings = copy.deepcopy(config)
        self._report(writes, self.rejected, log)
        log("Camera controls now: " + str({key: self.actual[key] for key in live}))
        return True

    def frame(self, after=0, timeout=15):
        deadline = time.monotonic() + timeout
        with self.condition:
            while self.sequence <= after:
                if self.error:
                    raise self.error
                if self.stop.is_set():
                    raise RuntimeError("Camera was closed.")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("Timed out waiting for a fresh camera frame.")
                self.condition.wait(remaining)
            return self.sequence, self.latest.copy()

    def close(self):
        self.stop.set()
        with self.condition:
            self.condition.notify_all()
        self.thread.join(timeout=2)
        self.cap.release()
        self.thread.join(timeout=2)
