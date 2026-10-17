# Context: Optical Book Tool

## What this is

This project turns the successful parts of an optical book-conversion experiment
into a reusable desktop tool. A camera photographs a visible reader page. Android
Debug Bridge can read the reader's visible page counter and send ordinary page
gestures. Tesseract recognizes the photographed text, and a separate stage packages
reviewed text into an EPUB with adjustable fonts.

The application is Python with a Tkinter UI and a command-line interface. It runs
locally. No book content is uploaded. There is no account integration, app patching,
rooting, private app-data access, or dependency on Sony software.

The original experiment successfully captured 676 pages with a Logitech C920 and
generated a Ukrainian EPUB. Its scripts mixed workstation paths, one book's page
numbers, chapter corrections, Windows APIs and packaging. Those useful scripts
are retained in `legacy/` for reference; they are not the new application's engine.
Personal phone identity and workstation paths were removed from that reference
scanner. Original workspace files were left in place.

The Sony A6400 experiment did not establish a reliable full-resolution tethered
capture path. Its drivers, service-control attempts and UI automation are not part
of this application. Sony still photographs can instead be imported as files.

## The two parts

```text
Camera + stand                  Numbered image folder
      |                                  |
Capture UI / CLI                 Import images UI / CLI
      |                                  |
      +------ capture.json + pages -------+
                         |
                Book generation UI / CLI
                         |
             Tesseract TSV -> paragraph geometry
                         |
                  Review and corrections
                         |
                EPUB + verification report
```

`capture.json` and its relative image paths are the boundary. Generation does not
import the Android module or start a camera. Its images may come from this tool,
the old scanner, a digital camera, or another capture program that writes the same
manifest. The old scanner's version-1 manifests can be read directly. Resuming an
old scan with the new capture engine is deliberately rejected because its capture
settings fingerprint has a different contract.

## Requirements

| Component | Capture | Book generation |
| --- | --- | --- |
| Python 3.10+ | Required | Required |
| Pillow and NumPy | Required | Required |
| Tkinter / Tcl-Tk | For UI; CLI works without it | For UI; CLI works without it |
| OpenCV Python | Webcam acquisition | Not required |
| Webcam recognized by the OS | Required for live capture | Not required |
| Firm stand, stable focus and screen brightness | Strongly recommended | Not required |
| ADB / Android Platform Tools | For verified counter capture and page gestures | Not required |
| Authorized Android device with USB debugging | For ADB mode | Not required |
| Accessible page counter in the reader | For verified ADB mode | Not required |
| Tesseract 5.x executable (installed or bundled) | For optical counter / page indicator checks | Required for OCR |
| Tesseract traineddata models | Not required | One per selected OCR language |
| wordfreq | Not required | Optional dictionary-based wrap-hyphen decisions |
| Java + EPUBCheck | Not required | Optional independent final EPUB validation |

External executables can be on PATH or configured by full path in `project.json` /
the UI. There are no personal paths, phone serials, page counts or chapter positions
in the new engine. FFmpeg is not required by the new engine; it remains relevant
only to the legacy DirectShow scanner.

## Capture implementation

`opticalbook/imaging.py` owns one OpenCV camera and continuously drains its frames
on a background thread. The UI displays the latest frame. Captures wait for new
frames after a configurable settle delay and select the sharpest of three actual
frames; they do not average or synthesize text. Camera properties are requested
through the chosen OS backend, with negotiated values logged and recorded.

Supported settings are source index/path, backend, width, height, FPS, FourCC,
autofocus, focus, auto-exposure value, exposure and brightness. Device drivers may
ignore properties or use different units. A successful request or a high sharpness
score does not prove that letters are readable; inspect a full-resolution crop.
Focus changes are applied when preview is restarted. Sony virtual-camera logos
can be sharp images too, so do not treat a focus score as a book-content detector.

Four preview clicks are mapped back into source-image pixels: book top-left,
top-right, bottom-right, bottom-left. Pillow/NumPy solve a perspective transform
into an upright rectangular page. This supports a sideways-mounted webcam when
the corners are chosen in book reading order. Crossed, degenerate and out-of-frame
corners are rejected. A calibration preview preserves the source and rectified crop.

Two optional regions use the same four-click order. The **phone screen** quadrilateral
gives a homography between camera pixels and 0–1 screen fractions: gestures may be
placed by clicking the preview, and the configured gesture is drawn over it. The
**page indicator** quadrilateral records where the reader's counter appears in the
photo (its crop may be as small as 10 px). Neither changes saved page images, so
neither is part of the frozen capture fingerprint.

`opticalbook/android.py` uses an explicit ADB serial for every device command. It
checks the focused package (`dumpsys window`, `mCurrentFocus`) before input: the
first focused app is locked in as the reader for that session (or an optional
`package` pin), and any other focused app stops. Page counters come
from fresh `uiautomator dump` XML, scoped to that package; unique remote filenames
prevent stale XML reuse. The counter regex must contain exactly two groups: current
page and total. Missing or ambiguous counters cause a stop.

Next-page input is configurable as a tap or a swipe. Positions are normalized to
0–1 and converted using the current Android display size. A right-to-left swipe
works for many readers; other readers use a tap near the right edge. The user must
test the gesture and confirm it advances exactly one page. The tool does not seek
to a starting page or automatically dismiss reader menus.

`opticalbook/capture.py` has three modes:

- **Verified:** checks the counter before and after each photograph, advances once
  between photographs, and verifies both the expected next page and unchanged total.
  It stops on a skipped page, unexpected app, possible duplicate or low sharpness.
- **Optical:** the same sequence, but the counter is Tesseract's reading of the
  calibrated page indicator in fresh camera frames (`opticalbook/indicator.py`), for
  readers that expose no counter to accessibility. ADB still sends gestures behind
  the foreground guard. Unreadable frames are tolerated only while waiting for a
  turn to finish; anywhere else they stop the run.
- **Manual:** saves exactly one photo per action, labels counter verification false,
  and sends no ADB input. The user provides the total and advances pages themselves.

When the page indicator is calibrated, the photo chosen for each page must itself
read as that page and total, in every mode; otherwise nothing is saved and
`review-indicator.png` holds the indicator crop. The reading is stored in the page
entry (`indicator`: OCR text, page, total), and a manual page with a matching
indicator is recorded as `counter_verified`. Indicator OCR is capture-side: its own
`tesseract`/`tessdata`/`indicator_language` settings in the capture section, single-line
mode, crop sent on stdin. The crop is dark-theme-inverted, upscaled, divided by its
local background (a closing about 0.2 line heights wide) and binarized: photographed
screens carry lighting bands and moiré that defeat Tesseract's global threshold (on a
real low-contrast `Сторінка 61 / 949` counter it read nothing until this was added).
It is read at two line heights; unless both give the same page and total, the frame
is unreadable, so one misread digit cannot become a wrong page. `indicator_format`
describes the counter as a template such as `Page {page}/{total}`. Tesseract output
is restricted to digits plus the template's own characters; the template becomes a
searched regex in which each label letter also matches its OCR look-alikes (digits,
Cyrillic/Latin twins), so a misread label glued to the number (`Pag610/100`) is eaten
as label instead of read as digits. Without a matching label, a number glued to other
letters or digits is refused. Exactly one distinct page/total match is required. On
generated 9–14 px `Page N/T` counters this read 236/240 correctly and never accepted
a wrong number; the misses were unreadable, which stops rather than misfiles.

Tesseract settings named `tesseract` (both sections) resolve to the optional bundled
copy in `opticalbook/bundled/tesseract/` before PATH (`common.tesseract()`), together
with its tessdata unless one is set. The bundle is made locally by `bundle-tesseract`
from an existing Windows install; OCR caches still key on the engine version and
model hashes, so switching engines re-runs OCR.

The manifest is committed after its original JPEG and rectified PNG are saved.
Each crop has a SHA-256 digest and dimensions. Resume verifies previous entries,
requires the next unsaved number, and accepts the phone on the last saved page or
the next page. Changed camera/crop/device settings require a new folder; quality
thresholds, settle time and gestures may be tuned for recovery. Unknown orphan images
are never overwritten. The UI runs one job at a time; a project lock also prevents
two capture processes writing concurrently.

Keep reader font, spacing, orientation, stand position and brightness stable during
a scan. Smaller font means fewer page turns but fewer camera pixels per letter.
The right choice depends on inspected source detail, not page count alone.

## OCR and document structure

`opticalbook/generation.py` validates the image sequence and checksums before OCR.
It converts images to grayscale, applies mild autocontrast, resizes by a configurable
factor, and runs the configured Tesseract executable with selected models and PSM.
TSV output preserves words, confidence and bounding boxes. The raw TSV is retained.
Single-column text is assumed; this is not a general multicolumn layout engine.

`opticalbook/layout.py` estimates repeated left edges, ordinary line height and line
pitch. It uses indentation, vertical gaps, short final lines, dialogue and heading
transitions to infer paragraphs. An OCR block ID change on its own is insufficient:
the experiment showed many such changes halfway through a continuous paragraph.
Screen-wrap lines within a paragraph are joined with spaces. The page cache also
records whether its final line fills the ordinary text width.

Across source-page boundaries, automatic joining requires adjacent pages, an aligned
first paragraph and a full last line. Headings, image pages and new dialogue stop
automatic joins. The Review UI offers Auto / Yes / No for the first paragraph's
continuation across a page. These geometric decisions remain heuristics: a true
paragraph can end exactly at the margin, and text without indentation can be
ambiguous. Review is the final authority.

The editor distinguishes three cases:

- A visual wrap is only a display effect and inserts no character.
- An explicit Enter is a hard line break, exported as `<br/>` inside the paragraph.
- A blank line starts a new paragraph, exported as a separate `<p>`.

This allows poetry, epigraphs and intentional short lines to be repaired without
freezing ordinary body text into photographed screen widths.

Wrap-hyphen handling is conservative. By default a final hyphen is retained while
the following screen line is joined. Optional wordfreq support removes it only
when dictionary frequency supports a joined word over a compound. It is not a
spellchecker; proper names and rare words need review.

OCR caches are bound to source image hashes, engine version, OCR parameters and
available model-file hashes. Changing title or author does not invalidate OCR.
Reviewer edits live separately in `ocr/edits/`, are bound to their source hash and
take precedence over machine output. Rerunning OCR does not overwrite reviews.
An empty OCR page cannot silently disappear from the EPUB: correct it or explicitly
mark it as an image page.

Chapter titles are supplied during review. There is no book-specific automatic
51-chapter map, fixed drop-cap coordinate, dedication text, or publisher correction
in the new engine. Lines beside a drop cap stay one paragraph (layout ignores the
OCR block change there), but with page-layout PSM 3 Tesseract usually drops the
initial letter itself, so chapter openings need review. Diagrams, footnotes and
unusual layouts need review too. Choose image preservation when appropriate.

`opticalbook/figures.py` finds illustrations after OCR, with NumPy/Pillow only. Ink is
anything differing from the median paper tone (so dark themes work); confidently
recognized letter-sized words (or high-confidence large headings) are erased, and what
remains, closed by about half a line height on a coarse grid, is a figure when it is
at least two text lines tall and wide. Ink with text beside it is a drop cap or
marginal ornament, not a figure: e-books place figures between paragraphs. OCR lines
mostly inside a figure (labels, noise read from a drawing) leave the text. Each figure
is placed before the first paragraph starting below its top. Export crops the source
image; a figure never splits a paragraph, so when the first paragraph of a page joins
the previous page, figures above it follow the joined paragraph. Detection is a
heuristic: Review shows the boxes and can delete or draw them.

## Project files and contract

```text
project.json                 UI/CLI configuration: separate capture and book sections
capture.json                 version 1 image manifest, independent of the generation engine
pages/00001.png              lossless rectified/imported page image
originals/00001.jpg          full camera frame (live capture only)
calibration/camera.png       preview photo (not a captured book page)
calibration/crop.png         last inspected calibration crop
calibration/screen.png       last inspected phone screen crop (when calibrated)
calibration/indicator.png    last inspected page indicator crop (when calibrated)
ocr/raw/00001.tsv            unmodified Tesseract word-position output
ocr/pages/00001.json         source-bound OCR cache and inferred paragraphs
ocr/edits/00001.json         source-bound explicit review, chapter, figure and image-page choices
.capture.lock               present only while capture writes (PID recorded)
.generation.lock            present only during OCR/export (PID recorded)
STOP                        optional user-created stop sentinel for capture
review-indicator.png         indicator crop of a photo whose page indicator did not match
book.epub                   chosen output location/name; existing files are refused
book.verification.json      export counts, source page coverage and output digest
```

Minimal `capture.json`:

```json
{
  "version": 1,
  "total": 1,
  "counter_verified": false,
  "pages": [{
    "page": 1,
    "image": "pages/00001.png",
    "sha256": "THE_ACTUAL_SHA256_OF_THE_IMAGE_FILE",
    "size": [1000, 1600]
  }]
}
```

Paths must remain inside the project. Page numbers are unique and consecutive.
A complete export must cover 1 through total; partial export requires an explicit
sample label. `import-images` creates this manifest from naturally sorted filenames
and normalizes EXIF orientation, without a camera or ADB dependency. Only the first
frame of a multipage TIFF is imported; split multipage documents before importing.

Camera/crop/device configuration is frozen once pages are saved. To change geometry,
font or page layout, create a new capture project. Book metadata and review edits
can change independently. Stop is cooperative between operations; an ADB read can
take up to 30 seconds and an OCR process up to its 120-second timeout.

After a crash, verify that the recorded PID/job is no longer active before removing
a stale lock. Inspect orphan images instead of deleting them blindly. A crash at
the final export publication step can leave a zero-byte reserved output; inspect
and remove that empty file, or choose a different output name. Original images are
not cleaned up automatically.

## Export and verification

Text EPUBs contain real paragraphs with relative font sizes, chapter navigation,
all source-page anchors and optional retained page images. Image-only export is an
explicit alternative and contains no recognized text. Packaging uses a stored first
`mimetype` entry, XML metadata, XHTML, CSS, navigation and a spine. XML is parsed
before the final ZIP is atomically published; a report records the output checksum.
EPUBCheck is the separate standards validator, not the internal XML check.

The new tool was checked with synthetic capture/phone tests, a visible Windows UI
test, and real Tesseract 5.5.3 English OCR on generated pages. That output passed
EPUBCheck 5.4.0 with zero errors/warnings. The reusable OpenCV acquisition backend
has not completed a real camera/phone book scan. macOS/Linux hardware and UI behavior
have not been exercised. Kindle import/rendering is also untested. See
`docs/VALIDATION.md` for the current checks and reproducible commands.

## Code map and next work

| File | Responsibility |
| --- | --- |
| `common.py` | configuration, atomic JSON, process calls, path containment, locks |
| `imaging.py` | OpenCV acquisition, perspective correction, quality metrics |
| `android.py` | ADB devices, foreground guard, visible page counter and gestures |
| `indicator.py` | Tesseract reading of the calibrated page indicator (optical counter) |
| `capture.py` | sequence verification, resume, image/manifest writing |
| `layout.py` | geometric paragraph-boundary inference |
| `figures.py` | illustration bounding boxes from ink unexplained by OCR words |
| `generation.py` | image import, Tesseract TSV, separate review, EPUB |
| `ui.py` | two-tab Tkinter UI and background-job/event-queue coordination |
| `__main__.py` | independent CLI commands and GUI launch |

Useful next improvements: test actual cameras on all three operating systems;
add device discovery and per-device camera-control ranges; expose OCR confidence
overlays; resize figure boxes in place; improve multicolumn/footnote layout;
add an optional generic fast Android counter adapter. The legacy Java monitor
demonstrates that last approach but is currently reader-specific and not invoked.

Keep future capture and generation features behind the manifest boundary. Do not
move one book's chapter numbers, corrections or camera-focus values into defaults.
