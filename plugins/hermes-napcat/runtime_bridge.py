"""A single-ingress bridge to short-lived, mount-isolated social workers.

Private dispatch is an explicit host callback. Neither this bridge nor its worker
loads a private profile, and neither connects a second OneBot receiver.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import shutil

from .isolation import Admission, AdmissionDenied, IsolationBridge
from .process_owner import ProcessCleanupError, ProcessOwner


class SandboxedWorker:
    def __init__(self, home, *, domain_id, policy_revision, network_reviewed=False,
                 timeout=30, bwrap=None, plugin_root=None, readonly_home=False):
        self.home = Path(home).resolve(strict=True)
        self.domain_id, self.policy_revision = domain_id, policy_revision
        self.network_reviewed = network_reviewed is True
        self.timeout = timeout
        self.readonly_home = readonly_home
        executable = bwrap or shutil.which('bwrap')
        if not executable:
            raise ValueError('bubblewrap is required for social execution')
        self.bwrap = str(Path(executable).resolve(strict=True))
        self.plugin_root = Path(plugin_root or Path(__file__).resolve().parents[1]).resolve(strict=True)
        if (self.home == Path('/') or not self.home.is_dir() or type(policy_revision) is not int
                or policy_revision < 1 or type(timeout) not in (int, float) or not 0 < timeout <= 120):
            raise ValueError('invalid isolated worker deployment')
        if (self.home / 'mem0.json').exists():
            raise AdmissionDenied('private_backend_in_social_home')
        candidates = [self.plugin_root/name for name in ('hermes-scoped-memory', 'scoped-memory')
                      if (self.plugin_root/name/'isolated_worker.py').is_file()]
        if len(candidates) != 1:
            raise ValueError('social worker source is unavailable')
        self.memory_source = candidates[0].resolve(strict=True)
        self._slots = asyncio.Semaphore(1)
        # Auxiliary media's final conversation must not occupy the ordinary
        # worker lane. Each process has a fresh context; adapter serializes a group.
        self._media_slots = asyncio.Semaphore(1)
        # Admission is local configuration attestation, not a model turn. A slow
        # chat may not hold the semaphore needed to admit cancellation controls.
        self._probe_slots = asyncio.Semaphore(1)
        self._cleanup_failed = False

    def command(self):
        command = [self.bwrap, '--unshare-all', '--die-with-parent', '--new-session', '--clearenv',
                   '--ro-bind', '/usr', '/usr', '--ro-bind', '/lib', '/lib',
                   '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp',
                   '--dir', '/work',
                   *(['--dir', '/work/home', '--ro-bind', str(self.home/'scoped-memory.json'), '/work/home/scoped-memory.json']
                     if self.readonly_home else ['--bind', str(self.home), '/work/home']),
                   '--dir', '/plugins', '--ro-bind', str(self.memory_source), '/plugins/hermes-scoped-memory',
                   '--chdir', '/work/home',
                   '--setenv', 'HOME', '/work/home', '--setenv', 'PATH', '/usr/bin:/bin']
        if Path('/lib64').exists():
            command += ['--ro-bind', '/lib64', '/lib64']
        # No general /etc or user home mounts. HTTPS may use only system CA certs.
        if Path('/etc/ssl/certs').is_dir():
            command += ['--ro-bind', '/etc/ssl/certs', '/etc/ssl/certs']
        if self.network_reviewed:
            command += ['--share-net']
            for name in ('resolv.conf', 'hosts', 'nsswitch.conf'):
                path = Path('/etc')/name
                if path.exists():
                    command += ['--ro-bind', str(path.resolve()), '/etc/'+name]
        return command + ['--', '/usr/bin/python3', '-I', '/plugins/hermes-scoped-memory/isolated_worker.py']

    async def request(self, payload):
        data = json.dumps(payload, ensure_ascii=False).encode()
        if len(data) > 65536:
            raise AdmissionDenied('worker_input_too_large')
        slots = (self._probe_slots if payload.get('operation') == 'probe' else
                 self._media_slots if payload.get('schema_version') == 2 else self._slots)
        async with slots:
            if self._cleanup_failed:
                raise AdmissionDenied('worker_shutdown_unconfirmed')
            owner = ProcessOwner(self.command())
            try:
                process = await owner.start()
                operation = owner.track(process.communicate(data))
                stdout, _ = await asyncio.wait_for(asyncio.shield(operation), self.timeout)
                if process.returncode or len(stdout) > 65536:
                    raise AdmissionDenied('worker_failed')
                result = json.loads(stdout)
                if not isinstance(result, dict) or result.get('status') != 'ok':
                    raise AdmissionDenied('worker_denied')
                return result
            finally:
                try:
                    await owner.close()
                except ProcessCleanupError:
                    self._cleanup_failed = True
                    raise

    async def probe(self, route):
        result = await self.request({'operation': 'probe', 'route': asdict(route)})
        if (result.get('domain_id') != self.domain_id or result.get('policy_revision') != self.policy_revision
                or result.get('tools') != ['scoped_memory'] or result.get('context_cache') != 'none'
                or result.get('media_contract') != 2):
            raise AdmissionDenied('worker_deployment_mismatch')


class ProcessIsolationBridge(IsolationBridge):
    media_contract = 2

    def __init__(self, worker, *, private_dispatch=None):
        self.worker, self.private_dispatch = worker, private_dispatch
        self._admissions = {}

    async def admit(self, route):
        if len(self._admissions) >= 64:
            raise AdmissionDenied('admission_capacity')
        if route.domain_id == 'private':
            if self.private_dispatch is None:
                raise AdmissionDenied('private_host_dispatch_unbound')
        else:
            if route.domain_id != self.worker.domain_id or route.policy_revision != self.worker.policy_revision:
                raise AdmissionDenied('worker_route_mismatch')
            await self.worker.probe(route)
        admission = Admission(route)
        # Capability is instance-local and consumed once, not reconstructed from JSON.
        self._admissions[id(admission)] = admission
        return admission

    async def dispatch(self, event, admission, reply):
        await self._dispatch(event, admission, reply)

    def release(self, admission):
        self._admissions.pop(id(admission), None)

    async def dispatch_media(self, event, admission, reply, attachments):
        if admission.route.chat_type != 'group' or admission.route.domain_id == 'private':
            raise AdmissionDenied('media_private_route_denied')
        await self._dispatch(event, admission, reply, attachments=attachments)

    async def _dispatch(self, event, admission, reply, *, attachments=None):
        if self._admissions.pop(id(admission), None) is not admission:
            raise AdmissionDenied('admission_not_issued_here')
        route = admission.route
        source = event.source
        if any(getattr(source, field, None) != getattr(route, field)
               for field in ('user_id', 'chat_id', 'chat_type', 'message_id')):
            raise AdmissionDenied('source_changed_after_admission')
        if route.domain_id == 'private':
            await self.private_dispatch(event, admission, reply)
            return
        text = getattr(event, 'text', '')
        if not isinstance(text, str):
            raise AdmissionDenied('invalid_message_text')
        payload = {'operation': 'chat', 'route': asdict(route), 'text': text}
        if attachments is not None:
            if not isinstance(attachments, list) or len(attachments) > 4:
                raise AdmissionDenied('invalid_media_contract')
            payload.update(schema_version=2, attachments=attachments)
        result = await self.worker.request(payload)
        response = result.get('reply')
        if not isinstance(response, str) or not response or len(response.encode()) > 16000:
            raise AdmissionDenied('invalid_worker_reply')
        # The worker cannot choose a QQ target. Adapter reply owns that capability.
        await reply(response)
