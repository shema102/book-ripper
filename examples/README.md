# Example settings

`project.json` contains generic defaults. Copy it into a new project folder or use
the UI's New project action. Fill in your own device serial, reader package,
camera settings and calibration. No device is selected by default.

ADB and Tesseract values may be executable names on PATH or absolute paths. Relative
executable/model paths are interpreted from the process working directory; prefer
absolute paths when launching the desktop app. The UI's browse buttons select them.

Leaving `tesseract` as the executable uses the bundled copy when one has been made
with `python -m opticalbook bundle-tesseract`, and otherwise the one on PATH.

Set `counter_mode` to `manual`, `total` to the known number of pages, and capture
count to 1 if you want photographs without ADB. Generation can independently import
any naturally ordered folder of page images.
