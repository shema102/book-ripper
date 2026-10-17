import copy
import io
import json
from pathlib import Path
import tempfile
import sys
import threading
import time
import types
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET
from zipfile import ZipFile, ZIP_STORED
from PIL import Image, ImageDraw, ImageFilter

from opticalbook.android import Phone, parse_counter
from opticalbook.capture import capture, validate_manifest
from opticalbook.common import defaults, digest, read_json, write_json
from opticalbook.indicator import parse_indicator, read_indicator, whitelist
from opticalbook.generation import build_epub, import_images, page_record, recognize, save_review
from opticalbook.__main__ import main
from opticalbook.imaging import REGIONS, Camera, controls, exposure_raw, exposure_seconds, from_screen, gesture_from, parse_shutter, peaking, rectify, rotate_point, shutter_text, to_screen
from opticalbook.layout import paragraph_starts


def page_image(number):
    image=Image.new('RGB',(160,240),'white')
    draw=ImageDraw.Draw(image)
    for y in range(15,220,12):
        draw.rectangle((10,y,60+(y*number)%85,y+4),fill='black')
    return image


class FakePhone:
    def __init__(self,total=2,step=1): self.current,self.total,self.step,self.turns=1,total,step,0
    def page(self): return self.current,self.total
    def turn(self): self.current+=self.step;self.turns+=1


class FakeCamera:
    sequence=0
    def __init__(self,phone,duplicate=False): self.phone,self.duplicate=phone,duplicate
    def frame(self,after=0):
        self.sequence=max(self.sequence,after)+1
        image=page_image(1 if self.duplicate else self.phone.current)
        image.info['shows']=self.phone.current,self.phone.total
        return self.sequence,image


def fake_indicator(misread=()):
    """Stands in for Tesseract: reads what the fake frame shows; `misread` pages come out unreadable."""
    def read(image,config):
        page,total=image.info['shows']
        return {'text':'?','page':None,'total':None} if page in misread else {'text':f'{page} / {total}','page':page,'total':total}
    return read


def fake_cv2():
    names=['CAP_ANY','CAP_DSHOW','CAP_MSMF','CAP_V4L2','CAP_AVFOUNDATION','CAP_PROP_FRAME_WIDTH','CAP_PROP_FRAME_HEIGHT',
           'CAP_PROP_FPS','CAP_PROP_FOURCC','CAP_PROP_AUTOFOCUS','CAP_PROP_FOCUS','CAP_PROP_AUTO_EXPOSURE','CAP_PROP_EXPOSURE',
           'CAP_PROP_BRIGHTNESS','CAP_PROP_AUTO_WB','CAP_PROP_WB_TEMPERATURE']
    module=types.SimpleNamespace(**{name:index for index,name in enumerate(names)},COLOR_BGR2RGB=0,
                                 VideoWriter_fourcc=lambda *code:0,cvtColor=lambda frame,code:frame)
    class Capture:
        def __init__(self,source,api,params=()):
            self.values,self.sets,self.params=dict(zip(params[::2],params[1::2])),[],list(params)
            module.opened.append(self)
        def isOpened(self): return not (module.refuse_params and self.params)
        def set(self,prop,value): self.values[prop]=value;self.sets.append(prop);return True
        def get(self,prop): return self.values.get(prop,0.0)
        def read(self): time.sleep(.005);return True,__import__('numpy').zeros((4,4,3),dtype='uint8')
        def release(self): pass
    module.VideoCapture,module.opened,module.refuse_params=Capture,[],False
    return module


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        base=Path(__file__).resolve().parents[1]/'.test-work'
        base.mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=base)
        self.folder=Path(self.temp.name)
        self.config=defaults()['capture']
        self.config.update(corners=[[0,0],[160,0],[160,240],[0,240]],settle_seconds=0,minimum_difference=.1,total=2)
        self.book=defaults()['book']
        self.book.update(title='A test book',author='Test author',language='en',ocr_language='eng')

    def tearDown(self): self.temp.cleanup()

    def scan(self,count=2,phone=None,**kwargs):
        phone=phone or FakePhone()
        return capture(self.folder,self.config,FakeCamera(phone),1,count,log=lambda _:None,phone=phone,**kwargs)

    def test_verified_capture_exactly_one_turn_and_complete_manifest(self):
        phone=FakePhone()
        self.assertEqual(self.scan(phone=phone),2)
        self.assertEqual(phone.turns,1)
        self.assertEqual([x['page'] for x in validate_manifest(self.folder)['pages']],[1,2])

    def test_skipped_page_stops_before_wrong_save(self):
        with self.assertRaisesRegex(RuntimeError,'Expected 2/2'):
            self.scan(phone=FakePhone(step=2))
        self.assertEqual(len(read_json(self.folder/'capture.json')['pages']),1)
        self.assertFalse((self.folder/'pages/00002.png').exists())

    def test_page_change_during_photo_saves_nothing(self):
        class MovingPhone(FakePhone):
            reads=0
            def page(self):
                self.reads+=1
                return (2 if self.reads>=3 else 1),2
        with self.assertRaisesRegex(RuntimeError,'changed during'):
            self.scan(count=1,phone=MovingPhone())
        self.assertFalse((self.folder/'capture.json').exists())

    def test_duplicate_photo_stops_before_second_save(self):
        phone=FakePhone()
        with self.assertRaisesRegex(RuntimeError,'duplicate'):
            capture(self.folder,self.config,FakeCamera(phone,True),1,2,log=lambda _:None,phone=phone)
        self.assertEqual(len(read_json(self.folder/'capture.json')['pages']),1)

    def test_blur_gate_and_stop_send_no_unnecessary_gestures(self):
        self.config['minimum_sharpness']=1e10
        phone=FakePhone()
        with self.assertRaisesRegex(RuntimeError,'sharpness'):
            self.scan(count=1,phone=phone)
        self.assertEqual(phone.turns,0)
        stop=threading.Event();stop.set()
        self.assertEqual(self.scan(phone=phone,stop=stop),0)

    def test_resume_from_last_saved_page_and_reject_layout_change(self):
        phone=FakePhone()
        self.scan(count=1,phone=phone)
        self.assertEqual(capture(self.folder,self.config,FakeCamera(phone),2,1,phone=phone,log=lambda _:None),1)
        self.config['camera']['width']=1280
        with self.assertRaisesRegex(ValueError,'settings changed'):
            capture(self.folder,self.config,FakeCamera(phone),3,1,phone=phone)

    def test_unset_white_balance_keeps_older_capture_resumable(self):
        phone=FakePhone()
        for key in ('auto_white_balance','white_balance'): self.config['camera'].pop(key,None)
        self.scan(count=1,phone=phone)
        self.config['camera'].update(auto_white_balance=None,white_balance=None)
        self.assertEqual(capture(self.folder,self.config,FakeCamera(phone),2,1,phone=phone,log=lambda _:None),1)
        self.config['camera'].update(auto_white_balance=False,white_balance=4500)
        with self.assertRaisesRegex(ValueError,'settings changed'):
            capture(self.folder,self.config,FakeCamera(phone),3,1,phone=phone)

    def test_focus_exposure_white_balance_apply_without_reopening(self):
        with patch.dict(sys.modules,{'cv2':fake_cv2()}):
            settings=copy.deepcopy(self.config['camera'])
            camera=Camera(settings)
            try:
                camera.frame()
                cap=camera.cap
                settings.update(autofocus=False,focus=40,auto_white_balance=False,white_balance=4500)
                self.assertTrue(camera.apply(settings))
                self.assertIs(camera.cap,cap)
                self.assertEqual((camera.actual['focus'],camera.actual['white_balance'],camera.actual['auto_white_balance']),(40,4500,0))
                self.assertEqual(camera.settings,settings)
                self.assertFalse(camera.apply(dict(settings,width=1280)))
                self.assertFalse(camera.apply(dict(settings,autofocus=None,focus=None)))
                self.assertEqual(camera.settings,settings)
                self.assertTrue(camera.apply(dict(settings,autofocus=True,focus=None)))
                self.assertEqual(camera.actual['autofocus'],1)
            finally: camera.close()

    def test_format_is_negotiated_while_opening_with_slow_fallback(self):
        fake=fake_cv2()
        with patch.dict(sys.modules,{'cv2':fake}):
            settings=dict(self.config['camera'],backend='msmf')
            Camera(settings).close()
            self.assertEqual(fake.opened[-1].params,[fake.CAP_PROP_FRAME_WIDTH,1920,fake.CAP_PROP_FRAME_HEIGHT,1080,fake.CAP_PROP_FPS,30])
            self.assertNotIn(fake.CAP_PROP_FRAME_WIDTH,fake.opened[-1].sets)
            Camera(dict(settings,backend='dshow')).close()
            self.assertEqual(fake.opened[-1].params[0],fake.CAP_PROP_FOURCC)
            fake.refuse_params=True
            logs=[]
            camera=Camera(dict(settings,backend='dshow'),logs.append)
            camera.close()
            self.assertEqual(fake.opened[-1].params,[])
            self.assertIn(fake.CAP_PROP_FRAME_WIDTH,fake.opened[-1].sets)
            self.assertTrue(any('separate settings' in line for line in logs))

    def test_manual_values_require_manual_mode_and_auto_exposure_follows_backend(self):
        camera=dict(defaults()['capture']['camera'],autofocus=False,focus=None)
        with self.assertRaisesRegex(ValueError,'needs a value'): controls(camera)
        with self.assertRaisesRegex(ValueError,'only applies in manual'): controls(dict(camera,autofocus=True,focus=40))
        camera.update(focus=40,auto_exposure=False,exposure=-6)
        self.assertEqual(controls(camera,'v4l2')[:4],[('autofocus','CAP_PROP_AUTOFOCUS',0),('focus','CAP_PROP_FOCUS',40),
                                                       ('auto_exposure','CAP_PROP_AUTO_EXPOSURE',1),('exposure','CAP_PROP_EXPOSURE',-6)])
        self.assertEqual(controls(dict(camera,auto_exposure=True,exposure=None),'v4l2')[2][2],3)
        self.assertEqual(controls(dict(camera,auto_exposure=True,exposure=None),'dshow')[2][2],1)
        self.assertEqual(controls(dict(camera,auto_exposure=False),'dshow')[2][2],0)

    def test_quality_tuning_can_resume_without_changing_geometry(self):
        phone=FakePhone()
        self.scan(count=1,phone=phone)
        self.config['minimum_difference']=.05
        self.assertEqual(capture(self.folder,self.config,FakeCamera(phone),2,1,phone=phone,log=lambda _:None),1)

    def test_manual_capture_never_uses_phone_and_refuses_batch(self):
        self.config['counter_mode']='manual'
        phone=FakePhone()
        with self.assertRaisesRegex(ValueError,'one page'):
            self.scan(phone=phone)
        self.assertEqual(self.scan(count=1,phone=phone),1)
        self.assertFalse(read_json(self.folder/'capture.json')['counter_verified'])
        self.assertEqual(phone.turns,0)

    def optical(self,reader,mode='optical',**kwargs):
        self.config.update(counter_mode=mode,indicator_corners=[[60,200],[120,200],[120,230],[60,230]])
        with patch('opticalbook.capture.read_indicator',side_effect=reader) as ocr,patch('opticalbook.capture.tesseract',return_value=('tesseract','')):
            return self.scan(**kwargs),ocr

    def test_optical_counter_turns_once_and_records_each_photo_indicator(self):
        phone=FakePhone()
        (saved,ocr)=self.optical(fake_indicator(),phone=phone)
        self.assertEqual((saved,phone.turns),(2,1))
        manifest=validate_manifest(self.folder)
        self.assertTrue(manifest['counter_verified'])
        self.assertEqual([x['indicator']['page'] for x in manifest['pages']],[1,2])
        self.assertTrue(all(x['counter_verified'] for x in manifest['pages']))

    def test_optical_counter_tolerates_unreadable_turn_animation_but_not_a_wrong_start(self):
        class Animated(FakePhone):
            def turn(self): super().turn();self.blurred=2
        phone=Animated()
        reads=fake_indicator()
        def animated(image,config):
            if getattr(phone,'blurred',0): phone.blurred-=1;return {'text':'','page':None,'total':None}
            return reads(image,config)
        self.assertEqual(self.optical(animated,phone=phone)[0],2)
        self.temp.cleanup();self.folder.mkdir()
        with self.assertRaisesRegex(RuntimeError,'unreadable'): self.optical(fake_indicator(misread=(1,)),phone=FakePhone())
        self.assertFalse((self.folder/'capture.json').exists())

    def test_photo_indicator_mismatch_saves_nothing_in_verified_mode(self):
        def wrong(image,config): return {'text':'7 / 2','page':None,'total':None}
        phone=FakePhone()
        with self.assertRaisesRegex(RuntimeError,'expected 1/2'): self.optical(wrong,mode='verified',count=1,phone=phone)
        self.assertFalse((self.folder/'pages/00001.png').exists())
        self.assertTrue((self.folder/'review-indicator.png').exists())

    def test_manual_capture_is_counter_verified_by_its_indicator(self):
        phone=FakePhone()
        (saved,ocr)=self.optical(fake_indicator(),mode='manual',count=1,phone=phone)
        entry=read_json(self.folder/'capture.json')['pages'][0]
        self.assertEqual((saved,phone.turns,ocr.call_count,entry['counter_verified']),(1,0,1,True))

    def test_optical_mode_needs_calibrated_indicator(self):
        self.config['counter_mode']='optical'
        with self.assertRaisesRegex(ValueError,'page indicator'): self.scan()

    def test_indicator_format_reads_numbers_not_the_label(self):
        page='Page {page}/{total}'
        for text in ('Page 10/100','Page10/100','Pag610/100','Раде 10/100','P4ge 10 / 100','10/100'):
            self.assertEqual(parse_indicator(text,page),(10,100),text)
        for text in ('x10/100','Page 10/100 Page 11/100','Page 10/9','Page 10'):
            self.assertEqual(parse_indicator(text,page),(None,None),text)
        self.assertEqual(parse_indicator('Pag610/100','{page}/{total}'),(None,None))  # glued, no label to eat it
        self.assertEqual(parse_indicator('12 0f 345 pages','{page} of {total} pages'),(12,345))
        self.assertEqual(parse_indicator('Сторінка 9 з 265','Сторінка {page} з {total}'),(9,265))
        self.assertEqual(parse_indicator('100 / 7','{total} / {page}'),(7,100))
        with self.assertRaisesRegex(ValueError,'once each'): parse_indicator('1/2','Page {page}')
        self.assertEqual(whitelist(page),'0123456789pPaAgGeE/')

    def test_indicator_ocr_command(self):
        frame=Image.new('RGB',(200,100),'black');ImageDraw.Draw(frame).text((52,42),'Page 12/34',fill='white')
        config=dict(defaults()['capture'],indicator_corners=[[50,40],[130,40],[130,56],[50,56]],indicator_format='Page {page}/{total}')
        with patch('opticalbook.indicator.tesseract',return_value=('tesseract','models')),patch('opticalbook.indicator.run',return_value=b'Page 12/34\n') as runner:
            self.assertEqual(read_indicator(frame,config),{'text':'Page 12/34','page':12,'total':34})
        arguments,options=runner.call_args
        self.assertEqual(arguments[0][:3]+arguments[0][-6:],['tesseract','stdin','stdout','--psm','7','-c','tessedit_char_whitelist=0123456789pPaAgGeE/','--tessdata-dir','models'])
        sent=Image.open(io.BytesIO(options['input']))
        self.assertEqual(sent.height,96+2*32)
        self.assertGreater(__import__('numpy').asarray(sent).mean(),128)  # dark theme inverted to dark-on-light

    def test_unlogged_image_is_not_overwritten(self):
        (self.folder/'pages').mkdir()
        image=self.folder/'pages/00001.png'
        image.write_bytes(b'preserve')
        with self.assertRaises(FileExistsError): self.scan(count=1)
        self.assertEqual(image.read_bytes(),b'preserve')

    def test_checksum_tampering_and_path_escape_are_rejected(self):
        self.scan()
        path=self.folder/'pages/00001.png'
        path.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'checksum'):validate_manifest(self.folder)
        data=read_json(self.folder/'capture.json')
        data['pages'][0]['image']='../outside.png'
        write_json(self.folder/'capture.json',data)
        with self.assertRaisesRegex(ValueError,'escapes'):validate_manifest(self.folder)

    def test_counter_is_package_scoped_fresh_and_unambiguous(self):
        pattern=defaults()['capture']['counter_regex']
        xml='<hierarchy><node package="com.reader.app" content-desc="Page 2 / 3"/></hierarchy>'
        self.assertEqual(parse_counter(xml,'com.reader.app',pattern),(2,3))
        with self.assertRaises(RuntimeError):parse_counter(xml,'com.wrong.app',pattern)
        xml=xml.replace('</hierarchy>','<node package="com.reader.app" text="1/3"/></hierarchy>')
        with self.assertRaises(RuntimeError):parse_counter(xml,'com.reader.app',pattern)

    def test_wrong_foreground_app_prevents_gesture(self):
        phone=Phone.__new__(Phone)
        phone.config={**defaults()['capture'],'package':'com.reader.app'}
        calls=[]
        def adb(*args): calls.append(args);return 'mCurrentFocus=Window{abc u0 com.other.app/.Main}'
        phone.adb=adb
        with self.assertRaisesRegex(RuntimeError,'not foreground'):phone.turn()
        self.assertFalse(any('input' in args for args in calls))

    def test_tap_uses_current_screen_size_and_normalized_coordinates(self):
        phone=Phone.__new__(Phone)
        phone.config={**defaults()['capture'],'package':'com.reader.app','gesture':{'kind':'tap','end':[1,.5]}}
        calls=[]
        def adb(*args):
            calls.append(args)
            return 'mCurrentFocus=Window{abc u0 com.reader.app/.Main}' if args[1]=='dumpsys' else 'Physical size: 1080x2340'
        phone.adb=adb
        phone.turn()
        self.assertEqual(calls[-1],('shell','input','tap','1079','1170'))

    def test_exposure_is_shown_as_shutter_time_in_backend_units(self):
        self.assertEqual([parse_shutter(text) for text in ('1/60','60','0.5','2s','1/8s')],[1/60,1/60,.5,2.0,1/8])
        for text in ('0','abc','1/0','-5'):
            with self.assertRaises(ValueError): parse_shutter(text)
        self.assertEqual(exposure_raw(1/60,'dshow'),-6.0)
        self.assertEqual(shutter_text(exposure_seconds(-6,'dshow')),'1/64')
        self.assertEqual(shutter_text(exposure_seconds(1,'msmf')),'2s')
        self.assertEqual(exposure_raw(1/60,'v4l2'),167.0)
        self.assertEqual(shutter_text(exposure_seconds(167,'v4l2')),'1/60')
        self.assertEqual(exposure_raw(1e-9,'v4l2'),1.0)

    def test_focus_peaking_marks_sharp_text_and_nothing_when_blurred(self):
        def painted(image): return sum(1 for pixel in image.getdata() if pixel==(255,32,32))
        sharp=page_image(1)
        self.assertEqual(peaking(sharp,(80,120),'Low').size,(80,120))
        self.assertGreater(painted(peaking(sharp,(80,120),'Low')),50)
        self.assertEqual(painted(peaking(sharp.filter(ImageFilter.GaussianBlur(4)),(80,120),'High')),0)

    def test_perspective_rejects_crossed_corners(self):
        with self.assertRaisesRegex(ValueError,'cross'):
            rectify(page_image(1),[[0,0],[160,240],[160,0],[0,240]])

    def test_preview_rotation_maps_points_like_the_rotated_image(self):
        image=Image.new('RGB',(40,30),'black');image.putpixel((7,3),(255,0,0))
        for turns,transpose in [(1,Image.Transpose.ROTATE_270),(2,Image.Transpose.ROTATE_180),(3,Image.Transpose.ROTATE_90)]:
            rotated=image.transpose(transpose)
            x,y=rotate_point([7.5,3.5],image.size,turns)  # pixel centre
            self.assertEqual(rotated.getpixel((int(x),int(y))),(255,0,0),turns)
            self.assertEqual(rotate_point([x,y],rotated.size,-turns),[7.5,3.5])
        self.assertEqual(rotate_point([7,3],image.size,4),[7,3])

    def test_screen_calibration_maps_preview_clicks_to_gesture_fractions(self):
        screen=[[100,50],[500,80],[480,900],[120,860]]
        for fraction in ([0,0],[1,1],[.8,.55],[.25,.9]):
            back=to_screen(screen,from_screen(screen,fraction))
            self.assertAlmostEqual(back[0],fraction[0]);self.assertAlmostEqual(back[1],fraction[1])
        swipe=gesture_from(screen,[from_screen(screen,[.8,.5]),from_screen(screen,[.2,.5])],'swipe')
        self.assertEqual(swipe,{'kind':'swipe','start':[.8,.5],'end':[.2,.5]})
        self.assertEqual(gesture_from(screen,[from_screen(screen,[.9,.5])],'tap'),{'kind':'tap','end':[.9,.5]})
        with self.assertRaisesRegex(ValueError,'inside'): gesture_from(screen,[[20,20]],'tap')
        with self.assertRaisesRegex(ValueError,'screen corners'): gesture_from([],[[200,200]],'tap')

    def test_small_page_indicator_crop_and_cli_calibration(self):
        photo=self.folder/'photo.png';page_image(1).save(photo)
        project=self.folder/'project';project.mkdir();write_json(project/'project.json',defaults())
        with self.assertRaisesRegex(ValueError,'too small'): rectify(page_image(1),[[10,10],[40,10],[40,22],[10,22]])
        self.assertEqual(rectify(page_image(1),[[10,10],[40,10],[40,22],[10,22]],REGIONS['indicator_corners'][1]).size,(30,12))
        argv=['opticalbook','calibrate','--project',str(project),'--photo',str(photo),'--corners','5','5','155','5','155','200','5','200',
              '--screen','0','0','160','0','160','240','0','240','--indicator','60','210','100','210','100','230','60','230','--tap','144','120']
        with patch.object(sys,'argv',argv),patch('builtins.print'): main()
        config=read_json(project/'project.json')['capture']
        self.assertEqual(config['indicator_corners'],[[60,210],[100,210],[100,230],[60,230]])
        self.assertEqual(config['gesture']['kind'],'tap');self.assertEqual(config['gesture']['end'],[.9,.5])
        self.assertEqual(config['gesture']['start'],defaults()['capture']['gesture']['start'])
        for name in ('crop.png','screen.png','indicator.png'): self.assertTrue((project/'calibration'/name).exists())

    def test_layout_ignores_ocr_block_split_and_preserves_indent(self):
        def line(y,left=20,group=1):
            return dict(text='A continuous ordinary text line',left=left,right=650,top=y,bottom=y+24,group=[group,1])
        starts,_=paragraph_starts([line(0),line(30,group=2),line(60,left=55),line(90)],680)
        self.assertEqual(starts,['page-start',None,'indentation',None])

    def test_image_import_uses_natural_order_without_adb_or_camera(self):
        source=self.folder/'input';source.mkdir()
        for name in ('page10.jpg','page2.jpg','page1.jpg'):page_image(1).save(source/name)
        target=self.folder/'imported'
        self.assertEqual(import_images(source,target),3)
        data=validate_manifest(target)
        self.assertEqual([x['source_filename'] for x in data['pages']],['page1.jpg','page2.jpg','page10.jpg'])

    def review_all(self):
        entries=validate_manifest(self.folder)['pages']
        for index,entry in enumerate(entries):
            save_review(self.folder,entry,'A first paragraph.\n\nA second paragraph.',chapter='Chapter 1' if index==0 else '')
        return entries

    def test_epub_keeps_paragraphs_chapter_and_every_page_anchor(self):
        self.scan();self.review_all()
        output=self.folder/'book.epub'
        report=build_epub(self.folder,output,self.book)
        self.assertEqual(report['reviewed_pages'],2)
        with ZipFile(output) as archive:
            self.assertEqual(archive.infolist()[0].filename,'mimetype')
            self.assertEqual(archive.infolist()[0].compress_type,ZIP_STORED)
            roots=[ET.fromstring(archive.read(n)) for n in archive.namelist() if n.endswith(('.xml','.opf','.xhtml'))]
            anchors=[x.attrib['id'] for root in roots for x in root.iter() if x.attrib.get('id','').startswith('page-')]
            self.assertEqual(sorted(anchors),['page-1','page-2'])
        with self.assertRaises(FileExistsError):build_epub(self.folder,output,self.book)

    def test_missing_ocr_is_not_silently_exported_and_images_are_explicit(self):
        self.scan()
        with self.assertRaisesRegex(ValueError,'No OCR'):build_epub(self.folder,self.folder/'text.epub',self.book)
        report=build_epub(self.folder,self.folder/'images.epub',self.book,image_only=True)
        self.assertEqual(report['image_pages'],2)

    def test_partial_export_requires_sample_flag(self):
        self.scan(count=1)
        with self.assertRaisesRegex(ValueError,'incomplete'):
            build_epub(self.folder,self.folder/'book.epub',self.book,image_only=True)
        report=build_epub(self.folder,self.folder/'sample.epub',self.book,partial=True,image_only=True)
        self.assertTrue(report['partial'])

    def test_review_is_source_bound_and_survives_ocr_cache_changes(self):
        self.scan();entries=self.review_all()
        write_json(self.folder/'ocr/pages/00001.json',{'source_sha256':entries[0]['sha256'],'paragraphs':[{'text':'different raw OCR'}]})
        self.assertTrue(page_record(self.folder,entries[0])['reviewed'])
        changed=dict(entries[0],sha256='changed')
        with self.assertRaisesRegex(ValueError,'another source'):page_record(self.folder,changed)

    def test_ocr_cache_keys_include_settings_and_source_hash(self):
        self.scan(count=1)
        raw='level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n5\t1\t1\t1\t1\t1\t10\t10\t100\t25\t95\tReadable\n'
        with patch('opticalbook.generation.tesseract',return_value=('tesseract','')),patch('opticalbook.generation.tesseract_info',return_value=('5.5','List of available languages (1):\neng')),patch('opticalbook.generation.run',return_value=raw.encode()) as runner:
            recognize(self.folder,self.book,log=lambda _:None)
            recognize(self.folder,self.book,log=lambda _:None)
            self.assertEqual(runner.call_count,1)
            self.book['title']='A renamed book'
            recognize(self.folder,self.book,log=lambda _:None)
            self.assertEqual(runner.call_count,1)
            self.book['psm']=6
            recognize(self.folder,self.book,log=lambda _:None)
            self.assertEqual(runner.call_count,2)

    def test_second_capture_process_cannot_bypass_project_lock(self):
        (self.folder/'.capture.lock').write_text('{"pid":123}',encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError,'already locked'):
            self.scan()
        self.assertFalse((self.folder/'capture.json').exists())

    def test_explicit_cross_page_continuation_preserves_page_anchor(self):
        self.scan()
        entries=validate_manifest(self.folder)['pages']
        save_review(self.folder,entries[0],'A paragraph which')
        save_review(self.folder,entries[1],'continues on the next page.',continuation=True)
        output=self.folder/'joined.epub'
        report=build_epub(self.folder,output,self.book)
        self.assertEqual(report['cross_page_joins'],[2])
        with ZipFile(output) as archive:
            root=ET.fromstring(archive.read('EPUB/section-000.xhtml'))
            paragraphs=root.findall('.//{http://www.w3.org/1999/xhtml}p')
            self.assertEqual(len(paragraphs),1)
            self.assertEqual(''.join(paragraphs[0].itertext()),'A paragraph which continues on the next page.')
            self.assertEqual([node.attrib['id'] for node in paragraphs[0] if 'id' in node.attrib],['page-1','page-2'])

    def test_review_distinguishes_hard_linebreaks_from_paragraphs(self):
        self.scan()
        entries=validate_manifest(self.folder)['pages']
        for entry in entries: save_review(self.folder,entry,'First verse line\nSecond verse line\n\nA new paragraph.')
        output=self.folder/'verse.epub'
        build_epub(self.folder,output,self.book)
        with ZipFile(output) as archive:
            root=ET.fromstring(archive.read('EPUB/section-000.xhtml'))
            ns={'x':'http://www.w3.org/1999/xhtml'}
            self.assertEqual(len(root.findall('.//x:p',ns)),4)
            self.assertEqual(len(root.findall('.//x:br',ns)),2)


if __name__=='__main__':unittest.main()
