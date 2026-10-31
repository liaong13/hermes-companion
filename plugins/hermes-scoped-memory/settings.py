"""Validated deployment settings. Configuration is never proof of host admission."""
from __future__ import annotations

import math
import re
import warnings


def integer(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{name} must be integer {low}..{high}')
    return value


def flag(config, name, default=False):
    value = config.get(name, default)
    if type(value) is not bool:
        raise ValueError(f'{name} must be boolean')
    return value


def validate(settings):
    if not isinstance(settings, dict):
        raise ValueError('scoped-memory settings must be an object')
    result = dict(settings)
    result['api_free'] = flag(result, 'api_free')
    mode = result.setdefault('mode', 'composite')
    if mode not in ('composite', 'local-only'):
        raise ValueError('mode must be composite or local-only')
    version = result.setdefault('schema_version', 1 if mode == 'composite' else 2)
    if type(version) is not int or version not in (1, 2):
        raise ValueError('unsupported settings schema_version')
    if mode == 'local-only' and version != 2:
        raise ValueError('local-only requires schema_version 2')
    domain = result.setdefault('domain_id', 'legacy' if mode == 'composite' else '')
    if (not isinstance(domain, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', domain)
            or (mode == 'local-only' and domain == 'legacy')):
        raise ValueError('local-only requires a distinct canonical domain_id')
    if mode == 'composite' and (version != 1 or domain != 'legacy'):
        raise ValueError('composite compatibility mode requires schema 1 / legacy domain')
    if mode == 'local-only' and 'owner_mem0_user_id' in result:
        raise ValueError('local-only cannot configure an owner backend')
    if mode == 'composite':
        owner = result.get('owner_mem0_user_id')
        if not isinstance(owner, str) or not owner.strip():
            raise PermissionError('composite requires an explicit non-empty owner_mem0_user_id')
    old, new = result.get('max_injection_tokens'), result.get('max_injection_bytes')
    if old is not None:
        integer(old, 'max_injection_tokens', 64, 8192)
        if new is not None and new != old:
            raise ValueError('conflicting injection byte budgets')
        warnings.warn('max_injection_tokens is a deprecated UTF-8 byte alias; use max_injection_bytes',
                      DeprecationWarning, stacklevel=2)
    result['max_injection_bytes'] = integer(new if new is not None else old if old is not None else 768,
                                             'max_injection_bytes', 64, 8192)
    days = result.get('source_retention_days')
    if days is not None and (type(days) not in (int, float) or not math.isfinite(days) or not 0 < days <= 36500):
        raise ValueError('source_retention_days must be null or 0 < days <= 36500')
    result['policy_revision'] = integer(result.get('policy_revision', 1), 'policy_revision', 1, 2**31-1)
    for section in ('extraction', 'embedding', 'public_group_memory', 'jobs', 'privacy'):
        config = result.setdefault(section, {})
        if not isinstance(config, dict):
            raise ValueError(f'{section} must be an object')
        result[section] = dict(config)
    extraction = result['extraction']
    extraction['enabled'] = flag(extraction, 'enabled')
    extraction['egress_reviewed'] = flag(extraction, 'egress_reviewed')
    extraction['max_turns'] = integer(extraction.get('max_turns', 8), 'extraction.max_turns', 1, 8)
    extraction['max_input_bytes'] = integer(extraction.get('max_input_bytes', 24000), 'extraction.max_input_bytes', 256, 64000)
    extraction['task'] = extraction.get('task', 'scoped_memory_extract')
    if extraction['task'] != 'scoped_memory_extract':
        raise ValueError('unknown extraction task')
    if extraction['enabled'] and (mode != 'local-only' or not extraction['egress_reviewed']):
        raise PermissionError('extraction requires local-only and separate egress review')
    result['embedding']['enabled'] = flag(result['embedding'], 'enabled')
    if result['api_free'] and (mode != 'local-only' or extraction['enabled']
            or result['embedding']['enabled'] or 'local_http' in result['embedding']):
        raise PermissionError('api_free requires local-only with extraction and all embedding APIs disabled')
    if 'local_http' in result['embedding']:
        if mode != 'local-only':
            raise ValueError('local embedding runtime requires local-only mode')
        from .local_embedding import validate_local_http
        validate_local_http(result['embedding']['local_http'])
        result['embedding']['local_http'] = dict(result['embedding']['local_http'])
    result['public_group_memory']['enabled'] = flag(result['public_group_memory'], 'enabled')
    if result['public_group_memory']['enabled']:
        if mode != 'local-only' or result['public_group_memory'].get('approval') != 'explicit':
            raise PermissionError('public group memory requires local-only / explicit confirmation')
    jobs = result['jobs']
    jobs['concurrency'] = integer(jobs.get('concurrency', 1), 'jobs.concurrency', 1, 1)
    jobs['max_attempts'] = integer(jobs.get('max_attempts', 5), 'jobs.max_attempts', 1, 5)
    return result
