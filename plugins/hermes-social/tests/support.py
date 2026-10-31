# pyright: reportMissingImports=false
"""Offline capabilities and real host LLM-facade fixture; never connects QQ.

The hyphenated distribution is loaded under its Python name at runtime.
"""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any, Callable

# The managed launcher uses isolated Python mode, which ignores PYTHON* env.
# Prevent imports of sibling source from producing runtime artifacts there.
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
if 'hermes_social' not in sys.modules:
    spec = importlib.util.spec_from_file_location('hermes_social', ROOT / '__init__.py',
                                                submodule_search_locations=[str(ROOT)])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules['hermes_social'] = module
    spec.loader.exec_module(module)

from hermes_social import create_controller
from agent.plugin_llm import PluginLlm, _TrustPolicy


def event(text='公开话题', mid='1', chat='group:20001', user='10001', name='当前群昵称'):
    source = SimpleNamespace(chat_id=chat, chat_type='group' if chat.startswith('group:') else 'dm',
                             user_id=user, message_id=mid)
    return SimpleNamespace(source=source, user_id=user, user_name=name, message_id=mid,
                           text=text, reply_expected=True, internal=False)


def policy(**overrides):
    raw = dict(enabled=True, targets=['group:20001', 'dm:10001'], quiet_hours=[0, 0])
    raw.update(overrides)
    return raw


class FakeContext:
    plugin_id = 'hermes-social'
    manifest = SimpleNamespace(name='hermes-social')

    def __init__(self):
        self.callbacks, self.tasks, self.handlers, self.skills = [], [], {}, {}
        self.channels = {'napcat': policy()}
        self.migrate: Any = False
        self.calls = []
        self.response = {'speak': True, 'text': '顺着刚才的话题补充一点。'}
        self.selection_response = None
        self.grounding_response = {'supported': True, 'about_source': True}
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.delayed = False
        self.resist_cancel = False
        async def provider(**kwargs):
            from gateway.session_context import get_session_env
            from hermes_constants import get_hermes_home
            self.calls.append((kwargs, get_session_env('HERMES_SESSION_CHAT_ID'),
                               get_session_env('HERMES_SESSION_ID'), str(get_hermes_home())))
            self.entered.set()
            if self.delayed:
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    if not self.resist_cancel:
                        raise
                    await self.release.wait()
            result = self.response
            if 'social_source_grounding' in kwargs['messages'][-1]['content']:
                result = self.grounding_response
            if 'public_participation_selection' in kwargs['messages'][-1]['content']:
                frame = json.loads(kwargs['messages'][-1]['content'])
                result = self.selection_response
                if result is None:
                    # Default transport fixture preserves the old latest-turn
                    # scenario. Semantic cases supply explicit model decisions.
                    result = {'selected': frame['candidates'][-1]['ref'], 'assessments': [
                        dict(candidate=row['ref'], audience='group', action='respond',
                             topic=row['ref'], reason='sharing') for row in frame['candidates']]}
            return ('offline', 'same-host-model', SimpleNamespace(choices=[SimpleNamespace(
                    message=SimpleNamespace(content=json.dumps(result, ensure_ascii=False)))], usage=None))
        self.llm = PluginLlm(plugin_id=self.plugin_id, policy_loader=lambda p: _TrustPolicy(plugin_id=p),
                             async_caller=provider)

    def get_config(self, key, default=None):
        if key == 'migrations.napcat_state_v1':
            return self.migrate
        assert key == 'channels'
        return self.channels

    def on_unload(self, callback):
        self.callbacks.append(callback)

    def spawn_task(self, coro, *, name=None):
        task = asyncio.create_task(coro, name=name)
        self.tasks.append(task)
        return task

    def register_platform_handler(self, platform, factory):
        self.handlers[platform] = factory

    def register_skill(self, name, path):
        assert path.is_file()
        self.skills[self.plugin_id + ':' + name] = path

    def unload(self):
        for callback in reversed(self.callbacks):
            callback()


class FakePort:
    platform, owner_id, groups, public_name = 'napcat', '10001', frozenset({'20001', '20002'}), '公开名字'
    contact_contract = 1
    public_context_contract = 1

    def observe_activity(self, event):
        self.invalidate_optional(event.source.chat_id, keep_pure_image=True)

    def __init__(self, home):
        self.home = Path(home)
        self.online, self.connection = True, 1
        self.controller: Any = None
        self.revision = {}
        self.optional, self.grants, self.receipts = {}, {}, {}
        self.dispatched, self.prepared, self.attempts, self.sequence = [], [], [], []
        self.recalls, self.invalidations = set(), []
        self.ambiguous, self.prepare_denied, self.claim_denied = False, False, False
        self.detached, self.registration_key = False, None
        self.before_send: Callable | None = None
        self.offer = None
        self.feedback = []

    def ready(self):
        return self.online and not self.detached and self.controller is not None and self.controller.active

    def epoch(self):
        return self.connection

    def recalled(self, chat, mid):
        return (chat, mid) in self.recalls

    def set_registration_key(self, key):
        self.registration_key = key

    def detach(self):
        assert not self.controller.active
        self.detached = True
        self.invalidate_optional()
        self.controller = None

    def issue_optional(self, item):
        chat = item.source.chat_id
        assert self.ready() and self.controller.allows_unaddressed(chat)
        assert item.source.message_id == item.message_id and item.source.user_id == item.user_id
        self.optional[id(item.source)] = (item.source, chat, self.revision.get(chat, 0), self.epoch())
        self.sequence.append('issue')

    def optional_valid(self, source):
        row = self.optional.get(id(source))
        return bool(row and row[0] is source and self.ready()
                    and row[2] == self.revision.get(row[1], 0) and row[3] == self.epoch())

    def invalidate_optional(self, chat=None, *, keep_pure_image=False):
        self.invalidations.append(chat)
        if chat is None:
            self.optional.clear()
            self.grants.clear()
            for target in self.revision:
                self.revision[target] += 1
        else:
            self.revision[chat] = self.revision.get(chat, 0) + 1
            self.optional = {key: row for key, row in self.optional.items() if row[1] != chat}
            self.grants = {key: row for key, row in self.grants.items() if row[0] != chat}

    def dispatch_optional(self, item):
        assert self.optional_valid(item.source)
        self.sequence.append('dispatch')
        self.dispatched.append(item)

    async def wait_visual_context(self, chat, *, max_seconds):
        assert chat in {'group:20001', 'group:20002'} and 0 < max_seconds <= 5

    async def prepare_idle(self, chat, task_id, context):
        self.prepared.append((chat, task_id, context))
        if self.prepare_denied:
            raise ValueError('worker denied')

    def claim_idle(self, chat, task_id):
        self.sequence.append('claim')
        if self.claim_denied:
            return None
        assert self.ready() and chat in self.controller.policy.targets
        grant = object()
        self.grants[grant] = (chat, self.revision.get(chat, 0), self.epoch())
        return grant

    async def send_idle(self, grant, text, consume, valid):
        if self.before_send:
            self.before_send()
        row = self.grants.get(grant)
        if (not row or not self.ready() or row[1] != self.revision.get(row[0], 0)
                or row[2] != self.epoch() or not valid()):
            return False
        self.grants.pop(grant)
        self.sequence.append('consume')
        consume()
        self.sequence.append('send')
        self.attempts.append((row[0], text))
        if self.ambiguous:
            return False
        mid = str(90001 + len(self.attempts))
        self.receipts[row[0]] = mid
        self.controller.record_self(row[0], text, mid)
        self.sequence.append('readback')
        return True

    def last_receipt(self, chat):
        return self.receipts.get(chat)

    def next_opportunity(self, chat, *, excluded=frozenset()):
        return self.offer if self.offer is not None and self.offer.reference not in excluded else None

    async def send_opportunity(self, grant, offer, consume, valid):
        def reserve():
            consume()
            offer.claim('offline')
        sent = await self.send_idle(grant, offer.text, reserve, lambda: valid() and offer.current())
        offer.finish('notified' if sent else 'unknown')
        return sent

    async def send_activity(self, grant, offer, consume, valid):
        return await self.send_idle(grant, offer.text, consume, lambda: valid() and offer.current())

    def report_contact_feedback(self, chat, recipient, reference, status):
        self.feedback.append((chat,recipient,reference,status))


class FakeAdapter:
    def __init__(self, port):
        self.port = port
        self.installed = []

    def participation_port(self):
        return self.port

    def install_participation(self, controller):
        assert controller.port is self.port and controller.binding.home == self.port.home
        self.port.controller = controller
        self.installed.append(controller)


def build(ctx, port, config=None):
    controller = create_controller(ctx, port, policy() if config is None else config)
    port.controller = controller
    return controller
