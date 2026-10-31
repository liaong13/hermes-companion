"""短期媒体引用的自然说法；只解析类型，不授权、搜索或下载资料。"""
import unittest
import test_plugin  # bootstraps the reviewed real package in a synthetic home
from hermes_napcat.media import selection


class NaturalMediaSelectionTests(unittest.TestCase):
    def test_common_image_reference_forms(self):
        for text in [
            '看一下上一张表情包是什么内容',
            '上一张图片里写了什么',
            '看下这张截图',
            '帮我描述一下这张照片',
            '刚才发的那张动图是什么',
            '上面那张相片是什么意思',
            '这张梗图讲什么',
            '看看图片内容',
            '麻烦你帮我分析图片内容',
            '图片里写了什么',
            '看看我刚发的图',
            '刚刚发的那张图',
        ]:
            with self.subTest(text=text):
                self.assertEqual(selection(text), ('image', None))

    def test_other_explicit_media_references_keep_their_type(self):
        for text, kind in [
            ('看一下上一份文件的内容', 'file'),
            ('刚发的文件', 'file'),
            ('转写上一条语音', 'record'),
        ]:
            with self.subTest(text=text):
                self.assertEqual(selection(text), (kind, None))

    def test_capability_or_creation_questions_are_not_source_references(self):
        for text in [
            '图片识别是怎么实现的', '怎么制作表情包',
            '你支持识别图片吗', '能识别图片吗',
            '解释一下图片识别算法', '今天的天气怎么样',
        ]:
            with self.subTest(text=text):
                self.assertIsNone(selection(text))

    def test_cancel_or_negative_request_does_not_start_media_analysis(self):
        for text in ['别看上一张图', '不要分析这张图片', '不用看刚发的图了', '不是上一张表情包']:
            with self.subTest(text=text):
                self.assertIsNone(selection(text))

    def test_explicit_handle_command_is_preserved(self):
        self.assertEqual(selection('/media image original-handle_42'), ('image', 'original-handle_42'))
        self.assertEqual(selection('/media file'), ('file', None))

    def test_malformed_command_is_not_repaired_by_natural_matching(self):
        self.assertIsNone(selection('/media image ../上一张图片'))
        self.assertIsNone(selection('/media unknown 上一张图片'))

    def test_mixed_media_references_are_not_arbitrarily_reduced_to_one(self):
        self.assertIsNone(selection('看看上一张图和上一份文件'))

    def test_remote_ordinals_are_not_guessed_as_the_previous_item(self):
        self.assertIsNone(selection('看看上上张表情包'))
        self.assertIsNone(selection('看倒数第二张图片'))


if __name__ == '__main__':
    unittest.main()
