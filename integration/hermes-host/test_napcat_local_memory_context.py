"""Actual patched host methods; fixtures capture model and transport I/O."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from test_napcat_ambient_quiet import turn_case  # noqa: F401


@pytest.fixture(autouse=True)
def matching_host(turn_case):
    from gateway.run_turn_runner import TurnRunner
    if getattr(TurnRunner, 'public_memory_context_contract', None) != 1:
        pytest.skip('requires napcat-local-memory-context.patch in an isolated host')


def _turn(case, policy):
    case.adapter.public_memory_history_policy = policy
    return SimpleNamespace(_ctx=SimpleNamespace(source=case.source,
        history=[{'role':'assistant','content':'DELETED_FACT_TRANSCRIPT_CANARY'}]), _runner=case.runner)


def test_raw_transcript_and_cached_agent_history_are_not_replayed(turn_case):
    from gateway.run_turn_runner import TurnRunner
    assert TurnRunner.public_memory_context_contract == 1
    case = turn_case.prepare()
    turn = _turn(case, lambda source: {'mode':'local_events','contract':1})
    agent = SimpleNamespace(_session_messages=[{'role':'assistant','content':'DELETED_FACT_CACHE_CANARY'}])
    assert TurnRunner._load_turn_history(turn, agent, True) == ([], None, set())


def test_local_context_constructs_a_fresh_agent_without_consulting_cache(turn_case):
    from gateway.session_execution import GatewaySessionAgentMixin
    case = turn_case.prepare()
    turn = _turn(case, lambda source: {'mode':'local_events','contract':1})
    turn._skip_context_files = Mock(return_value=True)
    fresh = object()
    turn._build_fresh_agent = Mock(return_value=fresh)
    turn._lookup_cached_agent = Mock(side_effect=AssertionError('stale agent must not be read'))
    assert GatewaySessionAgentMixin._resolve_turn_agent(turn, {}, 'napcat', 'LOCAL_CONTEXT', 4, {}, None) == (fresh, False)
    turn._build_fresh_agent.assert_called_once()
    turn._lookup_cached_agent.assert_not_called()


def test_expired_source_and_unknown_policy_fail_closed(turn_case):
    from gateway.napcat_memory_context import local_memory_context
    case = turn_case.prepare()
    def expired(source): raise ValueError('expired fixture permit')
    with pytest.raises(ValueError): local_memory_context(_turn(case, expired))
    with pytest.raises(ValueError): local_memory_context(_turn(case, lambda source: {'mode':'trust_all'}))


def test_unconfigured_and_private_sources_keep_original_host_policy(turn_case):
    from gateway.napcat_memory_context import local_memory_context
    case = turn_case.prepare()
    assert not local_memory_context(_turn(case, lambda source: None))
    case.source.chat_type = 'dm'
    assert not local_memory_context(_turn(case, Mock(side_effect=AssertionError('private context is unrelated'))))
