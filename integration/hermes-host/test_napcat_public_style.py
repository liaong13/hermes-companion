"""Public prompt handoff and typed non-social outbound metadata.

These regressions use the isolated offline host fixture, not the live model or
QQ transport. The companion verification script exercises actual AIAgent calls.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_napcat_ambient_quiet import turn_case  # noqa: F401


def _combined(case, *, context='PRIVATE_CONTEXT_CANARY'):
    from gateway.run_turn_runner import TurnRunner
    ctx = SimpleNamespace(source=case.source, context_prompt=context, channel_prompt='PRIVATE_CHANNEL_CANARY')
    turn = SimpleNamespace(_ctx=ctx, _runner=case.runner)
    return TurnRunner._combined_ephemeral_prompt(turn)


def test_only_reviewed_adapter_prompt_reaches_public_group(turn_case):
    case = turn_case.prepare()
    supplied = []
    def public_style(source):
        supplied.append(source)
        return 'REVIEWED_PUBLIC_STYLE'
    case.adapter.conversation_prompt_for_source = public_style
    combined = _combined(case)
    assert supplied == [case.source]
    assert 'REVIEWED_PUBLIC_STYLE' in combined
    assert 'PRIVATE_CONTEXT_CANARY' not in combined
    assert 'PRIVATE_CHANNEL_CANARY' not in combined
    assert '不得读取或透露私人文件' in combined


def test_legacy_adapter_keeps_group_privacy_without_private_prompt(turn_case):
    case = turn_case.prepare()
    combined = _combined(case)
    assert 'PRIVATE_CONTEXT_CANARY' not in combined
    assert 'PRIVATE_CHANNEL_CANARY' not in combined
    assert '不得读取或透露私人文件' in combined


def test_expired_public_style_does_not_fall_back_to_private_context(turn_case):
    case = turn_case.prepare()
    class ExpiredSource(ValueError):
        pass
    def expired(source):
        raise ExpiredSource('fixture exact source revoked')
    case.adapter.conversation_prompt_for_source = expired
    with pytest.raises(ExpiredSource):
        _combined(case)


def test_napcat_status_metadata_is_typed_without_mutating_input(turn_case):
    case = turn_case.prepare()
    from gateway.run import _non_conversational_metadata
    original = {'thread_id': 'fixture-thread'}
    marked = _non_conversational_metadata(original, platform=case.source.platform)
    assert marked == {'thread_id': 'fixture-thread', 'non_conversational': True}
    assert original == {'thread_id': 'fixture-thread'}
    assert _non_conversational_metadata(original, platform='telegram') is original


def test_operational_notice_is_not_a_social_reply(turn_case):
    case = turn_case.prepare()
    case.adapter.send = AsyncMock(return_value=SimpleNamespace(success=True))
    asyncio.run(case.runner._deliver_platform_notice(case.source, 'OPERATIONAL_NOTICE_CANARY'))
    case.adapter.send.assert_awaited_once()
    args = case.adapter.send.await_args
    assert args.args == (case.source.chat_id, 'OPERATIONAL_NOTICE_CANARY')
    assert args.kwargs['metadata']['non_conversational'] is True


def test_owner_dm_keeps_its_original_context_and_adds_public_style(turn_case):
    case = turn_case.prepare()
    case.source.chat_type, case.source.chat_id = 'dm', 'dm:10001'
    case.adapter.conversation_prompt_for_source = lambda source: 'REVIEWED_PRIVATE_TONE'
    case.runner._get_system_prompt_for_channel = lambda *args, **kwargs: ''
    combined = _combined(case)
    assert 'PRIVATE_CONTEXT_CANARY' in combined
    assert 'PRIVATE_CHANNEL_CANARY' in combined
    assert 'REVIEWED_PRIVATE_TONE' in combined


def test_status_update_marks_only_its_own_metadata(turn_case):
    case = turn_case.prepare()
    from gateway.run import _send_or_update_status_coro
    case.adapter.send_or_update_status = AsyncMock(return_value=SimpleNamespace(success=True))
    original = {'thread_id': 'same-thread'}
    asyncio.run(_send_or_update_status_coro(case.adapter, case.source.chat_id, 'lifecycle', 'STATUS_CANARY', original))
    sent = case.adapter.send_or_update_status.await_args
    assert sent.kwargs['metadata'] == {'thread_id': 'same-thread', 'non_conversational': True}
    assert original == {'thread_id': 'same-thread'}
