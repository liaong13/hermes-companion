"""Synthetic notification contract tests; never contact QQ."""
import unittest
from unittest.mock import AsyncMock, patch
from test_plugin import config
from hermes_napcat import standalone


class StandaloneTests(unittest.IsolatedAsyncioTestCase):
    def test_target_parser(self):
        self.assertEqual(standalone.parse_target('dm:10001'), ('dm:10001', None))
        self.assertEqual(standalone.parse_target('group:20001'), ('group:20001', None))
        for value in ('10001', 'dm:010001', 'dm:10001:5', 'dm:abc', ''):
            self.assertIsNone(standalone.parse_target(value))

    async def test_owner_success_and_cleanup(self):
        with patch.object(standalone, 'OneBotClient') as factory:
            client = factory.return_value
            client.start = AsyncMock()
            client.call = AsyncMock(return_value={'message_id': 123})
            client.close = AsyncMock()
            result = await standalone.send_owner_notice(config(), 'dm:10001', 'SYNTHETIC notice')
            self.assertEqual(result, {'success': True, 'message_id': '123'})
            client.call.assert_awaited_once_with('send_private_msg', {
                'user_id': 10001, 'message': [{'type': 'text', 'data': {'text': 'SYNTHETIC notice'}}]})
            client.close.assert_awaited_once()

    async def test_denied_requests_never_connect(self):
        with patch.object(standalone, 'OneBotClient') as factory:
            for target in ('dm:10002', 'group:20001', 'dm:99999'):
                self.assertIn('error', await standalone.send_owner_notice(config(), target, 'test'))
            for kwargs in ({'thread_id': '1'}, {'media_files': ['x']}, {'force_document': True}):
                self.assertIn('error', await standalone.send_owner_notice(config(), 'dm:10001', 'test', **kwargs))
            for text in ('', 'x' * 4001, None):
                self.assertIn('error', await standalone.send_owner_notice(config(), 'dm:10001', text))
            factory.assert_not_called()

    async def test_ambiguous_failure_no_retry_and_cleanup(self):
        with patch.object(standalone, 'OneBotClient') as factory:
            client = factory.return_value
            client.start = AsyncMock()
            client.call = AsyncMock(side_effect=RuntimeError('SYNTHETIC secret'))
            client.close = AsyncMock()
            result = await standalone.send_owner_notice(config(), 'dm:10001', 'test')
            self.assertIn('error', result)
            self.assertNotIn('secret', str(result))
            client.call.assert_awaited_once()
            client.close.assert_awaited_once()

    async def test_malformed_receipt_not_accepted(self):
        with patch.object(standalone, 'OneBotClient') as factory:
            client = factory.return_value
            client.start = AsyncMock()
            client.call = AsyncMock(return_value={})
            client.close = AsyncMock()
            self.assertIn('error', await standalone.send_owner_notice(config(), 'dm:10001', 'test'))
            client.close.assert_awaited_once()
