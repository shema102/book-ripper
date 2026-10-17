# Validation and current limits

Last checked on Windows with Python 3.13.1, Pillow 12.3.0, NumPy 2.5.4, OpenCV 4.14.0,
Tesseract 5.5.3 and EPUBCheck 5.4.0. Python dependencies are installed with pip (see the
README); they are not vendored into the source.

## Automated workflow checks

```sh
python -m unittest discover -s tests -v
```

The tests use synthetic photos and fake phone/camera objects, never real phone input.
Last result: **51 tests passed**.
They cover expected counter progression, exactly one turn, skipped pages, page
changes during photographs, duplicate and blur gates, stop, resume and frozen
settings, manual capture without phone actions, orphan preservation, manifest
checksum/path validation, counter scoping/ambiguity, wrong-foreground rejection and reader lock-in,
tap coordinates, perspective validation, screen/gesture calibration mapping,
optical page-indicator counter sequencing and mismatch refusal, indicator format
parsing (label misreads, glued numbers, ambiguity), preview rotation point mapping,
paragraph inference, natural-order import,
OCR cache invalidation, metadata changes without re-OCR, source-bound review edits,
exclusive capture locks, explicit page continuation, distinct hard breaks/paragraphs,
EPUB structure, all source-page anchors, image preservation and incomplete export,
figure boxes (light/dark theme, drop caps excluded, OCR noise removed), figure
markers in review and figure crops placed around joined paragraphs.

## Real Tesseract integration

```sh
python tests/real_ocr_smoke.py --tesseract /path/to/tesseract --tessdata /path/to/tessdata
python tests/real_ocr_smoke.py --tesseract tesseract     # the bundled copy, if present
```

This creates two generated English text pages under `.test-work`, imports them,
runs real OCR, checks recognized text/paragraphs and exports an EPUB. It also reads
a 12 px dark-theme page indicator through the capture-side indicator OCR. Its report is
`.test-work/real-ocr-state.json`. That generated EPUB was validated independently
with EPUBCheck: zero errors and zero warnings. No purchased book is included in
these fixtures.

A second EPUB containing a manually reviewed chapter, explicit hard line breaks,
paragraphs and a retained image page also passed EPUBCheck with zero errors/warnings.

## Visible UI smoke check

```sh
python tests/ui_smoke.py
python tests/ui_smoke.py capture
```

These open the actual Tkinter window for 30 seconds with generated sample data,
test review save, figure add/delete with marker renumbering, and a background-job completion, and record widget dimensions in
`.test-work/ui-state.json`. The Capture and Book generation tabs were visually
inspected at 1220×900. Neither test opens a webcam or calls ADB.

## Physical camera and phone check (2026-10-17)

With a 1080p USB webcam over a Samsung phone on Android 16 (reader rotated
90°, low-contrast gray `Сторінка N / 949` counter, visible lighting bands), the
optical indicator was compared with the ADB counter over 30 ADB page turns
(forward and back), three frames per page: 92 of 93 reads matched, none were wrong,
and one was refused as unreadable (the two scales disagreed). The previous
preprocessing read 0 of 63 such frames. ADB reader detection without a configured
package and the Android 16 `dumpsys window` focus format were verified on the
same phone.

Screen banding was traced on the same setup: band strength did not change with focus
(45–75) or capture resolution (720p–1080p), so it is not moiré; at 1/1024 s it became
hard black stripes even at brightness 200, so it is PWM flicker. Sweeping exposure
1/4096–1/16 s against brightness 1–255, brightness 10 with 1/16 s gave about 1 %
banding (from about 6 % at 150 with 1/512 s) with unchanged book OCR confidence (93.7)
and a correct page indicator.

## What remains unverified

- The OpenCV acquisition layer has been used with a physical webcam and phone (see
  above), but has not yet completed a full book scan. Capture sequencing is tested
  through injected fakes. The original FFmpeg/DirectShow scanner did complete a
  physical 676-page scan.
- macOS/Linux camera permissions, drivers, Tk packaging and device properties have
  not been exercised. The core uses portable Python APIs, with selected OpenCV
  backends and no unconditional Windows process flags.
- A different reader may expose no accessible counter, or need a different gesture.
  The default UiAutomator adapter is slower than the original temporary Java helper.
- Geometric paragraph inference is approximate. OCR is not full proofreading;
  confidence values are recognition estimates, not accuracy guarantees.
- There is no complete multicolumn, table, footnote or decorative-initial layout
  engine. Manually review those pages or preserve them as images. Lines beside a
  drop cap keep their paragraph, but the initial letter itself is usually lost.
- Actual Kindle import/rendering and retail EPUB interoperability beyond EPUBCheck
  have not been tested. The output is an OCR reconstruction.

## Primary technical references

- [Python Tkinter](https://docs.python.org/3/library/tkinter.html)
- [OpenCV VideoCapture properties](https://docs.opencv.org/4.x/d4/d15/group__videoio__flags__base.html)
- [Android Debug Bridge](https://developer.android.com/tools/adb)
- [Tesseract command-line usage](https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html)
- [Tesseract language models](https://github.com/tesseract-ocr/tessdata_best)
- [EPUBCheck](https://github.com/w3c/epubcheck)
