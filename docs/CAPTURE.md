# Capture

## Prepare

Mount phone and webcam firmly and parallel. Fill the image with the readable page
area. Use stable brightness, remove glare, and inspect actual letters before choosing
the smallest font. Avoid status bars, navigation buttons and page counters in the crop.
Keep the Android phone connected by USB if using ADB.

In the UI create a new project. Choose the backend first, then open the **Camera**
dropdown: it lists device names in that backend's index order without opening any
camera (Windows and Linux). Index order differs between backends — DirectShow also
lists virtual cameras that Media Foundation does not — so re-pick the camera after
changing backend. An index, a Linux path such as `/dev/video0`, or a URL can also be
typed. Backend `auto` means DirectShow on Windows: with the format negotiated while
opening, it starts in about a second, delivers MJPG at full frame rate and exposes
white balance. `msmf` starts as quickly (hardware transforms are disabled) but ignores
FourCC and has no white-balance control on many webcams. Linux uses `v4l2`, macOS
`avfoundation`. Resolution, FPS and FourCC must be supported by the device; if the
camera refuses them while opening, they are retried one by one (slower) and logged.
A virtual camera can produce a placeholder instead of a real image.

Start preview. Focus, exposure and white balance each have a mode:

- **Camera default** — nothing is sent; the camera keeps whatever it last used, which
  may have been set by another app.
- **Automatic** — the camera's own automatic mode; no value is sent.
- **Manual** — the automatic mode is switched off and the value you enter is used.
  The value field and its typical range appear only in this mode.

Typical ranges (OpenCV cannot ask the driver for real limits): focus usually 0–255,
often in steps of 5; exposure is entered as a shutter time (`1/60`, `60` = 1/60, `2s`);
white balance in kelvin, usually 2800–6500 (phone screens are near 6500); brightness
usually 0–255 with 128 neutral. For a stable page colour across the book, use manual
white balance. Windows backends only take whole powers of two for exposure, so `1/60`
becomes `1/64`. `project.json` keeps the backend's raw value (log2 seconds on Windows,
0.1 ms units on Linux V4L2).

In Manual mode, **-- - + ++** buttons under each value step it: focus ±5 / ±25,
exposure ±1 / ±2 EV (+ doubles the light), white balance ±100 / ±500 K. With the
preview open each click is saved and applied immediately. Stepping past the camera's
range keeps the last value it accepted and says so in the status bar.

Select **Start preview / apply** again to apply: these controls change on the open
camera without restarting it. The Camera tab shows the value the camera reports back
after each apply; if it did not change, the driver ignored it. Changing source, backend,
size, FPS or FourCC, or returning a control to Camera default, needs **Close preview**
and a fresh start. Rejected controls are also logged.

To set manual focus, tick **Focus peaking** under the preview: edges that are in focus
are painted red, and nothing is painted on a blurred image. Step the focus value with
its buttons and keep the one where the text lights up most; Low shows only the sharpest
edges, High also nearly-sharp ones. Peaking only changes the preview, never saved images. It is an
absolute contrast test, so a very dark or low-contrast image may show little even when
sharp — check with **Test crop**.

Pick a target in **Calibrate**, select **Mark** and click its four corners: top-left,
top-right, bottom-right, bottom-left as seen in book reading order, even if the camera
is mounted sideways. Right-click cancels and keeps the previous marks; **Clear**
removes an optional region. Each region is drawn on the preview in its own colour.
After marking, drag any dot (the cursor changes over it) to adjust that corner, or a
gesture end to move the tap/swipe; a drag that would cross the shape is put back.

**Rotate 90° ↻** turns the preview clockwise, one quarter per press; four presses
return to normal. Use it to see a sideways-mounted camera upright. It changes only the
view: marks stay attached to the same camera pixels, saved originals and crops are
not rotated, and capture settings are unaffected. Save settings keeps the view.

- **Text area** (required): the readable page area. This becomes every saved page image.
- **Phone screen** (optional): the whole lit display. It maps preview pixels to the
  phone's screen fractions, so gestures can be placed by clicking, and the configured
  next-page tap or swipe is drawn on the preview.
- **Page indicator** (optional): the reader's page counter (e.g. `Page 10/100`) as it
  appears in the photo. Leave a little margin around it. It may be small; its crop is
  checked at full resolution.
- **Next-page gesture** (needs the phone screen): with gesture kind *tap* click
  once; with *swipe* click the start, then the end. The ADB gesture fields are filled
  in with the matching screen fractions. Save settings and use **Test next page**.

Use **Test crop** and inspect its full-resolution scrolling view (and the indicator
crop, when set). It saves `calibration/crop.png`, `screen.png` and `indicator.png`.
Save settings. Opening the camera with different settings clears all calibrated
regions, because they are camera-pixel positions.
The sharpness threshold is a rejection aid; calibrate it against actual sample text.

## ADB / navigation

Enable Android developer options and USB debugging, authorize this PC on the phone,
and choose the executable and explicit device serial. **Refresh devices** lists
authorized devices. Fill the reader package, for example `com.readeat.app`.

Verified mode needs an accessible page counter. The default regex recognizes
`Page 1 / 265`, `Сторінка 1 / 265`, and `1 / 265`. Its two groups must be page and
total. Use **Read counter** with the actual reader open and its counter visible.

Configure a swipe or tap, or place it on the preview (see Calibration). Coordinates
are screen fractions:

```json
{"kind":"swipe","start":[0.8,0.55],"end":[0.2,0.55],"duration_ms":300}
```

For a right-edge tap use `{"kind":"tap","end":[0.9,0.5]}`; leave the other fields
present in `project.json`. **Test next page** sends one real gesture. Check that it
advances exactly one page; then manually return to the desired starting page.

The application does not change font settings, seek through the book, or toggle
reader controls. If the app has no accessible counter, choose optical or manual mode.

**Optical mode** reads the counter from the camera instead of ADB: calibrate the page
indicator (Capture tab) and, on the Scan tab, describe it in **Indicator format** with
`{page}` and `{total}` where the numbers are:

| Reader shows | Indicator format |
| --- | --- |
| `Page 10/100` | `Page {page}/{total}` |
| `10 / 100` | `{page}/{total}` (default) |
| `10 of 100` | `{page} of {total}` |
| `Сторінка 10 з 100` | `Сторінка {page} з {total}` |

Only the two numbers are used. The label is there so a misread "Page" is not
mistaken for digits: Tesseract may only output digits and the format's own letters,
and each label letter also accepts its usual misreads (`Pag6`, `P4ge`, `Раде`). An
unreadable or ambiguous indicator stops capture; it is never guessed.

The indicator Tesseract (default: the bundled copy, else PATH), optional tessdata folder
and model are on the Scan tab too; `eng` suffices for Latin labels, use e.g.
`ukr+eng` for Cyrillic ones. Optical mode still needs ADB, the serial and package to
turn pages. The indicator must stay visible on every page. **Read counter** and
**Test next page** use the camera in this mode; **Test crop** also shows what
Tesseract reads.

**Manual mode**: count 1, known total, one capture per click, and advance the reader
yourself. It sends no ADB input and records the lack of counter verification, unless
a calibrated page indicator confirms each page.

With the page indicator calibrated, every photo's indicator is read before saving,
in any mode, and must show the page being saved. A mismatch stops without saving and
writes `review-indicator.png`. Clear the indicator calibration to switch this off.

## Scan, stop and resume

For verified mode, set starting page and count 3. Inspect every saved page. Return
to page 1 and use a fresh folder for the full scan, or continue a successful sample
with the same calibration. The next Start value is the next unsaved page.

Capture saves an original JPEG plus a rectified PNG, verifies counters around the
photograph, and commits a checksummed manifest. It does not advance after the final
photo in the requested run. Only one capture job can write a project at once.

Stop cooperates between operations. The phone may be on the last saved page or the
next page after stopping. Resume accepts either position and verifies it. A `STOP`
file in the project also halts capture between pages; remove it before resuming.
If an image was saved but not logged during a crash, inspect it before moving it
aside. The tool refuses to overwrite it. Changed camera/crop/device configuration
requires a new folder; quality thresholds, settle delay and gesture tuning may be
adjusted to recover a stopped run.

## CLI

```sh
python -m opticalbook init projects/my-book
# Configure project.json: camera, explicit serial/package, gesture and counter.
python -m opticalbook photo --project projects/my-book --output projects/my-book/test.png
python -m opticalbook calibrate --project projects/my-book --photo projects/my-book/test.png --corners 0 0 1000 0 1000 1600 0 1600
python -m opticalbook calibrate --project projects/my-book --photo projects/my-book/test.png --screen 0 0 1080 0 1080 1700 0 1700 --indicator 450 1640 630 1640 630 1690 450 1690 --swipe 900 850 200 850
python -m opticalbook capture --project projects/my-book --start 1 --count 3
python -m opticalbook verify --project projects/my-book --sample
```

The example corners are placeholders. Supply the actual source-photo coordinates.
`--corners`, `--screen` and `--indicator` each take four x,y points; any subset may be
given and the others stay as saved. `--tap X Y` or `--swipe X1 Y1 X2 Y2` take photo
pixels and need a calibrated screen; they are stored as screen fractions.
Ctrl+C requests a stop. CLI capture closes its camera when it finishes; UI preview
stays open until **Close preview** or application exit.

## Troubleshooting

- No preview: check index/backend, OS camera permission, supported capture mode and
  competing camera applications. OpenCV cannot repair manufacturer USB drivers.
- Soft text: fix physical distance, focus, reflections and stand alignment before
  reducing font size. Upscaling cannot restore missing letter detail.
- Counter missing: show reader controls and test its regex. Some apps expose no
  counter to accessibility; optical and manual capture remain available.
- Page indicator unreadable or mismatched: open `review-indicator.png`, re-mark the
  indicator with some margin around the digits, improve focus, or raise the reader's
  counter size. The run never guesses a page number.
- Page did not advance: adjust gesture endpoints/duration and keep the reader in
  foreground. The run stops instead of guessing a number.
- False duplicate on a nearly blank page: inspect the saved review image, then
  consciously lower the difference threshold if warranted. Never ignore all duplicates.

The legacy C920 focus sweep found focus 60 useful at one particular distance.
That number is not a generic default. `legacy/camera-focus.ps1` is a Windows-only
reference for a driver that does not honor OpenCV controls; inspect/adapt it before use.
