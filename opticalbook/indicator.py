"""Optical page counter: Tesseract reads the calibrated page-indicator region of a camera frame.
Capture-side only; it shares nothing with book-generation OCR settings or caches."""
from __future__ import annotations
import io
import os
import re
import numpy as np
from PIL import Image, ImageOps
from .common import run, tesseract
from .imaging import REGIONS, rectify

# Upscaled line height: tiny camera-pixel counters read best around Tesseract's preferred glyph size.
HEIGHT = 96


def prepare(crop):
    gray = ImageOps.autocontrast(crop.convert("L"), cutoff=1)
    if np.asarray(gray).mean() < 128:
        gray = ImageOps.invert(gray)  # light text on a dark reader theme
    width = max(1, round(gray.width * HEIGHT / gray.height))
    return ImageOps.expand(gray.resize((width, HEIGHT), Image.Resampling.LANCZOS), border=HEIGHT // 3, fill=255)


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
    buffer = io.BytesIO()
    prepare(crop).save(buffer, "PNG")
    command, tessdata = tesseract(config)
    template = config["indicator_format"]
    arguments = [command, "stdin", "stdout", "-l", config["indicator_language"], "--oem", "1", "--psm", "7",
                 "-c", "tessedit_char_whitelist=" + whitelist(template)]
    if tessdata:
        arguments += ["--tessdata-dir", tessdata]
    raw = run(arguments, timeout=30, input=buffer.getvalue(), env=dict(os.environ, OMP_THREAD_LIMIT="1"))
    text = " ".join(raw.decode("utf-8", "replace").split())
    page, total = parse_indicator(text, template)
    return {"text": text, "page": page, "total": total}
