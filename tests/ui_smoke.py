"""Visible GUI smoke test using generated pages; never opens a camera or ADB."""
from pathlib import Path
import sys
import time
import tkinter as tk
import uuid
from PIL import Image, ImageDraw, ImageFont
from opticalbook.common import defaults, read_json, write_json
from opticalbook.generation import import_images, save_review
from opticalbook.ui import App

base=Path(__file__).resolve().parents[1]/'.test-work'
base.mkdir(exist_ok=True)
source=base/('ui-input-'+uuid.uuid4().hex[:8])
source.mkdir()
image=Image.new('RGB',(600,900),'white')
draw=ImageDraw.Draw(image)
font=ImageFont.load_default(size=27)
for line,text in enumerate(['A page for interface testing.','','This is one paragraph. It wraps','naturally when the reader is resized.','','A new paragraph starts here.']):
    draw.text((30,80+line*40),text,fill='black',font=font)
image.save(source/'page1.png')
project=base/('ui-project-'+uuid.uuid4().hex[:8])
import_images(source,project)
config=defaults()
config['book'].update(title='Interface test',author='Test author',language='en',ocr_language='eng')
write_json(project/'project.json',config)
entry=read_json(project/'capture.json')['pages'][0]
save_review(project,entry,'A page for interface testing.\n\nThis is one paragraph. It wraps naturally when the reader is resized.\n\nA new paragraph starts here.')
root=tk.Tk()
app=App(root,project)
root.title('Optical Book Tool — UI test (no camera or ADB)')
root.update()
app.collect_settings()
app.tabs.select(app.book_tab)
app.pages.selection_set('1')
root.update()
assert 'A page for interface' in app.editor.get('1.0','end')
assert app.editor.winfo_width()>300 and app.editor.winfo_height()>50
assert app.review_canvas.winfo_height()>=190
app.save_page_review()
app.work(lambda:'Background job and UI event queue verified.')
def finish():
    assert not app.busy
    write_json(base/'ui-state.json',{'handle':root.winfo_id(),'title':root.title(),'project':str(project),'width':root.winfo_width(),'height':root.winfo_height(),'editor_width':app.editor.winfo_width(),'editor_height':app.editor.winfo_height(),'review_ok':True,'job_ok':True})
root.after(800,finish)
if 'capture' in sys.argv:
    app.tabs.select(app.capture_tab)
root.after(30000,root.destroy)
root.mainloop()
