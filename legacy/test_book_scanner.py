"""Offline scanner checks: no camera or phone is opened."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET
from zipfile import ZipFile, ZIP_STORED

from PIL import Image

import book_scanner as scanner


class FakePhone:
    def __init__(self, serial):
        self.current = 1
        self.step = 1

    def page(self):
        return self.current, 3

    def next_page(self):
        self.current += self.step


class FakeCamera:
    def __init__(self, *args):
        pass

    def close(self):
        pass


class ScannerTests(unittest.TestCase):
    def setUp(self):
        self.test_workspace = Path(__file__).resolve().parent / "scanner-test-work"
        self.test_workspace.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=self.test_workspace)
        self.root = Path(self.temp.name).resolve()
        assert self.root.is_relative_to(self.test_workspace)
        self.config = self.root / "scanner.json"
        scanner.write_json(self.config, {"camera": "unused", "width": 120, "height": 180,
                           "corners": [[0, 0], [120, 0], [120, 180], [0, 180]],
                           "settle_seconds": 0})

    def tearDown(self):
        assert self.root.resolve().is_relative_to(self.test_workspace)
        self.temp.cleanup()

    def simulate(self, count=3, step=1, same_image=False):
        phone = FakePhone("")
        phone.step = step

        def photo(*args):
            image = Image.new("RGB", (120, 180), (60 if same_image else phone.current * 60,) * 3)
            return 100, image, image.copy()

        with patch.object(scanner, "Phone", return_value=phone), \
             patch.object(scanner, "Camera", FakeCamera), \
             patch.object(scanner, "best_photo", side_effect=photo):
            scanner.capture(self.config, count, start_page=1)

    def test_counter_requires_unambiguous_readeat_ui(self):
        xml = '<hierarchy><node package="com.readeat.app" text="15 / 949"/></hierarchy>'
        self.assertEqual(scanner.parse_page_xml(xml), (15, 949))
        self.assertEqual(scanner.parse_page_xml(xml.replace("15 / 949", "Сторінка 1 / 1548")),
                         (1, 1548))
        for invalid in [xml.replace("com.readeat.app", "other.app"),
                        xml.replace("15 / 949", ""),
                        xml.replace("15 / 949", "950 / 949")]:
            with self.assertRaises(RuntimeError):
                scanner.parse_page_xml(invalid)

    def test_capture_and_epub_preserve_sequence_and_xml(self):
        self.simulate()
        manifest = scanner.load_json(self.root / "capture.json")
        self.assertEqual([entry["page"] for entry in manifest["pages"]], [1, 2, 3])
        output = self.root / "test.epub"
        scanner.make_epub(self.root, output, 'Тест <&> "книга"', "Пірс Браун")
        with ZipFile(output) as archive:
            self.assertEqual(archive.namelist()[0], "mimetype")
            self.assertEqual(archive.getinfo("mimetype").compress_type, ZIP_STORED)
            self.assertEqual(archive.read("mimetype"), b"application/epub+zip")
            for name in archive.namelist():
                if name.endswith((".xml", ".xhtml", ".opf")):
                    ET.fromstring(archive.read(name))
            opf = ET.fromstring(archive.read("EPUB/package.opf"))
            ns = {"o": "http://www.idpf.org/2007/opf"}
            self.assertEqual([x.attrib["idref"] for x in opf.findall("o:spine/o:itemref", ns)],
                             ["p00001", "p00002", "p00003"])

    def test_skipped_page_stops_without_logging_gap(self):
        with self.assertRaisesRegex(RuntimeError, "Expected 2/3"):
            self.simulate(step=2)
        manifest = scanner.load_json(self.root / "capture.json")
        self.assertEqual([entry["page"] for entry in manifest["pages"]], [1])

    def test_duplicate_photo_stops_for_review(self):
        with self.assertRaisesRegex(RuntimeError, "resembles"):
            self.simulate(same_image=True)
        self.assertTrue((self.root / "review-possible-duplicate.png").is_file())
        self.assertEqual(len(scanner.load_json(self.root / "capture.json")["pages"]), 1)

    def test_resume_advances_from_last_saved_page(self):
        self.simulate(count=1)
        self.simulate(count=2)
        self.assertEqual([entry["page"] for entry in scanner.load_json(
            self.root / "capture.json")["pages"]], [1, 2, 3])

    def test_stop_file_prevents_camera_and_phone_access(self):
        (self.root / "STOP").touch()
        with patch.object(scanner, "Phone") as phone, patch.object(scanner, "Camera") as camera:
            with self.assertRaisesRegex(RuntimeError, "STOP file"):
                scanner.capture(self.config, 3)
            phone.assert_not_called()
            camera.assert_not_called()

    def test_incomplete_or_modified_book_cannot_be_exported_as_complete(self):
        self.simulate(count=1)
        output = self.root / "test.epub"
        with self.assertRaisesRegex(ValueError, "incomplete"):
            scanner.make_epub(self.root, output, "Test", "Author")
        scanner.make_epub(self.root, output, "Test", "Author", partial=True)
        with ZipFile(output) as archive:
            self.assertIn("sample, pages 1", archive.read("EPUB/package.opf").decode("utf-8"))
        Image.new("RGB", (120, 180)).save(self.root / "pages/00001.png")
        with self.assertRaisesRegex(ValueError, "checksum"):
            scanner.make_epub(self.root, self.root / "broken.epub", "Test", "Author", partial=True)
        self.assertFalse((self.root / "broken.epub").exists())
        self.assertFalse((self.root / "broken.epub.tmp").exists())

    def test_perspective_rectification_keeps_orientation(self):
        image = Image.new("RGB", (200, 300), "white")
        image.paste("red", (30, 40, 45, 55))
        page = scanner.rectify(image, {"corners": [[30, 40], [170, 40], [170, 260], [30, 260]]})
        self.assertEqual(page.size, (140, 220))
        self.assertEqual(page.getpixel((5, 5)), (255, 0, 0))
        self.assertEqual(page.getpixel((130, 210)), (255, 255, 255))

    def test_compact_epub_links_to_valid_jpeg_images(self):
        self.simulate()
        output = self.root / "compact.epub"
        scanner.make_epub(self.root, output, "Test", "Author", jpeg_quality=90)
        with ZipFile(output) as archive:
            self.assertIn('media-type="image/jpeg"', archive.read("EPUB/package.opf").decode())
            self.assertIn('images/p00001.jpg', archive.read("EPUB/p00001.xhtml").decode())
            with Image.open(__import__('io').BytesIO(archive.read("EPUB/images/p00001.jpg"))) as image:
                self.assertEqual(image.size, (120, 180))
                self.assertEqual(image.format, "JPEG")


if __name__ == "__main__":
    unittest.main()
