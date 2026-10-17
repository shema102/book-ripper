"""Copy the Tesseract runtime out of an existing Windows install into opticalbook/bundled/tesseract.

Only tesseract.exe, the DLLs it actually imports (found by reading PE import tables),
the chosen language models, Tesseract's configs and its licence notices are copied;
training tools and their GUI/text-rendering dependencies are left behind. Nothing is downloaded.
"""
from __future__ import annotations
from pathlib import Path
import shutil
import struct
from .common import BUNDLED_TESSERACT, digest, run, write_json


def imports(path):
    """DLL names in a PE file's import directory."""
    data = Path(path).read_bytes()
    header = struct.unpack_from("<I", data, 0x3C)[0]
    if data[header:header+4] != b"PE\0\0":
        raise ValueError(f"{Path(path).name} is not a Windows executable.")
    count, optional_size = struct.unpack_from("<H", data, header+6)[0], struct.unpack_from("<H", data, header+20)[0]
    optional = header + 24
    directories = optional + (112 if struct.unpack_from("<H", data, optional)[0] == 0x20B else 96)
    table = struct.unpack_from("<I", data, directories+8)[0]
    sections = [struct.unpack_from("<IIII", data, optional+optional_size+40*index+8) for index in range(count)]
    def offset(address):
        for size, virtual, raw_size, raw in sections:
            if virtual <= address < virtual + max(size, raw_size):
                return address - virtual + raw
        raise ValueError("Import table points outside the file.")
    names, position = [], offset(table) if table else None
    while position is not None:
        entry = struct.unpack_from("<5I", data, position)
        if not any(entry):
            break
        start = offset(entry[3])
        names.append(data[start:data.index(b"\0", start)].decode("ascii"))
        position += 20
    return names


def runtime(source):
    """tesseract.exe plus every DLL in `source` it needs, directly or through other DLLs."""
    source = Path(source)
    local = {path.name.lower(): path for path in source.glob("*.dll")}
    found, pending = {}, [source / "tesseract.exe"]
    while pending:
        path = pending.pop()
        if path.name.lower() in found:
            continue
        found[path.name.lower()] = path
        pending += [local[name.lower()] for name in imports(path) if name.lower() in local]
    return list(found.values())


def bundle_tesseract(source, languages=("eng",), replace=False, target=BUNDLED_TESSERACT):
    source, target = Path(source).resolve(), Path(target)
    if not (source / "tesseract.exe").is_file():
        raise FileNotFoundError("Choose the folder that contains tesseract.exe (for example C:\\Program Files\\Tesseract-OCR).")
    models = source / "tessdata"
    missing = [language for language in languages if not (models / f"{language}.traineddata").is_file()]
    if missing:
        raise FileNotFoundError("Missing models in the source tessdata: " + ", ".join(missing))
    if target.exists():
        if not replace:
            raise FileExistsError(f"{target} already exists. Pass --replace to rebuild it.")
        shutil.rmtree(target)
    temp = target.with_name(target.name + ".partial")
    shutil.rmtree(temp, ignore_errors=True)
    (temp / "tessdata").mkdir(parents=True)
    for path in runtime(source):
        shutil.copy2(path, temp / path.name)
    for language in languages:
        shutil.copy2(models / f"{language}.traineddata", temp / "tessdata")
    for folder in ("configs", "tessconfigs"):
        if (models / folder).is_dir():
            shutil.copytree(models / folder, temp / "tessdata" / folder)
    notices = [path for path in (source / "doc").glob("*") if path.is_file()] if (source / "doc").is_dir() else []
    for path in notices:
        shutil.copy2(path, temp / path.name)
    version = run([temp / "tesseract.exe", "--version"]).decode("utf-8", "replace").splitlines()[0]
    files = sorted(path for path in temp.rglob("*") if path.is_file())
    write_json(temp / "BUNDLE.json", {"source": str(source), "version": version, "languages": list(languages),
                                     "files": {path.relative_to(temp).as_posix(): digest(path) for path in files}})
    temp.replace(target)
    return version, len(files), sum(path.stat().st_size for path in target.rglob("*") if path.is_file())
