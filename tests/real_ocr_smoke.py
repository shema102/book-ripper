"""Optional real Tesseract integration check, using generated text only."""
import argparse
from pathlib import Path
import uuid
from PIL import Image, ImageDraw, ImageFont
from opticalbook.common import defaults, read_json, write_json
from opticalbook.generation import import_images, recognize, build_epub
from opticalbook.indicator import read_indicator

parser=argparse.ArgumentParser()
parser.add_argument('--tesseract',required=True)
parser.add_argument('--tessdata',default='')
args=parser.parse_args()
base=Path(__file__).resolve().parents[1]/'.test-work'
source=base/('real-ocr-input-'+uuid.uuid4().hex[:8])
source.mkdir(parents=True)
font=ImageFont.load_default(size=34)
for page in (1,2):
    image=Image.new('RGB',(1000,1100),'white')
    draw=ImageDraw.Draw(image)
    for x,y,text in [(40,70,'Optical book conversion'),(85,150,'This is the first paragraph.'),
                     (40,200,'It continues on another screen line.'),
                     (85,290,'A second paragraph starts here.'),
                     (40,340,f'This is generated test page {page}.')]:
        draw.text((x,y),text,font=font,fill='black')
    image.save(source/f'{page}.png')
project=base/('real-ocr-project-'+uuid.uuid4().hex[:8])
import_images(source,project)
config=defaults()
config['book'].update(title='Generated OCR integration test',author='Test author',language='en',ocr_language='eng',tesseract=args.tesseract,tessdata=args.tessdata,psm=6)
write_json(project/'project.json',config)
assert recognize(project,config['book'])==2
record=read_json(project/'ocr/pages/00001.json')
assert 'Optical book conversion' in ' '.join(p['text'] for p in record['paragraphs'])
assert len(record['paragraphs'])>=2
output=project/'integration.epub'
report=build_epub(project,output,config['book'])
# Capture-side page indicator: a 12 px dark-theme counter as a webcam frame would show it.
frame=Image.new('RGB',(400,120),'#111')
counter=Image.new('RGB',(600,120),'#111');ImageDraw.Draw(counter).text((20,10),'48 / 265',font=ImageFont.load_default(size=90),fill='#ddd')
frame.paste(counter.resize((80,16),Image.Resampling.LANCZOS),(100,50))
capture=dict(config['capture'],tesseract=args.tesseract,tessdata=args.tessdata,indicator_corners=[[96,46],[184,46],[184,70],[96,70]])
indicator=read_indicator(frame,capture)
assert (indicator['page'],indicator['total'])==(48,265),indicator
write_json(base/'real-ocr-state.json',{'project':str(project),'epub':str(output),'report':report,'indicator':indicator})
print(output)
