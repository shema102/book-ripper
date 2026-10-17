"""Optical page counter: Tesseract reads the calibrated page-indicator region of a camera frame.
Capture-side only; it shares nothing with book-generation OCR settings or caches."""
from __future__ import annotations
import io
import os
import re
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
from PIL import Image, ImageOps
from .common import run, tesseract
from .imaging import REGIONS, rectify

# Upscaled line heights: tiny camera-pixel counters read best around Tesseract's preferred glyph size.
# Both are read and must agree, so a misread digit becomes "unreadable" rather than a wrong page.
HEIGHTS = (96, 128)


def flatten(gray, size):
    """Divide by the local background, a size×size closing (max then min: removes strokes, keeps smooth ramps),
    so lighting bands and screen glare become white."""
    def window(values, reduce):
        padded = np.pad(values, size // 2, mode="edge")
        return reduce(sliding_window_view(reduce(sliding_window_view(padded, size, axis=0), -1), size, axis=1), -1)
    values = np.asarray(gray, float)
    background = window(window(values, np.max), np.min)
    return np.clip(values / np.maximum(background, 1) * 255, 0, 255).astype(np.uint8)


def otsu(values):
    histogram = np.bincount(values.ravel(), minlength=256) / values.size
    weight, mean = np.cumsum(histogram), np.cumsum(histogram * np.arange(256))
    return int(np.argmax((mean[-1]*weight - mean)**2 / np.maximum(weight*(1-weight), 1e-12)))


def prepare(crop, height=HEIGHTS[0]):
    """Dark-on-white binary line. Tesseract's own global threshold fails on uneven, banded screen photos."""
    gray = crop.convert("L")
    if np.asarray(gray).mean() < 128:
        gray = ImageOps.invert(gray)  # light text on a dark reader theme
    gray = gray.resize((max(1, round(gray.width * height / gray.height)), height), Image.Resampling.LANCZOS)
    # Kernel ~0.2 line heights: wider than strokes, narrow enough to follow banding (wider kernels failed on real photos).
    flat = flatten(gray, int(height * .2) | 1)
    # Ink is at least ~12% darker than its background; the cap keeps Otsu from splitting a near-white background.
    binary = Image.fromarray(np.where(flat > min(otsu(flat), 224), 255, 0).astype(np.uint8))
    return ImageOps.expand(binary, border=height // 3, fill=255)


# What OCR commonly reads a label letter as at a few pixels high: digits, Cyrillic/Latin twins, symbols.
LOOKALIKE = {"a": "аα4@", "b": "68ьв", "c": "с(", "d": "0", "e": "е6сc", "g": "9qд6", "h": "нb", "i": "1l|!іїj",
             "k": "к", "l": "1iI|і", "m": "мrn", "n": "пhr", "o": "0оQ", "p": "рþ", "q": "9g", "r": "гn", "s": "5$ѕ",
             "t": "7т+f", "u": "иvц", "x": "хж", "y": "уv", "z": "2", "а": "a4", "в": "b8", "е": "e6", "і": "i1l",
             "к": "k", "н": "h", "о": "o0", "р": "p", "с": "c", "т": "t7", "у": "y", "х": "x"}


def compile_format(template):
    """Regex for an indicator format such as 'Page {page}/{total}' or '{page} of {total}'.

    Only the numbers are read. Label words match their OCR look-alikes, so a misread letter
    glued to the number ('Pag610/100') is eaten as label, not read as a digit. A label may also be
    missing entirely, but then the page number must not be glued to other letters or digits.
    """
    parts = re.split(r"(\{page\}|\{total\})", template)
    if sorted(parts[1::2]) != ["{page}", "{total}"]:
        raise ValueError("Indicator format needs {page} and {total} once each, e.g. Page {page}/{total}.")
    def literal(text):
        out = []
        for char in text:
            if char.isspace(): out.append(r"\s*")
            elif char.isalpha():
                similar = char + LOOKALIKE.get(char.lower(), "")
                out.append("[" + re.escape("".join(dict.fromkeys(similar.lower() + similar.upper()))) + "]")
            elif char in "/|\\": out.append(r"\s*[/|\\]\s*")
            else: out.append(r"\s*" + re.escape(char) + r"\s*")
        return "".join(out)
    lead, first, middle, second, tail = parts
    unglued = r"(?<![^\W_])"
    pattern = (f"(?:{literal(lead.strip())}\\s*|{unglued})" if lead.strip() else unglued)
    pattern += f"(?P<{first[1:-1]}>\\d+){literal(middle) or r'\s+'}(?P<{second[1:-1]}>\\d+)(?!\\d)"
    if tail.strip():
        pattern += f"(?:\\s*{literal(tail.strip())})?"
    return re.compile(pattern)


def whitelist(template):
    """Characters Tesseract may output: digits plus the format's own label characters in both cases."""
    literal = re.sub(r"\{page\}|\{total\}", "", template)
    return "".join(dict.fromkeys("0123456789" + "".join(char.lower()+char.upper() for char in literal if not char.isspace())))


def parse_indicator(text, template):
    """(page, total) from exactly one distinct format match in the OCR line, else (None, None)."""
    found = {(int(match["page"]), int(match["total"])) for match in compile_format(template).finditer(" ".join(text.split()))}
    found = {(page, total) for page, total in found if 1 <= page <= total}
    return found.pop() if len(found) == 1 else (None, None)


def read_indicator(frame, config):
    """OCR the page indicator in one camera frame: {'text', 'page', 'total'} (None when unreadable)."""
    crop = rectify(frame, config["indicator_corners"], REGIONS["indicator_corners"][1])
    command, tessdata = tesseract(config)
    template = config["indicator_format"]
    arguments = [command, "stdin", "stdout", "-l", config["indicator_language"], "--oem", "1", "--psm", "7",
                 "-c", "tessedit_char_whitelist=" + whitelist(template)]
    if tessdata:
        arguments += ["--tessdata-dir", tessdata]
    texts, readings = [], set()
    for height in HEIGHTS:
        buffer = io.BytesIO()
        prepare(crop, height).save(buffer, "PNG")
        raw = run(arguments, timeout=30, input=buffer.getvalue(), env=dict(os.environ, OMP_THREAD_LIMIT="1"))
        texts.append(" ".join(raw.decode("utf-8", "replace").split()))
        readings.add(parse_indicator(texts[-1], template))
    page, total = readings.pop() if len(readings) == 1 else (None, None)
    return {"text": " | ".join(dict.fromkeys(texts)), "page": page, "total": total}
