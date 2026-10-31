"""Load only the versioned public entry of the reviewed memory distribution."""
from __future__ import annotations

import hashlib
import importlib
from pathlib import Path
import sys
from types import ModuleType


def memory_api():
    root = Path(__file__).resolve().parent.parent / 'hermes-scoped-memory'
    if not (root/'api.py').is_file():
        raise RuntimeError('memory public API source is required')
    name = '_companion_memory_api_' + hashlib.sha256(str(root).encode()).hexdigest()[:16]
    namespace = ModuleType(name)
    namespace.__path__ = [str(root)]
    sys.modules.setdefault(name, namespace)
    # import_module owns the process-wide module lock, including concurrent
    # imports through different plugin/profile instances. No provider init runs.
    api = importlib.import_module(name+'.api')
    if getattr(api, 'API_VERSION', None) != 1:
        raise RuntimeError('memory public API version 1 required')
    return api


def storage_types():
    api = memory_api()
    return api.Store, api.ScopeContext, api.PeopleMemory
