# Collected experiment scripts

Reference copies, not application entry points. The original workspace scripts and
book outputs remain in place. Most files still assume the original book's geometry,
review files or Windows tooling. Personal paths and serial were removed from
`book_scanner.py`; it now reads `FFMPEG`, `ADB` and `ANDROID_SERIAL` from the environment.
The new application lives in `../opticalbook/` and does not import this folder.

| Script | Reusable idea | Constraints |
| --- | --- | --- |
| `book_scanner.py` | fresh FFmpeg frames, calibration, sequence checks, image EPUB | DirectShow/Windows; original reader |
| `camera-focus.ps1` | IAMCameraControl focus and autofocus | Windows, driver-dependent |
| `focus_sweep.py` | inspect a focus range by photographing samples | C920-specific range and folder assumptions |
| `PageMonitor.java` | fast visible counter + standard input gestures over ADB forward | original package/regex; Android SDK/JDK compilation |
| `ocr_reflow.py` | local Tesseract, TSV, dictionary hyphens, drop caps | fixed book/source/model paths and geometry |
| `paragraph_layout.py` | distinguish OCR block changes from real paragraphs | promoted into new engine as `layout.py` |
| `repair_paragraphs.py` | conservative repair without overwriting reviewed source | original 676-page layout assumptions |
| `refine_openings.py` | review and recover decorative initial letters | original chapter-page map and manual letter readings |
| `build_reflow.py` | source-bound reviews, chapter EPUB and page anchors | original title, parts, maps and correction files |
| `finalize_scan.py` | wait for completion, verify, package, EPUBCheck | fixed original source/output names |
| `test_book_scanner.py` | fake-camera/phone capture and package checks | legacy scanner imports |
| `test_reflow.py` | hyphenation, review binding, page joins and chapter checks | legacy OCR dependencies |
| `test_paragraph_layout.py` | geometry and conservative false-break repair | includes legacy repair checks |
| `SCANNING.md` | successful physical scanning workflow and focus lessons | historical run notes; book/photos not included |

Emulator setup, account binding, memory inspection, app hooks, APKs, credentials,
Sony service/UI-control attempts, recordings and book-specific OCR review data were
excluded because they are not part of this reusable optical workflow.
