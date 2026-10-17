"""Package local OCR as real reflowable text, retaining maps and chapter art."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import time
import uuid
from datetime import datetime, timezone
from xml.sax.saxutils import escape
from zipfile import ZipFile, ZIP_DEFLATED, ZIP_STORED

import numpy as np
from PIL import Image

from book_scanner import load_json, write_json
from ocr_reflow import ROOT, SOURCE, OCR, VERSION, joining_parts, normalize_text

CSS = '''body{font-family:serif;line-height:1.35;margin:0 3%}
p{margin:0;text-indent:1.2em}h1,h2{text-align:center;text-indent:0;margin:1.6em 0 1em}
.front p{margin:.5em 0;text-indent:0}.center{text-align:center;text-indent:0}
.art{display:block;max-width:100%;height:auto;margin:1em auto}.ornament{max-width:60%}
.source-page{font-size:0;line-height:0}'''


def normalized_paragraphs(page_data, corrections):
    values = [dict(value) for value in page_data["paragraphs"]]
    reviewed_heading = corrections.get("_chapter_headings", {}).get(str(page_data["page"]))
    if reviewed_heading:
        if reviewed_heading.get("source_sha256", page_data["source_sha256"]) != page_data["source_sha256"]:
            raise RuntimeError("Reviewed chapter title belongs to another image.")
        values = [value for value in values if value["top"] >= 580]
        values.insert(0, {"text": f"{reviewed_heading['number']}. {reviewed_heading['title']}",
                          "heading": True, "first_indent": True, "top": 450, "bottom": 540,
                          "confidence": None})
    override = corrections.get("_page_overrides", {}).get(str(page_data["page"]))
    if override:
        if override["source_sha256"] != page_data["source_sha256"]:
            raise RuntimeError("Proofreading override belongs to another source image.")
        values = [dict(value) for value in override["paragraphs"]]
    for value in values:
        for old, new in corrections.get(str(page_data["page"]), {}).items():
            value["text"] = value["text"].replace(old, new)
    if page_data.get("illustration"):
        joined = []
        for value in values:
            if (joined and 580 <= value["top"] <= 720 and value["text"][:1].islower()
                    and not joined[-1]["heading"] and not re.search(r'[.!?…:»)]$', joined[-1]["text"])):
                left, gap, right = joining_parts(joined[-1]["text"], value["text"])
                joined[-1]["text"] = left + gap + right
                joined[-1]["bottom"] = value["bottom"]
            else:
                joined.append(value)
        values = joined
    result = []
    index = 0
    while index < len(values):
        value = values[index]
        # A chapter number and its title belong in one TOC entry.
        label = value["text"]
        if page_data.get("illustration") and len(label) <= 3:
            label = label.translate(str.maketrans("ОоІіlIЗзВ", "001111338"))
        if re.fullmatch(r"\d{1,3}", label) and index+1 < len(values) and values[index+1]["heading"]:
            following = values[index+1]
            value = dict(following, text=label + ". " + following["text"])
            index += 1
        if value["heading"]:
            while index+1 < len(values) and values[index+1]["heading"]:
                index += 1
                value["text"] += " — " + values[index]["text"]
        result.append(value)
        index += 1
    return result


def map_bounds(image):
    gray = np.asarray(image.convert("L"))
    rows = np.flatnonzero((gray < 145).mean(axis=1) > .15)
    if len(rows) < 40:
        return (0, 0, image.width, image.height)
    top, bottom = max(0, int(rows[0])-8), min(image.height, int(rows[-1])+9)
    columns = np.flatnonzero((gray[top:bottom] < 145).any(axis=0))
    return (max(0, int(columns[0])-5), top, min(image.width, int(columns[-1])+6), bottom)


def assemble(records, corrections):
    sections = [{"title": "Початок книги", "nodes": [], "front": True}]
    page_links = []
    assets = []
    last_paragraph = None
    joins = []
    for record in records:
        page = record["page"]
        section = sections[-1]
        anchor = f'<span id="page-{page}" class="source-page" epub:type="pagebreak" role="doc-pagebreak" aria-label="{page}"></span>'
        used_anchor = False

        def consume_anchor():
            nonlocal used_anchor
            if used_anchor:
                return ""
            used_anchor = True
            page_links.append((page, len(sections)-1))
            return anchor

        def illustration(kind, bounds=None):
            name = f"{kind}-{page:05d}.jpg"
            assets.append({"page": page, "name": name, "bounds": bounds, "kind": kind})
            classes = "art ornament" if kind == "chapter" else "art"
            return f'<img class="{classes}" src="images/{name}" alt="Ілюстрація зі сторінки {page}"/>'

        if record.get("map"):
            section["nodes"].append({"kind": "raw", "html": consume_anchor() + illustration("map")})
            last_paragraph = None
            continue
        ornament = record.get("illustration")
        paragraphs = normalized_paragraphs(record, corrections)
        part_page = any(value["text"].startswith("ЧАСТИНА ") for value in paragraphs)
        for number, paragraph in enumerate(paragraphs):
            text = normalize_text(paragraph["text"])
            if not text:
                continue
            heading = paragraph["heading"] and page >= 13 and (record.get("illustration") or part_page)
            if heading:
                section = {"title": text, "nodes": [], "front": False}
                sections.append(section)
                section["nodes"].append({"kind": "raw", "html": consume_anchor() + "<h1>" + escape(text) + "</h1>"})
                if ornament:
                    section["nodes"].append({"kind": "raw", "html": illustration("chapter", ornament)})
                    ornament = None
                last_paragraph = None
                continue
            if ornament:
                section["nodes"].append({"kind": "raw", "html": consume_anchor() + illustration("chapter", ornament)})
                ornament = None
                last_paragraph = None
            merge = (number == 0 and page >= 12 and not paragraph["first_indent"]
                     and last_paragraph is not None and last_paragraph["last_page"] == page-1
                     and not re.match(r"^[—–-]\s", text))
            if merge:
                left, gap, right = joining_parts(last_paragraph["text"], text)
                if left != last_paragraph["text"]:
                    # The only supported deletion is the dictionary-verified final hyphen.
                    last_paragraph["html"] = last_paragraph["html"][:-1]
                last_paragraph["html"] += consume_anchor() + escape(gap + right)
                last_paragraph["text"] = left + gap + right
                last_paragraph["last_page"] = page
                joins.append(page)
            else:
                class_name = ' class="center"' if page in (1, 4) else ""
                node = {"kind": "paragraph", "html": consume_anchor() + escape(text),
                        "text": text, "last_page": page, "class": class_name}
                section["nodes"].append(node)
                last_paragraph = node
        if not used_anchor:
            section["nodes"].append({"kind": "raw", "html": consume_anchor()})
            last_paragraph = None
    return sections, assets, page_links, joins


def build(output, sample_pages=None):
    output = Path(output)
    manifest = load_json(SOURCE / "capture.json")
    entries = manifest["pages"][:sample_pages] if sample_pages else manifest["pages"]
    if not sample_pages and len(entries) != manifest["total"]:
        raise RuntimeError("The optical scan is incomplete.")
    if [entry["page"] for entry in entries] != list(range(1, len(entries)+1)):
        raise RuntimeError("Source pages are missing or unordered.")
    correction_file = OCR / "corrections.json"
    corrections = load_json(correction_file) if correction_file.exists() else {}
    records = []
    for entry in entries:
        record = load_json(OCR / "pages" / f"{entry['page']:05d}.json")
        if record["version"] != VERSION or record["source_sha256"] != entry["sha256"]:
            raise RuntimeError(f"Stale OCR cache for page {entry['page']}.")
        if (not record.get("map") and not record["paragraphs"]
                and str(entry["page"]) not in corrections.get("_page_overrides", {})):
            raise RuntimeError(f"No OCR text on page {entry['page']}; inspect before packaging.")
        records.append(record)
    reviewed_heading_file = OCR / "chapter-headings-reviewed.json"
    if reviewed_heading_file.exists():
        corrections["_chapter_headings"] = load_json(reviewed_heading_file)
    sections, assets, page_links, joins = assemble(records, corrections)
    if [page for page, _ in page_links] != list(range(1, len(entries)+1)):
        raise RuntimeError("A source-page anchor was lost during paragraph assembly.")
    if output.exists():
        raise FileExistsError(output)
    title = "Золотий син. Червоне повстання. Книга 2"
    if sample_pages:
        title += f" (OCR sample, pages 1–{sample_pages})"
    uid = "urn:uuid:" + str(uuid.uuid4())
    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    items, spine, toc = [], [], []
    temp = output.with_suffix(output.suffix + ".tmp")
    try:
        with ZipFile(temp, "x", compression=ZIP_DEFLATED) as archive:
            archive.writestr("mimetype", "application/epub+zip", compress_type=ZIP_STORED)
            archive.writestr("META-INF/container.xml", '<?xml version="1.0"?><container version="1.0" '
                              'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                              '<rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml"/>'
                              '</rootfiles></container>')
            archive.writestr("EPUB/style.css", CSS)
            for index, section in enumerate(sections):
                name = f"section-{index:03d}.xhtml"
                body = "".join(("<p" + node["class"] + ">" + node["html"] + "</p>")
                               if node["kind"] == "paragraph" else node["html"] for node in section["nodes"])
                class_name = ' class="front"' if section["front"] else ""
                archive.writestr("EPUB/"+name, f'''<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="uk" xml:lang="uk">
<head><title>{escape(section['title'])}</title><link rel="stylesheet" type="text/css" href="style.css"/></head>
<body{class_name}>{body}</body></html>''')
                items.append(f'<item id="s{index}" href="{name}" media-type="application/xhtml+xml"/>')
                spine.append(f'<itemref idref="s{index}"/>')
                toc.append(f'<li><a href="{name}">{escape(section["title"])}</a></li>')
            for index, asset in enumerate(assets):
                with Image.open(SOURCE / f"pages/{asset['page']:05d}.png") as image:
                    bounds = asset["bounds"] if asset["bounds"] else map_bounds(image)
                    cropped = image.crop(bounds).convert("RGB")
                    buffer = io.BytesIO()
                    cropped.save(buffer, format="JPEG", quality=92, subsampling=0, optimize=True)
                archive.writestr("EPUB/images/"+asset["name"], buffer.getvalue(), compress_type=ZIP_STORED)
                items.append(f'<item id="i{index}" href="images/{asset["name"]}" media-type="image/jpeg"/>')
            page_navigation = "".join(f'<li><a href="section-{section:03d}.xhtml#page-{page}">{page}</a></li>'
                                      for page, section in page_links)
            archive.writestr("EPUB/nav.xhtml", f'''<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="uk" xml:lang="uk">
<head><title>Зміст</title></head><body><nav epub:type="toc"><h1>Зміст</h1><ol>{''.join(toc)}</ol></nav>
<nav epub:type="page-list" hidden="hidden"><h2>Сторінки оригіналу</h2><ol>{page_navigation}</ol></nav></body></html>''')
            archive.writestr("EPUB/package.opf", f'''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="book-id">{uid}</dc:identifier>
<dc:title>{escape(title)}</dc:title><dc:creator>Пірс Браун</dc:creator><dc:language>uk</dc:language>
<dc:description>Reflowable text recovered by local OCR from the purchased edition. Not fully proofread.</dc:description>
<meta property="dcterms:modified">{modified}</meta></metadata><manifest>
<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
<item id="style" href="style.css" media-type="text/css"/>{''.join(items)}</manifest>
<spine page-progression-direction="ltr">{''.join(spine)}</spine></package>''')
        temp.replace(output)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    report = {"output": str(output), "source_pages": len(entries), "sections": len(sections),
              "illustrations": len(assets), "cross_page_paragraph_joins": len(joins),
              "paragraphs": sum(node["kind"] == "paragraph" for s in sections for node in s["nodes"]),
              "characters": sum(len(node.get("text", "")) for s in sections for node in s["nodes"]),
              "unresolved_dropcaps": [r["page"] for r in records if (r.get("dropcap_repair") or {}).get("unresolved")],
              "low_confidence_pages": [r["page"] for r in records if r["mean_confidence"] is not None and r["mean_confidence"] < 80
                                       and str(r["page"]) not in corrections.get("_page_overrides", {})],
              "bytes": output.stat().st_size, "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
              "ocr_not_fully_proofread": True}
    write_json(output.with_suffix(".verification.json"), report)
    print(json.dumps(report, ensure_ascii=True), flush=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sample-pages", type=int)
    parser.add_argument("--ocr-dir", type=Path, help="Use a separate reviewed OCR copy.")
    arguments = parser.parse_args()
    if arguments.ocr_dir:
        OCR = arguments.ocr_dir.resolve()
    build(arguments.output, arguments.sample_pages)
