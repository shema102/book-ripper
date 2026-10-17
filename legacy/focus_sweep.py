"""Calibrate and lock the physical webcam's focus while photographing the book."""
import json
from pathlib import Path
import subprocess
import time

from book_scanner import Camera, best_photo, load_json, rectify, sharpness, write_json

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "focus-test"


def focus(value=None, automatic=False):
    command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
               str(ROOT / "camera-focus.ps1")]
    if value is not None:
        command += ["-Focus", str(value)]
    if automatic:
        command += ["-AutoFocus"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=20,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout)


def main():
    OUTPUT.mkdir(exist_ok=False)
    original = focus()
    write_json(OUTPUT / "original-focus.json", original)
    config = load_json(ROOT / "scan-test-close/scanner.json")
    config["settle_seconds"] = .7
    camera = Camera(config, OUTPUT / "camera.log")
    results = {}
    selected = False
    try:
        camera.frame()
        for candidates in [range(0, 251, 25), None]:
            if candidates is None:
                best = max(results, key=results.get)
                candidates = range(max(0, best-20), min(250, best+20)+1, 5)
            for value in candidates:
                if value in results:
                    continue
                focus(value)
                _, original_image, page = best_photo(camera, config)
                score = sharpness(page.crop((0, 0, page.width, min(400, page.height))))
                results[value] = score
                page.save(OUTPUT / f"focus-{value:03d}.png")
                print(f"Focus {value}: sharpness {score:.1f}", flush=True)
        best = max(results, key=results.get)
        current = focus(best)
        selected = True
        write_json(OUTPUT / "selected-focus.json", {"settings": current, "scores": results})
        print(f"Locked focus at {best}.", flush=True)
    finally:
        camera.close()
        if not selected:
            focus(original["Value"], automatic=bool(original["Flags"] & 1))


if __name__ == "__main__":
    main()
