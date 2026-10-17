import unittest

from ocr_reflow import join_lines, normalize_text
from build_reflow import assemble, normalized_paragraphs


def paragraph(text, indented=False, heading=False):
    return {"text":text,"first_indent":indented,"heading":heading,"top":610,"bottom":650}


def page(number, paragraphs, **extra):
    return {"page":number,"paragraphs":paragraphs,"map":False,"illustration":None,
            "source_sha256":"source",**extra}


class ReflowTests(unittest.TestCase):
    def test_wrap_hyphens_use_dictionary_and_preserve_compounds(self):
        self.assertEqual(join_lines(["пере-","міщення"]),"переміщення")
        self.assertEqual(join_lines(["суверен-","правительки"]),"суверен-правительки")
        self.assertEqual(join_lines(["Він сказав, -","що повернеться."]),"Він сказав, — що повернеться.")

    def test_known_optical_pipe_and_dialogue_normalization(self):
        self.assertEqual(normalize_text("- Він. | тоді - вона."),"— Він. І тоді — вона.")

    def test_page_break_inside_paragraph_reflows_without_losing_anchor(self):
        records=[page(11,[paragraph("Він побачив пере-")]),
                 page(12,[paragraph("міщення корабля.")])]
        sections,assets,links,joins=assemble(records,{})
        nodes=sections[0]['nodes']
        self.assertEqual(len(nodes),1)
        self.assertEqual(nodes[0]['text'],"Він побачив переміщення корабля.")
        self.assertIn('id="page-11"',nodes[0]['html'])
        self.assertIn('id="page-12"',nodes[0]['html'])
        self.assertEqual(links,[(11,0),(12,0)])
        self.assertEqual(joins,[12])

    def test_new_dialogue_and_indentation_prevent_page_merge(self):
        for following in [paragraph("— Ні."),paragraph("Новий абзац.",indented=True)]:
            sections,*_=assemble([page(11,[paragraph("Кінець.")]),page(12,[following])],{})
            self.assertEqual(len(sections[0]['nodes']),2)

    def test_reviewed_heading_replaces_art_noise_and_keeps_body(self):
        record=page(14,[{**paragraph("noise"),"top":200},{**paragraph("wrong title"),"top":450},
                        paragraph("Моє мовчання.")],illustration=[100,10,500,425])
        corrections={"_chapter_headings":{"14":{"number":1,"title":"ВОЄНАЧАЛЬНИКИ","source_sha256":"source"}}}
        values=normalized_paragraphs(record,corrections)
        self.assertEqual([v['text'] for v in values],["1. ВОЄНАЧАЛЬНИКИ","Моє мовчання."])
        sections,assets,links,_=assemble([record],corrections)
        self.assertEqual(sections[1]['title'],"1. ВОЄНАЧАЛЬНИКИ")
        self.assertEqual(len(assets),1)
        self.assertEqual(links,[(14,1)])

    def test_proofread_override_is_bound_to_source_checksum(self):
        record=page(4,[paragraph("bad OCR")])
        corrections={"_page_overrides":{"4":{"source_sha256":"different","paragraphs":[]}}}
        with self.assertRaises(RuntimeError):normalized_paragraphs(record,corrections)

    def test_maps_are_images_and_have_page_navigation(self):
        sections,assets,links,_=assemble([page(5,[],map=True)],{})
        self.assertEqual(links,[(5,0)])
        self.assertEqual(assets[0]['kind'],'map')
        self.assertIn('<img ',sections[0]['nodes'][0]['html'])


if __name__=='__main__':unittest.main()
