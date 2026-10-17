"""Illustration regions on a page image: ink that recognized text does not explain.

Runs after OCR, on the source image and its OCR lines; NumPy/Pillow only (no OpenCV on the generation side).
"""
from collections import deque
import statistics
import numpy as np


def dilate(grid, rx, ry):
    """Binary dilation by a (2rx+1)×(2ry+1) rectangle, separable."""
    out = grid.copy()
    for k in range(1, rx+1):
        out[:, k:] |= grid[:, :-k]; out[:, :-k] |= grid[:, k:]
    grid = out.copy()
    for k in range(1, ry+1):
        out[k:] |= grid[:-k]; out[:-k] |= grid[k:]
    return out


def components(grid):
    """4-connected components of a boolean grid -> bounding boxes (x0, y0, x1, y1) in cells, exclusive ends."""
    seen, boxes = np.zeros_like(grid), []
    height, width = grid.shape
    for y, x in zip(*np.nonzero(grid)):
        if seen[y, x]: continue
        seen[y, x] = True; queue = deque([(y, x)]); x0, y0, x1, y1 = x, y, x+1, y+1
        while queue:
            cy, cx = queue.popleft()
            x0, y0, x1, y1 = min(x0, cx), min(y0, cy), max(x1, cx+1), max(y1, cy+1)
            for ny, nx in ((cy-1, cx), (cy+1, cx), (cy, cx-1), (cy, cx+1)):
                if 0 <= ny < height and 0 <= nx < width and grid[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True; queue.append((ny, nx))
        boxes.append((int(x0), int(y0), int(x1), int(y1)))
    return boxes


def area(box): return max(0, box[2]-box[0]) * max(0, box[3]-box[1])


def common(a, b): return area((max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])))


def union(a, b): return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]


def line_box(line): return (line['left'], line['top'], line['right'], line['bottom'])


def text_word(word, height):
    """Confidently recognized, letter-sized words explain their ink; OCR noise read out of a drawing does not.
    A large heading counts when it is read with high confidence."""
    letters = sum(c.isalnum() for c in word['text'])
    return letters and word['confidence'] >= 50 and (word['height'] <= height*2.5 or (word['confidence'] >= 85 and letters >= 2))


def detect(image, lines):
    """-> (figure boxes [left, top, right, bottom] in source pixels sorted top-down, lines not inside a figure).

    Ink is anything that differs from the paper (light or dark theme). Confident words are erased; what remains,
    closed by about half a line height, is a figure when it is at least two text lines tall and wide. Ink with text
    beside it (a drop cap, a marginal ornament) is not a figure: an e-book places figures between paragraphs.
    Lines mostly inside a figure are its labels or OCR noise and leave the text.
    """
    gray = np.asarray(image.convert('L'), dtype=np.int16)
    rows, columns = gray.shape
    paper = int(np.median(gray[::4, ::4]))
    ink = np.abs(gray - paper) > 40
    height = statistics.median(line['bottom']-line['top'] for line in lines) if lines else rows/40
    cell = max(1, round(height/4))
    pad = round(height*.15)
    remaining = ink.copy()
    for line in lines:
        for word in line['words']:
            if text_word(word, height):
                left, top = max(0, int(word['left'])-pad), max(0, int(word['top'])-pad)
                remaining[top:int(word['top']+word['height'])+pad+1, left:int(word['left']+word['width'])+pad+1] = False
    # Coarse grid: a cell counts when a few percent of it is ink, which ignores isolated noise pixels.
    gy, gx = -(-rows//cell), -(-columns//cell)
    padded = np.zeros((gy*cell, gx*cell), bool); padded[:rows, :columns] = remaining
    grid = padded.reshape(gy, cell, gx, cell).mean(axis=(1, 3)) > .04
    closing = max(1, round(height*.5/cell))
    boxes = []
    for x0, y0, x1, y1 in components(dilate(grid, closing, closing)):
        x0, y0, x1, y1 = max(0, x0+closing)*cell, max(0, y0+closing)*cell, min(gx, x1-closing)*cell, min(gy, y1-closing)*cell
        if x1-x0 < height*2 or y1-y0 < height*2: continue
        # Tighten to the actual ink (labels inside included), then leave a small white margin.
        window = ink[y0:y1, x0:x1]
        ys, xs = np.nonzero(window.any(axis=1))[0], np.nonzero(window.any(axis=0))[0]
        if not len(ys): continue
        box = [x0+int(xs[0]), y0+int(ys[0]), x0+int(xs[-1])+1, y0+int(ys[-1])+1]
        beside = [line for line in lines if (line['right'] <= box[0] or line['left'] >= box[2]) and
                  min(line['bottom'], box[3]) - max(line['top'], box[1]) > (line['bottom']-line['top'])*.5 and
                  any(text_word(word, height) for word in line['words'])]
        if not beside: boxes.append(box)
    kept = []
    for line in lines:
        inner = [index for index, box in enumerate(boxes) if common(line_box(line), box) >= area(line_box(line))*.5]
        for index in inner: boxes[index] = union(boxes[index], [int(line['left']), int(line['top']), int(line['right'])+1, int(line['bottom'])+1])
        if not inner: kept.append(line)
    merged = True
    while merged:
        merged = False
        for a in range(len(boxes)):
            for b in range(a+1, len(boxes)):
                if common(boxes[a], boxes[b]):
                    boxes[a] = union(boxes[a], boxes.pop(b)); merged = True; break
            if merged: break
    margin = round(height*.2)
    boxes = [[max(0, l-margin), max(0, t-margin), min(columns, r+margin), min(rows, b+margin)] for l, t, r, b in boxes]
    return sorted(boxes, key=lambda box: (box[1], box[0])), kept


def place(boxes, values):
    """Each figure goes before the first paragraph that starts below its top edge."""
    return [{'box': box, 'at': sum(value['top'] < box[1] for value in values)} for box in boxes]
