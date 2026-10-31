"""Real GatewayRunner regressions for optional NapCat group replies.

Run independently (no host conftest is needed)::

    "$HERMES_TEST_PYTHON" -m pytest /path/to/test_napcat_ambient_quiet.py -q

HERMES_TEST_PYTHON must be a prepared isolated test interpreter with pytest;
the application runtime selected by ``hermes --run-module`` may not include it.

Set HERMES_SOURCE to a Hermes checkout to select the host under test; otherwise
use the source of the importable gateway package. Only AIAgent and transport
I/O are fake: display resolution, TurnRunner, callback wiring, progress queues,
and the text stream consumer execute their real host implementations.
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import os
from pathlib import Path
import sys
import time
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


def _host_source() -> Path:
    explicit = os.environ.get("HERMES_SOURCE")
    if explicit:
        root = Path(explicit).expanduser().resolve()
    else:
        spec = importlib.util.find_spec("gateway")
        if spec is None or not spec.submodule_search_locations:
            raise RuntimeError("Cannot find Hermes gateway; set HERMES_SOURCE to its source checkout")
        root = Path(next(iter(spec.submodule_search_locations))).resolve().parent
    helper = root / "tests/gateway/test_run_progress_topics.py"
    if not helper.is_file() or not (root / "gateway/run_turn.py").is_file():
        raise RuntimeError(f"HERMES_SOURCE must contain gateway source and host test helpers: {root}")
    return root


# Source discovery is read-only. All host imports are deferred until after the
# monkeypatch fixture isolates the profile, including collection-time globals.
_HOST_SOURCE = _host_source()


@pytest.fixture
def turn_case(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(_HOST_SOURCE))
    # Reuse the host's credential/behavior filtering without importing its
    # conftest (which is not an ancestor of this independent test file).
    from tests._fixtures.env_filter import _HERMES_BEHAVIORAL_VARS, _looks_like_credential

    for name in list(os.environ):
        if _looks_like_credential(name) or name in _HERMES_BEHAVIORAL_VARS or name.startswith("HERMES_SESSION_"):
            monkeypatch.delenv(name, raising=False)
    home = tmp_path / "hermes"
    home.mkdir()
    for name in ("memories", "sessions", "skills", "plugins"):
        (home / name).mkdir()
    os_home = tmp_path / "os-home"
    os_home.mkdir()
    monkeypatch.setenv("HOME", str(os_home))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_TEST_ISOLATION", str(home))
    monkeypatch.setenv("HERMES_DISABLE_LAZY_INSTALLS", "1")
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "gateway-locks"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(os_home / ".config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(os_home / ".local/state"))
    monkeypatch.setenv("HERMES_TOOL_PROGRESS_MODE", "all")
    monkeypatch.delenv("HERMES_STATE_DB_GUARD_BYPASS", raising=False)
    monkeypatch.chdir(tmp_path)

    import hermes_constants

    monkeypatch.setattr(hermes_constants, "_get_platform_default_hermes_home", lambda: home)
    monkeypatch.setattr(hermes_constants, "_PINNED_PROCESS_HERMES_HOME", None)
    home_token = hermes_constants.set_hermes_home_override(home)

    state = SimpleNamespace(agent_calls=[], contexts=[], callback_attempts=[])

    class CandidateAgent:
        """Like SilentHeartbeatAgent, but also attempts commentary and deltas."""

        def __init__(self, **kwargs):
            self.tool_progress_callback = kwargs.get("tool_progress_callback")
            self.interim_assistant_callback = kwargs.get("interim_assistant_callback")
            self.stream_delta_callback = kwargs.get("stream_delta_callback")
            self.tools = []

        def run_conversation(self, message, conversation_history=None, task_id=None, **kwargs):
            state.agent_calls.append((message, task_id, kwargs))
            state.callback_attempts.append("tool")
            if self.tool_progress_callback:
                self.tool_progress_callback("tool.started", "terminal", "ambient-progress-probe", {})
                self.tool_progress_callback("_thinking", "_thinking", "ambient-thinking-probe", {})
            # Allow the real asynchronous progress task to drain before the
            # commentary boundary resets the stream/progress cursor.
            time.sleep(0.35)
            state.callback_attempts.append("interim")
            if self.interim_assistant_callback:
                self.interim_assistant_callback("ambient-interim-probe", already_streamed=False)
            time.sleep(0.1)
            state.callback_attempts.append("delta")
            if self.stream_delta_callback:
                self.stream_delta_callback("ambient-delta-probe")
            time.sleep(0.35)
            return {"final_response": state.final_response, "messages": [], "api_calls": 1}

    fake_agent = types.ModuleType("run_agent")
    setattr(fake_agent, "AIAgent", CandidateAgent)
    monkeypatch.setitem(sys.modules, "run_agent", fake_agent)
    fake_dotenv = types.ModuleType("dotenv")
    setattr(fake_dotenv, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    try:
        helper_spec = importlib.util.spec_from_file_location(
            "_companion_napcat_ambient_progress_helpers",
            _HOST_SOURCE / "tests/gateway/test_run_progress_topics.py",
        )
        assert helper_spec is not None and helper_spec.loader is not None
        helpers = importlib.util.module_from_spec(helper_spec)
        monkeypatch.setitem(sys.modules, helper_spec.name, helpers)
        helper_spec.loader.exec_module(helpers)
        gateway_run = importlib.import_module("gateway.run")
        assert gateway_run.__file__ is not None
        assert Path(gateway_run.__file__).resolve().parent.parent == _HOST_SOURCE
        monkeypatch.setattr(gateway_run, "_hermes_home", home)
        monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "test-only"})
        from agent import secret_scope
        from gateway.platform_registry import PlatformEntry, platform_registry

        monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", False)
        # Register only the capture adapter, never discover/install the live
        # NapCat plugin or contact OneBot. Keep registry changes reversible.
        monkeypatch.setitem(platform_registry._entries, "napcat", PlatformEntry(
            name="napcat", label="NapCat test", adapter_factory=lambda config: None,
            check_fn=lambda: True,
        ))
        monkeypatch.setattr(helpers.Platform, "_value2member_map_", dict(helpers.Platform._value2member_map_))
        monkeypatch.setattr(helpers.Platform, "_member_map_", dict(helpers.Platform._member_map_))

        class CaptureAdapter(helpers.ProgressCaptureAdapter):
            def tool_authority_for_source(self, source):
                assert source is state.source
                # Satisfy the real group authority seam without authorizing any
                # tool execution. CandidateAgent emits callbacks, not tools.
                return lambda name, arguments: "test_tool_execution_denied"

        def prepare(platform_name="napcat", *, streaming=True):
            platform = helpers.Platform(platform_name)
            adapter = CaptureAdapter(platform=platform)
            runner = helpers._make_runner(adapter)
            runner.config.streaming = helpers.StreamingConfig(
                enabled=streaming, transport="edit", buffer_threshold=1, edit_interval=0.01,
            )
            # Every surface is explicitly enabled. Quiet assertions must be
            # caused by the optional-reply gate, not platform-tier defaults.
            import hermes_yaml as yaml

            display = {"tool_progress": "all", "interim_assistant_messages": True,
                       "thinking_progress": True, "streaming": True,
                       "long_running_notifications": True}
            (home / "config.yaml").write_text(yaml.safe_dump({
                "display": {"platforms": {platform_name: display}},
            }), encoding="utf-8")
            source = helpers.SessionSource(
                platform=platform, chat_id="group:20001", chat_type="group",
                user_id="10001", user_name="member", message_id="901",
            )
            state.source = source
            build_context = runner._run_agent_build_turn_context

            def capture_context(*args, **kwargs):
                built = build_context(*args, **kwargs)
                state.contexts.append(built[0])
                return built

            monkeypatch.setattr(runner, "_run_agent_build_turn_context", capture_context)
            state.tts_start = Mock(wraps=runner._run_agent_start_streaming_tts)
            state.long_running = AsyncMock(wraps=runner._run_agent_notify_long_running)
            monkeypatch.setattr(runner, "_run_agent_start_streaming_tts", state.tts_start)
            monkeypatch.setattr(runner, "_run_agent_notify_long_running", state.long_running)
            state.adapter, state.runner = adapter, runner
            return state

        async def run(reply_expected, *, final_response="done"):
            state.final_response = final_response
            event = helpers.MessageEvent(
                text="ordinary group conversation", message_type=helpers.MessageType.TEXT,
                source=state.source, message_id="901", internal=False,
                reply_expected=reply_expected,
            )
            source_before = state.source.to_dict()
            result = await state.runner._run_agent(
                message=event.text, context_prompt="", history=[], source=event.source,
                session_id="ambient-quiet-session",
                session_key=f"agent:main:{state.source.platform.value}:group:20001",
                event_message_id=event.message_id, inbound_message_id=event.message_id,
                reply_expected=event.reply_expected,
            )
            # Give thread-safe status sends a final event-loop scheduling turn.
            await asyncio.sleep(0.05)
            assert event.internal is False
            assert not getattr(event, "_heartbeat_session_id", None)
            ctx = state.contexts[-1]
            assert ctx.source is event.source is state.source
            assert state.source.to_dict() == source_before
            assert ctx.source.message_id == ctx.event_message_id == ctx.inbound_message_id == "901"
            assert ctx.scheduled_heartbeat is False
            assert ctx.persist_user_display_kind is None
            assert ctx.reply_expected is reply_expected
            assert ctx.mute_notification_reply is False
            assert callable(ctx._status_callback_sync), "actionable status callbacks must stay wired"
            return result

        state.prepare, state.run = prepare, run
        yield state
    finally:
        hermes_constants.reset_hermes_home_override(home_token)


@pytest.mark.parametrize("streaming", [False, True], ids=["interim-only", "stream-enabled"])
def test_unaddressed_napcat_candidate_has_no_visible_progress(turn_case, streaming):
    case = turn_case.prepare(streaming=streaming)
    result = asyncio.run(case.run(False, final_response="NO_REPLY"))

    assert len(case.agent_calls) == 1, "the normal agent must run, not be skipped as internal/heartbeat work"
    assert case.callback_attempts == ["tool", "interim", "delta"]
    assert result["final_response"] == "NO_REPLY"
    assert case.adapter.sent == []
    assert case.adapter.edits == []
    assert case.adapter.typing == []
    assert case.contexts[-1].stream_consumer_holder == [None]
    assert case.contexts[-1].streaming_tts_consumer_holder == [None]
    case.tts_start.assert_not_called()
    case.long_running.assert_not_called()


@pytest.mark.parametrize("reply_expected", [True, None], ids=["addressed", "legacy-unspecified"])
def test_napcat_expected_or_unspecified_reply_keeps_progress(turn_case, reply_expected):
    case = turn_case.prepare()
    result = asyncio.run(case.run(reply_expected))

    assert result["final_response"] == "done"
    assert len(case.agent_calls) == 1
    assert any("ambient-progress-probe" in call["content"] for call in case.adapter.sent)
    assert any("ambient-interim-probe" in call["content"] for call in case.adapter.sent)
    assert any("ambient-delta-probe" in call["content"] for call in case.adapter.sent + case.adapter.edits)
    assert case.adapter.typing
    case.tts_start.assert_called_once()
    case.long_running.assert_awaited_once()


@pytest.mark.parametrize("platform_name", ["telegram", "qqbot"])
def test_other_platform_optional_reply_keeps_normal_progress(turn_case, platform_name):
    case = turn_case.prepare(platform_name)
    result = asyncio.run(case.run(False))

    assert result["final_response"] == "done"
    assert len(case.agent_calls) == 1
    assert any("ambient-progress-probe" in call["content"] for call in case.adapter.sent)
    assert any("ambient-interim-probe" in call["content"] for call in case.adapter.sent)
    assert case.adapter.typing
    case.tts_start.assert_called_once()
    case.long_running.assert_awaited_once()
