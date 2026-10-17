from __future__ import annotations
import argparse
import importlib.util
import json
from pathlib import Path
import signal
import sys
import threading
import time
from PIL import Image
from .common import defaults, fingerprint, load_project, write_json


def main():
    parser = argparse.ArgumentParser(description='Local optical book capture and OCR-to-EPUB. With no command, opens the desktop UI.')
    sub = parser.add_subparsers(dest='command')
    gui = sub.add_parser('gui',help='Open desktop UI')
    gui.add_argument('--project',type=Path)
    init = sub.add_parser('init',help='Create a project with configurable defaults')
    init.add_argument('project',type=Path)
    importer = sub.add_parser('import-images',help='Import naturally sorted image files into a NEW project')
    importer.add_argument('source',type=Path)
    importer.add_argument('project',type=Path)
    doctor = sub.add_parser('doctor',help='Report Python dependencies and optional configured external tools')
    doctor.add_argument('--project',type=Path)
    bundler = sub.add_parser('bundle-tesseract',help='Copy the Tesseract runtime and models from a local Windows install into the app')
    bundler.add_argument('source',type=Path,help='Folder containing tesseract.exe and tessdata')
    bundler.add_argument('--languages',nargs='+',default=['eng','ukr'],help='Models to include (default: eng ukr)')
    bundler.add_argument('--replace',action='store_true',help='Rebuild an existing bundle')
    for name in ('capture','photo','calibrate','ocr','build','verify'):
        command = sub.add_parser(name)
        command.add_argument('--project',type=Path,required=True)
        if name == 'capture':
            command.add_argument('--start',type=int,required=True)
            command.add_argument('--count',type=int,default=3)
        if name == 'photo': command.add_argument('--output',type=Path,required=True)
        if name == 'calibrate':
            command.add_argument('--photo',type=Path,required=True)
            command.add_argument('--corners',type=float,nargs=8,help='Readable text area (cropped page), 4 x,y photo pixels')
            command.add_argument('--screen',type=float,nargs=8,help='Whole phone screen, aligns gestures to the photo')
            command.add_argument('--indicator',type=float,nargs=8,help='Reader page indicator / counter')
            command.add_argument('--tap',type=float,nargs=2,help='Next-page tap at this photo pixel (needs screen)')
            command.add_argument('--swipe',type=float,nargs=4,help='Next-page swipe start x,y and end x,y in photo pixels (needs screen)')
        if name == 'build':
            command.add_argument('--output',type=Path,required=True)
            command.add_argument('--sample',action='store_true')
            command.add_argument('--images',action='store_true')
        if name == 'verify': command.add_argument('--sample',action='store_true')
    args = parser.parse_args()
    if not args.command or args.command=='gui':
        from .ui import launch
        launch(getattr(args,'project',None))
        return
    try:
        if args.command=='init':
            args.project.mkdir(parents=True,exist_ok=False)
            write_json(args.project/'project.json',defaults())
            print('Created',args.project)
            return
        if args.command=='import-images':
            from .generation import import_images
            count = import_images(args.source,args.project)
            write_json(args.project/'project.json',defaults())
            print('Imported',count,'images. Check order before OCR.')
            return
        if args.command=='bundle-tesseract':
            from .bundle import bundle_tesseract
            version,count,size=bundle_tesseract(args.source,args.languages,args.replace)
            print(f'Bundled {version}: {count} files, {size/1e6:.0f} MB. Projects using the default "tesseract" now use it.')
            return
        if args.command=='doctor':
            from .common import bundled_tesseract
            result = {'python':sys.version.split()[0], 'bundled_tesseract':str(bundled_tesseract() or ''), 'modules':{name:bool(importlib.util.find_spec(name)) for name in ('PIL','numpy','cv2','tkinter','wordfreq')}}
            if args.project:
                from .generation import tesseract_info
                from .android import devices
                config=load_project(args.project)
                for name,call in [('tesseract',lambda:tesseract_info(config['book'])),('adb_devices',lambda:devices(config['capture']['adb']))]:
                    try: result[name]=call()
                    except Exception as error: result[name]={'error':str(error)}
            print(json.dumps(result,indent=2,ensure_ascii=True))
            return
        config = load_project(args.project)
        stop = threading.Event()
        signal.signal(signal.SIGINT,lambda *_:stop.set())
        if args.command=='verify':
            from .capture import validate_manifest
            value=validate_manifest(args.project,complete=not args.sample)
            print('Verified',len(value['pages']),'page image checksums and dimensions.')
        elif args.command=='ocr':
            from .generation import recognize
            print('OCR pages ready:',recognize(args.project,config['book'],stop))
        elif args.command=='build':
            from .generation import build_epub
            print(json.dumps(build_epub(args.project,args.output,config['book'],args.sample,args.images),indent=2))
        elif args.command=='calibrate':
            from .imaging import REGIONS,gesture_from,rectify,sharpness
            given={'corners':args.corners,'screen_corners':args.screen,'indicator_corners':args.indicator}
            if not any(given.values()) and not (args.tap or args.swipe): raise ValueError('Give --corners, --screen, --indicator, --tap or --swipe.')
            if args.tap and args.swipe: raise ValueError('Choose either --tap or --swipe.')
            directory=args.project/'calibration'
            directory.mkdir(exist_ok=True)
            with Image.open(args.photo) as image:
                for key,values in given.items():
                    if not values: continue
                    corners=[values[i:i+2] for i in range(0,8,2)]
                    name,minimum=REGIONS[key]
                    crop=rectify(image,corners,minimum)
                    config['capture'][key]=corners
                    crop.save(directory/name)
                    print(f'Saved {key} calibration to calibration/{name}. Crop sharpness: {sharpness(crop):.1f}')
                if any(given.values()): config['capture']['calibration_camera']=fingerprint(config['capture']['camera'])
                if args.indicator:
                    from .indicator import read_indicator
                    try: print('Page indicator OCR:',read_indicator(image,config['capture']))
                    except (OSError,RuntimeError) as error: print('Page indicator OCR unavailable:',error)
                if args.tap or args.swipe:
                    values=args.tap or args.swipe
                    if any(not 0<=x<=size for x,size in zip(values,image.size*2)): raise ValueError('Gesture points lie outside the photo.')
                    gesture=gesture_from(config['capture']['screen_corners'],[values[i:i+2] for i in range(0,len(values),2)],'tap' if args.tap else 'swipe')
                    config['capture']['gesture'].update(gesture)
                    print('Saved next-page gesture (screen fractions):',gesture)
            write_json(args.project/'project.json',config)
        else:
            from .imaging import Camera
            from .capture import capture
            camera=Camera(config['capture']['camera'])
            try:
                camera.frame()
                if args.command=='photo':
                    if args.output.exists(): raise FileExistsError(args.output)
                    if not stop.wait(3):
                        _,image=camera.frame(after=camera.sequence)
                        args.output.parent.mkdir(parents=True,exist_ok=True)
                        image.save(args.output)
                        print('Saved test photo:',args.output)
                else:
                    print('Pages saved:',capture(args.project,config['capture'],camera,args.start,args.count,stop))
            finally:
                camera.close()
    except (Exception,KeyboardInterrupt) as error:
        print('Error:',error,file=sys.stderr)
        raise SystemExit(1)


if __name__=='__main__':
    main()
