from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid
from functools import wraps


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() if hasattr(hashlib, "file_digest") else hashlib.sha256(stream.read()).hexdigest()


def fingerprint(value):
    def canonical(item):
        if isinstance(item, dict): return {key: canonical(val) for key, val in item.items()}
        if isinstance(item, (list, tuple)): return [canonical(val) for val in item]
        if isinstance(item, float) and item.is_integer(): return int(item)
        return item
    return hashlib.sha256(json.dumps(canonical(value), sort_keys=True, ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def locked(part):
    """Prevent two processes from mutating the same stage's files at once."""
    def decorate(function):
        @wraps(function)
        def wrapper(folder, *args, **kwargs):
            root = Path(folder)
            root.mkdir(parents=True, exist_ok=True)
            path = root / ('.' + part + '.lock')
            try:
                stream = path.open('x', encoding='utf-8')
            except FileExistsError as error:
                raise RuntimeError(f'{part} is already locked. If a previous process crashed, verify it has exited before removing {path.name}.') from error
            try:
                with stream:
                    json.dump({'pid': os.getpid(), 'stage': part}, stream)
                return function(folder, *args, **kwargs)
            finally:
                path.unlink(missing_ok=True)
        return wrapper
    return decorate


def inside(root, relative):
    root = Path(root).resolve()
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise ValueError("Manifest path escapes its project folder.")
    return target


def executable(value):
    found = shutil.which(str(value))
    if found:
        return found
    raise FileNotFoundError(f"Executable not found: {value}. Set its path in the UI or project.json.")


# Optional local copy made by `python -m opticalbook bundle-tesseract`; never downloaded.
BUNDLED_TESSERACT = Path(__file__).resolve().parent / "bundled" / "tesseract"


def bundled_tesseract():
    path = BUNDLED_TESSERACT / ("tesseract.exe" if os.name == "nt" else "tesseract")
    return path if path.is_file() else None


def tesseract(config):
    """(executable, tessdata dir or '') for a book or capture config section.
    The default name 'tesseract' prefers the bundled copy (known version and models), then PATH."""
    name, tessdata = str(config["tesseract"]).strip(), config.get("tessdata") or ""
    bundled = bundled_tesseract()
    if name in ("", "tesseract") and bundled:
        return str(bundled), tessdata or str(BUNDLED_TESSERACT / "tessdata")
    return executable(name or "tesseract"), tessdata


def run(arguments, timeout=60, **kwargs):
    options = {"capture_output": True, "timeout": timeout}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    options.update(kwargs)
    result = subprocess.run([str(x) for x in arguments], **options)
    if result.returncode:
        message = result.stderr.decode("utf-8", "replace") if isinstance(result.stderr, bytes) else result.stderr
        raise RuntimeError(f"{Path(str(arguments[0])).name}: {message.strip() or 'exit ' + str(result.returncode)}")
    return result.stdout


def defaults():
    return {
        "schema_version": 1,
        "capture": {
            "camera": {"source": "0", "backend": "auto", "width": 1920, "height": 1080,
                       "fps": 30, "fourcc": "MJPG", "autofocus": True,
                       "focus": None, "exposure": None, "auto_exposure": None, "brightness": None,
                       "auto_white_balance": None, "white_balance": None},
            "corners": [], "screen_corners": [], "indicator_corners": [], "preview_rotation": 0, "settle_seconds": 1.0, "minimum_sharpness": 0.0,
            "minimum_difference": .6, "counter_mode": "verified", "total": 1,
            "adb": "adb", "serial": "", "package": "",
            "tesseract": "tesseract", "tessdata": "", "indicator_language": "eng", "indicator_format": "{page}/{total}",
            "counter_regex": r"(?i)\s*(?:(?:Page|Сторінка)\s+)?(\d+)\s*/\s*(\d+)\s*",
            "gesture": {"kind": "swipe", "start": [.8, .55], "end": [.2, .55], "duration_ms": 300}},
        "book": {"title": "Untitled book", "author": "", "language": "uk",
                 "tesseract": "tesseract", "tessdata": "", "ocr_language": "ukr+eng",
                 "psm": 3, "scale": 1.5, "dictionary_hyphens": False}
    }


def load_project(folder):
    path = Path(folder) / "project.json"
    config = defaults()
    if path.exists():
        saved = read_json(path)
        if saved.get("schema_version") != 1:
            raise ValueError("Unsupported project schema version.")
        def merge(base, override):
            for key, value in override.items():
                if isinstance(value, dict) and isinstance(base.get(key), dict):
                    merge(base[key], value)
                else:
                    base[key] = value
        merge(config, saved)
    return config
