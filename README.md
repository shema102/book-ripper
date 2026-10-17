# Optical Book Tool

A local Python desktop application with two independent workflows:

- **Capture:** webcam preview, camera controls, four-corner perspective correction,
  photographs, and optional Android page navigation through ADB.
- **Book generation:** local Tesseract OCR, paragraph/line-break review, and
  reflowable EPUB export. Also accepts an existing folder of page photographs.

Start with [CONTEXT.md](CONTEXT.md) for architecture, requirements, data formats,
lessons from the experiment, and current limitations. Useful original scripts are
collected under [legacy/](legacy/INDEX.md); the new application does not run them.

## Run

Python 3.10+ and Tkinter are required. From this folder:

```sh
python -m venv .venv
```

Activate the environment, then install:

```sh
python -m pip install ".[capture]"
python run.py
```

Windows activation: `.venv\Scripts\activate`. macOS/Linux activation:
`source .venv/bin/activate`. On some Linux distributions Tkinter is supplied by
the separate `python3-tk` system package.

**On this workstation the environment is already installed:** double-click
`Launch.cmd`, or run `python run.py`. The launcher prefers the local `.venv`.
For generation alone, `pip install .` omits the camera dependency.
Optional dictionary-based wrap-hyphen repair: `pip install ".[dictionary]"`.

ADB and Tesseract are separate executables. Install them or select existing paths
in the UI. Install the required Tesseract language models, such as `ukr` and `eng`.
On Windows, Tesseract can instead be bundled into the app from an existing install:
`python -m opticalbook bundle-tesseract "C:\Program Files\Tesseract-OCR"`
([details](docs/GENERATION.md#tesseract)).
The tool never downloads models, opens a camera, or sends phone input on startup.

## First use

1. Create a project. Select camera index/backend, resolution and controls.
2. Start preview, mark the text area's four corners in book reading order (optionally
   also the phone screen and page indicator), then inspect **Test crop** at full
   resolution. Use a firm stand and readable text.
3. Choose verified ADB capture, optical capture (ADB turns pages, the page indicator
   is read from the photo with Tesseract) or one-photo-at-a-time manual capture. Test three
   pages before a full run. [Capture instructions](docs/CAPTURE.md)
4. In **Book generation**, set title, author, languages and Tesseract paths; run OCR.
5. Review pages beside their source images. A blank line means a paragraph;
   an explicit Enter means a hard line break; visual wrapping is automatic.
6. Export EPUB. Use **Label as sample** for an incomplete scan and **Keep page as
   image** for maps or pages that should retain their visual layout.
   [Generation instructions](docs/GENERATION.md)

For an existing image folder, start with **Import images**. ADB and a webcam are
not needed for OCR, review, or export.

## Command line and checks

```sh
python -m opticalbook --help
python -m opticalbook doctor
python -m unittest discover -s tests -v
```

The application targets Windows, macOS and Linux. Windows UI and a real Tesseract
integration were checked here; the new camera backend and other operating systems
still need hardware testing. See [validation notes](docs/VALIDATION.md).
