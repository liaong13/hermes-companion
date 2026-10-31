"""Out-of-process text notifications to the configured owner's private chat.

No inbound handlers, social agents, history, media or group capabilities are
created. An ambiguous send is never retried within an invocation.
"""
import os
import re
from .transport import OneBotClient


def parse_target(target):
    if isinstance(target, str) and re.fullmatch(r'(?:dm|group):[1-9][0-9]{0,19}', target):
        return target, None
    return None


async def send_owner_notice(config, chat_id, message, *, thread_id=None,
                            media_files=None, force_document=False):
    from .adapter import Settings, message_id
    client = None
    stage = 'configuration'
    try:
        settings = Settings(config.extra or {})
        if (chat_id != 'dm:' + settings.owner_id or thread_id is not None
                or media_files or force_document or not isinstance(message, str)
                or not message or len(message) > 4000):
            return {'error': 'Standalone NapCat supports owner private text notices only (1-4000 characters)'}

        async def ignore_event(event):
            pass

        client = OneBotClient(settings.url, os.environ.get('NAPCAT_ACCESS_TOKEN', ''),
                              settings.self_id, ignore_event, timeout=10)
        stage = 'connection'
        await client.start()
        stage = 'send'
        result = await client.call('send_private_msg', {
            'user_id': int(settings.owner_id),
            'message': [{'type': 'text', 'data': {'text': message}}]})
        stage = 'receipt'
        if not isinstance(result, dict):
            raise ValueError('invalid receipt')
        return {'success': True, 'message_id': message_id(result.get('message_id', ''))}
    except Exception as exc:
        return {'error': 'NapCat standalone ' + stage + ' failed (' + type(exc).__name__ + '); delivery may be unknown; no automatic retry'}
    finally:
        if client is not None:
            await client.close()
