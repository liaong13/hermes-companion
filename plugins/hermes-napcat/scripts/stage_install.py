#!/usr/bin/env python3
"""Reviewable staging only. Never installs into an active Hermes profile."""
import argparse
from pathlib import Path
import shutil


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path, help='New staging directory; not a Hermes plugins directory')
    parser.add_argument('--apply', action='store_true', help='Actually copy to the new staging directory')
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    destination = (args.destination or source / '.stage' / 'hermes-napcat').expanduser().resolve()
    # A monorepo source itself can live under plugins/. Only exempt that
    # source prefix; still reject plugin/profile targets below it or elsewhere.
    checked_parts = (destination.relative_to(source).parts
                     if destination.is_relative_to(source) else destination.parts)
    if ('plugins' in checked_parts or 'profiles' in checked_parts
            or '.hermes' in destination.parts
            or destination == source or destination in source.parents
            or destination.exists()):
        parser.error('destination must be a new non-profile, non-plugins staging directory')
    names = ['__init__.py', 'adapter.py', 'transport.py', 'isolation.py', 'runtime_bridge.py',
             'media.py', 'media_io.py', 'media_provider.py', 'media_worker.py', 'process_owner.py', 'host_runtime.py',
             'standalone.py', 'participation.py', 'social_memory.py',
             'plugin.yaml', 'pyproject.toml', 'LICENSE', 'README.md', 'SECURITY.md']
    print(('APPLY' if args.apply else 'DRY RUN') + ': stage -> ' + str(destination))
    for name in names:
        print('  ' + name)
    if args.apply:
        destination.mkdir(parents=True, exist_ok=False)
        for name in names:
            shutil.copy2(source / name, destination / name)
    print('No active profile/config changes. No restart. No dependencies installed.')


if __name__ == '__main__':
    main()
