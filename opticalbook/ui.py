"""Tkinter desktop shell. Camera and long-running jobs never run on Tk's thread."""
from __future__ import annotations
import copy
from pathlib import Path
import queue
import re
import sys
import threading
import time
import tkinter as tk
import numpy as np
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageTk
from . import __version__
from . import android, generation
from .capture import capture
from .common import defaults, load_project, read_json, write_json, inside, fingerprint
from .cameras import cameras
from .imaging import Camera, PEAKING, REGIONS, exposure_raw, exposure_seconds, from_screen, gesture_from, parse_shutter, peaking, quad, rectify, rotate_point, sharpness, shutter_text, to_screen
from .indicator import prepare, read_indicator

# Preview rotation in clockwise quarter turns -> PIL transpose (PIL's ROTATE_* turn counter-clockwise).
TURNS = {1: Image.Transpose.ROTATE_270, 2: Image.Transpose.ROTATE_180, 3: Image.Transpose.ROTATE_90}
MODE_NAMES = {'Camera default': None, 'Automatic': True, 'Manual': False}
CAMERA_MODES = [('Focus', 'autofocus', 'focus'), ('Exposure', 'auto_exposure', 'exposure'), ('White balance', 'auto_white_balance', 'white_balance')]
MODE_KEYS = {mode for _,mode,_ in CAMERA_MODES}
# Nudge buttons: (small step, large step, lowest value). Exposure steps are EV: x2 / x4 the shutter time.
NUDGE = {'focus': (5, 25, 0), 'exposure': (1, 2, None), 'white_balance': (100, 500, 0)}
# Typical ranges only: OpenCV cannot ask the driver for a control's limits. Keyed by backend; '' = any.
CAMERA_HINTS = {
    'focus': {'': 'Usually 0–255, often in steps of 5. Which end focuses close differs per camera: try a few values and compare letters in Test crop.'},
    'exposure': {'': 'Shutter time such as 1/30, 1/60, 1/125 (60 also means 1/60; 2s = two seconds). This backend only takes powers of two: 1/60 becomes 1/64. + / − = 1 EV (double / half the light), ++ / −− = 2 EV. Slower than one frame (1/30 at 30 FPS) lowers FPS. Diagonal stripes on a phone screen are its flicker: use a long shutter (1/16–1/32) with a dim screen (Phone screen, below), not a short one.',
                 'v4l2': 'Shutter time such as 1/30, 1/60, 1/125 (60 also means 1/60; 2s = two seconds). + / − = 1 EV (double / half the light), ++ / −− = 2 EV. Slower than one frame (1/30 at 30 FPS) lowers FPS. Diagonal stripes on a phone screen are its flicker: use a long shutter (1/16–1/32) with a dim screen (Phone screen, below), not a short one.',
                 'avfoundation': 'macOS cameras usually ignore manual exposure through OpenCV.'},
    'white_balance': {'': 'Colour temperature in kelvin, usually 2800–6500. Phone screens are near 6500. Page looks yellow → lower it; bluish → raise it.'},
    'brightness': {'': 'Image offset, usually 0–255 with 128 ≈ neutral (some drivers −64 … 64). Prefer exposure; leave blank unless needed.'}}
# Calibration target -> (project key, preview colour, marking instructions). Corners go clockwise from top-left in reading order.
CALIBRATION = {
    'Text area': ('corners', '#56e0bb', 'Click the readable text area: top-left → top-right → bottom-right → bottom-left. This becomes the saved page.'),
    'Phone screen': ('screen_corners', '#f0b44c', 'Click the whole lit phone screen: top-left → top-right → bottom-right → bottom-left. Aligns taps and swipes with the photo.'),
    'Page indicator': ('indicator_corners', '#6fa8ff', 'Click around the page indicator (e.g. 12 / 345): top-left → top-right → bottom-right → bottom-left.'),
    'Next-page gesture': ('gesture', '#ff6b8a', 'Click where the next-page tap lands, or the swipe start then its end, inside the marked screen.')}
# OCR test: words below this Tesseract confidence are highlighted for a closer look.
LOW_CONFIDENCE = 80
PHONE_MODES = {
    'verified': 'Reads the counter from the reader\'s accessibility text over ADB before and after every turn. The regex needs two groups: page and total.',
    'optical': 'Reads the calibrated page indicator from the camera image (Page indicator OCR, below); ADB only sends the gestures.',
    'manual': 'Saves one photo per click and never sends ADB input; turn the page yourself.'}


class App:
    def __init__(self, root, project=None):
        self.root = root
        root.title(f'Optical Book Tool {__version__}')
        root.geometry('1220x900')
        root.minsize(1000, 730)
        self.folder = None
        self.config = defaults()
        self.camera = None
        self.releasing = None
        self.busy = False
        self.closing = False
        self.stop = threading.Event()
        self.events = queue.Queue()
        self.variables = {}
        self.regions = {key: [] for key in REGIONS}
        self.marking = None
        self.marked = []
        self.backup = None
        self.rotation = 0
        self.dragging = None
        self.preview_transform = None
        self.preview_key = None
        # Indicator OCR view: inset key, last live reading, and throttling of its background OCR.
        self.indicator_key, self.indicator_reading, self.indicator_busy, self.indicator_next = None, '', False, 0
        self.entry = None
        self.figures = []  # Review: figure boxes of the selected page in source pixels; [figure N] in the text is figures[N-1]
        self.review_initial = None
        self.photo_refs = {}
        self.status = tk.StringVar(value='Create or open a project. No camera or phone is used until you choose an action.')
        bar = ttk.Frame(root, padding=8)
        bar.pack(fill='x')
        self.path = tk.StringVar(value='No project selected')
        ttk.Label(bar, textvariable=self.path).pack(side='left', fill='x', expand=True)
        for label, action in [('New project', self.new_project), ('Open project', self.open_project),
                              ('Import images', self.import_images), ('Save settings', self.save_settings)]:
            ttk.Button(bar, text=label, command=lambda action=action: self.safe(action)).pack(side='left', padx=3)
        self.tabs = ttk.Notebook(root)
        self.tabs.pack(fill='both', expand=True, padx=8)
        self.capture_tab = ttk.Frame(self.tabs, padding=8)
        self.book_tab = ttk.Frame(self.tabs, padding=8)
        self.tabs.add(self.capture_tab, text='1 · Capture')
        self.tabs.add(self.book_tab, text='2 · Book generation')
        self._capture_ui()
        self._book_ui()
        footer = ttk.Frame(root, padding=8)
        footer.pack(fill='x')
        self.stop_button = ttk.Button(footer, text='Stop job', command=self.stop.set, state='disabled')
        self.stop_button.pack(side='right')
        ttk.Label(footer, textvariable=self.status, wraplength=1050).pack(side='left', fill='x', expand=True)
        self.log_box = tk.Text(root, height=5, wrap='word', state='disabled', font=('TkFixedFont', 9))
        self.log_box.pack(fill='x', padx=8, pady=(0, 8))
        self.fill_settings()
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.after(100, self.tick)
        if project:
            self.load_folder(project)

    def field(self, parent, label, key, row, choices=None, width=27):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky='w', pady=3, padx=(0, 7))
        variable = tk.StringVar()
        self.variables[key] = variable
        widget = ttk.Combobox(parent, textvariable=variable, values=choices, width=width) if choices is not None else ttk.Entry(parent, textvariable=variable, width=width)
        widget.grid(row=row, column=1, sticky='ew', pady=3)
        if key in ('capture.adb','capture.tesseract','capture.tessdata','book.tesseract','book.tessdata'):
            def browse():
                path = filedialog.askdirectory(title='Choose tessdata folder',parent=self.root) if key.endswith('.tessdata') else filedialog.askopenfilename(title='Choose executable',parent=self.root)
                if path: variable.set(path)
            ttk.Button(parent,text='…',width=3,command=browse).grid(row=row,column=2,padx=(3,0))
        parent.columnconfigure(1, weight=1)
        return widget

    def hint_backend(self, backend=None):
        """Backend whose value units apply: what 'auto' resolves to on this OS."""
        backend = backend or self.variables['camera.backend'].get()
        return {'win32':'dshow','darwin':'avfoundation'}.get(sys.platform,'v4l2') if backend == 'auto' else backend

    def show_camera_hints(self, *_):
        """Show manual-value rows only in Manual mode, with typical ranges and what the camera reports."""
        backend = self.hint_backend()
        actual = self.camera.actual if self.camera else {}
        def now(*keys):
            found = [f'{key} {shutter_text(exposure_seconds(actual[key],self.camera.backend)) if key=="exposure" else format(actual[key],"g")}'
                     for key in keys if isinstance(actual.get(key),(int,float))]
            return '\nCamera reports: '+', '.join(found) if found else ''
        for label, mode, value in CAMERA_MODES:
            selected = self.variables['camera.'+mode].get()
            for widget in self.manual_widgets[mode]:
                widget.grid() if selected == 'Manual' else widget.grid_remove()
            text = CAMERA_HINTS[value].get(backend, CAMERA_HINTS[value]['']) if selected == 'Manual' else \
                'Not sent: the camera keeps whatever it last used, possibly set by another app.' if selected == 'Camera default' else ''
            self.camera_hints[mode].configure(text=(text+now(mode, value)).strip())
        self.camera_hints['brightness'].configure(text=CAMERA_HINTS['brightness']['']+now('brightness'))

    def show_phone_fields(self, *_):
        """Counter regex only for verified mode; swipe start and duration only for swipes."""
        mode = self.variables['capture.counter_mode'].get()
        for widget in self.regex_widgets: widget.grid() if mode == 'verified' else widget.grid_remove()
        for widget in self.swipe_widgets: widget.grid() if self.variables['gesture.kind'].get() == 'swipe' else widget.grid_remove()
        self.mode_hint.configure(text=PHONE_MODES.get(mode,''))

    def button(self, parent, text, action):
        widget = ttk.Button(parent, text=text, command=lambda: self.safe(action))
        widget.pack(side='left', padx=3, pady=3)
        return widget

    def scrolled(self, notebook, title):
        """A notebook tab whose content scrolls vertically when the window is too short for it."""
        outer = ttk.Frame(notebook)
        notebook.add(outer, text=title)
        canvas = tk.Canvas(outer, highlightthickness=0, borderwidth=0)
        bar = ttk.Scrollbar(outer, orient='vertical', command=canvas.yview)
        inner = ttk.Frame(canvas, padding=8)
        window = canvas.create_window(0, 0, window=inner, anchor='nw')
        inner.bind('<Configure>', lambda event: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda event: canvas.itemconfigure(window, width=event.width))
        canvas.configure(yscrollcommand=bar.set)
        background = ttk.Style().lookup('TFrame', 'background')
        if background: canvas.configure(background=background)
        bar.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)
        # The wheel scrolls whichever tab is under the pointer.
        wheel = lambda event: canvas.yview_scroll(-1 if (event.delta > 0 or event.num == 4) else 1, 'units')
        def enter(_):
            for sequence in ('<MouseWheel>', '<Button-4>', '<Button-5>'): canvas.bind_all(sequence, wheel)
        def leave(_):
            for sequence in ('<MouseWheel>', '<Button-4>', '<Button-5>'): canvas.unbind_all(sequence)
        outer.bind('<Enter>', enter)
        outer.bind('<Leave>', leave)
        return inner

    def nudges(self, parent, row, step):
        """-- / - / + / ++ buttons calling step(direction, large)."""
        frame = ttk.Frame(parent)
        frame.grid(row=row, column=1, sticky='w')
        for text, direction, large in [('--',-1,True),('-',-1,False),('+',1,False),('++',1,True)]:
            ttk.Button(frame, text=text, width=4, command=lambda direction=direction, large=large:
                       self.safe(lambda: step(direction, large))).pack(side='left', padx=(0,2))
        return frame

    def _capture_ui(self):
        left = ttk.Frame(self.capture_tab, width=380)
        left.pack(side='left', fill='y', padx=(0, 12))
        left.pack_propagate(False)
        settings = ttk.Notebook(left)
        settings.pack(fill='both', expand=True)
        # Setup: which camera and phone. Image: what the photo looks like. Scanning: how pages are read and turned.
        setup, image, scan = (self.scrolled(settings, title) for title in ('Setup', 'Image', 'Scanning'))
        hint = lambda parent, text, row, columns=2: ttk.Label(parent, text=text, wraplength=300, foreground='gray35').grid(
            row=row, column=0, columnspan=columns, sticky='w', pady=(4, 0))
        camera, device = (ttk.LabelFrame(setup, text=title, padding=6) for title in ('Camera', 'Phone (ADB)'))
        for frame in (camera, device): frame.pack(fill='x', pady=(0, 6))
        fields = [('Backend', 'backend', ['auto','dshow','msmf','v4l2','avfoundation']), ('Camera', 'source', []),
                  ('Width', 'width', None), ('Height', 'height', None), ('FPS', 'fps', None), ('FourCC (blank = default)', 'fourcc', None)]
        for row, (label, name, choices) in enumerate(fields):
            self.field(camera, label, 'camera.'+name, row, choices, 17)
        self.source_widget = camera.grid_slaves(row=1, column=1)[0]
        self.source_widget.configure(postcommand=lambda: self.safe(self.list_cameras))
        hint(camera, 'These need Close preview, then Start preview. Backend auto = DirectShow on Windows. Open Camera to pick a device; an index, path or URL can also be typed.', len(fields))
        self.field(device, 'ADB executable', 'capture.adb', 0, width=17)
        self.serial_widget = self.field(device, 'Device serial', 'capture.serial', 1, choices=[], width=17)
        ttk.Button(device, text='↻', width=3, command=lambda: self.safe(self.refresh_devices)).grid(row=1, column=2, padx=(3,0))
        hint(device, 'Open the reader on the phone; the app in front at the first ADB read or gesture is treated as the reader, and input stops if another app takes over.', 2, 3)
        controls, screen = (ttk.LabelFrame(image, text=title, padding=6) for title in ('Camera', 'Phone screen'))
        for frame in (controls, screen): frame.pack(fill='x', pady=(0, 6))
        self.camera_hints, self.manual_widgets = {}, {}
        row = 0
        for label, mode, value in CAMERA_MODES:
            self.field(controls, label, 'camera.'+mode, row, list(MODE_NAMES), 17).state(['readonly'])
            self.field(controls, '    Manual value', 'camera.'+value, row+1, width=17)
            nudge = self.nudges(controls, row+2, lambda direction, large, value=value: self.nudge(value, direction, large))
            self.manual_widgets[mode] = controls.grid_slaves(row=row+1)+[nudge]
            self.camera_hints[mode] = ttk.Label(controls, wraplength=300, foreground='gray35')
            self.camera_hints[mode].grid(row=row+3, column=0, columnspan=2, sticky='w', pady=(0,4))
            self.variables['camera.'+mode].trace_add('write', self.show_camera_hints)
            row += 4
        self.field(controls, 'Brightness (blank = default)', 'camera.brightness', row, width=17)
        self.camera_hints['brightness'] = ttk.Label(controls, wraplength=300, foreground='gray35')
        self.camera_hints['brightness'].grid(row=row+1, column=0, columnspan=2, sticky='w', pady=(0,4))
        self.variables['camera.backend'].trace_add('write', self.show_camera_hints)
        self.reported = None
        hint(controls, 'These apply live with Start preview / apply, or at once with the step buttons.', row+2)
        self.field(screen, 'Screen brightness', 'capture.screen_brightness', 0, width=17)
        ttk.Button(screen, text='Set', width=4, command=lambda: self.safe(self.set_brightness)).grid(row=0, column=2, padx=(3,0))
        self.nudges(screen, 1, self.nudge_screen)
        hint(screen, '1–255 over ADB; blank = leave the phone alone. Held during ADB capture and restored afterwards; Set and the step buttons apply it now. OLED screens flicker: diagonal stripes in the preview mean a dimmer screen (about 10) with a longer shutter (1/16–1/32).', 2, 3)
        counter, indicator, gesture, run = (ttk.LabelFrame(scan, text=title, padding=6) for title in ('Page counter', 'Page indicator OCR', 'Next-page gesture', 'Run'))
        for frame in (counter, indicator, gesture): frame.pack(fill='x', pady=(0, 6))
        self.field(counter, 'Mode', 'capture.counter_mode', 0, ['verified','optical','manual'], 17).state(['readonly'])
        self.field(counter, 'Counter regex', 'capture.counter_regex', 1, width=17)
        self.regex_widgets = counter.grid_slaves(row=1)
        self.mode_hint = ttk.Label(counter, wraplength=300, foreground='gray35')
        self.mode_hint.grid(row=2, column=0, columnspan=2, sticky='w', pady=(4,0))
        for row, (label, key) in enumerate([('Format','indicator_format'),('Tesseract','tesseract'),('Tessdata (optional)','tessdata'),('OCR model','indicator_language')]):
            self.field(indicator, label, 'capture.'+key, row, width=17)
        hint(indicator, 'Format: how the reader shows the counter, e.g. Page {page}/{total} or {page} of {total}; only the numbers are read. Once the indicator is calibrated, every photo\'s indicator must read as the page being saved, in any mode. Tesseract "tesseract" uses the bundled copy when present.', 4, 3)
        self.field(gesture, 'Kind', 'gesture.kind', 0, ['swipe','tap'], 17).state(['readonly'])
        self.swipe_widgets = []
        for row, (label, key) in enumerate([('Start x / y','s'),('End / tap x / y','e'),('Duration ms','duration_ms')], 1):
            if key == 'duration_ms': self.field(gesture, label, 'gesture.'+key, row, width=17)
            else:
                ttk.Label(gesture, text=label).grid(row=row, column=0, sticky='w', pady=3, padx=(0,7))
                pair = ttk.Frame(gesture)
                pair.grid(row=row, column=1, sticky='w', pady=3)
                for axis in 'xy':
                    self.variables[f'gesture.{key}{axis}'] = tk.StringVar()
                    ttk.Entry(pair, textvariable=self.variables[f'gesture.{key}{axis}'], width=8).pack(side='left', padx=(0,4))
            if key != 'e': self.swipe_widgets += gesture.grid_slaves(row=row)
        hint(gesture, 'Screen fractions, 0–1 from the top-left, or place it on the preview: Calibrate → Next-page gesture. Previous page reverses the swipe, or mirrors the tap left↔right.', 4)
        tests = ttk.Frame(gesture)
        tests.grid(row=5, column=0, columnspan=2, sticky='w', pady=(6,0))
        self.button(tests, 'Read counter', self.read_counter)
        self.button(tests, '◀ Previous page', lambda: self.test_turn(back=True))
        self.button(tests, 'Next page ▶', self.test_turn)
        self.variables['capture.counter_mode'].trace_add('write', self.show_phone_fields)
        self.variables['gesture.kind'].trace_add('write', self.show_phone_fields)
        run.pack(fill='x', pady=(0, 6))
        self.start_var = tk.StringVar(value='1')
        self.count_var = tk.StringVar(value='3')
        for row, (label, var) in enumerate([('Start / next unsaved page',self.start_var),('Pages this run (sample: 3)',self.count_var)]):
            ttk.Label(run, text=label).grid(row=row, column=0, sticky='w', pady=3, padx=(0,7))
            ttk.Entry(run, textvariable=var, width=17).grid(row=row, column=1, sticky='ew', pady=3)
        for row, (label, key) in enumerate([('Total (manual mode)','total'),('Settle seconds','settle_seconds'),('Minimum sharpness','minimum_sharpness'),('Minimum image difference','minimum_difference')], 2):
            self.field(run, label, 'capture.'+key, row, width=17)
        hint(run, 'Manual mode: count 1, and advance the reader yourself after each saved photo. Keep font, spacing, brightness, orientation and stand position fixed for the whole project. Stop is checked between photos and gestures; an ADB read in progress may take up to 30 s.', 6)
        right = ttk.Frame(self.capture_tab)
        right.pack(fill='both', expand=True)
        self.preview = tk.Canvas(right, background='#18212b', highlightthickness=0)
        self.preview.pack(fill='both', expand=True)
        self.preview.create_text(20,25,anchor='nw',text='Start preview to focus the camera and mark the page corners.',fill='#cad6e2',width=600)
        # Click marks while marking; otherwise calibrated dots and gesture ends can be dragged.
        self.preview.bind('<Button-1>', self.press)
        self.preview.bind('<B1-Motion>', self.drag)
        self.preview.bind('<ButtonRelease-1>', self.release)
        self.preview.bind('<Motion>', self.hover)
        self.preview.bind('<Button-3>', lambda event: self.cancel_mark())
        self.calibration_help = tk.StringVar()
        # Fixed two-line height: changing instructions must not resize the preview under the cursor.
        help_frame = ttk.Frame(right,height=40)
        help_frame.pack(fill='x',pady=(5,0))
        help_frame.pack_propagate(False)
        ttk.Label(help_frame,textvariable=self.calibration_help,wraplength=780).pack(anchor='nw')
        self.corner_label = tk.StringVar(value='No calibration')
        ttk.Label(right,textvariable=self.corner_label).pack(anchor='w')
        actions = ttk.Frame(right)
        actions.pack(fill='x')
        ttk.Label(actions,text='Calibrate').pack(side='left',padx=(3,0))
        self.calibration_var = tk.StringVar(value='Text area')
        ttk.Combobox(actions,textvariable=self.calibration_var,values=list(CALIBRATION),width=18,state='readonly').pack(side='left',padx=3)
        self.calibration_var.trace_add('write',lambda *_: self.cancel_mark())
        self.button(actions,'Mark',self.begin_mark)
        self.button(actions,'Clear',self.clear_mark)
        self.button(actions,'Test crop',self.test_crop)
        self.button(actions,'OCR test',self.ocr_test)
        actions = ttk.Frame(right)
        actions.pack(fill='x')
        for text,action in [('Start preview / apply',self.start_preview),('Close preview',self.close_camera)]:
            self.button(actions,text,action)
        self.rotate_button = self.button(actions,'Rotate 90° ↻',self.rotate_preview)
        # Preview-only aid: never changes saved images or settings.
        self.peaking_var, self.peaking_level = tk.BooleanVar(value=False), tk.StringVar(value='Mid')
        ttk.Checkbutton(actions,text='Focus peaking',variable=self.peaking_var).pack(side='left',padx=(12,3))
        ttk.Combobox(actions,textvariable=self.peaking_level,values=list(PEAKING),width=5,state='readonly').pack(side='left')
        self.indicator_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(actions,text='Indicator OCR view',variable=self.indicator_var).pack(side='left',padx=(12,3))
        actions = ttk.Frame(right)
        actions.pack(fill='x')
        self.button(actions,'Capture pages',self.start_capture)
        self.button(actions,'Open saved page',self.show_last_page)

    def _book_ui(self):
        form = ttk.Frame(self.book_tab)
        form.pack(fill='x')
        left, right = ttk.Frame(form), ttk.Frame(form)
        left.pack(side='left',fill='x',expand=True,padx=(0,18))
        right.pack(side='left',fill='x',expand=True)
        for row,(label,key) in enumerate([('Title','title'),('Author','author'),('Book language tag','language'),('Tesseract executable','tesseract')]):
            self.field(left,label,'book.'+key,row)
        for row,(label,key) in enumerate([('Tessdata folder (optional)','tessdata'),('OCR models (e.g. ukr+eng)','ocr_language'),('Page segmentation mode','psm'),('OCR resize factor','scale')]):
            self.field(right,label,'book.'+key,row)
        self.dictionary_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(self.book_tab,text='Dictionary-assisted wrap hyphens (requires optional wordfreq)',variable=self.dictionary_var).pack(anchor='w')
        self.figures_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(self.book_tab,text='Detect illustrations: crop them out as figures and drop OCR noise read from them',variable=self.figures_var).pack(anchor='w')
        actions = ttk.Frame(self.book_tab)
        actions.pack(fill='x',pady=6)
        self.button(actions,'Check Tesseract / models',self.check_tesseract)
        self.button(actions,'Run / resume OCR',self.start_ocr)
        self.button(actions,'Export EPUB',self.export_epub)
        self.partial_var, self.images_var = tk.BooleanVar(), tk.BooleanVar()
        ttk.Checkbutton(actions,text='Label as sample',variable=self.partial_var).pack(side='left',padx=8)
        ttk.Checkbutton(actions,text='Image-only EPUB',variable=self.images_var).pack(side='left')
        panes = ttk.Panedwindow(self.book_tab, orient='horizontal')
        panes.pack(fill='both',expand=True)
        listing = ttk.Frame(panes)
        panes.add(listing,weight=1)
        self.pages = ttk.Treeview(listing,columns=('status',),show='tree headings',selectmode='browse',height=10)
        self.pages.heading('#0',text='Page')
        self.pages.heading('status',text='State')
        self.pages.column('#0',width=65,stretch=False)
        self.pages.column('status',width=125)
        self.pages.pack(side='left',fill='both',expand=True)
        scroll = ttk.Scrollbar(listing,command=self.pages.yview)
        scroll.pack(side='right',fill='y')
        self.pages.configure(yscrollcommand=scroll.set)
        self.pages.bind('<<TreeviewSelect>>',lambda event:self.safe(self.select_page))
        editing = ttk.Frame(panes,padding=(10,0,0,0))
        panes.add(editing,weight=4)
        self.review_canvas = tk.Canvas(editing,height=200,background='#273442',highlightthickness=0)
        self.review_canvas.pack(fill='x')
        self.review_canvas.bind('<Configure>',lambda event:self.draw_review_image())
        source_actions = ttk.Frame(editing)
        source_actions.pack(fill='x')
        self.button(source_actions,'View source at full resolution',self.show_source)
        self.button(source_actions,'Edit figures',self.edit_figures)
        ttk.Label(editing,text='Blank line = paragraph. Enter = hard line break. Display wrapping is automatic. A [figure N] paragraph places figure N.').pack(anchor='w')
        editor_frame = ttk.Frame(editing)
        editor_frame.pack(fill='both',expand=True)
        self.editor = tk.Text(editor_frame,wrap='word',height=8,undo=True,font=('TkDefaultFont',11))
        self.editor.pack(side='left',fill='both',expand=True)
        scroll = ttk.Scrollbar(editor_frame,command=self.editor.yview)
        scroll.pack(side='right',fill='y')
        self.editor.configure(yscrollcommand=scroll.set)
        edit_form = ttk.Frame(editing)
        edit_form.pack(fill='x',pady=4)
        ttk.Label(edit_form,text='Chapter title (optional)').pack(side='left')
        self.chapter_var = tk.StringVar()
        ttk.Entry(edit_form,textvariable=self.chapter_var).pack(side='left',fill='x',expand=True,padx=5)
        self.image_page_var = tk.BooleanVar()
        ttk.Checkbutton(edit_form,text='Keep page as image',variable=self.image_page_var).pack(side='left')
        edit_form = ttk.Frame(editing)
        edit_form.pack(fill='x')
        ttk.Label(edit_form,text='First paragraph continues previous page:').pack(side='left')
        self.continuation_var = tk.StringVar(value='Auto')
        ttk.Combobox(edit_form,textvariable=self.continuation_var,values=['Auto','Yes','No'],state='readonly',width=7).pack(side='left',padx=5)
        self.button(edit_form,'Save page review',self.save_page_review)

    def safe(self, function):
        try:
            return function()
        except Exception as error:
            self.log(str(error))
            messagebox.showerror('Optical Book Tool',str(error),parent=self.root)

    def log(self, text):
        self.log_box.configure(state='normal')
        self.log_box.insert('end',str(text)+'\n')
        self.log_box.see('end')
        self.log_box.configure(state='disabled')
        self.status.set(str(text))

    def fill_settings(self):
        for key,var in self.variables.items():
            group,name = key.split('.')
            if group == 'camera':
                camera = self.config['capture']['camera']
                value = camera.get(name)
                if name in MODE_KEYS:
                    # Older projects stored a raw auto-exposure number; a set exposure implies manual.
                    if not (value is None or isinstance(value,bool)): value = camera.get('exposure') is None
                    value = next(label for label,mode in MODE_NAMES.items() if mode is value)
                elif name == 'exposure' and value is not None:
                    value = shutter_text(exposure_seconds(value,self.hint_backend(camera['backend'])))
            elif group == 'gesture':
                gesture = self.config['capture']['gesture']
                value = gesture['start' if name[0]=='s' else 'end'][0 if name[1]=='x' else 1] if name in ('sx','sy','ex','ey') else gesture[name]
            else: value = self.config[group][name]
            var.set('' if value is None else str(value))
        self.dictionary_var.set(self.config['book'].get('dictionary_hyphens',False))
        self.figures_var.set(self.config['book'].get('figures',True))
        self.show_camera_hints()
        self.show_phone_fields()
        self.regions ={key: copy.deepcopy(self.config['capture'].get(key,[])) for key in REGIONS}
        self.marking, self.marked, self.backup, self.dragging = None, [], None, None
        self.rotation = int(self.config['capture'].get('preview_rotation') or 0)//90 % 4
        self.show_rotation()
        self.update_corner_label()

    def collect_settings(self):
        result = copy.deepcopy(self.config)
        integer = {'width','height','duration_ms','total','psm'}
        floating = {'fps','brightness','settle_seconds','minimum_sharpness','minimum_difference','scale'}
        manual = {value: self.variables['camera.'+mode].get() == 'Manual' for _,mode,value in CAMERA_MODES}
        for key,var in self.variables.items():
            group,name = key.split('.')
            value = var.get().strip()
            if group == 'gesture' and name in ('sx','sy','ex','ey'):
                result['capture']['gesture']['start' if name[0]=='s' else 'end'][0 if name[1]=='x' else 1] = float(value)
                continue
            if group == 'camera' and name == 'source': value = value.split(' · ')[0].strip()
            if name in integer: value = int(value)
            elif group == 'camera' and name in manual:
                if not manual[name]: value = None
                elif not value: raise ValueError(f'Manual {name.replace("_"," ")} needs a value (see the range shown below it).')
                elif name == 'exposure':
                    # Stored as the backend's raw value; shown as the shutter time it actually gives.
                    backend = self.hint_backend()
                    value = exposure_raw(parse_shutter(value),backend)
                    var.set(shutter_text(exposure_seconds(value,backend)))
                else: value = float(value)
            elif name in floating: value = float(value) if value else None
            elif name == 'screen_brightness':
                value = int(value) if value else None
                if value is not None and not 1 <= value <= 255: raise ValueError('Screen brightness must be 1–255, or blank to leave it alone.')
            elif name in MODE_KEYS:
                if value not in MODE_NAMES: raise ValueError(f'{name.replace("_"," ").capitalize()} must be one of: '+', '.join(MODE_NAMES)+'.')
                value = MODE_NAMES[value]
            target = result['capture']['camera'] if group == 'camera' else result['capture']['gesture'] if group == 'gesture' else result[group]
            target[name] = value
        result['capture'].update(copy.deepcopy(self.regions))
        result['capture']['preview_rotation'] = self.rotation*90
        result['book']['dictionary_hyphens'] = self.dictionary_var.get()
        result['book']['figures'] = self.figures_var.get()
        for key in ('width','height','fps'):
            if result['capture']['camera'][key] <= 0: raise ValueError(f'Camera {key} must be positive.')
        for key in ('settle_seconds','minimum_sharpness','minimum_difference'):
            if result['capture'][key] is None or result['capture'][key] < 0: raise ValueError(f'{key} must be nonnegative.')
        self.config = result
        return copy.deepcopy(result)

    def require_project(self):
        if not self.folder: raise ValueError('Create or open a project folder first.')

    def save_settings(self):
        self.require_project()
        config = self.collect_settings()
        write_json(self.folder/'project.json',config)
        self.log('Project settings saved.')

    def new_project(self):
        if not self.confirm_review(): return
        path = filedialog.asksaveasfilename(title='New project folder name',initialfile='book-project',parent=self.root)
        if not path: return
        folder = Path(path)
        folder.mkdir(exist_ok=False)
        write_json(folder/'project.json',defaults())
        self.load_folder(folder)

    def open_project(self):
        if not self.confirm_review(): return
        path = filedialog.askdirectory(title='Open project / capture folder',parent=self.root)
        if path: self.load_folder(path)

    def load_folder(self, folder):
        self.close_camera()
        self.folder = Path(folder).resolve()
        self.config = load_project(self.folder)
        self.entry, self.review_initial, self.figures = None, None, []
        self.path.set(str(self.folder))
        self.fill_settings()
        self.editor.delete('1.0','end')
        self.refresh_pages()
        self.log('Opened '+str(self.folder))

    def import_images(self):
        if not self.confirm_review(): return
        source = filedialog.askdirectory(title='Folder of page images (natural filename order)',parent=self.root)
        if not source: return
        target = filedialog.asksaveasfilename(title='Create new imported project folder',initialfile='imported-book',parent=self.root)
        if not target: return
        def job():
            count = generation.import_images(source,target)
            write_json(Path(target)/'project.json',defaults())
            self.events.put(('project',target))
            return f'Imported {count} images. Review filename order before OCR.'
        self.work(job)

    def work(self, function):
        if self.busy: raise RuntimeError('Another job is running.')
        self.busy = True
        self.stop.clear()
        self.disabled = []
        def disable(widget):
            for child in widget.winfo_children():
                if isinstance(child,(ttk.Button,ttk.Entry,ttk.Combobox,ttk.Checkbutton)):
                    self.disabled.append((child,child.state()))
                    child.state(['disabled'])
                disable(child)
        disable(self.root)
        self.editor.configure(state='disabled')
        self.pages.state(['disabled'])
        self.stop_button.state(['!disabled'])
        def target():
            try:
                result = function()
                self.events.put(('done',str(result) if result is not None else 'Done.'))
            except Exception as error:
                self.events.put(('error',str(error)))
        threading.Thread(target=target,daemon=True).start()

    def background_log(self,text): self.events.put(('log',text))

    def list_cameras(self):
        """Fill the camera dropdown with device names (never opens a camera); keep the selected index."""
        labels = [f'{index} · {name}' for index,name in cameras(self.variables['camera.backend'].get())]
        self.source_widget.configure(values=labels)
        current = self.variables['camera.source'].get().split(' · ')[0].strip()
        self.variables['camera.source'].set(next((label for label in labels if label.split(' · ')[0]==current),current))
        if not labels: self.log('This backend cannot list camera names here; type an index such as 0 or a path.')

    def nudge(self, name, direction, large=False):
        """Step a manual camera value and, with the preview open, save and apply it at once.
        Stepping past the camera's range keeps the last value it accepted."""
        variable, label = self.variables['camera.'+name], name.replace('_',' ').capitalize()
        small, big, lowest = NUDGE[name]
        step, previous = direction*(big if large else small), variable.get()
        if name == 'exposure':
            backend = self.hint_backend()
            variable.set(shutter_text(exposure_seconds(exposure_raw(parse_shutter(previous or '1/30')*2**step,backend),backend)))
        else:
            try: value = float(previous or 0)+step
            except ValueError: raise ValueError(f'Manual {label.lower()} value must be a number.')
            variable.set(f'{value if lowest is None else max(lowest,value):g}')
        if not self.camera or self.camera.error or self.busy: return
        requested = self.apply_live()[name]
        reported = self.camera.actual.get(name)
        text = lambda raw: shutter_text(exposure_seconds(raw,self.camera.backend)) if name == 'exposure' else f'{raw:g}'
        if name in self.camera.rejected:
            # Drivers may jump to another value after a refusal (a C920 drops white balance to 4000 K): restore.
            variable.set(previous)
            self.apply_live()
            self.status.set(f'{label} limit reached: the camera refused {text(requested)} and stays at {previous}.')
        elif isinstance(reported,(int,float)) and abs(reported-requested) > 1e-6:
            # Accepted but clamped (a C920 takes any exposure and stops at 1/4 s): show what it really uses.
            variable.set(text(reported))
            self.apply_live()
            self.status.set(f'{label} limit reached: the camera stays at {variable.get()}.')
        else:
            self.status.set(f'{label} {variable.get()} applied. Test crop again before capture.')

    def apply_live(self):
        """Save the settings and apply them to the open camera; returns the camera settings."""
        config = self.collect_settings()
        write_json(self.folder/'project.json',config)
        if not self.camera.apply(config['capture']['camera'],lambda text: text.startswith('Camera did not') and self.log(text),timeout=3):
            raise RuntimeError('Other camera settings changed too. Select Start preview / apply, or Close preview and start it again.')
        return config['capture']['camera']

    def start_preview(self):
        self.require_project()
        self.save_settings()
        config = copy.deepcopy(self.config['capture']['camera'])
        if self.camera and not self.camera.error:
            camera = self.camera
            def adjust():
                if not camera.apply(config,self.background_log):
                    raise RuntimeError('Source, backend, size, FPS, FourCC or a cleared control changed. Close preview and start it again.')
                camera.frame(camera.sequence)
                return 'Camera settings applied without restart. Test crop again before capture.'
            self.work(adjust)
            return
        self.close_camera()
        if self.config['capture'].get('calibration_camera') != fingerprint(config):
            # Every calibrated region is in camera pixels of the old camera view.
            self.regions = {key: [] for key in REGIONS}
            self.marking, self.marked, self.backup = None, [], None
            self.update_corner_label()
        releasing = self.releasing
        def job():
            if releasing: releasing.join()
            name = dict(cameras(config['backend'])).get(int(config['source'])) if str(config['source']).isdecimal() else None
            if name: self.background_log(f'Opening camera {config["source"]}: {name}')
            camera = Camera(config,self.background_log)
            try:
                camera.frame()
            except BaseException:
                camera.close()
                raise
            self.events.put(('camera',camera))
            return 'Preview ready. Inspect focus, then calibrate the text area, phone screen and page indicator.'
        self.work(job)

    def close_camera(self):
        if self.camera:
            # Releasing takes seconds on Windows; do it off the UI thread. Not a daemon, so exit waits for it.
            self.releasing = threading.Thread(target=self.camera.close)
            self.releasing.start()
            self.camera = None
        self.preview.delete('all')
        self.preview.create_text(20,25,anchor='nw',text='Start preview to focus the camera and mark the page corners.',fill='#cad6e2',width=600)
        self.preview_transform = None
        self.preview_key = None
        self.show_camera_hints()

    def begin_mark(self):
        if not self.camera: raise ValueError('Start camera preview first.')
        self.cancel_mark()
        key = CALIBRATION[self.calibration_var.get()][0]
        if key == 'gesture':
            if len(self.regions['screen_corners']) != 4: raise ValueError('Calibrate the phone screen first.')
            quad(self.regions['screen_corners'])
        else:
            self.backup, self.regions[key] = self.regions[key], []
        self.marking = key
        self.update_corner_label()

    def cancel_mark(self):
        """Right-click: stop marking and keep what was calibrated before."""
        if self.marking in self.regions: self.regions[self.marking] = self.backup
        self.marking, self.marked, self.backup = None, [], None
        self.update_corner_label()

    def clear_mark(self):
        self.cancel_mark()
        key = CALIBRATION[self.calibration_var.get()][0]
        if key == 'gesture': raise ValueError('Verified mode always needs a gesture; mark it again to change it.')
        self.regions[key] = []
        self.update_corner_label()

    def rotate_preview(self):
        """View only: calibration stays in camera pixels, so marks follow the image and saved photos are unchanged."""
        self.rotation = (self.rotation+1) % 4
        self.show_rotation()
        self.status.set(f'Preview rotated {self.rotation*90}° clockwise. Saved photos and calibration are not rotated; Save settings keeps this view.')

    def show_rotation(self):
        self.rotate_button.configure(text='Rotate 90° ↻' + (f' (now {self.rotation*90}°)' if self.rotation else ''))
        self.preview_key = None

    def to_canvas(self,point):
        x,y,scale,width,height,turns = self.preview_transform
        px,py = rotate_point(point,(width,height),turns)
        return x+px*scale,y+py*scale

    def from_canvas(self,cx,cy,clamp=False):
        """Camera-image pixel under a canvas position (None outside the image unless clamped)."""
        x,y,scale,width,height,turns = self.preview_transform
        size = (height,width) if turns % 2 else (width,height)
        px,py = (cx-x)/scale,(cy-y)/scale
        if clamp: px,py = min(max(px,0),size[0]),min(max(py,0),size[1])
        elif not (0<=px<=size[0] and 0<=py<=size[1]): return None
        return [round(value,2) for value in rotate_point([px,py],size,-turns)]

    def gesture_ends(self):
        """Configured gesture ends as {'s'/'e': screen fractions}, or {} if not drawable."""
        if len(self.regions['screen_corners'] or [])!=4 or self.marking=='gesture': return {}
        try:
            ends = ('s','e') if self.variables['gesture.kind'].get()=='swipe' else ('e',)
            return {end: [float(self.variables[f'gesture.{end}x'].get()),float(self.variables[f'gesture.{end}y'].get())] for end in ends}
        except ValueError:
            return {}

    def handle_at(self,cx,cy):
        """Nearest draggable dot within reach: (region key, corner index) or ('gesture', 's'/'e')."""
        found = [((key,index),point) for key in REGIONS for index,point in enumerate(self.regions[key] or [])]
        try: found += [(('gesture',end),from_screen(self.regions['screen_corners'],fraction)) for end,fraction in self.gesture_ends().items()]
        except (ValueError,np.linalg.LinAlgError): pass
        distance = lambda item: sum((a-b)**2 for a,b in zip(self.to_canvas(item[1]),(cx,cy)))
        nearest = min(found,key=distance,default=None)
        return nearest[0] if nearest and distance(nearest) <= 10**2 else None

    def hover(self,event):
        if not self.preview_transform or self.dragging: return
        self.preview.configure(cursor='crosshair' if self.marking else 'fleur' if self.handle_at(event.x,event.y) else '')

    def press(self,event):
        if self.busy or not self.preview_transform: return
        if self.marking:
            self.mark_corner(event)
            return
        handle = self.handle_at(event.x,event.y)
        if handle: self.dragging = (handle,copy.deepcopy(self.regions),{key: self.variables['gesture.'+key].get() for key in ('sx','sy','ex','ey')})

    def drag(self,event):
        if not self.dragging or not self.preview_transform: return
        (key,index),_,_ = self.dragging
        point = self.from_canvas(event.x,event.y,clamp=True)
        if key == 'gesture':
            fraction = [min(1,max(0,round(value,4))) for value in to_screen(self.regions['screen_corners'],point)]
            self.variables[f'gesture.{index}x'].set(f'{fraction[0]:g}')
            self.variables[f'gesture.{index}y'].set(f'{fraction[1]:g}')
        else: self.regions[key][index] = point
        self.redraw()

    def release(self,event):
        if not self.dragging: return
        (key,index),regions,gesture = self.dragging
        self.dragging = None
        if key == 'gesture':
            self.log('Next-page gesture moved. Save settings, then use Test next page.')
            return
        name = next(name for name,(region,_,_) in CALIBRATION.items() if region==key)
        _,_,_,width,height,_ = self.preview_transform
        try: quad(self.regions[key],(width,height))
        except ValueError as error:
            self.regions = regions
            for name_,value in gesture.items(): self.variables['gesture.'+name_].set(value)
            self.log(f'{name}: {error} The corner was put back.')
        else:
            self.log(f'{name} corner {index+1} moved. Use Test crop to check it; Save settings keeps it.')
        self.update_corner_label()
        self.redraw()

    def mark_corner(self,event):
        if not self.marking or self.busy or not self.preview_transform: return
        point = self.from_canvas(event.x,event.y)
        if point is None: return
        if self.marking == 'gesture':
            self.safe(lambda: self.mark_gesture(point))
            return
        self.regions[self.marking].append(point)
        if len(self.regions[self.marking])==4: self.marking,self.backup=None,None
        self.update_corner_label()

    def mark_gesture(self,point):
        kind = self.variables['gesture.kind'].get()
        gesture_from(self.regions['screen_corners'],[point],'tap')
        self.marked.append(point)
        if kind == 'swipe' and len(self.marked) < 2:
            self.update_corner_label()
            return
        gesture = gesture_from(self.regions['screen_corners'],self.marked,kind)
        for end in ('start','end'):
            if end in gesture:
                self.variables[f'gesture.{end[0]}x'].set(f'{gesture[end][0]:g}')
                self.variables[f'gesture.{end[0]}y'].set(f'{gesture[end][1]:g}')
        self.marking, self.marked = None, []
        self.update_corner_label()
        self.log(f'Next-page {kind} set to screen fractions {gesture}. Save settings, then use Test next page.')

    def update_corner_label(self):
        name = self.calibration_var.get()
        if self.marking == 'gesture':
            step = ' Now click the swipe end.' if self.marked else ''
            self.calibration_help.set(CALIBRATION[name][2]+step+' Right-click cancels.')
        elif self.marking:
            self.calibration_help.set(CALIBRATION[name][2]+f' Corner {len(self.regions[self.marking])+1} of 4. Right-click cancels.')
        else:
            self.calibration_help.set('Choose what to calibrate, then Mark. '+CALIBRATION[name][2])
        def state(key):
            count = len(self.regions[key])
            return 'set' if count == 4 else 'not set' if not count else f'{count}/4'
        self.corner_label.set(' · '.join(f'{name}: {state(key)}' for name,(key,_,_) in CALIBRATION.items() if key in self.regions))

    def test_crop(self):
        if not self.camera: raise ValueError('Start camera preview first.')
        self.cancel_mark()
        for name,(key,_,_) in CALIBRATION.items():
            if key in self.regions and len(self.regions[key]) not in ((4,) if key=='corners' else (0,4)):
                raise ValueError(f'{name}: mark all four corners'+('.' if key=='corners' else ' or clear it.'))
        self.save_settings()
        _,image = self.camera.frame()
        crops = {key: rectify(image,self.regions[key],REGIONS[key][1]) for key in REGIONS if self.regions[key]}
        self.config['capture']['calibration_camera'] = fingerprint(self.camera.settings)
        write_json(self.folder/'project.json',self.config)
        directory = self.folder/'calibration'
        directory.mkdir(exist_ok=True)
        image.save(directory/'camera.png')
        for key,crop in crops.items(): crop.save(directory/REGIONS[key][0])
        self.log(f'Calibration crop sharpness: {sharpness(crops["corners"]):.1f}. Inspect actual text; sharpness alone is not a legibility score. Saved '+', '.join('calibration/'+REGIONS[key][0] for key in crops)+'.')
        self.image_window(crops['corners'],'Calibration crop — full resolution')
        if 'indicator_corners' in crops:
            self.image_window(crops['indicator_corners'],'Page indicator crop — full resolution')
            config = copy.deepcopy(self.config['capture'])
            self.work(lambda: self.indicator_text(config,image))

    def ocr_test(self):
        """Book OCR of the current text crop with the Book generation settings, shown as Review would get it."""
        if not self.camera: raise ValueError('Start camera preview first.')
        if len(self.regions['corners'])!=4: raise ValueError('Calibrate the text area first.')
        config = self.collect_settings()['book']
        _,image = self.camera.frame()
        crop = rectify(image,self.regions['corners'],REGIONS['corners'][1])
        def job():
            values,lines,figures = generation.ocr_test(crop,config)
            self.events.put(('ocr_test',(crop,values,lines,figures,config)))
            return f'OCR test: {len(values)} paragraph(s), {sum(len(line["words"]) for line in lines)} words, {len(figures)} figure(s).'
        self.work(job)

    def ocr_window(self,crop,values,lines,figures,config):
        window = tk.Toplevel(self.root)
        window.title('OCR test — text as the book will get it')
        window.geometry('1150x780')
        words = [word for line in lines for word in line['words']]
        mean = sum(word['confidence'] for word in words)/len(words) if words else 0
        ttk.Label(window,padding=(8,6),wraplength=1100,text=f'{config["ocr_language"]}, PSM {config["psm"]}, scale {config["scale"]} · '
                  f'{len(values)} paragraph(s), {len(words)} words, mean confidence {mean:.0f} · yellow: confidence below {LOW_CONFIDENCE}; blue: Latin look-alike re-read without eng ({sum("ocr_text" in word for word in words)}); orange: detected figures ({len(figures)}). '
                  "Blank line = paragraph break, as Review and the EPUB get it; wrapping inside a paragraph is only this window's.").pack(fill='x')
        panes = ttk.Panedwindow(window,orient='horizontal')
        panes.pack(fill='both',expand=True,padx=8,pady=(0,8))
        picture = tk.Canvas(panes,background='#273442',highlightthickness=0,width=420)
        panes.add(picture,weight=2)
        def fit(event):
            scale = min(event.width/crop.width,event.height/crop.height)
            picture.photo = ImageTk.PhotoImage(crop.resize((max(1,round(crop.width*scale)),max(1,round(crop.height*scale))),Image.Resampling.LANCZOS))
            picture.delete('all');picture.create_image(event.width/2,event.height/2,image=picture.photo)
            x,y = (event.width-crop.width*scale)/2,(event.height-crop.height*scale)/2
            for number,figure in enumerate(figures,1):
                left,top,right,bottom = figure['box']
                picture.create_rectangle(x+left*scale,y+top*scale,x+right*scale,y+bottom*scale,outline='#ff9f43',width=2)
                picture.create_text(x+left*scale+3,y+top*scale+2,anchor='nw',text=str(number),fill='#ff9f43',font=('TkDefaultFont',9,'bold'))
        picture.bind('<Configure>',fit)
        frame = ttk.Frame(panes)
        panes.add(frame,weight=3)
        text = tk.Text(frame,wrap='word',font=('TkDefaultFont',11),padx=8,pady=6)
        scroll = ttk.Scrollbar(frame,command=text.yview)
        scroll.pack(side='right',fill='y');text.pack(side='left',fill='both',expand=True)
        text.configure(yscrollcommand=scroll.set)
        content = generation.review_text({'paragraphs':values,'figures':figures}) or '(no text recognized)'
        text.insert('1.0',content)
        text.tag_configure('low',background='#ffe08a')
        text.tag_configure('fixed',background='#bfe0ff')
        # Words appear in reading order in the joined text; a dehyphenated word may lose its trailing '-'.
        cursor = 0
        for word in words:
            for token in (word['text'],word['text'].rstrip('-')):
                at = content.find(token,cursor) if token else -1
                if at >= 0: break
            else: continue
            if 'ocr_text' in word: text.tag_add('fixed',f'1.0+{at}c',f'1.0+{at+len(token)}c')
            elif word['confidence'] < LOW_CONFIDENCE: text.tag_add('low',f'1.0+{at}c',f'1.0+{at+len(token)}c')
            cursor = at+len(token)

    def indicator_text(self,config,image):
        reading = read_indicator(image,config)
        found = f'{reading["page"]}/{reading["total"]}' if reading['page'] else 'no single counter-regex match'
        return f'Page indicator OCR: {reading["text"]!r} → {found}'

    def indicator_camera(self,config):
        """The open camera when the page indicator can be read from it, else None (optical mode requires it)."""
        ready = self.camera and not self.camera.error and len(config.get('indicator_corners') or [])==4
        if config['counter_mode']=='optical' and not ready:
            raise ValueError('Optical mode reads the counter from the camera: calibrate the page indicator and start preview.')
        return self.camera if ready else None

    def nudge_screen(self, direction, large=False):
        """Step the phone screen brightness (1 or 10 levels; dim screens need fine steps) and apply it at once."""
        variable = self.variables['capture.screen_brightness']
        try: value = int(variable.get() or 0)
        except ValueError: raise ValueError('Screen brightness must be a whole number 1–255.')
        variable.set(str(min(255, max(1, value + direction*(10 if large else 1)))))
        self.set_brightness()

    def set_brightness(self):
        """Apply the screen brightness now, so the preview shows what capture will photograph. Not restored later."""
        settings = self.collect_settings()
        config = settings['capture']
        if not config['screen_brightness']: raise ValueError('Enter a screen brightness (1–255) first.')
        if self.folder: write_json(self.folder/'project.json',settings)
        def job():
            before = android.Phone(config).screen_brightness(config['screen_brightness'])
            return f'Screen brightness set to {config["screen_brightness"]} (was {before[0]}{", automatic" if before[1] else ""}).'
        self.work(job)

    def refresh_devices(self):
        config = self.collect_settings()['capture']
        def job():
            found = android.devices(config['adb'])
            self.events.put(('devices',found))
            return 'Authorized ADB devices: '+(', '.join(found) or 'none')
        self.work(job)

    def read_counter(self):
        config = self.collect_settings()['capture']
        camera = self.indicator_camera(config)
        def job():
            found = []
            if config['counter_mode']=='verified' or not camera:
                phone = android.Phone(config)
                page, total = phone.page()
                found.append(f'ADB counter: {page}/{total} in {phone.package}')
            if camera: found.append(self.indicator_text(config,camera.frame(after=camera.sequence)[1]))
            return ' · '.join(found)
        self.work(job)

    def test_turn(self, back=False):
        config = self.collect_settings()['capture']
        if config['counter_mode']=='manual': raise ValueError('Manual mode never sends ADB gestures.')
        camera = self.indicator_camera(config)
        def job():
            phone=android.Phone(config)
            if config['counter_mode']=='optical':
                page = lambda: self.indicator_text(config,camera.frame(after=camera.sequence)[1])
            else: page = phone.page
            before=page()
            phone.turn(back)
            if config['counter_mode']=='optical': time.sleep(float(config['settle_seconds']))
            after=page()
            return f'{"Previous" if back else "Next"}-page test in {phone.package}: {before} → {after}. Check that exactly one page {"went back" if back else "advanced"}.'
        self.work(job)

    def start_capture(self):
        self.require_project()
        if not self.camera: raise ValueError('Start preview and inspect a crop first.')
        self.save_settings()
        folder, config, camera = self.folder,copy.deepcopy(self.config['capture']),self.camera
        if fingerprint(config['camera']) != fingerprint(camera.settings):
            raise ValueError('Camera settings changed. Select Start preview / apply, then test a crop.')
        if config.get('calibration_camera') != fingerprint(camera.settings):
            raise ValueError('Use Test crop to inspect and save this calibration before capture.')
        start,count = int(self.start_var.get()),int(self.count_var.get())
        self.work(lambda: f'Saved {capture(folder,config,camera,start,count,self.stop,self.background_log)} page(s).')

    def check_tesseract(self):
        config = self.collect_settings()['book']
        self.work(lambda:'\n'.join(generation.tesseract_info(config)))

    def start_ocr(self):
        if not self.confirm_review(): return
        self.save_settings()
        folder,config = self.folder,copy.deepcopy(self.config['book'])
        self.work(lambda:f'OCR ready for {generation.recognize(folder,config,self.stop,self.background_log)} page(s). Review paragraphs and low-confidence text before export.')

    def refresh_pages(self):
        self.pages.delete(*self.pages.get_children())
        path=self.folder/'capture.json' if self.folder else None
        if not path or not path.exists():
            self.entries=[]
            return
        self.entries=read_json(path)['pages']
        for entry in self.entries:
            number=entry['page']
            prefix=self.folder/'ocr'
            state='Reviewed' if (prefix/'edits'/f'{number:05d}.json').exists() else 'OCR' if (prefix/'pages'/f'{number:05d}.json').exists() else 'Image only'
            self.pages.insert('','end',iid=str(number),text=str(number),values=(state,))
        if self.entries: self.start_var.set(str(self.entries[-1]['page']+1))

    def review_state(self):
        return (self.editor.get('1.0','end-1c'),self.chapter_var.get(),self.image_page_var.get(),self.continuation_var.get(),tuple(map(tuple,self.figures)))

    def confirm_review(self):
        if self.entry and self.review_initial is not None and self.review_state()!=self.review_initial:
            answer=messagebox.askyesnocancel('Unsaved page review','Save edits to this page before continuing?',parent=self.root)
            if answer is None: return False
            if answer: self.save_page_review()
        return True

    def select_page(self):
        if self.busy: return
        selected=self.pages.selection()
        if not selected: return
        number=int(selected[0])
        if self.entry and number==self.entry['page']: return
        if not self.confirm_review():
            if self.entry: self.pages.selection_set(str(self.entry['page']))
            return
        self.entry=next(entry for entry in self.entries if entry['page']==number)
        try: record=generation.page_record(self.folder,self.entry)
        except ValueError as error:
            self.log(error)
            record={'paragraphs':[]}
        self.editor.delete('1.0','end')
        self.editor.insert('1.0',generation.review_text(record))
        self.editor.edit_reset()
        self.figures=[list(figure['box']) for figure in record.get('figures',[])]
        self.chapter_var.set(record.get('chapter',''))
        self.image_page_var.set(record.get('image_only',False))
        self.continuation_var.set({True:'Yes',False:'No',None:'Auto'}[record.get('continuation')])
        self.review_initial=self.review_state()
        self.draw_review_image()

    def draw_review_image(self):
        self.review_canvas.delete('all')
        if not self.entry: return
        with Image.open(inside(self.folder,self.entry['image'])) as image:
            image=image.convert('RGB')
            width=image.width
            image.thumbnail((max(10,self.review_canvas.winfo_width()),max(10,self.review_canvas.winfo_height())))
            self.photo_refs['review']=ImageTk.PhotoImage(image)
        cx,cy=self.review_canvas.winfo_width()/2,self.review_canvas.winfo_height()/2
        self.review_canvas.create_image(cx,cy,image=self.photo_refs['review'])
        scale,x,y=image.width/width,cx-image.width/2,cy-image.height/2
        for number,(left,top,right,bottom) in enumerate(self.figures,1):
            self.review_canvas.create_rectangle(x+left*scale,y+top*scale,x+right*scale,y+bottom*scale,outline='#ff9f43',width=2)
            self.review_canvas.create_text(x+left*scale+3,y+top*scale+2,anchor='nw',text=str(number),fill='#ff9f43',font=('TkDefaultFont',9,'bold'))

    def add_figure(self,box):
        """New figure box; its marker goes at the end of the text for the reviewer to move."""
        self.figures.append(box)
        self.editor.insert('end',('\n\n' if self.editor.get('1.0','end-1c').strip() else '')+f'[figure {len(self.figures)}]')
        self.editor.see('end')
        self.draw_review_image()

    def remove_figure(self,number):
        """Delete figure N and its marker; later figures move down one number."""
        del self.figures[number-1]
        def renumber(value):
            match=generation.FIGURE.fullmatch(value.strip())
            if not match: return value
            other=int(match.group(1))
            return None if other==number else f'[figure {other-(other>number)}]'
        values=[renumber(value) for value in re.split(r'\n\s*\n',self.editor.get('1.0','end-1c').strip()) if value.strip()]
        self.editor.delete('1.0','end')
        self.editor.insert('1.0','\n\n'.join(value for value in values if value is not None))
        self.draw_review_image()

    def edit_figures(self):
        """Full page with its figure boxes: drag to add one, right-click one to delete it. Saved with the page review."""
        if not self.entry: raise ValueError('Select a page first.')
        entry=self.entry
        with Image.open(inside(self.folder,entry['image'])) as image: page=image.convert('RGB')
        window=tk.Toplevel(self.root)
        window.title(f'Figures on page {entry["page"]}')
        window.geometry(f'{min(900,page.width+20)}x{min(880,page.height+80)}')
        ttk.Label(window,padding=(8,6),wraplength=860,text='Drag a box around an illustration to add it as a figure; its [figure N] marker is added at the end of the '
                  'text, so move it to where the figure belongs. Right-click a box to delete it and its marker. Save page review keeps the changes.').pack(fill='x')
        canvas=tk.Canvas(window,background='#273442',highlightthickness=0,cursor='crosshair')
        canvas.pack(fill='both',expand=True)
        view={'scale':1,'x':0,'y':0,'start':None}
        def current():
            if self.entry is entry: return True
            window.destroy()
            return False
        def draw(event=None):
            if not current(): return
            width,height=max(10,canvas.winfo_width()),max(10,canvas.winfo_height())
            scale=min(width/page.width,height/page.height)
            view.update(scale=scale,x=(width-page.width*scale)/2,y=(height-page.height*scale)/2)
            if event is not None or 'photo' not in view:
                view['photo']=ImageTk.PhotoImage(page.resize((max(1,round(page.width*scale)),max(1,round(page.height*scale))),Image.Resampling.LANCZOS))
            canvas.delete('all')
            canvas.create_image(view['x'],view['y'],anchor='nw',image=view['photo'])
            for number,box in enumerate(self.figures,1):
                left,top,right,bottom=(view['x']+box[0]*scale,view['y']+box[1]*scale,view['x']+box[2]*scale,view['y']+box[3]*scale)
                canvas.create_rectangle(left,top,right,bottom,outline='#ff9f43',width=2)
                canvas.create_text(left+4,top+3,anchor='nw',text=f'figure {number}',fill='#ff9f43',font=('TkDefaultFont',10,'bold'))
        def source(event):
            return (min(page.width,max(0,(event.x-view['x'])/view['scale'])),min(page.height,max(0,(event.y-view['y'])/view['scale'])))
        def press(event):
            if current(): view['start']=source(event)
        def motion(event):
            if not view['start']: return
            canvas.delete('rubber')
            x,y=view['start']
            canvas.create_rectangle(view['x']+x*view['scale'],view['y']+y*view['scale'],event.x,event.y,outline='#ffd166',dash=(4,2),tags='rubber')
        def release(event):
            start,view['start']=view['start'],None
            if not start or not current(): return
            (x0,y0),(x1,y1)=start,source(event)
            box=[round(min(x0,x1)),round(min(y0,y1)),round(max(x0,x1)),round(max(y0,y1))]
            if box[2]-box[0]>=8 and box[3]-box[1]>=8: self.add_figure(box)
            draw()
        def delete(event):
            if not current(): return
            x,y=source(event)
            hits=[number for number,box in enumerate(self.figures,1) if box[0]<=x<=box[2] and box[1]<=y<=box[3]]
            if hits: self.remove_figure(hits[-1])
            draw()
        canvas.bind('<Configure>',draw)
        canvas.bind('<Button-1>',press)
        canvas.bind('<B1-Motion>',motion)
        canvas.bind('<ButtonRelease-1>',release)
        canvas.bind('<Button-3>',delete)

    def save_page_review(self):
        if not self.entry: raise ValueError('Select a page first.')
        text,chapter,image_only,continuation,figures=self.review_state()
        generation.save_review(self.folder,self.entry,text,chapter,image_only,{'Auto':None,'Yes':True,'No':False}[continuation],figures)
        self.review_initial=self.review_state()
        self.pages.set(str(self.entry['page']),'status','Reviewed')
        self.log(f'Saved review for page {self.entry["page"]}.')

    def export_epub(self):
        if not self.confirm_review(): return
        self.save_settings()
        path=filedialog.asksaveasfilename(title='Export EPUB (choose a new filename)',defaultextension='.epub',filetypes=[('EPUB','*.epub')],initialdir=self.folder,parent=self.root)
        if not path: return
        folder,config,partial,images=self.folder,copy.deepcopy(self.config['book']),self.partial_var.get(),self.images_var.get()
        self.work(lambda:generation.build_epub(folder,path,config,partial,images))

    def image_window(self,image,title):
        window=tk.Toplevel(self.root)
        window.title(title)
        window.geometry(f'{min(850,image.width+40)}x{min(750,image.height+40)}')
        canvas=tk.Canvas(window,background='#273442')
        horizontal=ttk.Scrollbar(window,orient='horizontal',command=canvas.xview)
        vertical=ttk.Scrollbar(window,orient='vertical',command=canvas.yview)
        horizontal.pack(side='bottom',fill='x')
        vertical.pack(side='right',fill='y')
        canvas.pack(fill='both',expand=True)
        canvas.configure(xscrollcommand=horizontal.set,yscrollcommand=vertical.set,scrollregion=(0,0,image.width,image.height))
        canvas.photo=ImageTk.PhotoImage(image)
        canvas.create_image(0,0,image=canvas.photo,anchor='nw')

    def show_source(self):
        if not self.entry: raise ValueError('Select a page first.')
        with Image.open(inside(self.folder,self.entry['image'])) as image:
            self.image_window(image.copy(),f'Source page {self.entry["page"]} — full resolution')

    def show_last_page(self):
        self.require_project()
        manifest=read_json(self.folder/'capture.json')
        with Image.open(inside(self.folder,manifest['pages'][-1]['image'])) as image:
            self.image_window(image.copy(),'Last saved page — full resolution')

    def tick(self):
        try:
            while True:
                kind,value=self.events.get_nowait()
                if kind=='log': self.log(value)
                elif kind=='camera': self.camera=value
                elif kind=='ocr_test': self.ocr_window(*value)
                elif kind=='indicator': self.indicator_reading,self.indicator_busy,self.indicator_next=value,False,time.monotonic()+.5
                elif kind=='devices':
                    self.serial_widget.configure(values=value)
                    if len(value)==1: self.variables['capture.serial'].set(value[0])
                elif kind=='project': self.load_folder(value)
                elif kind in ('done','error'):
                    self.busy=False
                    for widget,state in self.disabled:
                        widget.state(['!disabled'])
                        if 'disabled' in state: widget.state(['disabled'])
                        if 'readonly' in state: widget.state(['readonly'])
                    self.stop_button.state(['disabled'])
                    self.editor.configure(state='normal')
                    self.pages.state(['!disabled'])
                    selected_page = self.entry['page'] if self.entry else None
                    self.refresh_pages()
                    if selected_page is not None and self.pages.exists(str(selected_page)):
                        self.entry = None
                        self.review_initial = None
                        self.pages.selection_set(str(selected_page))
                    self.log(value)
                    if kind=='error' and not self.closing: messagebox.showerror('Job stopped',value,parent=self.root)
        except queue.Empty:
            pass
        except Exception as error:
            self.log('UI update failed: '+str(error))
        if self.closing and not self.busy:
            self.close_camera()
            self.root.destroy()
            return
        if self.camera:
            if self.camera.error:
                self.log('Camera preview stopped: '+str(self.camera.error))
                self.close_camera()
                self.root.after(100,self.tick)
                return
            reported={key:value for key,value in self.camera.actual.items() if key not in ('width','height')}
            if reported!=self.reported:
                self.reported=reported
                self.show_camera_hints()
            with self.camera.condition:
                image,sequence=self.camera.latest,self.camera.sequence
            if image:
                width,height=max(1,self.preview.winfo_width()),max(1,self.preview.winfo_height())
                turns=self.rotation
                shown=(image.height,image.width) if turns % 2 else image.size
                scale=min(width/shown[0],height/shown[1])
                unrotated=(max(1,round(image.width*scale)),max(1,round(image.height*scale)))
                size=unrotated[::-1] if turns % 2 else unrotated
                level=self.peaking_level.get() if self.peaking_var.get() else None
                if self.preview_key!=(sequence,size,level,turns):
                    self.preview_key=(sequence,size,level,turns)
                    # Peaking works at full camera resolution, so resize (and peak) first, then rotate the small view.
                    resized=peaking(image,unrotated,level) if level else image.resize(unrotated)
                    self.photo_refs['preview']=ImageTk.PhotoImage(resized.transpose(TURNS[turns]) if turns else resized)
                if self.indicator_var.get(): self.update_indicator_view(image,sequence)
                else: self.photo_refs.pop('indicator',None);self.indicator_key=None
                self.preview_transform=((width-size[0])/2,(height-size[1])/2,scale,image.width,image.height,turns)
                self.redraw()
        self.root.after(100,self.tick)

    def redraw(self):
        if not self.preview_transform or 'preview' not in self.photo_refs: return
        self.preview.delete('all')
        self.preview.create_image(*self.preview_transform[:2],anchor='nw',image=self.photo_refs['preview'])
        self.draw_calibration()
        if self.indicator_var.get():
            colour,bottom = CALIBRATION['Page indicator'][1],8
            if 'indicator' in self.photo_refs:
                inset = self.photo_refs['indicator']
                self.preview.create_rectangle(7,7,inset.width()+9,inset.height()+9,outline=colour,width=2)
                self.preview.create_image(8,8,anchor='nw',image=inset)
                bottom += inset.height()+6
            self.overlay_text(8,bottom,self.indicator_reading or 'Reading…',colour,'nw')

    def update_indicator_view(self,image,sequence):
        """View-only aid like focus peaking: the indicator crop exactly as Tesseract receives it, plus a live reading.
        OCR runs on its own throttled thread (not a job), so it never blocks the UI or other work."""
        corners = self.regions.get('indicator_corners') or []
        if len(corners)!=4 or self.marking=='indicator_corners':
            self.photo_refs.pop('indicator',None);self.indicator_key=None
            self.indicator_reading='Calibrate the page indicator to see what OCR reads.'
            return
        key = (sequence,str(corners))
        if self.indicator_key!=key:
            self.indicator_key = key
            try: prepared = prepare(rectify(image,corners,REGIONS['indicator_corners'][1]))
            except ValueError as error:
                self.photo_refs.pop('indicator',None);self.indicator_reading=str(error)
                return
            width = min(360,prepared.width)
            self.photo_refs['indicator'] = ImageTk.PhotoImage(prepared.resize((width,max(1,round(prepared.height*width/prepared.width)))))
        if self.indicator_busy or time.monotonic()<self.indicator_next: return
        config = dict(self.config['capture'],indicator_corners=copy.deepcopy(corners),
                      **{name: self.variables['capture.'+name].get().strip() for name in ('tesseract','tessdata','indicator_language','indicator_format')})
        self.indicator_busy = True
        def target():
            try:
                reading = read_indicator(image,config)
                text = f'OCR {reading["text"]!r} → '+(f'{reading["page"]}/{reading["total"]}' if reading['page'] else 'unreadable')
            except Exception as error: text = 'Indicator OCR failed: '+str(error)
            self.events.put(('indicator',text))
        threading.Thread(target=target,daemon=True).start()

    def overlay_text(self,x,y,text,colour,anchor):
        """Label on a dark box so it stays readable over white pages, dark screens and busy backgrounds."""
        item = self.preview.create_text(x,y,text=text,fill=colour,anchor=anchor)
        left,top,right,bottom = self.preview.bbox(item)
        self.preview.create_rectangle(left-3,top-1,right+3,bottom+1,fill='#18212b',outline=colour)
        self.preview.tag_raise(item)

    def draw_calibration(self):
        """Overlay calibrated regions and the next-page gesture on the preview (camera pixels -> rotated canvas)."""
        canvas = self.to_canvas
        for name,(key,colour,_) in CALIBRATION.items():
            points = self.regions.get(key) or []
            for index,point in enumerate(points):
                cx,cy = canvas(point)
                self.preview.create_oval(cx-5,cy-5,cx+5,cy+5,fill=colour,outline='white')
                if self.marking==key: self.overlay_text(cx+12,cy-12,str(index+1),colour,'center')
            if len(points)==4:
                self.preview.create_polygon(*[v for point in points for v in canvas(point)],fill='',outline=colour,width=2)
                self.overlay_text(*canvas(points[0]),name,colour,'sw')
        colour = CALIBRATION['Next-page gesture'][1]
        for point in self.marked:
            cx,cy = canvas(point)
            self.preview.create_oval(cx-6,cy-6,cx+6,cy+6,outline=colour,width=2)
        try: ends = [canvas(from_screen(self.regions['screen_corners'],end)) for end in self.gesture_ends().values()]
        except (ValueError,np.linalg.LinAlgError): return
        if not ends: return
        cx,cy = ends[0]
        self.preview.create_oval(cx-7,cy-7,cx+7,cy+7,outline=colour,width=2)
        if len(ends)==2: self.preview.create_line(*ends[0],*ends[1],fill=colour,width=3,arrow='last',arrowshape=(14,16,6))

    def close(self):
        if not self.busy and not self.confirm_review(): return
        self.closing=True
        self.stop.set()
        if self.busy: self.status.set('Stopping the current job before closing…')


def launch(project=None):
    root=tk.Tk()
    style=ttk.Style(root)
    # Native widgets on Windows; clam elsewhere (vista exists only on Windows).
    theme = next((name for name in ('vista','clam') if name in style.theme_names()), None)
    if theme: style.theme_use(theme)
    App(root,project)
    root.mainloop()
