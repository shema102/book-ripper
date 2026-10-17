# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Local Python/Tkinter desktop app + CLI with two independent workflows: **capture** (webcam + optional Android ADB page turning → rectified page images) and **book generation** (Tesseract OCR → paragraph inference → human review → reflowable EPUB). `CONTEXT.md` is the authoritative design document (requirements, data contracts, lessons from the original experiment, limitations); read it before non-trivial changes. `docs/CAPTURE.md`, `docs/GENERATION.md` and `docs/VALIDATION.md` cover user workflows and current verification status.

## Commands

The local `.venv` already has the package installed in editable mode (`pip install -e ".[capture]"`). On Windows use `.venv\Scripts\python` (or activate the venv).

```sh
python run.py                                   # GUI (re-execs into .venv if present); Launch.cmd does the same
python -m opticalbook --help                    # CLI: gui, init, import-images, doctor, capture, photo, calibrate, ocr, build, verify
python -m opticalbook doctor [--project DIR]    # dependency / external-tool report
python -m unittest discover -s tests -v         # all automated tests (synthetic data, fake camera/phone)
python -m unittest discover -s tests -k test_perspective   # single test by name substring
python tests/real_ocr_smoke.py --tesseract PATH [--tessdata DIR]   # optional real-Tesseract integration
python tests/ui_smoke.py [capture]              # opens the real Tk window ~30s
```

There is no linter/formatter configured. Smoke tests write into `.test-work/` (gitignored). Optional extras: `[capture]` (opencv-python), `[dictionary]` (wordfreq). Tesseract, ADB, Java/EPUBCheck are external executables configured by path in `project.json` / the UI. Tesseract may also be bundled locally (`python -m opticalbook bundle-tesseract <install dir>` → `opticalbook/bundled/tesseract/`, runtime DLLs found via PE imports in `bundle.py`); always resolve it through `common.tesseract(config)`, which prefers the bundle when the setting is the default `tesseract`.

## Architecture

**`capture.json` + relative image paths is the hard boundary between the two halves.** Generation must not import `android.py` or open a camera; its input may come from this tool, `import-images`, the legacy scanner (v1 manifests), or any program writing the same manifest. Keep new features on the correct side of this boundary.

- `common.py` — config defaults (`defaults()`, `project.json` with separate `capture`/`book` sections), atomic JSON writes, subprocess calls, path containment (`inside`), PID lock files, fingerprints.
- `imaging.py` — single OpenCV camera drained on a background thread; capture picks the sharpest of 3 real post-settle frames (never averages/synthesizes); four-corner perspective rectification via Pillow/NumPy; `REGIONS` lists the calibrated quads (`corners` = text crop, `screen_corners` = phone screen ↔ gesture fractions via `to_screen`/`from_screen`/`gesture_from`, `indicator_corners` = page counter). Format (FourCC/size/FPS) is passed to `VideoCapture` at open — setting it afterwards restarts the stream per property; `auto` = DirectShow on Windows; MSMF hardware transforms are disabled (each open/format change otherwise costs ~6 s).
- `cameras.py` — lists camera names in each backend's index order (DirectShow / Media Foundation via ctypes COM, Linux sysfs) without opening a device.
- `android.py` — every ADB command uses an explicit serial; foreground-package guard before input; page counter parsed from fresh, package-scoped `uiautomator dump` with a 2-group regex (page, total); ambiguity → stop.
- `indicator.py` — capture-side Tesseract read of the calibrated page indicator (psm 7; crop dark-theme-inverted, upscaled, background-flattened by a closing of ~0.2 line heights and binarized, because lighting bands defeat Tesseract's own threshold; read at two heights that must agree, so a misread is "unreadable", never a wrong page; PNG on stdin; character whitelist from `indicator_format` such as `Page {page}/{total}`; the template compiles to a searched regex whose label letters accept OCR look-alikes, exactly one distinct match). Uses the capture section's own `tesseract`/`tessdata`/`indicator_language`, never the book OCR settings.
- `capture.py` — verified mode (counter checked before/after each photo, exactly one turn between), optical mode (same sequence, counter = indicator OCR of fresh frames, ADB only for gestures) and manual mode (no ADB input, `counter_verified: false`). With the indicator calibrated, every chosen photo's indicator must read as the page being saved in any mode. Manifest is committed only after JPEG original + PNG crop are saved, with SHA-256 + size. Resume re-verifies entries; camera/crop/device settings are fingerprinted and frozen once pages exist (quality thresholds/settle/gesture may change). Orphan images are never overwritten.
- `layout.py` — geometric paragraph inference (indent, line pitch, gaps, short last lines, dialogue/headings). OCR block-ID changes alone are deliberately *not* paragraph breaks — including inside a section's opening run of equally indented lines beside a drop cap.
- `generation.py` — `import_images` (natural sort, EXIF normalize), `ocr_page` (one image → TSV lines, PNG on stdin; with `eng` among several models a mostly non-Latin page's Latin look-alike words are re-read without `eng`; free-standing `-`/`--` → `—`; shared by `recognize` and the capture-side `ocr_test` button, which writes nothing), `recognize` (Tesseract TSV; raw TSV kept in `ocr/raw/`, cache in `ocr/pages/`), `save_review` (edits in `ocr/edits/`), `build_epub` (+ `*.verification.json`).
- `ui.py` — two-tab Tk `App`; capture settings sit on three scrollable sub-tabs by purpose — Setup (camera device/format, ADB device), Image (camera controls and phone screen brightness), Scanning (counter, indicator OCR, gesture with next/previous tests, run thresholds); calibration points are always stored in raw camera pixels — preview rotation (`preview_rotation`, view-only) and dragging go through `to_canvas`/`from_canvas` (`imaging.rotate_point`); long work runs one-at-a-time in a background thread and reports through an event queue (`self.events`), never touching widgets off the main thread.
- `__main__.py` — CLI mirroring the UI; running it with no subcommand (or `gui`) launches the GUI.

Key invariants (tests enforce most of them in `tests/test_workflow.py`):
- OCR caches are keyed by source image hash, `OCR_VERSION`, OCR params and model-file hashes; book metadata changes must not invalidate OCR. Bump `OCR_VERSION` when OCR/layout output semantics change.
- Reviewer edits are source-hash-bound, override machine output, and must survive re-OCR.
- Review text semantics: blank line = new `<p>`; single newline = `<br/>`; visual wrap = nothing stored.
- Cross-page paragraph joins require adjacent pages, aligned first paragraph and full-width last line; Review's Auto/Yes/No overrides.
- Empty OCR pages may not silently vanish — must be corrected or marked as image pages. Partial export requires an explicit sample flag. Existing output files are refused; EPUB is XML-parsed before atomic publish.
- All manifest paths must stay inside the project; page numbers unique and consecutive.
- `.capture.lock` / `.generation.lock` prevent concurrent writers; a `STOP` file halts capture cooperatively.

## Constraints

- No book-specific data in the engine or defaults: no chapter maps, correction dictionaries, fixed coordinates, focus values, device serials or workstation paths. Those lived in the original experiment.
- `legacy/` is reference-only (see `legacy/INDEX.md`); the app never imports or runs it.
- Nothing is downloaded, no camera opened and no phone input sent on startup. Tests use fakes and must never send real phone input.
- Code style is compact (short lines joined with `;`, minimal whitespace around `=`/`,` in many places); match the surrounding file.
