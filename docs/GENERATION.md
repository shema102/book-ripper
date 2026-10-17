# Book generation

No webcam or ADB is required. Open a capture folder containing `capture.json`, or
choose **Import images** to create a new project from an image folder. Natural
filename order puts `page2` before `page10`; confirm the resulting page order. One
image means one page. Split multipage TIFFs into separate images before importing.

## Tesseract

Install a Tesseract 5.x executable separately. Set its path and, if needed, the folder
containing `.traineddata` models.

A bundled copy is optional. On Windows, copy the runtime out of an existing Tesseract
install into the app once:

```sh
python -m opticalbook bundle-tesseract "C:\Program Files\Tesseract-OCR" --languages eng ukr
```

This copies `tesseract.exe`, only the DLLs it actually imports (training tools and
their dependencies are left out), the chosen models, Tesseract's configs and licence
notices into `opticalbook/bundled/tesseract/` (about 75 MB for `eng` + `ukr`), with a
`BUNDLE.json` of file checksums. Nothing is downloaded. Whenever a Tesseract setting is
the default `tesseract`, the bundled copy and its tessdata are used; an explicit path
still wins. `doctor` reports which bundle is present. If you redistribute the bundle,
keep the licence notices of Tesseract and the copied libraries with it.

 OCR language names use Tesseract codes (`ukr`,
`eng`, `ukr+eng`), while EPUB language uses a language tag (`uk`, `en`).

Use **Check Tesseract / models** before OCR. Missing models cause an explicit error.
Models are available from the official [tessdata_best](https://github.com/tesseract-ocr/tessdata_best)
and [tessdata_fast](https://github.com/tesseract-ocr/tessdata_fast) repositories.
Best models prioritize recognition quality and take longer. Their LSTM models use
the application's `--oem 1`. See the official [Tesseract installation guide](https://tesseract-ocr.github.io/tessdoc/Installation.html).

Start with PSM 3 for automatic page segmentation or 6 for a simple uniform text
block. PSM 7 is a single line and 11 is sparse text; neither is a universal book-page
setting. A resize factor of 1.5 is a starting point, not a substitute for a sharp
source photograph. [Tesseract command-line documentation](https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html)
describes language, PSM, TSV and other options.

OCR stores raw word-position TSV and a JSON page cache. Resume skips matching
source/settings caches. Stop is checked between pages. Corrected reviews are kept
separately and take precedence when exporting.

## Review structure

Select a page to compare text with its image. The full-resolution source opens in a
scrolling window. The editor automatically wraps to its current width:

- **Blank line:** new paragraph.
- **Enter once:** intentional hard line break, such as a line of verse.
- **Visual wrapping:** no stored break; EPUB text reflows with the reader font.

Set **First paragraph continues previous page** to Yes/No to resolve page-boundary
ambiguity, or leave Auto for conservative geometric inference. A chapter title
starts a navigation section; remove any duplicate title from the body yourself.
For maps, diagrams, decorative pages or failed OCR, choose **Keep page as image**.
Save the page review before moving on. The UI prompts about unsaved page edits.

Automatic OCR/layout is not full proofreading. Check names, punctuation, dialogue,
poetry, large initials, footnotes, short pages and page-to-page continuations.
There is no silently applied correction dictionary for a particular novel.

Without the optional dictionary package, end-of-line hyphens are conservatively
retained. `pip install ".[dictionary]"` enables the optional word-frequency choice;
select its checkbox in the UI. Genuine compound words and uncommon names still
need review. This is not general spelling correction.

## Export

Set title, author and book language. Export a new filename; existing output files
are refused. All page image checksums and sequence numbers are rechecked. Complete
export requires all pages; **Label as sample** permits a clearly marked partial
export. Empty OCR must be corrected or explicitly represented as an image.

Text EPUBs retain paragraphs and hard breaks, chapter navigation, all source-page
anchors and selected image pages. **Image-only EPUB** is the explicit alternative
with no recognized text. The accompanying verification JSON records page counts,
review counts and a SHA-256 output digest. Internal XML checks do not replace
independent standards validation:

```sh
java -jar /path/to/epubcheck.jar /path/to/book.epub
```

Download [EPUBCheck from its official project](https://github.com/w3c/epubcheck).
Actual Kindle import is a separate check.

## CLI

```sh
python -m opticalbook import-images /path/to/numbered-photos projects/imported-book
# Edit projects/imported-book/project.json: Tesseract paths and book settings.
python -m opticalbook doctor --project projects/imported-book
python -m opticalbook ocr --project projects/imported-book
python -m opticalbook build --project projects/imported-book --output my-book.epub
```

The desktop review UI opens the same project/cache files. For a partial sample add
`--sample` to build; for an image-only export add `--images`.
