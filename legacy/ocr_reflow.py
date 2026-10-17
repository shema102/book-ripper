"""Local Ukrainian OCR and a reflowable EPUB from the verified optical scan."""
from __future__ import annotations

import argparse
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import statistics
import subprocess
import sys
import time
import uuid
from xml.sax.saxutils import escape
from zipfile import ZipFile, ZIP_STORED, ZIP_DEFLATED

import numpy as np
from PIL import Image, ImageOps

from book_scanner import load_json, write_json
from paragraph_layout import paragraph_starts, is_heading

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "scan-full"
OCR = ROOT / "ocr"
TESS = ROOT / "tools/tesseract-5.5.3/tesseract.exe"
MODELS = TESS.parent / "tessdata"
sys.path.insert(0, str(ROOT / "tools/ocr-python-deps"))
from wordfreq import zipf_frequency

VERSION = "ukr-best-gray1.5-trim8-dropcaps-v2"
MAP_PAGES = {5, 6, 7}  # Reviewed colour maps / caste diagrams in this edition.
CYRILLIC = r"А-Яа-яІіЇїЄєҐґ"


def normalize_text(value):
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"(?<!\S)\|(?=\s|$)", "І", value)
    value = re.sub(r"(?<=\s)[-–](?=\s)", "—", value)
    if value.startswith(("- ", "– ")):
        value = "— " + value[2:]
    return value


def joining_parts(left, right):
    """Undo only dictionary-supported line-wrap hyphens; keep real compounds."""
    before = re.search(r"([" + CYRILLIC + r"'’]+)-$", left)
    after = re.match(r"([а-яіїєґ][" + CYRILLIC + r"'’]*)", right)
    if before and after:
        combined = before.group(1) + after.group(1)
        compound = before.group(1) + "-" + after.group(1)
        if zipf_frequency(combined, "uk") >= max(1.5, zipf_frequency(compound, "uk") + .3):
            return left[:-1], "", right
        return left, "", right
    return left, " ", right


def join_lines(lines):
    result = ""
    for line in lines:
        text = normalize_text(line)
        if not result:
            result = text
        else:
            left, gap, right = joining_parts(result, text)
            result = left + gap + right
    return normalize_text(result)


def chapter_illustration(image):
    """Find the reviewed edition's dark circular chapter ornament above its title."""
    gray = np.asarray(image.convert("L"))
    height, width = gray.shape
    ceiling = min(390, height // 3)
    dark = gray[:ceiling] < 105
    center = dark[:, round(width*.3):round(width*.7)].mean(axis=1)
    outside = np.concatenate([dark[:, :round(width*.2)], dark[:, round(width*.8):]], axis=1).mean(axis=1)
    rows = np.flatnonzero((center > .42) & (outside < .025))
    if len(rows) < 45:
        return None
    groups = np.split(rows, np.where(np.diff(rows) > 8)[0] + 1)
    run = max(groups, key=len)
    if len(run) < 45:
        return None
    top, bottom = max(8, int(run[0])-55), min(ceiling, int(run[-1])+55)
    columns = np.flatnonzero(dark[top:bottom].any(axis=0))
    if not len(columns):
        return None
    left, right = max(0, int(columns[0])-5), min(width, int(columns[-1])+6)
    if right-left < width*.2 or right-left > width*.7:
        return None
    return [left, top, right, bottom]


def read_tsv(path):
    groups = OrderedDict()
    confidences = []
    uncertain = []
    for row in csv.DictReader(path.read_text(encoding="utf-8").splitlines(), delimiter="\t"):
        text = row.get("text", "").strip()
        if row["level"] != "5" or not text:
            continue
        confidence = float(row["conf"])
        key = tuple(int(row[k]) for k in ("block_num", "par_num", "line_num"))
        word = {"text": text, "confidence": confidence,
                **{k: int(row[k]) / 1.5 for k in ("left", "top", "width", "height")}}
        groups.setdefault(key, []).append(word)
        if confidence >= 0:
            confidences.append(confidence)
        if confidence < 60 and len(text) > 1:
            uncertain.append({"text": text, "confidence": round(confidence, 1)})
    lines = []
    for key, words in groups.items():
        left = min(word["left"] for word in words)
        right = max(word["left"] + word["width"] for word in words)
        top = min(word["top"] for word in words) + 8
        bottom = max(word["top"] + word["height"] for word in words) + 8
        lines.append({"group": list(key[:2]), "left": left, "right": right, "top": top,
                      "bottom": bottom, "text": normalize_text(" ".join(w["text"] for w in words)),
                      "confidence": statistics.mean(w["confidence"] for w in words), "words": words})
    return lines, confidences, uncertain


def recognize_snippet(image, page, label, mode):
    work = OCR / "work" / f"{page:05d}-{label}.png"
    output = OCR / "raw" / f"{page:05d}-{label}"
    image.save(work)
    try:
        result = subprocess.run([str(TESS), str(work), str(output), "--tessdata-dir", str(MODELS),
                                 "-l", "ukr", "--oem", "1", "--psm", str(mode), "txt"],
                                capture_output=True, timeout=30,
                                env=dict(__import__("os").environ, OMP_THREAD_LIMIT="1"),
                                creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise RuntimeError(result.stderr.decode("utf-8", "replace"))
        return output.with_suffix(".txt").read_text(encoding="utf-8").strip()
    finally:
        work.unlink(missing_ok=True)


def repair_dropcap(image, lines, page):
    """Locate the large glyph geometrically, then recognize it separately."""
    if len(lines) < 2:
        return None
    typical = statistics.median(line["bottom"]-line["top"] for line in lines)
    floor = 120 if page == 11 else 535
    candidates = [line for line in lines if line["top"] >= floor and len(line["text"]) > 15
                  and line["right"]-line["left"] > image.width*.45]
    if not candidates:
        return None
    first = candidates[0]
    y0 = max(0, int(first["top"])-15)
    y1 = min(image.height, int(first["top"])+105)
    gray = image.convert("L")
    mask = np.asarray(gray)[y0:y1, :min(155, image.width)] < 125
    seen = np.zeros(mask.shape, dtype=bool)
    components = []
    for yy, xx in zip(*np.nonzero(mask)):
        if seen[yy, xx]:
            continue
        stack = [(int(yy), int(xx))]; seen[yy, xx] = True
        points = []
        while stack:
            y, x = stack.pop(); points.append((x,y))
            for dy, dx in ((-1,0),(1,0),(0,-1),(0,1),(-1,-1),(-1,1),(1,-1),(1,1)):
                ny, nx = y+dy, x+dx
                if 0 <= ny < mask.shape[0] and 0 <= nx < mask.shape[1] and mask[ny,nx] and not seen[ny,nx]:
                    seen[ny,nx] = True; stack.append((ny,nx))
        xs, ys = zip(*points)
        box = [min(xs), min(ys)+y0, max(xs)+1, max(ys)+y0+1]
        height, width = box[3]-box[1], box[2]-box[0]
        if height >= max(35,typical*1.45) and 5 <= width <= 110 and box[0] < 80 and box[1] <= first["top"]+15:
            components.append((height, len(points), box))
    if not components:
        return None
    cap_box = max(components)[2]
    second_candidates = [line for line in lines if first["top"]+typical*.6 <= line["top"] <= first["top"]+typical*2.2
                         and len(line["text"]) > 5]
    if not second_candidates:
        return {"unresolved": True, "bounds": cap_box, "reason": "second line not found"}
    second = min(second_candidates, key=lambda line: line["top"])
    cap = ImageOps.expand(ImageOps.autocontrast(gray.crop(cap_box)),20,fill="white")
    cap = cap.resize((cap.width*3,cap.height*3),Image.Resampling.LANCZOS)
    letter = recognize_snippet(cap,page,"dropcap",10).strip().upper()
    letter = letter.translate(str.maketrans("ABCEHKMOPTX30618", "АВСЕНКМОРТХЗОБІВ"))
    if not re.fullmatch("[А-ЯІЇЄҐ]",letter):
        return {"unresolved": True,"recognized":letter,"bounds":cap_box}
    box = (cap_box[2]+2,max(0,int(first["top"])-4),gray.width-10,min(gray.height,int(second["bottom"])+1))
    pair = ImageOps.expand(ImageOps.autocontrast(gray.crop(box),cutoff=.5),12,fill="white")
    pair = pair.resize((pair.width*2,pair.height*2),Image.Resampling.LANCZOS)
    texts = [normalize_text(t) for t in recognize_snippet(pair,page,"drop-lines",6).splitlines() if t.strip()]
    if len(texts) != 2 or not texts[0][:1].isalpha():
        return {"unresolved": True,"recognized":letter,"line_count":len(texts),"bounds":cap_box}
    tail = texts[0][0].lower()+texts[0][1:]
    word = re.match(r"[А-Яа-яІіЇїЄєҐґ'’]+",tail)
    separate = False
    if word and letter in "ЯІАУВО":
        separate = zipf_frequency(word.group(),'uk') >= 2 and zipf_frequency(letter+word.group(),'uk') < 1
    original = [first["text"],second["text"]]
    first["text"] = letter + (" " if separate else "") + tail
    second["text"] = texts[1]
    if second["group"] == first["group"]:
        second["continuation"] = True
    # Remove a duplicate standalone letter that the page segmenter placed elsewhere.
    for line in list(lines):
        if line is first or line is second:
            continue
        if (len(line["text"].strip()) <= 2 and line["left"] < cap_box[2]
                and line["top"] < cap_box[3] and line["bottom"] > cap_box[1]):
            lines.remove(line)
    return {"letter":letter,"bounds":cap_box,"before":original,
            "after":[first["text"],second["text"]],"standalone_word":separate}


def paragraphs_from_lines(lines, width):
    if not lines:
        return []
    starts, metrics = paragraph_starts(lines, width)
    typical_height, baseline = metrics['height'], metrics['margin']
    paragraphs = []
    current = []
    for line, boundary in zip(lines, starts):
        if current and boundary:
            paragraphs.append(current)
            current = []
        current.append(line)
    if current:
        paragraphs.append(current)
    result = []
    for group in paragraphs:
        text = join_lines([line["text"] for line in group])
        heading = is_heading(text)
        first = group[0]
        result.append({"text": text, "heading": heading,
                       "first_indent": first["left"] > baseline + typical_height*.65,
                       "top": first["top"], "bottom": group[-1]["bottom"],
                       "confidence": statistics.mean(line["confidence"] for line in group)})
    return result


def recognize(entry, force=False):
    page = entry["page"]
    target = OCR / "pages" / f"{page:05d}.json"
    if target.exists():
        cached = load_json(target)
        if (cached.get("source_sha256") == entry["sha256"] and cached.get("version") == VERSION
                and (cached.get("map") or cached.get("paragraphs")) and not force):
            return cached
    source = SOURCE / entry["image"]
    if hashlib.sha256(source.read_bytes()).hexdigest() != entry["sha256"]:
        raise RuntimeError(f"Source checksum mismatch on page {page}.")
    result = {"page": page, "version": VERSION, "source_sha256": entry["sha256"],
              "paragraphs": [], "illustration": None, "map": page in MAP_PAGES,
              "mean_confidence": None, "uncertain_words": []}
    if page in MAP_PAGES:
        write_json(target, result)
        return result
    with Image.open(source) as original:
        image = original.convert("RGB")
    ornament = chapter_illustration(image) if page >= 13 else None
    gray = image.convert("L")
    if ornament:
        result["illustration"] = ornament
        # Avoid recognizing lines/shapes in decorative art as letters.
        gray.paste(255, (0, max(0, ornament[1]-3), gray.width, ornament[3]+4))
    gray = ImageOps.autocontrast(gray.crop((0, 8, gray.width, gray.height-8)), cutoff=.5)
    gray = gray.resize((round(gray.width*1.5), round(gray.height*1.5)), Image.Resampling.LANCZOS)
    work = OCR / "work" / f"{page:05d}.png"
    gray.save(work)
    output = OCR / "raw" / f"{page:05d}"
    language = "ukr+eng" if page in (2, 13) else "ukr"
    environment = dict(__import__("os").environ, OMP_THREAD_LIMIT="1")
    try:
        process = subprocess.run([str(TESS), str(work), str(output), "--tessdata-dir", str(MODELS),
                                  "-l", language, "--oem", "1", "--psm", "3", "txt", "tsv"],
                                 capture_output=True, timeout=90, env=environment,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        if process.returncode:
            raise RuntimeError(process.stderr.decode("utf-8", "replace"))
        lines, confidences, uncertain = read_tsv(output.with_suffix(".tsv"))
        if not lines:
            sparse = OCR / "raw" / f"{page:05d}-sparse"
            retry = subprocess.run([str(TESS), str(work), str(sparse), "--tessdata-dir", str(MODELS),
                                    "-l", language, "--oem", "1", "--psm", "11", "txt", "tsv"],
                                   capture_output=True, timeout=90, env=environment,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
            if retry.returncode:
                raise RuntimeError(retry.stderr.decode("utf-8", "replace"))
            lines, confidences, uncertain = read_tsv(sparse.with_suffix(".tsv"))
            result["recognition_mode"] = "sparse-text fallback"
        if ornament or page == 11:
            result["dropcap_repair"] = repair_dropcap(image, lines, page)
            result["dropcap_refinement"] = 3
        result["paragraphs"] = paragraphs_from_lines(lines, image.width)
        result["mean_confidence"] = round(statistics.mean(confidences), 2) if confidences else 0
        result["uncertain_words"] = uncertain
        result["word_count"] = len(confidences)
        result["lines"] = lines
        write_json(target, result)
        return result
    finally:
        work.unlink(missing_ok=True)


def run_ocr(watch=False, selected=None, force=False):
    for name in ("pages", "raw", "work"):
        (OCR / name).mkdir(parents=True, exist_ok=True)
    pending, submitted, done = {}, set(), set()
    previous_count = -1
    last_capture_progress = time.monotonic()
    with ThreadPoolExecutor(max_workers=2) as workers:
        while True:
            manifest = load_json(SOURCE / "capture.json")
            entries = [entry for entry in manifest["pages"] if selected is None or entry["page"] in selected]
            if len(manifest["pages"]) != previous_count:
                previous_count = len(manifest["pages"])
                last_capture_progress = time.monotonic()
            for entry in entries:
                if len(pending) >= 4:
                    break
                if entry["page"] not in submitted:
                    pending[workers.submit(recognize, entry, force)] = entry["page"]
                    submitted.add(entry["page"])
            if pending:
                finished, _ = wait(pending, timeout=1, return_when=FIRST_COMPLETED)
                for task in finished:
                    page = pending.pop(task)
                    value = task.result()
                    done.add(page)
                    if len(done) % 25 == 0 or selected is not None:
                        print(f"OCR {len(done)} pages; latest {page}; confidence {value['mean_confidence']}", flush=True)
            else:
                time.sleep(2)
            all_available = len(done) == len(entries)
            if all_available and (not watch or len(entries) == manifest["total"]):
                print(f"OCR finished: {len(done)} pages.", flush=True)
                return
            if watch and time.monotonic()-last_capture_progress > 240 and all_available:
                raise RuntimeError("Capture made no progress for four minutes; OCR cache preserved.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--pages", help="Comma-separated sample page numbers.")
    parser.add_argument("--refine-art", action="store_true")
    arguments = parser.parse_args()
    selected = {int(x) for x in arguments.pages.split(",")} if arguments.pages else None
    if arguments.refine_art:
        selected = set()
        for path in (OCR / "pages").glob("*.json"):
            record = load_json(path)
            if (record.get("illustration") or record["page"] == 11) and record.get("dropcap_refinement") != 3:
                selected.add(record["page"])
                backup = path.with_suffix(".before-dropcap-refinement.bak")
                if not backup.exists():
                    write_json(backup, record)
        print("Refining",len(selected),"chapter openings.",flush=True)
    run_ocr(arguments.watch, selected, arguments.refine_art)
