"""Conservatively merge proven OCR block splits in a separate review copy."""
import argparse
import json
from pathlib import Path
import re
import shutil

from book_scanner import load_json, write_json
from ocr_reflow import joining_parts
from paragraph_layout import paragraph_starts


def repair_record(record, width=680):
    # Preserve manually reconstructed front matter and chapter openings.
    if record['page'] <= 13 or record['page'] >= 674 or record.get('illustration'):
        return record, []
    lines = record.get('lines', [])
    starts, metrics = paragraph_starts(lines, width)
    paragraphs, changes = [], []
    for original in record['paragraphs']:
        value = dict(original)
        candidates = [(abs(x['top'] - value['top']), i) for i, x in enumerate(lines)]
        distance, index = min(candidates, default=(999, 0))
        # Require physical evidence on both sides of the old boundary. Do not
        # alter letters or infer a join merely because the next word is lowercase.
        merge = (paragraphs and index > 0 and distance < 3 and starts[index] is None
                 and not value.get('first_indent') and not value.get('heading')
                 and not paragraphs[-1].get('heading')
                 and not re.match(r'^[—–-]\s', value['text'])
                 and abs(paragraphs[-1]['bottom'] - lines[index-1]['bottom']) < 3
                 and lines[index-1]['right'] >= metrics['right'] - width * .04)
        if merge:
            before = paragraphs[-1]
            changes.append({'page': record['page'], 'top': value['top'],
                            'left_text': before['text'][-80:], 'right_text': value['text'][:80]})
            left, gap, right = joining_parts(before['text'], value['text'])
            before['text'] = left + gap + right
            before['bottom'] = value['bottom']
        else:
            paragraphs.append(value)
    return dict(record, paragraphs=paragraphs, paragraph_layout='geometry-review-v1'), changes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(exist_ok=False)
    (args.output / 'pages').mkdir()
    changes = []
    for source in sorted((args.source / 'pages').glob('*.json')):
        value, page_changes = repair_record(load_json(source))
        write_json(args.output / 'pages' / source.name, value)
        changes.extend(page_changes)
    for source in args.source.glob('*.json'):
        shutil.copy2(source, args.output / source.name)
    write_json(args.output / 'paragraph-changes.json', changes)
    print(json.dumps({'merged_false_breaks': len(changes), 'pages_changed': len({x['page'] for x in changes})}))


if __name__ == '__main__':
    main()
