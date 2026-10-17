from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
import threading
import time
from PIL import Image
from .android import Phone
from .common import digest, fingerprint, inside, read_json, tesseract, write_json, locked
from .imaging import REGIONS, rectify, sharpness, difference
from .indicator import compile_format, read_indicator


def validate_manifest(folder, complete=True, verify=True):
    folder = Path(folder)
    manifest = read_json(folder / "capture.json")
    if manifest.get("version") != 1:
        raise ValueError("Unsupported capture manifest version.")
    pages = manifest["pages"]
    numbers = [entry["page"] for entry in pages]
    if not numbers or numbers != list(range(numbers[0], numbers[-1]+1)) or numbers[0] < 1:
        raise ValueError("Capture has no pages, duplicates, or gaps.")
    total = manifest["total"]
    if numbers[-1] > total:
        raise ValueError("Capture contains a page beyond the book's total.")
    if complete and numbers != list(range(1, total+1)):
        raise ValueError("Book is incomplete. Use an explicitly labelled sample export to inspect a partial capture.")
    for entry in pages:
        path = inside(folder, entry["image"])
        if verify:
            if digest(path) != entry["sha256"]:
                raise ValueError(f"Page {entry['page']} image checksum changed.")
            with Image.open(path) as image:
                if "size" in entry and list(image.size) != entry["size"]:
                    raise ValueError(f"Page {entry['page']} image dimensions changed.")
    return manifest


@locked('capture')
def capture(folder, config, camera, start, count, stop=None, log=print, phone=None):
    """Automated capture with a verified counter, or one-at-a-time manual photography.

    The counter comes from ADB accessibility text (verified) or OCR of the calibrated
    page indicator in fresh camera frames (optical). When the indicator is calibrated,
    every photo's own indicator must also read as the page being saved, in any mode.
    Injecting camera/phone objects also makes sequence guards testable without
    sending input to a real device. The caller owns and closes its camera.
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    stop = stop or threading.Event()
    if count < 1 or start < 1:
        raise ValueError("Start page and count must be positive.")
    mode = config["counter_mode"]
    if mode not in ("verified", "optical", "manual"):
        raise ValueError("Counter mode must be verified, optical or manual.")
    turning = mode != "manual"
    if not turning and count != 1:
        raise ValueError("Manual capture saves one page per click; it never advances the phone.")
    if len(config.get("corners", [])) != 4:
        raise ValueError("Calibrate the four screen corners first.")
    indicator = len(config.get("indicator_corners") or []) == 4
    if mode == "optical" and not indicator:
        raise ValueError("Optical counter mode needs the page indicator calibrated.")
    if indicator:
        tesseract(config)  # fail before any gesture, not after the first turn
        compile_format(config["indicator_format"])
    # Recovery thresholds and gestures do not change source-page geometry.
    frozen = fingerprint({key: config.get(key) for key in ('camera','corners','serial','package','counter_mode')} |
                         {'camera': {key: value for key, value in config['camera'].items()
                                     if value is not None or key not in ('auto_white_balance','white_balance')}})
    manifest_path = folder / "capture.json"
    manifest = read_json(manifest_path) if manifest_path.exists() else {
        "version": 1, "capture_settings_sha256": frozen, "counter_verified": turning,
        "camera_actual": getattr(camera, 'actual', {}),
        "total": int(config["total"]), "pages": []}
    if manifest.get("capture_settings_sha256") != frozen:
        raise ValueError("Camera/layout/device settings changed or this is a legacy capture. Start a new folder for this layout.")
    entries = manifest["pages"]
    if entries:
        validate_manifest(folder, complete=False)
        expected = entries[-1]["page"]+1
        if start != expected:
            raise ValueError(f"Next unsaved page is {expected}. Set Start/next page to that number.")
    else:
        expected = start
    phone = (phone or Phone(config)) if turning else None
    def counter(strict=True):
        """Reader (page, total) from ADB, or from OCR of a fresh frame; None if unreadable and not strict."""
        if mode == "verified":
            return phone.page()
        reading = read_indicator(camera.frame(after=camera.sequence)[1], config)
        if reading["page"] is None:
            if strict:
                raise RuntimeError(f"Page indicator unreadable (OCR: {reading['text']!r}). Keep it visible; check it with Test crop or adjust the counter regex.")
            return None
        return reading["page"], reading["total"]
    if turning:
        current, total = counter()
        if entries and total != manifest["total"]:
            raise RuntimeError("Reader page count changed; restore its font, spacing and orientation.")
        if current not in ((expected-1, expected) if entries else (expected,)):
            raise RuntimeError(f"Reader must show page {expected}" + (f" or {expected-1}." if entries else "."))
        manifest["total"] = total
    total = manifest["total"]
    if not 1 <= expected <= total:
        raise ValueError("All pages are already captured, or next page exceeds the total.")
    previous = None
    if entries:
        with Image.open(inside(folder, entries[-1]["image"])) as source:
            previous = source.copy()
    (folder / "pages").mkdir(exist_ok=True)
    (folder / "originals").mkdir(exist_ok=True)
    saved = 0
    for number in range(expected, min(expected+count, total+1)):
        if stop.is_set() or (folder / "STOP").exists():
            break
        if turning:
            reading = counter()
            if reading == (number-1, total):
                if stop.is_set():
                    break
                phone.turn()
                if stop.wait(float(config["settle_seconds"])):
                    break
                deadline = time.monotonic()+8
                # A page-turn animation may leave the optical indicator briefly unreadable.
                reading = counter(strict=False)
                while reading in ((number-1, total), None) and time.monotonic() < deadline:
                    if stop.wait(.3):
                        return saved
                    reading = counter(strict=False)
            if reading != (number, total):
                shown = f"{reading[0]}/{reading[1]}" if reading else "an unreadable page indicator"
                raise RuntimeError(f"Expected {number}/{total}; reader shows {shown}. Capture stopped.")
        if stop.wait(float(config["settle_seconds"])):
            break
        sequence = camera.sequence
        candidates = []
        for _ in range(3):
            sequence, original = camera.frame(after=sequence)
            page = rectify(original, config["corners"])
            candidates.append((sharpness(page), original, page))
        quality, original, page = max(candidates, key=lambda item: item[0])
        if mode == "verified" and phone.page() != (number, total):
            raise RuntimeError("Reader changed during the photograph. No page was saved.")
        # The saved photo itself must show the page it is filed as.
        reading = read_indicator(original, config) if indicator else None
        if reading and (reading["page"], reading["total"]) != (number, total):
            rectify(original, config["indicator_corners"], REGIONS["indicator_corners"][1]).save(folder / "review-indicator.png")
            raise RuntimeError(f"Page indicator in the photo reads {reading['text']!r}, expected {number}/{total}. "
                               "No page was saved; inspect review-indicator.png.")
        delta = difference(previous, page) if previous is not None else None
        if quality < float(config["minimum_sharpness"]):
            page.save(folder / "review-blur.png")
            raise RuntimeError("Photo below sharpness threshold. Inspect review-blur.png and recalibrate.")
        if delta is not None and delta < float(config["minimum_difference"]):
            page.save(folder / "review-duplicate.png")
            raise RuntimeError("Possible duplicate image. Inspect review-duplicate.png before resuming.")
        crop_path = folder / "pages" / f"{number:05d}.png"
        original_path = folder / "originals" / f"{number:05d}.jpg"
        if crop_path.exists() or original_path.exists():
            raise FileExistsError("Unlogged photograph exists. Review it before resuming; it will not be overwritten.")
        original.save(original_path, quality=98, subsampling=0)
        page.save(crop_path)
        entries.append({"page": number, "image": f"pages/{number:05d}.png",
                        "original": f"originals/{number:05d}.jpg", "sha256": digest(crop_path),
                        "size": list(page.size), "sharpness": quality, "difference": delta,
                        "counter_verified": turning or reading is not None,
                        "captured_utc": datetime.now(timezone.utc).isoformat()} | ({"indicator": reading} if reading else {}))
        write_json(manifest_path, manifest)
        previous = page
        saved += 1
        log(f"Saved page {number}/{total}; sharpness={quality:.1f}; difference={delta}" +
            (f"; indicator reads {reading['text']!r}." if reading else "."))
    return saved
