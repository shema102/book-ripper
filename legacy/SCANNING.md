# Physical-screen book scanner

Status: complete. All 676 pages captured and verified. The primary deliverable is
`Zolotyi-syn-reflowable.epub` in the workspace root (about 2.7 MB). It contains
reflowable Ukrainian OCR text, 51 visually checked chapter titles, four parts,
54 illustrations, and all 676 source-page anchors. EPUBCheck reports zero errors
or warnings. OCR is not fully proofread and can contain typos.

`Zolotyi-syn.epub` is the separate page-image backup (about 182 MB). Original
photos remain under `lab/scan-full`. Capture, OCR, and phone helper processes have
finished. Webcam autofocus is restored; the phone is back on page 1.

The physical Samsung remains unrooted. The camera photographs its actual display;
ADB is used only for reader page counters and normal page-turn gestures. Audio is
never recorded. Photos and output stay in the chosen local scan folder.

## Setup

Connect the webcam and keep the phone connected over USB with Readeat open.
Mount the camera and phone firmly, parallel to one another. Use a light page
background, steady screen brightness, and avoid reflections. Fill the camera frame
with the phone screen. A landscape camera can be mounted sideways to give a portrait
phone more camera pixels. Calibration corners must then be supplied in the order
corresponding to the book's top-left, top-right, bottom-right, bottom-left.

Check actual letter shapes at full resolution before reducing reader font size.
Do not change the font, orientation, or reading layout during a scan: these change
the page count. Start with three pages and inspect all three. Go to page 1 before
the full run. Show reader controls so the accessible page counter is present.

## Commands (PowerShell, from workspace root)

```powershell
$scannerPython='python' # Historical commands; adapt paths before use.
& $scannerPython .\lab\book_scanner.py devices
& $scannerPython .\lab\book_scanner.py options --camera 'ACTUAL WEBCAM NAME'
& $scannerPython .\lab\book_scanner.py photo --camera 'HD Pro Webcam C920' --size 1920x1080 --fps 30 --input-format mjpeg --output .\lab\NEW-scan-test\framing.png
```

Choose a supported size/rate from `options`. Add `--input-format mjpeg` to `photo`
only when the selected webcam mode requires MJPEG. `photo` creates `scanner.json`
alongside the test photo and never advances the reader. It refuses to overwrite
an existing photo/configuration; use a fresh folder for a new camera setup.

Inspect the photo. Supply the four corners of the page's text area in **source
photo pixels**, not a resized preview's coordinates:

```powershell
& $scannerPython .\lab\book_scanner.py calibrate --config .\lab\scan-test\scanner.json --photo .\lab\scan-test\framing.png --corners 'X1,Y1,X2,Y2,X3,Y3,X4,Y4'
```

Inspect `calibrated-preview.png` for orientation, missing text, focus and glare.
Once the photo is good, capture a small sample:

```powershell
& $scannerPython .\lab\book_scanner.py capture --config .\lab\scan-test\scanner.json --count 3
```

The tool reads the counter before and after each photo, advances one page at a
time, stops for page-count changes or suspected duplicate/blurred photos, and
records a checksum per saved page in `capture.json`. It saves both a cropped PNG
and a high-quality full-frame JPEG. These checks help catch errors but do not
replace reviewing sample photos for legibility. The C920's working mode is
1920x1080, MJPEG, 30 fps. Some other advertised modes failed to open.

Autofocus proved unreliable. `camera-focus.ps1` reads/sets its standard DirectShow
focus control; calibration found manual focus 60 sharp for this particular stand.
The scanner config's `manual_focus` reapplies it after the video stream opens.
Do not copy 60 for a different distance without testing. Original autofocus state
is saved in `lab/focus-test/original-focus.json`; restore with:

```powershell
& .\lab\camera-focus.ps1 -Focus 0 -AutoFocus
```

For faster counters, the optional temporary `PageMonitor.java` helper is compiled
to DEX/JAR and run through `adb shell app_process`, without root or an APK install.
It binds to phone localhost port 38493 and requires an ADB forward. Scanner config
`monitor_port: 38493` selects it. It clears the accessibility cache for each read,
verifies Readeat is active, and accepts only PAGE, NEXT (standard Android swipe),
and QUIT. It does not read app-private files. Avoid starting another uiautomator
session while the helper is active. Send QUIT and remove the forward at the end.

`capture` can resume if the phone is on the last saved page or the next one.
Ctrl+C stops the run. A file named `STOP` in the scan folder prevents a new run
and is checked between captures. Reader controls must stay visible for the page
counter; the scanner stops if the counter disappears. It does not automatically
toggle controls, change reader settings, dismiss prompts, or navigate other apps.

For the full book, create a separate scan folder using the same tested camera
setup, calibrate it, start the reader at page 1, then run `capture` with
`--start-page 1 --count TOTAL`. Keep the phone and camera in place throughout.

## EPUB

```powershell
& $scannerPython .\lab\book_scanner.py epub --directory .\lab\scan-full --output .\lab\book.epub --title 'Золотий син. Червоне повстання. Книга 2'
```

This creates a **fixed-layout image EPUB**, not searchable or reflowable text.
It rejects missing pages, checksum changes, and incomplete books. `--partial`
can create an explicitly labelled sample. OCR for a reflowable Ukrainian EPUB
is implemented separately by `ocr_reflow.py`, `refine_openings.py`, and
`build_reflow.py`. Actual Kindle import/rendering has not been tested.

The default EPUB uses JPEG quality 90 to reduce file size while retaining the
lossless captured PNGs. Use `--jpeg-quality` or `--lossless-images` if needed.
`finalize_scan.py` waits for all pages, verifies image checksums and dimensions,
builds `Zolotyi-syn.epub` in the workspace root, then runs the locally downloaded
official EPUBCheck validator. Its final report is `lab/scan-full/verification.json`.
The partial layout sample passed EPUBCheck with zero errors or warnings.

The complete reflowable output has its own
`Zolotyi-syn-reflowable.verification.json` report. Proofreading overrides and
visually checked chapter titles are in `lab/ocr`; each is tied to its source
image checksum. The OCR cache and original TSV results are retained for later
corrections. `test_reflow.py` checks paragraph joins, source anchors, dialogue,
map assets, and dictionary-supported dehyphenation.

Offline verification:

```powershell
Push-Location .\lab
& $scannerPython -m unittest -v test_book_scanner.py
Pop-Location
```

Implementation references: [FFmpeg DirectShow](https://ffmpeg.org/ffmpeg-devices.html#dshow),
[EPUB 3.3](https://www.w3.org/TR/epub-33/).
