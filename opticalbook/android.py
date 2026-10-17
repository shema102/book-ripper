"""ADB only reads visible UI and sends configured taps/swipes; no app internals."""
from __future__ import annotations
import re
import uuid
from xml.etree import ElementTree as ET
from .common import executable, run


def devices(adb):
    text = run([executable(adb), "devices"]).decode("utf-8", "replace")
    return [line.split()[0] for line in text.splitlines()[1:]
            if len(line.split()) >= 2 and line.split()[1] == "device"]


def parse_counter(xml, package, pattern):
    root = ET.fromstring(xml)
    regex = re.compile(pattern)
    if regex.groups != 2:
        raise ValueError("Counter regex must have exactly two capture groups: page and total.")
    matches = set()
    for node in root.iter("node"):
        if node.get("package") != package:
            continue
        for key in ("text", "content-desc"):
            match = regex.fullmatch(node.get(key, ""))
            if match:
                page, total = map(int, match.groups())
                if 1 <= page <= total:
                    matches.add((page, total))
    if len(matches) != 1:
        raise RuntimeError("Page counter unavailable or ambiguous. Show the reader counter, or adjust its regex.")
    return matches.pop()


PACKAGE = r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+"


def focused(state):
    """Package of the focused window in `dumpsys window` output; None if absent or ambiguous.
    Android 16 prints mCurrentFocus only there, no longer under `dumpsys window windows`."""
    found = set(re.findall(r"mCurrentFocus=Window\{\S+ u\d+ (" + PACKAGE + r")/", state))
    return found.pop() if len(found) == 1 else None


class Phone:
    def __init__(self, config):
        self.config = config
        self.executable = executable(config["adb"])
        if not config["serial"]:
            raise ValueError("Choose an explicit ADB device serial.")
        # Optional pin in project.json; otherwise the first focused app is locked in as the reader.
        self.package = config.get("package") or None
        if self.package and not re.fullmatch(PACKAGE, self.package):
            raise ValueError("Reader package must look like com.example.reader, or be left empty.")

    def adb(self, *args):
        return run([self.executable, "-s", self.config["serial"], *args], timeout=30).decode("utf-8", "replace")

    def foreground(self):
        """Refuse input unless the focused app is still the reader seen first (or the pinned package)."""
        package = focused(self.adb("shell", "dumpsys", "window"))
        if not package:
            raise RuntimeError("Cannot tell which app is in the foreground (popup, lock screen or transition). No gesture was sent.")
        if self.package is None:
            self.package = package
        elif package != self.package:
            raise RuntimeError(f"{package} is in the foreground instead of the reader {self.package}. No gesture was sent.")

    def page(self):
        self.foreground()
        target = "/data/local/tmp/opticalbook-" + uuid.uuid4().hex + ".xml"
        try:
            status = self.adb("shell", "uiautomator", "dump", "--compressed", target)
            if "dumped to" not in status.lower():
                raise RuntimeError("Fresh UI dump failed; no old page counter will be reused.")
            xml = self.adb("exec-out", "cat", target)
            return parse_counter(xml, self.package,self.config["counter_regex"])
        finally:
            try:
                self.adb("shell", "rm", "-f", target)
            except Exception:
                pass

    def turn(self):
        self.foreground()
        dimensions = re.findall(r"(\d+)x(\d+)", self.adb("shell", "wm", "size"))
        if not dimensions:
            raise RuntimeError("Cannot read Android display size.")
        width, height = map(int, dimensions[-1])
        gesture = self.config["gesture"]
        def point(value):
            if len(value) != 2 or any(not 0 <= float(x) <= 1 for x in value):
                raise ValueError("Gesture coordinates must be fractions between 0 and 1.")
            return [str(round(float(x) * (size-1))) for x, size in zip(value, (width, height))]
        if gesture["kind"] == "tap":
            self.adb("shell", "input", "tap", *point(gesture["end"]))
        elif gesture["kind"] == "swipe":
            duration = int(gesture["duration_ms"])
            if not 1 <= duration <= 5000:
                raise ValueError("Swipe duration must be 1–5000 milliseconds.")
            self.adb("shell", "input", "swipe", *point(gesture["start"]), *point(gesture["end"]), str(duration))
        else:
            raise ValueError("Gesture must be tap or swipe.")
