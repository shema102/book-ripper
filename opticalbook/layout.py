"""Recover paragraph boundaries from line geometry, not OCR block IDs alone."""
import re
import statistics


def is_heading(text):
    letters = ''.join(c for c in text if c.isalpha())
    return (4 <= len(letters) <= 70
            and sum(c.isupper() for c in letters) / len(letters) >= .85
            and not text.startswith(('ISBN', 'УДК', 'Copyright')))


def layout_metrics(lines, width):
    body = [x for x in lines if len(x['text']) > 15 and not is_heading(x['text'])] or lines
    height = statistics.median(x['bottom'] - x['top'] for x in body)
    # The smallest x coordinate can be a speck or a damaged word. Use a
    # repeated left-edge cluster when the page has enough ordinary text.
    lefts = sorted(x['left'] for x in body if x['left'] < width * .3)
    margin = min(x['left'] for x in body)
    needed = max(2, len(body) * .08)
    for left in lefts:
        cluster = [x for x in lefts if left <= x <= left + height * .35]
        if len(cluster) >= needed:
            margin = statistics.median(cluster)
            break
    steps = [b['top'] - a['top'] for a, b in zip(body, body[1:])
             if height * .6 < b['top'] - a['top'] < height * 2]
    pitch = statistics.median(steps) if steps else height * 1.25
    right = sorted(x['right'] for x in body)[int((len(body)-1) * .8)]
    return {'height': height, 'margin': margin, 'pitch': pitch, 'right': right}


def paragraph_starts(lines, width):
    """One reason per line; None means an automatic screen wrap.

    OCR groups are supporting evidence only. A group change at a full-width
    line followed by an aligned line is not an editorial paragraph boundary.
    """
    if not lines:
        return [], {}
    metrics = layout_metrics(lines, width)
    h, margin, pitch, right = (metrics[k] for k in ('height', 'margin', 'pitch', 'right'))
    reasons = ['page-start']
    # Inside a section's opening run of equally indented lines: the lines beside a drop cap.
    # Tesseract often drops the cap and splits its block there, so a group change is no evidence.
    opening = False
    for previous, line in zip(lines, lines[1:]):
        text = line['text']
        reason = None
        group_changed = line.get('group') != previous.get('group')
        indented = line['left'] > margin + h * .65
        short_previous = previous['right'] < right - max(h * 1.5, width * .06)
        gap = line['top'] - previous['bottom']
        if line.get('continuation'):
            pass
        elif gap > h * .6 and line['top'] - previous['top'] > pitch * 1.5:
            reason = 'vertical-space'
        elif is_heading(text) != is_heading(previous['text']):
            reason = 'heading-transition'
        elif re.match(r'^[—–-]\s', text) and (indented or group_changed or short_previous):
            reason = 'dialogue'
        elif indented:
            hanging = (previous['left'] > margin + h * .65
                       and abs(previous['left'] - line['left']) < h * .35
                       and not short_previous and (opening or not group_changed))
            if not hanging:
                reason = 'indentation'
        elif group_changed and short_previous:
            reason = 'short-line-and-group'
        reasons.append(reason)
        opening = indented and (reason in ('vertical-space', 'heading-transition') or (reason is None and opening))
    return reasons, metrics
