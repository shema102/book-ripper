"""Wait for a complete optical scan, verify every image, package and validate it."""
import hashlib
import json
from pathlib import Path
import subprocess
import time

from PIL import Image
from book_scanner import load_json, make_epub, write_json

ROOT = Path(__file__).resolve().parent
DIRECTORY = ROOT / "scan-full"
OUTPUT = ROOT.parent / "Zolotyi-syn.epub"


def main():
    previous = -1
    last_progress = time.monotonic()
    while True:
        manifest = load_json(DIRECTORY / "capture.json")
        count = len(manifest["pages"])
        if count != previous:
            previous = count
            last_progress = time.monotonic()
            if count % 25 == 0 or count == manifest["total"]:
                print(f"Awaiting completion: {count}/{manifest['total']}", flush=True)
        if count == manifest["total"]:
            break
        if time.monotonic() - last_progress > 180:
            raise RuntimeError("Capture has made no progress for three minutes; review before finalizing.")
        time.sleep(5)
    entries = manifest["pages"]
    if [entry["page"] for entry in entries] != list(range(1, manifest["total"] + 1)):
        raise RuntimeError("Missing or unordered pages; not packaging.")
    for entry in entries:
        source = DIRECTORY / entry["image"]
        if hashlib.sha256(source.read_bytes()).hexdigest() != entry["sha256"]:
            raise RuntimeError(f"Checksum mismatch on page {entry['page']}")
        with Image.open(source) as image:
            image.verify()
        with Image.open(source) as image:
            if list(image.size) != entry["size"]:
                raise RuntimeError(f"Dimensions changed on page {entry['page']}")
    quality = 90
    make_epub(DIRECTORY, OUTPUT, "Золотий син. Червоне повстання. Книга 2", "Пірс Браун",
              jpeg_quality=quality)
    if OUTPUT.stat().st_size > 195_000_000:
        retained = OUTPUT.with_name("Zolotyi-syn-quality90.epub")
        if retained.exists():
            raise FileExistsError(retained)
        OUTPUT.rename(retained)
        quality = 85
        make_epub(DIRECTORY, OUTPUT, "Золотий син. Червоне повстання. Книга 2", "Пірс Браун",
                  jpeg_quality=quality)
    checker = ROOT / "tools/epubcheck-5.4.0/epubcheck-5.4.0/epubcheck.jar"
    result = subprocess.run(["java", "-jar", str(checker), str(OUTPUT)], capture_output=True,
                            timeout=180, creationflags=subprocess.CREATE_NO_WINDOW)
    validation = result.stdout.decode("utf-8", "replace") + result.stderr.decode("utf-8", "replace")
    (DIRECTORY / "epubcheck.txt").write_text(validation, encoding="utf-8")
    if result.returncode:
        raise RuntimeError("EPUBCheck reported a problem; see epubcheck.txt")
    report = {"pages": len(entries), "first_page": 1, "last_page": entries[-1]["page"],
              "all_source_image_checksums_verified": True, "epubcheck_exit_code": result.returncode,
              "output": str(OUTPUT), "bytes": OUTPUT.stat().st_size,
              "sha256": hashlib.sha256(OUTPUT.read_bytes()).hexdigest(), "jpeg_quality": quality,
              "layout": "fixed-layout page images", "ocr": False,
              "first_capture_utc": entries[0]["captured_utc"],
              "last_capture_utc": entries[-1]["captured_utc"]}
    write_json(DIRECTORY / "verification.json", report)
    print(json.dumps(report, ensure_ascii=True), flush=True)
    print(validation, flush=True)


if __name__ == "__main__":
    main()
