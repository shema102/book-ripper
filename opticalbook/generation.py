"""Image folders / capture manifests -> cached local OCR -> reviewed EPUB."""
from __future__ import annotations
from collections import OrderedDict
import csv
from datetime import datetime, timezone
import io
import os
from pathlib import Path
import re
import statistics
import threading
import uuid
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile
from PIL import Image, ImageOps
from .capture import validate_manifest
from .common import digest, fingerprint, inside, read_json, run, tesseract, write_json, locked
from .layout import paragraph_starts

OCR_VERSION = "geometry-tsv-v3"  # v2: drop-cap lines stay one paragraph; v3: Latin look-alikes re-read, free hyphens are dashes


def import_images(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    files = [p for p in source.iterdir() if p.is_file() and p.suffix.lower() in ('.png', '.jpg', '.jpeg', '.tif', '.tiff', '.bmp')]
    def natural(path):
        return [int(x) if x.isdecimal() else x.lower() for x in re.split(r'(\d+)', path.name)]
    files.sort(key=natural)
    if not files:
        raise ValueError("No supported page images in that folder.")
    destination.mkdir(exist_ok=False, parents=True)
    (destination / 'pages').mkdir()
    entries = []
    for number, path in enumerate(files, 1):
        target = destination / 'pages' / f'{number:05d}.png'
        with Image.open(path) as image:
            image = ImageOps.exif_transpose(image).convert('RGB')
            image.save(target)
            entries.append({'page': number, 'image': f'pages/{number:05d}.png',
                            'sha256': digest(target), 'size': list(image.size), 'source_filename': path.name})
    write_json(destination / 'capture.json', {'version': 1, 'total': len(entries),
                                             'counter_verified': False, 'pages': entries})
    return len(entries)


def join_text(left, right, config):
    left, right = left.rstrip(), right.lstrip()
    if left.endswith('-') and right[:1].islower():
        before = re.search(r"([^\W\d_]+)-$", left, re.UNICODE)
        after = re.match(r"[^\W\d_]+", right, re.UNICODE)
        if config.get('dictionary_hyphens') and before and after:
            try:
                from wordfreq import zipf_frequency
            except ImportError as error:
                raise RuntimeError('Install the dictionary extra: pip install ".[dictionary]"') from error
            language = config['language']
            combined = before.group(1) + after.group()
            compound = before.group(1) + '-' + after.group()
            if zipf_frequency(combined, language) >= max(1.5, zipf_frequency(compound, language)+.3):
                return left[:-1] + right
        # Without lexical evidence, retain the hyphen but remove the screen break.
        return left + right
    return left + ' ' + right


def tsv_words(text, scale):
    """Recognized words in source pixels, each with its Tesseract (block, paragraph, line) key."""
    words = []
    for row in csv.DictReader(io.StringIO(text), delimiter='	', quoting=csv.QUOTE_NONE):
        value = (row.get('text') or '').strip()
        if row.get('level') != '5' or not value:
            continue
        words.append({'key': tuple(int(row[k]) for k in ('block_num', 'par_num', 'line_num')), 'text': value,
                      'confidence': float(row['conf']), **{k: float(row[k])/scale for k in ('left', 'top', 'width', 'height')}})
    return words


def group_lines(words):
    groups = OrderedDict()
    for word in words:
        groups.setdefault(word['key'], []).append({k: v for k, v in word.items() if k != 'key'})
    lines = []
    for key, words in groups.items():
        lines.append({'group': list(key[:2]), 'left': min(x['left'] for x in words),
                      'right': max(x['left']+x['width'] for x in words),
                      'top': min(x['top'] for x in words),
                      'bottom': max(x['top']+x['height'] for x in words),
                      'text': ' '.join(x['text'] for x in words),
                      'confidence': statistics.mean(x['confidence'] for x in words), 'words': words})
    return sorted(lines, key=lambda line: (line['top'], line['left']))


# Latin letters that Cyrillic (or Greek) glyphs are read as when eng is among the models — В→B, Н→H, г→r, и→u,
# п→n, т→t … — and symbols only eng outputs. Words made only of these are suspects on a mostly non-Latin page;
# a real Latin word nearly always has a letter without a twin (h, l, S, N …) and is left alone.
TWINS, ENG_ONLY = set('AaBbCcEeHIiKkMmOoPpTtXxYyrun'), set('¢£©®€§')


def suspect(text):
    latin = [c for c in text if (c.isascii() and c.isalpha()) or c in ENG_ONLY]
    return bool(latin) and all(c in TWINS or c in ENG_ONLY for c in latin)


def overlap(a, b):
    width = min(a['left']+a['width'], b['left']+b['width']) - max(a['left'], b['left'])
    height = min(a['top']+a['height'], b['top']+b['height']) - max(a['top'], b['top'])
    common = max(0, width) * max(0, height)
    return common / (a['width']*a['height'] + b['width']*b['height'] - common or 1)


def reread_lookalikes(words, other):
    """Replace suspect words by the same-position reading from the models without eng (other: words of that pass)."""
    for word in words:
        if not suspect(word['text']):
            continue
        best = max(other, key=lambda candidate: overlap(word, candidate), default=None)
        if best and overlap(word, best) >= .5 and not any(c.isascii() and c.isalpha() for c in best['text']) and best['text'] != word['text']:
            word['ocr_text'], word['text'], word['confidence'] = word['text'], best['text'], best['confidence']


def paragraphs(lines, width, config):
    starts, metrics = paragraph_starts(lines, width)
    groups = []
    for line, boundary in zip(lines, starts):
        if boundary or not groups:
            groups.append([])
        groups[-1].append(line)
    result = []
    for group in groups:
        text = group[0]['text']
        for line in group[1:]:
            text = join_text(text, line['text'], config)
        result.append({'text': text, 'first_indent': group[0]['left'] > metrics['margin']+metrics['height']*.65,
                       'top': group[0]['top'], 'bottom': group[-1]['bottom'],
                       'last_line_full': group[-1]['right'] >= metrics['right']-width*.04})
    return result


def ocr_page(image, config, command, tessdata):
    """Tesseract TSV of one page image -> (raw TSV, lines in source pixels). Shared by recognize and the OCR test."""
    scale = float(config['scale'])
    gray = ImageOps.autocontrast(image.convert('L'), cutoff=.5)
    buffer = io.BytesIO()
    gray.resize((round(image.width*scale), round(image.height*scale)), Image.Resampling.LANCZOS).save(buffer, 'PNG')
    def tsv(languages):
        arguments = [command, 'stdin', 'stdout', '-l', languages, '--oem', '1', '--psm', str(config['psm'])]
        if tessdata:
            arguments += ['--tessdata-dir', tessdata]
        return run(arguments+['tsv'], timeout=120, input=buffer.getvalue(), env=dict(os.environ, OMP_THREAD_LIMIT='1')).decode('utf-8', 'replace')
    raw = tsv(config['ocr_language'])
    words = tsv_words(raw, scale)
    # With eng beside another script's model, glyphs shared by both alphabets often come out Latin (НЕ → HE).
    models = config['ocr_language'].split('+')
    letters = [c for word in words for c in word['text'] if c.isalpha()]
    if 'eng' in models and len(models) > 1 and sum(not c.isascii() for c in letters) > len(letters)/2             and any(suspect(word['text']) for word in words):
        reread_lookalikes(words, tsv_words(tsv('+'.join(model for model in models if model != 'eng')), scale))
    for word in words:
        if word['text'] in ('-', '--'):
            word['text'] = '—'  # a free-standing hyphen is a dash; left as '-', a line-final one would glue to the next line
    return raw, group_lines(words)


def ocr_test(image, config):
    """One-off OCR of an image with the book settings, exactly as recognize would do it; nothing is cached or written."""
    if not .5 <= float(config['scale']) <= 4 or int(config['psm']) not in range(14):
        raise ValueError('OCR scale must be 0.5–4 and PSM must be 0–13.')
    _, lines = ocr_page(image, config, *tesseract(config))
    return paragraphs(lines, image.width, config), lines


def tesseract_info(config):
    command, tessdata = tesseract(config)
    version = run([command, '--version']).decode('utf-8', 'replace').splitlines()[0]
    arguments = [command] + (['--tessdata-dir', tessdata] if tessdata else [])
    languages = run(arguments+['--list-langs']).decode('utf-8', 'replace').strip()
    return version, languages


@locked('generation')
def recognize(folder, config, stop=None, log=print):
    folder = Path(folder)
    manifest = validate_manifest(folder, complete=False)
    stop = stop or threading.Event()
    command, tessdata = tesseract(config)
    version, languages = tesseract_info(config)
    available = set(languages.splitlines()[1:])
    missing = set(config['ocr_language'].split('+')) - available
    if missing:
        raise ValueError('Missing Tesseract models: ' + ', '.join(sorted(missing)))
    scale = float(config['scale'])
    if not .5 <= scale <= 4 or int(config['psm']) not in range(14):
        raise ValueError('OCR scale must be 0.5–4 and PSM must be 0–13.')
    ocr_settings = {key: config[key] for key in ('tesseract','tessdata','ocr_language','psm','scale','dictionary_hyphens')}
    if config.get('dictionary_hyphens'):
        ocr_settings['language'] = config['language']
    model_directory = tessdata
    if not model_directory:
        match = re.search(r'"([^"]+)"', languages.splitlines()[0])
        model_directory = match.group(1) if match else ''
    models = {}
    if model_directory:
        for language in config['ocr_language'].split('+'):
            model = Path(model_directory) / (language + '.traineddata')
            if model.exists(): models[language] = digest(model)
    settings = fingerprint({'version': OCR_VERSION, 'engine': version, 'settings': ocr_settings, 'models': models})
    done = 0
    for entry in manifest['pages']:
        if stop.is_set():
            break
        number = entry['page']
        target = folder / 'ocr' / 'pages' / f'{number:05d}.json'
        if target.exists():
            cache = read_json(target)
            if cache.get('source_sha256') == entry['sha256'] and cache.get('settings_sha256') == settings:
                done += 1
                log(f'OCR cached: page {number}.')
                continue
        with Image.open(inside(folder, entry['image'])) as original:
            width = original.width
            raw, lines = ocr_page(original, config, command, tessdata)
        values = paragraphs(lines, width, config)
        record = {'page': number, 'source_sha256': entry['sha256'], 'settings_sha256': settings,
                  'engine': version, 'paragraphs': values, 'lines': lines,
                  'mean_confidence': statistics.mean(x['confidence'] for x in lines) if lines else None,
                  'needs_review': not lines or any(x['confidence'] < 70 for x in lines)}
        raw_path = folder / 'ocr' / 'raw' / f'{number:05d}.tsv'
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(raw, encoding='utf-8')
        write_json(target, record)
        done += 1
        log(f'OCR page {number}/{manifest["total"]}: {len(values)} paragraphs' + ('; REVIEW suggested.' if record['needs_review'] else '.'))
    return done


def page_record(folder, entry):
    folder = Path(folder)
    edit_path = folder / 'ocr' / 'edits' / f'{entry["page"]:05d}.json'
    cache_path = folder / 'ocr' / 'pages' / f'{entry["page"]:05d}.json'
    if edit_path.exists():
        record = read_json(edit_path)
    elif cache_path.exists():
        record = read_json(cache_path)
    else:
        raise ValueError(f'No OCR for page {entry["page"]}. Run OCR or explicitly mark this page as an image in Review.')
    if record['source_sha256'] != entry['sha256']:
        raise ValueError(f'Page {entry["page"]} OCR/review belongs to another source image.')
    return record


def save_review(folder, entry, text, chapter='', image_only=False, continuation=None):
    # Tk's visual word wrapping inserts no newline. Explicit Enter presses
    # therefore remain hard line breaks; blank lines separate paragraphs.
    values = ['\n'.join(' '.join(line.split()) for line in value.splitlines())
              for value in re.split(r'\n\s*\n', text.strip()) if value.strip()]
    if not values and not image_only:
        raise ValueError('Empty text needs an explicit image-page selection; no source page is silently dropped.')
    record = {'page': entry['page'], 'source_sha256': entry['sha256'], 'reviewed': True,
              'paragraphs': [{'text': value, 'first_indent': True, 'last_line_full': False} for value in values],
              'chapter': chapter.strip(), 'image_only': bool(image_only), 'continuation': continuation}
    write_json(Path(folder) / 'ocr' / 'edits' / f'{entry["page"]:05d}.json', record)
    return record


@locked('generation')
def build_epub(folder, output, config, partial=False, image_only=False):
    folder, output = Path(folder), Path(output)
    manifest = validate_manifest(folder, complete=not partial)
    if output.exists():
        raise FileExistsError(f'Output already exists: {output}. Choose another filename.')
    if not config['title'].strip():
        raise ValueError('Enter a book title.')
    title = config['title'] + (' (sample)' if partial else '')
    language = config['language'].strip()
    if not re.fullmatch(r'[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*', language):
        raise ValueError('Book language must be a language tag, for example uk or en-US.')
    sections = [{'title': 'Start', 'nodes': []}]
    assets, links, joins = [], [], []
    last = None
    reviewed = 0
    def body_text(text):
        return escape(text).replace('\n','<br/>')
    for entry in manifest['pages']:
        page = entry['page']
        record = {'image_only': True} if image_only else page_record(folder, entry)
        reviewed += bool(record.get('reviewed'))
        if record.get('chapter'):
            chapter_section = {'title': record['chapter'], 'nodes': [('raw', '<h1>'+escape(record['chapter'])+'</h1>')]}
            if len(sections) == 1 and not sections[0]['nodes']:
                sections[0] = chapter_section
            else:
                sections.append(chapter_section)
            last = None
        section = sections[-1]
        links.append((page, len(sections)-1))
        anchor = f'<span id="page-{page}" epub:type="pagebreak" role="doc-pagebreak" aria-label="{page}"/>'
        if record.get('image_only'):
            name = f'page-{page:05d}.png'
            assets.append((name, inside(folder, entry['image'])))
            section['nodes'].append(('raw', anchor+f'<img src="images/{name}" alt="Source page {page}"/>'))
            last = None
            continue
        values = record.get('paragraphs', [])
        if not values or not any(value['text'].strip() for value in values):
            raise ValueError(f'Page {page} has empty OCR. Correct it or mark it as an image in Review.')
        for index, value in enumerate(values):
            text = value['text'].strip()
            continuation = record.get('continuation')
            auto_join = not value.get('first_indent', True) and last and last['full']
            merge = (index == 0 and last and last['page'] == page-1 and
                     (continuation is True or (continuation is None and auto_join))
                     and not re.match(r'^[—–-]\s', text))
            if merge:
                joined = join_text(last['text'], text, config)
                left_length = len(last['text'])
                if last['text'].endswith('-') and not joined.startswith(last['text']):
                    last['html'] = last['html'][:-1]
                    left_length -= 1
                last['html'] += anchor + body_text(joined[left_length:])
                last['text'] = joined
                last['page'] = page
                last['full'] = value.get('last_line_full', False)
                joins.append(page)
            else:
                node = {'html': anchor + body_text(text), 'text': text, 'page': page,
                        'full': value.get('last_line_full', False)}
                section['nodes'].append(('p', node))
                last = node
            anchor = ''
    uid = 'urn:uuid:' + str(uuid.uuid4())
    modified = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    temp = output.with_name(output.name + '.' + uuid.uuid4().hex + '.tmp')
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with ZipFile(temp, 'x', compression=ZIP_DEFLATED) as archive:
            archive.writestr('mimetype', 'application/epub+zip', compress_type=ZIP_STORED)
            archive.writestr('META-INF/container.xml', '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0"><rootfiles><rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
            archive.writestr('EPUB/style.css', 'body{font-family:serif;line-height:1.4;margin:0 4%}p{margin:0;text-indent:1.2em}h1{text-align:center;margin:1.5em 0}img{display:block;max-width:100%;height:auto;margin:auto}[epub\\:type="pagebreak"]{font-size:0;line-height:0}')
            items = ['<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>', '<item id="css" href="style.css" media-type="text/css"/>']
            spine, toc = [], []
            def xhtml(name, body):
                return f'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="{escape(language)}" xml:lang="{escape(language)}"><head><title>{escape(name)}</title><link rel="stylesheet" href="style.css" type="text/css"/></head><body>{body}</body></html>'
            for index, section in enumerate(sections):
                name = f'section-{index:03d}.xhtml'
                body = ''.join('<p>'+value['html']+'</p>' if kind == 'p' else value for kind, value in section['nodes'])
                archive.writestr('EPUB/'+name, xhtml(section['title'], body))
                items.append(f'<item id="s{index}" href="{name}" media-type="application/xhtml+xml"/>')
                spine.append(f'<itemref idref="s{index}"/>')
                toc.append(f'<li><a href="{name}">{escape(section["title"])}</a></li>')
            for index, (name, path) in enumerate(assets):
                # Re-encode to PNG because imported manifests may point to JPEG.
                with Image.open(path) as image:
                    buffer = io.BytesIO()
                    image.convert('RGB').save(buffer, format='PNG')
                archive.writestr('EPUB/images/'+name, buffer.getvalue())
                items.append(f'<item id="i{index}" href="images/{name}" media-type="image/png"/>')
            page_nav = ''.join(f'<li><a href="section-{section:03d}.xhtml#page-{page}">{page}</a></li>' for page, section in links)
            archive.writestr('EPUB/nav.xhtml', xhtml('Contents', '<nav epub:type="toc"><h1>Contents</h1><ol>'+''.join(toc)+'</ol></nav><nav epub:type="page-list" hidden="hidden"><h2>Source pages</h2><ol>'+page_nav+'</ol></nav>'))
            creator = '<dc:creator>'+escape(config['author'])+'</dc:creator>' if config['author'].strip() else ''
            archive.writestr('EPUB/package.opf', f'<?xml version="1.0" encoding="utf-8"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="book-id">{uid}</dc:identifier><dc:title>{escape(title)}</dc:title>{creator}<dc:language>{escape(language)}</dc:language><meta property="dcterms:modified">{modified}</meta></metadata><manifest>{"".join(items)}</manifest><spine>{"".join(spine)}</spine></package>')
        # Parse every XML artifact before exposing the finished file.
        with ZipFile(temp) as archive:
            for name in archive.namelist():
                if name.endswith(('.xhtml', '.opf', '.xml')):
                    ET.fromstring(archive.read(name))
        if output.exists():
            raise FileExistsError(output)
        # Exclusively reserve the destination, then atomically publish the
        # already validated ZIP. This also works on filesystems without links.
        # A crash between these operations leaves an obvious zero-byte orphan.
        with output.open('xb'):
            pass
        os.replace(temp, output)
    finally:
        temp.unlink(missing_ok=True)
    report = {'pages': len(manifest['pages']), 'total': manifest['total'], 'partial': partial,
              'sections': len(sections), 'image_pages': len(assets), 'reviewed_pages': reviewed,
              'paragraphs': sum(kind=='p' for section in sections for kind, _ in section['nodes']),
              'cross_page_joins': joins, 'sha256': digest(output), 'output': str(output)}
    write_json(output.with_suffix('.verification.json'), report)
    return report
