"""捕获验收器的内容判据：无需调用宿主、网络或模型。"""
import importlib.util
import json
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location(
    'ambient_media_live_helpers', Path(__file__).with_name('verify_napcat_ambient_media_live.py'))
assert spec is not None and spec.loader is not None
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class LiveMediaEvidenceTests(unittest.TestCase):
    def test_general_content_question_accepts_correct_color_shape_pairs(self):
        text = '上一张表情包里是一个红色正方形和一个蓝色圆形，并排放着。'
        self.assertTrue(helper.answer_evidence(text, require_positions=False)['passed'])

    def test_direct_position_question_still_requires_positions(self):
        text = '一个红色正方形和一个蓝色圆形。'
        self.assertFalse(helper.answer_evidence(text)['passed'])
        self.assertTrue(helper.answer_evidence('左边是红色正方形，右边是蓝色圆形。')['passed'])

    def test_wrong_pairs_and_unrelated_keywords_are_rejected(self):
        for text in ['红色圆形和蓝色正方形', '红色和蓝色，但不知道形状', '正方形和圆形，但不知道颜色']:
            with self.subTest(text=text):
                self.assertFalse(helper.answer_evidence(text, require_positions=False)['passed'])

    def test_incorrect_positions_are_rejected_even_for_general_question(self):
        text = '左边是蓝色圆形，右边是红色正方形。'
        self.assertFalse(helper.answer_evidence(text, require_positions=False)['passed'])

    def test_multiline_derivative_is_compared_after_json_decoding(self):
        derivative = {'kind': 'image', 'origin': '1901', 'author': '10001', 'text': '红色正方形\n蓝色圆形'}
        text = '本群已核验媒体资料：' + json.dumps([derivative], ensure_ascii=False)
        self.assertTrue(helper.contains_derivative(text, derivative))


if __name__ == '__main__':
    unittest.main()
