# Optical Book Tool

Turns a book you can see on a screen (an e-reader app on a phone, for example)
or on paper into a reflowable EPUB. It photographs pages with a webcam,
recognizes the text with Tesseract OCR, rebuilds the paragraphs, and lets you
check and correct the result before export.

Everything runs locally. Nothing is uploaded, no accounts are involved, and the
reader app is never patched or rooted: the tool only looks at the screen and,
optionally, sends ordinary page-turn gestures over ADB.

## What it does

The tool has two independent halves, available both as a desktop UI (Tkinter)
and as a command line:

- **Capture** — live webcam preview with camera controls, four-corner
  perspective correction of the page area, and a sharpness/duplicate check on
  every photo. Pages can be turned by hand, or automatically on an Android phone
  through ADB. In verified mode the reader's page counter is checked before and
  after each turn, so skipped or repeated pages stop the run instead of ending
  up in the book. Interrupted runs resume where they left off.
- **Book generation** — Tesseract OCR, paragraph detection from page geometry
  (indents, line spacing, short last lines, dialogue, headings), illustration
  detection, and a review screen that shows each page's text beside its photo.
  Export produces an EPUB with adjustable fonts plus a verification report.

The two halves meet only at a `capture.json` manifest and a folder of images, so
generation also works on photos from any other source: use **Import images** on
a folder of page photographs. A webcam and ADB are not needed for OCR, review
or export.

## Install

Python 3.10+ with Tkinter is required. On some Linux distributions Tkinter comes
from the separate `python3-tk` package.

```sh
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
python -m pip install ".[capture]"
python run.py
```

`run.py` (and `Launch.cmd` on Windows) uses the local `.venv` when it exists.

- Generation only: `pip install .` skips the OpenCV camera dependency.
- Dictionary-based repair of words hyphenated across lines:
  `pip install ".[dictionary]"`.

External programs are configured by path in the UI or `project.json`:

- **Tesseract** with the language models you need (for example `ukr`, `eng`).
  On Windows it can be bundled into the app from an existing install:
  `python -m opticalbook bundle-tesseract "C:\Program Files\Tesseract-OCR"`
  ([details](docs/GENERATION.md#tesseract)).
- **ADB** — only for automatic page turning on Android.
- **Java + EPUBCheck** — optional EPUB validation.

The tool never downloads anything, opens a camera, or sends phone input on
startup. `python -m opticalbook doctor` reports what is installed.

## First use

1. Create a project. Pick the camera, its format and controls.
2. Start the preview, mark the text area's four corners in reading order
   (optionally also the phone screen and the page indicator), and check
   **Test crop** at full resolution. A firm stand and stable focus matter.
3. Choose a capture mode: verified (ADB reads the page counter and turns pages),
   optical (ADB turns pages, the page indicator is read from the photo) or
   manual (you turn pages). Try three pages before a full run.
   [Capture guide](docs/CAPTURE.md)
4. In **Book generation**, set title, author and OCR languages, then run OCR.
5. Review pages beside their photos. A blank line starts a new paragraph, a
   single Enter is a hard line break, and visual wrapping is automatic.
6. Export the EPUB. Use **Keep page as image** for maps or pages that should
   keep their layout, and **Label as sample** to export an incomplete scan.
   [Generation guide](docs/GENERATION.md)

## Command line

```sh
python -m opticalbook --help    # gui, init, import-images, doctor, capture, photo, calibrate, ocr, build, verify
python -m unittest discover -s tests -v
```

## Status

Targets Windows, macOS and Linux. The Windows UI and real Tesseract integration
have been tested; camera capture on other operating systems still needs hardware
testing. See [validation notes](docs/VALIDATION.md).

## More

- [CONTEXT.md](CONTEXT.md) — design, data formats, requirements and limitations.
- [legacy/](legacy/INDEX.md) — scripts from the original experiment, kept for
  reference only; the application does not use them.
