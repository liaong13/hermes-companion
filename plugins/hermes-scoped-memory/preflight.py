"""Read-only activation preflight. Never installs, changes config, or contacts Mem0."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', required=True, type=Path, help='Explicit active-profile HERMES_HOME')
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location('scoped_preflight', root / '__init__.py', submodule_search_locations=[str(root)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    try:
        _, settings, budget = module.ScopedMemoryProvider()._read_settings(args.home.resolve())
        if settings['mode'] == 'composite':
            cfg = json.loads((args.home / 'mem0.json').read_text())
            if cfg.get('user_id') != settings['owner_mem0_user_id']:
                raise PermissionError('configured owner Mem0 principal mismatch')
    except Exception as exc:
        # Validation errors are deliberately static strings; never dump config or secrets.
        reason = str(exc) if isinstance(exc, (PermissionError, ValueError)) and not isinstance(exc, json.JSONDecodeError) else 'missing/unreadable/malformed configuration'
        print(json.dumps({'configuration_gate': 'blocked', 'reason': reason, 'safe_to_activate': False}))
        return 2
    print(json.dumps({'configuration_gate': 'passed', 'mode': settings['mode'], 'max_injection_utf8_bytes': budget,
                      'safe_to_activate': 'NOT_CERTIFIED',
                      'extraction': 'blocked_until_verified_host_facade' if settings['extraction']['enabled'] else 'disabled',
                      'embedding': ('disabled' if not settings['embedding']['enabled'] else
                                    'configured_local_runtime_unprobed' if settings['embedding'].get('local_http')
                                    else 'blocked_missing_runtime'),
                      'remaining': 'Review host filesystem/history/tools, shared-session transcripts, LLM egress and existing agent caches; explicit operator approval still required.'}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
