"""Photograph a physical reader via DirectShow; all files stay on this PC.

Use the bundled Python (Pillow + NumPy). Run --help for commands. Camera
acquisition starts only for `photo` or `capture`, never during device listing.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import numpy as np
from PIL import Image

# Personal workstation paths and phone identity removed from the reference copy.
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
ADB = os.environ.get("ADB", "adb")
SERIAL = os.environ.get("ANDROID_SERIAL", "")
PACKAGE = "com.readeat.app"


def write_json(path, value):
    path = Path(path)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


class Camera:
    """Drain FFmpeg continuously so a saved photo is a fresh camera frame."""

    def __init__(self, config, log_path):
        self.width, self.height = config["width"], config["height"]
        self.condition = threading.Condition()
        self.latest = None
        self.sequence = 0
        self.ended = False
        self.log = open(log_path, "ab")
        command = [FFMPEG, "-nostdin", "-hide_banner", "-nostats", "-loglevel", "info",
                   "-f", "dshow", "-rtbufsize", "32M", "-video_size",
                   f"{self.width}x{self.height}", "-framerate", str(config.get("fps", 15))]
        if config.get("input_format"):
            command += ["-vcodec", config["input_format"]]
        command += ["-i", "video=" + config["camera"], "-an", "-vf",
                    f"scale={self.width}:{self.height}", "-pix_fmt", "rgb24",
                    "-f", "rawvideo", "pipe:1"]
        self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=self.log,
                                        creationflags=subprocess.CREATE_NO_WINDOW)
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()
        if config.get("manual_focus") is not None:
            try:
                self.frame()
                subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                                "-File", str(Path(__file__).with_name("camera-focus.ps1")),
                                "-CameraName", config["camera"], "-Focus", str(config["manual_focus"])],
                               capture_output=True, check=True, timeout=20,
                               creationflags=subprocess.CREATE_NO_WINDOW)
                time.sleep(.7)
            except BaseException:
                self.close()
                raise

    def _read(self):
        size = self.width * self.height * 3
        try:
            while True:
                frame = bytearray()
                while len(frame) < size:
                    block = self.process.stdout.read(size - len(frame))
                    if not block:
                        return
                    frame.extend(block)
                with self.condition:
                    self.latest = bytes(frame)
                    self.sequence += 1
                    self.condition.notify_all()
        finally:
            with self.condition:
                self.ended = True
                self.condition.notify_all()

    def frame(self, after=0, timeout=15):
        deadline = time.monotonic() + timeout
        with self.condition:
            while self.sequence <= after:
                if self.ended:
                    raise RuntimeError("Camera stream stopped; check camera.log.")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("Camera produced no new frame; check camera.log.")
                self.condition.wait(remaining)
            return self.sequence, Image.frombytes("RGB", (self.width, self.height), self.latest)

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.thread.join(timeout=5)
        self.process.stdout.close()
        self.log.close()


def rectify(image, config):
    corners = config.get("corners")
    if not corners:
        return image.copy()
    # Corners: top-left, top-right, bottom-right, bottom-left, in source pixels.
    a, b, c, d = np.asarray(corners, dtype=float)
    width = round((np.linalg.norm(b - a) + np.linalg.norm(c - d)) / 2)
    height = round((np.linalg.norm(d - a) + np.linalg.norm(c - b)) / 2)
    if min(width, height) < 50:
        raise ValueError("Selected screen is too small.")
    rows, rhs = [], []
    for (x, y), (u, v) in zip([(0, 0), (width, 0), (width, height), (0, height)], corners):
        rows.extend([[x, y, 1, 0, 0, 0, -u*x, -u*y],
                     [0, 0, 0, x, y, 1, -v*x, -v*y]])
        rhs.extend([u, v])
    coefficients = np.linalg.solve(rows, rhs)
    return image.transform((width, height), Image.Transform.PERSPECTIVE,
                           coefficients, Image.Resampling.BICUBIC)


def sharpness(image):
    gray = np.asarray(image.convert("L"), dtype=np.float32)
    laplacian = (4*gray[1:-1, 1:-1] - gray[:-2, 1:-1] - gray[2:, 1:-1]
                 - gray[1:-1, :-2] - gray[1:-1, 2:])
    return float(laplacian.var())


def image_difference(left, right):
    def thumb(image):
        return np.asarray(image.convert("L").resize((192, 256)), dtype=float)
    return float(np.abs(thumb(left) - thumb(right)).mean())


def best_photo(camera, config):
    # Discard pre-settle frames; choose a sharp frame without blending text.
    time.sleep(config.get("settle_seconds", 1.5))
    sequence = camera.sequence
    choices = []
    for _ in range(3):
        sequence, original = camera.frame(after=sequence)
        page = rectify(original, config)
        choices.append((sharpness(page), original, page))
    return max(choices, key=lambda item: item[0])


class Phone:
    def __init__(self, serial=SERIAL, monitor_port=None):
        self.serial = serial
        self.monitor_port = monitor_port

    def adb(self, *args, timeout=25):
        result = subprocess.run([ADB, "-s", self.serial, *args], capture_output=True,
                                timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise RuntimeError(result.stderr.decode("utf-8", "replace").strip())
        return result.stdout.decode("utf-8", "replace")

    def foreground(self):
        state = self.adb("shell", "dumpsys window | grep mCurrentFocus")
        if PACKAGE + "/" not in state:
            raise RuntimeError("Readeat is not foreground. No page-turn input was sent.")

    def monitor_request(self, command):
        with socket.create_connection(("127.0.0.1", self.monitor_port), timeout=5) as connection:
            connection.sendall((command + "\n").encode("ascii"))
            return connection.makefile("r", encoding="utf-8").readline().strip()

    def page(self):
        if self.monitor_port:
            response = self.monitor_request("PAGE")
            match = re.fullmatch(r"(\d+)/(\d+)", response)
            if not match:
                raise RuntimeError("Counter monitor: " + response)
            page, total = map(int, match.groups())
            if not 1 <= page <= total:
                raise RuntimeError("Counter monitor returned an invalid page.")
            return page, total
        self.foreground()
        target = "/data/local/tmp/readeat-scan-ui.xml"
        result = self.adb("shell", "uiautomator", "dump", "--compressed", target)
        if "dumped to" not in result.lower():
            raise RuntimeError("Reader UI dump failed; no page number verified.")
        return parse_page_xml(self.adb("exec-out", "cat", target))

    def next_page(self):
        if self.monitor_port:
            response = self.monitor_request("NEXT")
            if response != "OK":
                raise RuntimeError("Page gesture monitor: " + response)
            return
        self.foreground()
        size = self.adb("shell", "wm", "size")
        dimensions = re.findall(r"(\d+)x(\d+)", size)
        if not dimensions:
            raise RuntimeError("Cannot read phone display size.")
        width, height = map(int, dimensions[-1])
        self.adb("shell", "input", "swipe", str(round(width*.8)), str(round(height*.55)),
                 str(round(width*.2)), str(round(height*.55)), "300")


def parse_page_xml(xml):
    root = ET.fromstring(xml)
    if not any(node.get("package") == PACKAGE for node in root.iter("node")):
        raise RuntimeError("UI is not Readeat.")
    candidates = set()
    for node in root.iter("node"):
        for attribute in ("text", "content-desc"):
            match = re.fullmatch(r"\s*(?:(?:Сторінка|Page)\s+)?(\d+)\s*/\s*(\d+)\s*",
                                 node.get(attribute, ""), flags=re.IGNORECASE)
            if match:
                page, total = map(int, match.groups())
                if 1 <= page <= total:
                    candidates.add((page, total))
    if len(candidates) != 1:
        raise RuntimeError("Page counter unavailable or ambiguous. Show reader controls and retry.")
    return candidates.pop()


def capture(config_path, count, start_page=None):
    config_path = Path(config_path).resolve()
    directory = config_path.parent
    config = load_json(config_path)
    if not config.get("corners"):
        raise ValueError("Calibrate screen corners before scanning.")
    if count < 1:
        raise ValueError("Capture count must be positive.")
    if (directory / "STOP").exists():
        raise RuntimeError("STOP file exists. Remove it when ready to resume.")
    manifest_path = directory / "capture.json"
    manifest = load_json(manifest_path) if manifest_path.exists() else {
        "version": 1, "serial": config.get("serial", SERIAL), "pages": [],
        "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest()}
    if manifest["config_sha256"] != hashlib.sha256(config_path.read_bytes()).hexdigest():
        raise RuntimeError("Calibration changed; start a separate capture folder.")
    phone = Phone(manifest["serial"], monitor_port=config.get("monitor_port"))
    current, total = phone.page()
    entries = manifest["pages"]
    if entries:
        if total != manifest["total"]:
            raise RuntimeError("Page count changed. Restore the original reading settings.")
        expected = entries[-1]["page"] + 1
        if current not in (expected - 1, expected):
            raise RuntimeError(f"Resume at page {expected-1} or {expected}; phone shows {current}.")
    else:
        expected = current if start_page is None else start_page
        if current != expected:
            raise RuntimeError(f"Expected starting page {expected}; phone shows {current}.")
        manifest["total"] = total
    if expected > total:
        print("All remaining pages already captured.")
        return
    last_image = Image.open(directory / entries[-1]["image"]).copy() if entries else None
    (directory / "pages").mkdir(exist_ok=True)
    (directory / "originals").mkdir(exist_ok=True)
    camera = Camera(config, directory / "camera.log")
    try:
        for page in range(expected, min(expected + count, total + 1)):
            if (directory / "STOP").exists():
                print("Stopped before next capture/turn.", flush=True)
                break
            current, checked_total = phone.page()
            if current == page - 1:
                phone.next_page()
                time.sleep(config.get("settle_seconds", 1.5))
                current, checked_total = phone.page()
                deadline = time.monotonic() + 5
                while (current, checked_total) == (page - 1, total) and time.monotonic() < deadline:
                    time.sleep(.25)
                    current, checked_total = phone.page()
            if (current, checked_total) != (page, total):
                raise RuntimeError(f"Expected {page}/{total}, phone shows {current}/{checked_total}. Stopped.")
            quality, original, image = best_photo(camera, config)
            # The optical image is acquired between matching UI page reads.
            after = phone.page()
            if after != (page, total):
                raise RuntimeError("Page changed during photo; stopped without saving it.")
            difference = image_difference(last_image, image) if last_image is not None else None
            if difference is not None and difference < config.get("minimum_difference", .6):
                image.save(directory / "review-possible-duplicate.png")
                raise RuntimeError("Photo resembles the previous page; stopped for review.")
            if quality < config.get("minimum_sharpness", 0):
                image.save(directory / "review-low-sharpness.png")
                raise RuntimeError("Photo is below calibrated sharpness; stopped for review.")
            name = f"{page:05d}"
            image_path = directory / "pages" / (name + ".png")
            raw_path = directory / "originals" / (name + ".jpg")
            if image_path.exists() or raw_path.exists():
                raise RuntimeError("Unlogged image exists; review it before resuming. Nothing overwritten.")
            original.save(raw_path, quality=98, subsampling=0)
            image.save(image_path)
            entries.append({"page": page, "image": f"pages/{name}.png",
                            "original": f"originals/{name}.jpg", "sharpness": quality,
                            "difference": difference, "size": list(image.size),
                            "sha256": hashlib.sha256(image_path.read_bytes()).hexdigest(),
                            "captured_utc": datetime.now(timezone.utc).isoformat()})
            write_json(manifest_path, manifest)
            last_image = image
            print(f"Saved {page}/{total}; sharpness {quality:.1f}; difference {difference}", flush=True)
    finally:
        camera.close()


def make_epub(directory, output, title, author, partial=False, jpeg_quality=None):
    directory, output = Path(directory), Path(output)
    manifest = load_json(directory / "capture.json")
    pages = manifest["pages"]
    numbers = [entry["page"] for entry in pages]
    if not numbers or numbers != list(range(numbers[0], numbers[-1] + 1)):
        raise ValueError("Captured pages are empty, duplicated, or have gaps.")
    if not partial and numbers != list(range(1, manifest["total"] + 1)):
        raise ValueError("Book is incomplete. --partial is available only for a labelled sample.")
    if output.exists():
        raise FileExistsError(output)
    if partial:
        title += f" (sample, pages {numbers[0]}–{numbers[-1]})"
    uid = "urn:uuid:" + str(uuid.uuid4())
    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    items, spine, navigation = [], [], []
    temp = output.with_suffix(output.suffix + ".tmp")
    if temp.exists():
        raise FileExistsError(temp)
    try:
        with ZipFile(temp, "x", compression=ZIP_DEFLATED) as book:
            book.writestr("mimetype", "application/epub+zip", compress_type=ZIP_STORED)
            book.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" '
                          'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                          '<rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml"/>'
                          '</rootfiles></container>')
            for entry in pages:
                number = entry["page"]
                source = directory / entry["image"]
                if hashlib.sha256(source.read_bytes()).hexdigest() != entry["sha256"]:
                    raise ValueError(f"Image checksum mismatch on page {number}.")
                name = f"p{number:05d}"
                extension, media_type = "png", "image/png"
                with Image.open(source) as image:
                    width, height = image.size
                    if jpeg_quality is None:
                        book.write(source, f"EPUB/images/{name}.png")
                    else:
                        if not 60 <= jpeg_quality <= 100:
                            raise ValueError("JPEG quality must be between 60 and 100.")
                        extension, media_type = "jpg", "image/jpeg"
                        encoded = io.BytesIO()
                        image.convert("RGB").save(encoded, format="JPEG", quality=jpeg_quality,
                                                  subsampling=0, optimize=True)
                        book.writestr(f"EPUB/images/{name}.jpg", encoded.getvalue(), compress_type=ZIP_STORED)
                book.writestr(f"EPUB/{name}.xhtml", f'''<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" lang="uk" xml:lang="uk"><head>
<title>Page {number}</title><meta name="viewport" content="width={width}, height={height}"/>
<style>html,body{{margin:0;padding:0;width:100%;height:100%}}img{{display:block;width:100%;height:100%}}</style>
</head><body><img src="images/{name}.{extension}" alt="Photographed book page {number}"/></body></html>''')
                items.extend([f'<item id="{name}" href="{name}.xhtml" media-type="application/xhtml+xml"/>',
                              f'<item id="i{number}" href="images/{name}.{extension}" media-type="{media_type}"/>'])
                spine.append(f'<itemref idref="{name}"/>')
                navigation.append(f'<li><a href="{name}.xhtml">{number}</a></li>')
            book.writestr("EPUB/nav.xhtml", f'''<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="uk" xml:lang="uk">
<head><title>Contents</title></head><body><nav epub:type="toc"><h1>Pages</h1><ol>
{''.join(navigation)}</ol></nav></body></html>''')
            book.writestr("EPUB/package.opf", f'''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id"
 prefix="rendition: http://www.idpf.org/vocab/rendition/#"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="book-id">{uid}</dc:identifier><dc:title>{escape(title)}</dc:title>
<dc:creator>{escape(author)}</dc:creator><dc:language>uk</dc:language>
<meta property="dcterms:modified">{modified}</meta><meta property="rendition:layout">pre-paginated</meta>
<meta property="rendition:spread">none</meta></metadata><manifest>
<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
{''.join(items)}</manifest><spine page-progression-direction="ltr">{''.join(spine)}</spine></package>''')
        os.replace(temp, output)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    print(f"Created {output} ({len(pages)} photographed pages).")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("devices", help="List camera names without recording.")
    options = commands.add_parser("options", help="List camera resolution/format modes.")
    options.add_argument("--camera", required=True)
    photo = commands.add_parser("photo", help="Take one framing photo; does not turn pages.")
    photo.add_argument("--camera", required=True)
    photo.add_argument("--size", default="1920x1080")
    photo.add_argument("--fps", type=float, default=15)
    photo.add_argument("--input-format", default="")
    photo.add_argument("--output", type=Path, required=True)
    calibration = commands.add_parser("calibrate", help="Set screen/text corners on the test photo.")
    calibration.add_argument("--config", type=Path, required=True)
    calibration.add_argument("--photo", type=Path, required=True)
    calibration.add_argument("--corners", required=True, help="TL,TR,BR,BL: x,y,x,y,x,y,x,y")
    scanning = commands.add_parser("capture", help="Capture a bounded number of verified pages.")
    scanning.add_argument("--config", type=Path, required=True)
    scanning.add_argument("--count", type=int, required=True)
    scanning.add_argument("--start-page", type=int)
    epub = commands.add_parser("epub", help="Package verified images as a fixed-layout EPUB.")
    epub.add_argument("--directory", type=Path, required=True)
    epub.add_argument("--output", type=Path, required=True)
    epub.add_argument("--title", required=True)
    epub.add_argument("--author", default="Пірс Браун")
    epub.add_argument("--partial", action="store_true")
    epub.add_argument("--jpeg-quality", type=int, choices=range(60, 101), default=90,
                      help="JPEG quality for compact reader copies (default 90).")
    epub.add_argument("--lossless-images", action="store_true", help="Embed original PNGs; larger file.")
    args = parser.parse_args()
    if args.command in ("devices", "options"):
        command = [FFMPEG, "-hide_banner", "-f", "dshow"]
        command += (["-list_devices", "true", "-i", "dummy"] if args.command == "devices"
                    else ["-list_options", "true", "-i", "video=" + args.camera])
        subprocess.run(command, timeout=20)
    elif args.command == "photo":
        width, height = map(int, args.size.split("x"))
        config = {"camera": args.camera, "width": width, "height": height,
                  "fps": args.fps, "input_format": args.input_format, "serial": SERIAL,
                  "settle_seconds": 1.5, "minimum_difference": .6}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        config_path = args.output.parent / "scanner.json"
        if args.output.exists() or config_path.exists():
            raise FileExistsError("Use a new photo directory to preserve earlier calibration.")
        camera = Camera(config, args.output.parent / "camera.log")
        try:
            camera.frame()
            time.sleep(5)  # Allow the C920's autofocus/exposure to settle after opening.
            quality, _, image = best_photo(camera, config)
            image.save(args.output)
            write_json(config_path, config)
            print(f"Saved {args.output}; sharpness {quality:.1f}.")
        finally:
            camera.close()
    elif args.command == "calibrate":
        config = load_json(args.config)
        coordinates = list(map(float, args.corners.split(",")))
        if len(coordinates) != 8 or not all(math.isfinite(x) for x in coordinates):
            raise ValueError("Supply eight finite coordinates, TL,TR,BR,BL.")
        config["corners"] = list(zip(coordinates[::2], coordinates[1::2]))
        if (args.config.parent / "capture.json").exists():
            raise RuntimeError("Cannot change calibration after capture; use a new folder.")
        with Image.open(args.photo) as image:
            if image.size != (config["width"], config["height"]):
                raise ValueError("Photo dimensions do not match camera config.")
            if any(not (0 <= x <= image.width and 0 <= y <= image.height)
                   for x, y in config["corners"]):
                raise ValueError("Screen corners must be inside the photo.")
            page = rectify(image, config)
            page.save(args.config.parent / "calibrated-preview.png")
            config["minimum_sharpness"] = round(sharpness(page) * .45, 2)
        write_json(args.config, config)
        print("Saved calibrated-preview.png and scanner.json. Inspect before scanning.")
    elif args.command == "capture":
        capture(args.config, args.count, args.start_page)
    elif args.command == "epub":
        make_epub(args.directory, args.output, args.title, args.author, args.partial,
                  jpeg_quality=None if args.lossless_images else args.jpeg_quality)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Stopped. Successfully saved pages remain in capture.json.")
