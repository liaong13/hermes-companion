"""Versioned, stdlib-only public memory API; importing never registers a provider.

Consumers load this single entry point, not internal implementation modules.
The plugin directory remains the distribution unit, including isolated workers.
"""
from .scope import ScopeContext
from .store import Store, clip, terms
from .people import PeopleMemory

API_VERSION = 1
__all__ = ['API_VERSION', 'ScopeContext', 'Store', 'PeopleMemory', 'clip', 'terms']
