import unittest
from paragraph_layout import paragraph_starts
from repair_paragraphs import repair_record


def line(y, text='Звичайний рядок продовження тексту', left=20, right=660, group=1, **extra):
    return dict(top=y, bottom=y+24, left=left, right=right,
                text=text, group=[1, group], **extra)


class ParagraphLayoutTests(unittest.TestCase):
    def test_ocr_block_change_inside_full_line_is_a_wrap(self):
        values = [line(0), line(30, group=2), line(60, group=3)]
        starts, _ = paragraph_starts(values, 680)
        self.assertEqual(starts, ['page-start', None, None])

    def test_period_at_right_edge_does_not_force_a_paragraph(self):
        starts, _ = paragraph_starts([line(0, text='Речення завершилося.'),
                                     line(30, group=2), line(60)], 680)
        self.assertIsNone(starts[1])

    def test_real_indent_and_blank_line_are_preserved(self):
        values = [line(0), line(30), line(60, left=56), line(90), line(150)]
        starts, _ = paragraph_starts(values, 680)
        self.assertEqual(starts, ['page-start', None, 'indentation', None, 'vertical-space'])

    def test_short_line_and_new_group_preserve_unindented_break(self):
        starts, _ = paragraph_starts([line(0), line(30, right=300), line(60, group=2)], 680)
        self.assertEqual(starts[2], 'short-line-and-group')

    def test_dialogue_and_explicit_dropcap_continuation(self):
        values = [line(0), line(30, text='— Нова репліка.', left=56),
                  line(60, left=56, continuation=True), line(90)]
        starts, _ = paragraph_starts(values, 680)
        self.assertEqual(starts, ['page-start', 'dialogue', None, None])

    def test_single_stray_left_edge_does_not_shift_body_margin(self):
        starts, metrics = paragraph_starts([line(0, left=0), line(30), line(60), line(90)], 680)
        self.assertEqual(metrics['margin'], 20)
        self.assertEqual(starts, ['page-start', None, None, None])

    def test_review_copy_merges_only_a_proven_false_break(self):
        values = [line(0), line(30, group=2), line(60, left=56)]
        record = {'page': 21, 'lines': values, 'paragraphs': [
            dict(text='Початок речення', top=0, bottom=24, first_indent=False, heading=False),
            dict(text='його продовження.', top=30, bottom=54, first_indent=False, heading=False),
            dict(text='Новий абзац.', top=60, bottom=84, first_indent=True, heading=False)]}
        repaired, changes = repair_record(record)
        self.assertEqual(len(changes), 1)
        self.assertEqual([p['text'] for p in repaired['paragraphs']],
                         ['Початок речення його продовження.', 'Новий абзац.'])
        self.assertEqual(len(record['paragraphs']), 3)
        opening = dict(record, illustration=[1, 2, 3, 4])
        self.assertEqual(repair_record(opening), (opening, []))


if __name__ == '__main__':
    unittest.main()
