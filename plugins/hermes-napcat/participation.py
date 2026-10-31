"""Transport-owned participation capabilities, without any social policy/model.

Plugins are trusted host code, not Python sandboxes. This port narrows their
integration surface; source ledgers and final OneBot authorization stay here.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import logging
import re
import time
import weakref

from .isolation import AdmissionDenied

logger = logging.getLogger(__name__)


class NullParticipation:
    """A missing/disabled social plugin never changes ordinary addressed chat."""
    active = False

    def enabled_for(self, chat): return False
    def allows_unaddressed(self, chat): return False
    def observes(self, chat): return False
    def recognizes_control(self, chat, text): return False
    def observe(self, event): pass
    def record_image_context(self, chat, mid, text): return False
    def record_self(self, chat, text, mid, *, reply_to_message_id=None): pass
    def recall(self, chat, mid): pass
    def forget_messages(self, chat, message_ids): return False
    def contact_control_feedback(self, chat, actor, message_id, feedback): pass
    def control(self, event): return None
    def queue_ambient(self, event): pass
    def processing_start(self, event): pass
    def processing_complete(self, event): pass
    def prompt_for_chat(self, chat): return ''
    def start(self): pass
    async def stop(self): pass
    def reset_connection(self): pass


class OptionalSources:
    """Exact optional-source permits and transport activity revisions.

    A social decision is not an ingress proof. Markers only restrict; the real
    source identity, immutable route, live channel and ledger are all required.
    """
    def __init__(self, adapter):
        self._receiver = weakref.ref(adapter)
        self.revisions = {}
        self.seen = OrderedDict()
        self.grants = OrderedDict()
        self.continuations = {}

    def revision(self, chat):
        return self.revisions.get(chat, 0)

    def advance(self, chat):
        self.revisions[chat] = self.revision(chat) + 1

    def observe(self, event):
        key = (event.source.chat_id, event.message_id)
        if key in self.seen:
            return
        self.seen[key] = time.monotonic()
        while len(self.seen) > 4096:
            self.seen.popitem(last=False)
        chat = event.source.chat_id
        previous = self.revision(chat)
        self.advance(chat)
        for key,grant in list(self.grants.items()):
            source, original, route, port, revision, epoch, deadline = grant
            if route[0] != chat or revision != previous:
                continue
            accepts = getattr(port._controller,'accepts_update',None)
            count = self.continuations.get(key,0)
            # Only conversational freshness may advance. Exact source, target,
            # connection epoch and original deadline are never renewed here.
            compatible = (callable(accepts) and count<8 and time.monotonic()<=deadline-280
                          and not getattr(original,'_napcat_optional_media',False)
                          and accepts(original,event) is True)
            if compatible:
                self.grants[key] = (source,original,route,port,self.revision(chat),epoch,deadline)
                self.continuations[key] = count+1
            else:
                logger.info('NapCat optional decision chat=%s message=%s stage=invalidated reason=conversation_changed',
                            chat,route[3])
        adapter = self._receiver()
        if adapter is not None:
            # A new utterance invalidates speculative replies, not the visual
            # observation of an earlier, still-live image in the same group.
            adapter._cancel_optional_media(event.source.chat_id, keep_pure_image=True)

    def invalidate(self, chat=None, *, keep_pure_image=False):
        if chat is None:
            self.grants.clear()
            self.continuations.clear()
            for target in self.revisions:
                self.advance(target)
        else:
            self.advance(chat)
            for key, grant in list(self.grants.items()):
                if grant[2][0] == chat:
                    self.grants.pop(key, None)
                    self.continuations.pop(key,None)
        adapter = self._receiver()
        if adapter is not None:
            adapter._cancel_optional_media(chat, keep_pure_image=keep_pure_image)

    @staticmethod
    def _route(source):
        return tuple(getattr(source, key, None) for key in
                     ('chat_id', 'chat_type', 'user_id', 'message_id'))

    def revoke_port(self, port):
        for key, grant in list(self.grants.items()):
            if grant[3] is port:
                self.grants.pop(key, None)
                self.continuations.pop(key,None)
        adapter = self._receiver()
        if adapter is not None:
            adapter._cancel_optional_media(port=port)

    def issue(self, port, event):
        adapter, source, now = self._receiver(), event.source, time.monotonic()
        chat = source.chat_id
        if (adapter is None or not port.current() or not adapter.social_ready()
                or source.chat_type != 'group' or not port._controller.allows_unaddressed(chat)
                or source.user_id != event.user_id or source.message_id != event.message_id
                or chat.partition(':')[2] not in adapter.settings.groups
                or not adapter.settings.isolation.allows_group_user(source.user_id, chat.partition(':')[2])):
            raise AdmissionDenied('optional_source_cannot_be_issued')
        deadline = now + 300
        if getattr(event, '_napcat_optional_media', False):
            proof = adapter._optional_media.get(id(event))
            if proof is None or proof.port is not port or not adapter._optional_media_current(proof):
                raise AdmissionDenied('optional_media_expired')
            deadline = min(deadline, proof.deadline)
        for key, grant in list(self.grants.items()):
            if grant[-1] <= now:
                self.grants.pop(key, None)
                self.continuations.pop(key,None)
        while len(self.grants) >= 64:
            key,_ = self.grants.popitem(last=False)
            self.continuations.pop(key,None)
        self.grants[id(source)] = (source, event, self._route(source), port,
                                   self.revision(chat), adapter._connection_epoch, deadline)
        self.continuations[id(source)] = 0
        source._napcat_ambient_candidate = True

    def valid(self, source):
        if not getattr(source, '_napcat_ambient_candidate', False):
            return True
        adapter = self._receiver()
        grant = self.grants.get(id(source))
        if adapter is None or grant is None or grant[0] is not source:
            return False
        issued, event, route, port, revision, epoch, deadline = grant
        chat = route[0]
        return (port.current() and adapter.social_ready() and port._controller.allows_unaddressed(chat)
                and self._route(issued) == route
                and event.user_id == route[2] and event.message_id == route[3]
                and revision == self.revision(chat) and epoch == adapter._connection_epoch
                and time.monotonic() < deadline
                and not adapter._media_recalled(chat, route[3]))

    def release(self, source):
        self.grants.pop(id(source), None)
        self.continuations.pop(id(source),None)


@dataclass(eq=False)
class _IdleGrant:
    chat: str
    task_id: str
    revision: int
    epoch: int
    deadline: float
    spent: bool = False


class ParticipationPort:
    """One controller's revocable, fixed-channel capabilities.

    No OneBot client, worker, credential, private agent or user reply permit is
    exposed. Replacing/unloading a controller invalidates this exact port.
    """
    platform = 'napcat'
    contact_contract = 1
    public_context_contract = 1

    def __init__(self, adapter):
        self._receiver = weakref.ref(adapter)
        self.home = adapter.binding.home
        self.owner_id = adapter.settings.owner_id
        self.groups = adapter.settings.groups
        self.domain_id = adapter.settings.isolation.domain_id
        self.policy_revision = adapter.settings.isolation.revision
        self._controller = None
        self._idle_targets = frozenset()
        self._idle_grants = OrderedDict()
        self._receipts = {}
        self._dispatching = {}
        self._registration_key = None
        self._contact_offers = {}
        self._contact_delivery = {}

    def _adapter(self):
        adapter = self._receiver()
        if adapter is None:
            raise AdmissionDenied('participation_channel_gone')
        return adapter

    def set_registration_key(self, key):
        if (not isinstance(key, tuple) or len(key) != 2
                or not all(isinstance(part, str) and part for part in key)):
            raise ValueError('SDK factory registration key required')
        self._registration_key = key

    def detach(self):
        adapter = self._receiver()
        if adapter is not None:
            adapter.uninstall_participation(self)

    def belongs_to(self, adapter):
        return self._receiver() is adapter

    def bind(self, controller):
        adapter = self._adapter()
        permitted = {'dm:' + adapter.settings.owner_id} | {'group:' + g for g in adapter.settings.groups}
        targets = frozenset(controller.policy.targets)
        if not targets.issubset(permitted):
            raise AdmissionDenied('initiative_targets_outside_channel')
        self._controller, self._idle_targets = controller, targets

    def current(self):
        adapter = self._receiver()
        return (adapter is not None and self._controller is not None
                and adapter.participation is self._controller
                and bool(self._controller.active) and adapter.binding.active)

    def ready(self):
        adapter = self._receiver()
        return bool(adapter is not None and self.current() and adapter.social_ready())

    def epoch(self):
        adapter = self._receiver()
        return adapter._connection_epoch if adapter is not None else -1

    def recalled(self, chat, mid):
        adapter = self._receiver()
        return adapter is None or adapter._media_recalled(chat, mid)

    def issue_optional(self, event):
        self._adapter()._optional_sources.issue(self, event)

    def observe_activity(self, event):
        self._adapter()._optional_sources.observe(event)

    def invalidate_optional(self, chat=None, *, keep_pure_image=False):
        adapter = self._receiver()
        if adapter is not None:
            if self.current():
                adapter._optional_sources.invalidate(chat, keep_pure_image=keep_pure_image)
            else:
                adapter._optional_sources.revoke_port(self)
        for key, grant in list(self._idle_grants.items()):
            if chat is None or grant.chat == chat:
                grant.spent = True
                self._idle_grants.pop(key, None)

    def dispatch_optional(self, event):
        adapter = self._adapter()
        grant = adapter._optional_sources.grants.get(id(event.source))
        if (not self.current() or not adapter._optional_sources.valid(event.source)
                or grant is None or grant[1] is not event or grant[3] is not self
                or not getattr(event.source, '_napcat_ambient_candidate', False)):
            raise AdmissionDenied('optional_source_expired')
        adapter._dispatch_optional(event)

    async def wait_visual_context(self, chat, *, max_seconds):
        adapter = self._adapter()
        if not self.ready() or not chat.startswith('group:'):
            return
        tasks = [task for task, job in list(adapter._media_tasks.items())
                 if job.get('pure_image') and job.get('chat') == chat and not task.done()]
        if tasks:
            await asyncio.wait(tasks, timeout=min(5, max_seconds))

    async def prepare_idle(self, chat, task_id, context):
        adapter = self._adapter()
        if not self.ready() or chat not in self._idle_targets:
            raise AdmissionDenied('initiative_target_denied')
        if not chat.startswith('group:'):
            return
        policy = adapter.settings.isolation
        payload = {'operation': 'prepare_proactive', 'schema_version': 1,
                   'automation': {'platform': 'napcat', 'domain_id': policy.domain_id,
                                  'policy_revision': policy.revision, 'chat_id': chat,
                                  'chat_type': 'group', 'task_id': task_id}, 'context': context}
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                           separators=(',', ':')).encode()).hexdigest()
        result = await adapter._isolation_bridge.worker.request(payload)
        if result != {'status': 'ok', 'host_contract': 4, 'digest': digest}:
            raise AdmissionDenied('initiative_worker_preparation_denied')

    def claim_idle(self, chat, task_id):
        adapter = self._adapter()
        if (not self.ready() or chat not in self._idle_targets
                or not isinstance(task_id, str) or not re.fullmatch(r'[a-f0-9]{32}', task_id)):
            raise AdmissionDenied('initiative_not_authorized')
        # Validate canonical current target once more at the actual boundary.
        adapter._target(chat)
        now = time.monotonic()
        for key, grant in list(self._idle_grants.items()):
            if grant.spent or grant.deadline <= now:
                self._idle_grants.pop(key, None)
        while len(self._idle_grants) >= 32:
            self._idle_grants.popitem(last=False)
        grant = _IdleGrant(chat, task_id, adapter._optional_sources.revision(chat),
                           adapter._connection_epoch, now + 90)
        self._idle_grants[id(grant)] = grant
        return grant

    def _grant_valid(self, grant):
        adapter = self._receiver()
        return (adapter is not None and isinstance(grant, _IdleGrant) and self._idle_grants.get(id(grant)) is grant
                and not grant.spent and self.ready() and grant.chat in self._idle_targets
                and grant.epoch == adapter._connection_epoch
                and grant.revision == adapter._optional_sources.revision(grant.chat)
                and time.monotonic() < grant.deadline)

    async def send_idle(self, grant, text, consume, valid):
        if (not self._grant_valid(grant) or not isinstance(text, str)
                or not 1 <= len(text) <= 1000 or not text.strip() or not valid()):
            return False
        adapter = self._adapter()
        adapter.binding.validate_active()
        adapter._target(grant.chat)
        # No await between last validation, durable consumption, and the sole
        # side effect. Ambiguous outcomes stay consumed; never retry/replay.
        grant.spent = True
        self._idle_grants.pop(id(grant), None)
        consume()
        self._dispatching[id(grant)] = grant
        try:
            receipt = await adapter._send_initiative_text(grant, text, self)
        finally:
            self._dispatching.pop(id(grant), None)
        if receipt:
            self._receipts[grant.chat] = receipt
            return True
        return False

    def consume_delivery(self, grant):
        adapter = self._adapter()
        if (not isinstance(grant, _IdleGrant) or self._dispatching.pop(id(grant), None) is not grant
                or not grant.spent or not self.ready() or grant.chat not in self._idle_targets
                or grant.epoch != adapter._connection_epoch
                or grant.revision != adapter._optional_sources.revision(grant.chat)
                or time.monotonic() >= grant.deadline):
            raise AdmissionDenied('initiative_delivery_capability_expired')
        return grant.chat, grant.task_id

    def last_receipt(self, chat):
        return self._receipts.get(chat)

    def next_opportunity(self, chat, *, excluded=frozenset()):
        adapter = self._adapter()
        if not self.ready() or chat not in self._idle_targets or not chat.startswith('group:'):
            return None
        service = adapter._memory_service(chat)
        offer = (service.contact_opportunity(chat, excluded=excluded)
                 if getattr(service,'agreement_contract',None)==1 else None)
        self._contact_offers.pop(chat, None)
        if offer is not None and adapter.settings.isolation.allows_group_user(offer.recipient, chat.partition(':')[2]):
            self._contact_offers[chat] = offer
            return offer
        return None

    async def send_opportunity(self, grant, offer, consume, valid):
        adapter = self._adapter()
        if (not self._grant_valid(grant) or self._contact_offers.get(grant.chat) is not offer
                or not offer.current() or not valid()):
            return False
        # QQ transport membership is checked without involving a model. A
        # stored identity cannot authorize contact after leaving this group.
        async with asyncio.timeout(3):
            member = await adapter.client.call('get_group_member_info',
                {'group_id':int(grant.chat.partition(':')[2]), 'user_id':int(offer.recipient), 'no_cache':True})
        if (not isinstance(member, dict) or str(member.get('user_id')) != offer.recipient
                or str(member.get('group_id')) != grant.chat.partition(':')[2]
                or not offer.current() or not self._grant_valid(grant) or not valid()):
            return False
        claimed = [False]
        def reserve():
            consume()
            offer.claim(grant.task_id)
            claimed[0] = True
        self._contact_delivery[id(grant)] = offer
        sent = False
        try:
            sent = await self.send_idle(grant, offer.text, reserve, lambda: valid() and offer.current())
            return sent
        finally:
            self._contact_delivery.pop(id(grant), None)
            self._contact_offers.pop(grant.chat, None)
            if claimed[0]:
                try:
                    offer.finish('notified' if sent else 'unknown')
                except (PermissionError, ValueError):
                    pass  # Revoked consent must never be restored by a late ACK.

    def contact_for_delivery(self, grant, text):
        offer = self._contact_delivery.get(id(grant))
        if offer is not None and (self._contact_offers.get(grant.chat) is not offer
                                  or text != offer.text or not offer.current()):
            raise AdmissionDenied('agreement_contact_expired')
        return offer

    def report_contact_feedback(self, chat, recipient, reference, status):
        service = self._adapter()._memory_service(chat)
        if getattr(service,'agreement_contract',None)==1:
            try:
                service.contact_feedback(chat, recipient, reference, status)
            except PermissionError:
                pass  # Unloaded/revoked memories do not change journal evidence.
