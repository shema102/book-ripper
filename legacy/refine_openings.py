"""Review chapter initials against photos, then OCR their ordinary text rows."""
from collections import Counter
from pathlib import Path
import json
import re

import numpy as np
from PIL import Image, ImageOps

from ocr_reflow import (ROOT, SOURCE, OCR, read_tsv, recognize_snippet,
                        paragraphs_from_lines, normalize_text, zipf_frequency)
from book_scanner import load_json, write_json

# Visually checked in chapter-initials.png. More entries can be reviewed as scanning finishes.
CHECKED = {11:'К',14:'М',23:'Н',38:'В',49:'Я',65:'Н',71:'П',86:'Н',98:'М',109:'Е',
           131:'Я',145:'С',164:'П',191:'М',206:'Ж',217:'Л',228:'Б',233:'О',242:'Я',263:'В',
           281:'Я',292:'П',309:'М',322:'З',335:'С',350:'Н',363:'П',373:'У',395:'Я',
           378:'Т',411:'С',416:'Б',423:'М',436:'М',447:'Р',458:'Н',471:'В',487:'П',494:'В',511:'В'}
PREFIX = {206:'— ',350:'— ',378:'— ',411:'— ',416:'— ',471:'«'}


def cap_bounds(image, page):
    gray = np.asarray(image.convert('L'))
    y0 = 150 if page == 11 else 580
    y1 = min(image.height, y0+180)
    mask = gray[y0:y1,:min(300,image.width)] < 125
    seen=np.zeros(mask.shape,dtype=bool);found=[]
    for yy,xx in zip(*np.nonzero(mask)):
        if seen[yy,xx]:continue
        stack=[(int(yy),int(xx))];seen[yy,xx]=True;points=[]
        while stack:
            y,x=stack.pop();points.append((x,y))
            for dy,dx in ((-1,0),(1,0),(0,-1),(0,1),(-1,-1),(-1,1),(1,-1),(1,1)):
                ny,nx=y+dy,x+dx
                if 0<=ny<mask.shape[0] and 0<=nx<mask.shape[1] and mask[ny,nx] and not seen[ny,nx]:
                    seen[ny,nx]=True;stack.append((ny,nx))
        xs,ys=zip(*points);box=[min(xs),min(ys)+y0,max(xs)+1,max(ys)+y0+1]
        width,height=box[2]-box[0],box[3]-box[1]
        if 40<=height<=100 and 5<=width<=110 and len(points)>120:
            found.append((height,len(points),box))
    return max(found)[2] if found else None


def refine(page, letter, force=False):
    path=OCR/'pages'/f'{page:05d}.json';record=load_json(path)
    if (not force and (record.get('opening_review') or {}).get('letter')==letter
            and (record.get('opening_review') or {}).get('version')==2):return
    with Image.open(SOURCE/f'pages/{page:05d}.png') as source:image=source.convert('RGB')
    bounds=cap_bounds(image,page)
    if not bounds:raise RuntimeError(f'Cannot locate initial on page {page}')
    lines,_,_=read_tsv(OCR/'raw'/f'{page:05d}.tsv')
    normal=[]
    for line in lines:
        for word in line['words']:
            top=word['top']+8;center=top+word['height']/2
            if bounds[1]-12<=center<=bounds[3]+10 and 10<=word['height']<=38 and word['left']>=bounds[2]-3:
                normal.append((center,word,line))
    clusters=[]
    for center,word,line in sorted(normal,key=lambda x:x[0]):
        if not clusters or center-np.mean([x[0] for x in clusters[-1]])>12:
            clusters.append([])
        clusters[-1].append((center,word,line))
    clusters=[cluster for cluster in clusters if len(cluster)>=1][:2]
    if not clusters:raise RuntimeError(f'Cannot find text rows on page {page}')
    new_lines=[];matched_ids=set();gray=image.convert('L')
    prefix_words=[]
    for line in lines:
        for word in line['words']:
            center=word['top']+8+word['height']/2
            if word['left']<bounds[0] and bounds[1]-10<=center<=bounds[3] and re.fullmatch(r'[—–\-«“„]+',word['text']):
                prefix_words.append(word)
    prefix=''.join(w['text'] for w in sorted(prefix_words,key=lambda w:w['left']))
    prefix=prefix.replace('-','—').replace('–','—')
    if prefix.endswith('—'):prefix+=' '
    prefix=PREFIX.get(page,prefix)
    for index,cluster in enumerate(clusters):
        words=[item[1] for item in cluster]
        top=min(w['top']+8 for w in words);bottom=max(w['top']+8+w['height'] for w in words)
        crop=gray.crop((bounds[2]+2,max(0,int(top)-3),image.width-10,min(image.height,int(bottom)+3)))
        crop=ImageOps.expand(ImageOps.autocontrast(crop,cutoff=.5),15,fill='white')
        crop=crop.resize((crop.width*3,crop.height*3),Image.Resampling.LANCZOS)
        text=normalize_text(recognize_snippet(crop,page,f'review-line-{index}',7))
        if not text:raise RuntimeError(f'Empty reviewed opening line on page {page}')
        original_row=normalize_text(' '.join(w['text'] for w in sorted(words,key=lambda w:w['left'])))
        if (max(map(len,text.split()),default=0)>25 or len(text.split())<len(original_row.split())*.6):
            text=original_row
        group=Counter(tuple(item[2]['group']) for item in cluster).most_common(1)[0][0]
        matched_ids.update(id(item[2]) for item in cluster)
        if index==0:
            if prefix:
                text=re.sub(r'^[—–\-«“„]+\s*','',text)
            tail=text[0].lower()+text[1:]
            word=re.match(r"[А-Яа-яІіЇїЄєҐґ'’]+",tail)
            separate=bool(word and letter in 'ЯІАУВО' and zipf_frequency(word.group(),'uk')>=2
                          and zipf_frequency(letter+word.group(),'uk')<1)
            text=prefix+letter+(' ' if separate else '')+tail
        node={'group':list(group),'left':bounds[0] if index==0 else bounds[2]+3,
              'right':image.width-10,'top':top,'bottom':bottom,'text':text,
              'confidence':float(np.mean([w['confidence'] for w in words]))}
        if index and group==tuple(new_lines[0]['group']):node['continuation']=True
        new_lines.append(node)
    kept=[]
    for line in lines:
        if id(line) in matched_ids:continue
        if page!=11 and line['bottom']<430:continue  # residual ink from the chapter ornament
        if (line['left']<bounds[2] and line['top']<bounds[3]+5 and line['bottom']>bounds[1]-5
                and len(line['text'])<=5):continue
        kept.append(line)
    kept.extend(new_lines);kept.sort(key=lambda line:line['top'])
    backup=path.with_suffix('.before-opening-review.bak')
    if not backup.exists():write_json(backup,record)
    record['paragraphs']=paragraphs_from_lines(kept,image.width)
    record['lines']=kept
    record['opening_review']={'version':2,'letter':letter,'bounds':bounds,'prefix':prefix,
                              'text_rows':[line['text'] for line in new_lines],
                              'method':'Initial visually checked against source photo; rows recognized separately'}
    record['dropcap_repair']={'letter':letter,'bounds':bounds,'reviewed':True}
    if record.get('illustration') and record['illustration'][3]<425:
        record['illustration'][3]=425
    write_json(path,record)
    print('Reviewed opening',page,flush=True)


if __name__=='__main__':
    additional=OCR/'initials-reviewed.json'
    checked=dict(CHECKED)
    if additional.exists():checked.update({int(k):v for k,v in load_json(additional).items()})
    for page,letter in sorted(checked.items()):
        if (OCR/'pages'/f'{page:05d}.json').exists():refine(page,letter)
