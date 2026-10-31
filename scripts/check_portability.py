#!/usr/bin/env python3
"""Check distribution text; optionally audit reachable Git objects and identities."""
import argparse
import json
from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
LEGACY_NAME = 'hermes' + '-plugins'
PERSONAL_HOME = re.compile(r'''(?<![\w./\\])(?:/(?:home|Users)/[^/\s'"<>]+(?:/|(?=[\s'"`]|$))|[A-Za-z]:[\\/]Users[\\/][^\\/\s'"<>]+[\\/])''')
PERSONAL_PROJECT_URL = re.compile(r'https?://github\.com/[^/\s]+/hermes-companion(?:[/.#\s]|$)')
ANONYMOUS_NAME = 'Hermes Companion contributors'
ANONYMOUS_EMAIL = 'contributors@hermes-companion.invalid'


def categories(content, forbidden):
    found = []
    if PERSONAL_HOME.search(content):
        found.append('personal home path')
    if LEGACY_NAME in content:
        found.append('legacy project name')
    if PERSONAL_PROJECT_URL.search(content):
        found.append('embedded personal project URL')
    folded = content.casefold()
    if any(term in folded for term in forbidden):
        found.append('private audit term')
    return found


def history_findings(forbidden):
    objects = subprocess.check_output(['git', 'rev-list', '--objects', '--all', '--no-object-names'], cwd=ROOT).splitlines()
    findings = []
    expected = (ANONYMOUS_NAME+' <'+ANONYMOUS_EMAIL+'> ').encode()
    process = subprocess.Popen(['git', 'cat-file', '--batch'], cwd=ROOT,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        for oid in objects:
            process.stdin.write(oid+b'\n')
            process.stdin.flush()
            header = process.stdout.readline().split()
            if len(header) != 3:
                raise RuntimeError('invalid Git object response')
            kind, size = header[1], int(header[2])
            data = process.stdout.read(size)
            if len(data) != size or process.stdout.read(1) != b'\n':
                raise RuntimeError('incomplete Git object response')
            label = 'object:'+oid.decode()
            for category in categories(data.decode('utf-8', errors='replace'), forbidden):
                findings.append((label, 0, category))
            if kind in {b'commit', b'tag'}:
                for line in data.split(b'\n\n', 1)[0].splitlines():
                    if line.startswith((b'author ', b'committer ', b'tagger ')) and not line.split(b' ', 1)[1].startswith(expected):
                        findings.append((label, 0, 'non-anonymous Git identity'))
        process.stdin.close()
        if process.wait() != 0:
            raise RuntimeError('Git object scan failed')
    finally:
        process.stdout.close()
        if process.poll() is None:
            process.kill()
            process.wait()
    return findings, len(objects)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history', action='store_true', help='audit every reachable Git object and author/committer')
    parser.add_argument('--forbid-file', type=Path, help='private JSON string list outside the checkout; terms are never printed')
    args = parser.parse_args(argv)
    forbidden = []
    if args.forbid_file:
        path = args.forbid_file.resolve(strict=True)
        if path == ROOT or ROOT in path.parents:
            parser.error('private audit terms must remain outside the checkout')
        values = json.loads(path.read_text())
        if not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values):
            parser.error('private audit terms must be nonempty strings')
        forbidden = [value.casefold() for value in values]
    result = subprocess.run(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'],
                            cwd=ROOT, check=True, capture_output=True)
    names = sorted(set(result.stdout.decode('utf-8').split('\0')) - {''})
    findings = []
    checked = 0
    for name in names:
        path = ROOT / name
        if path.is_symlink():
            findings.append((name, 0, 'symlink requires a separate distribution review'))
            continue
        if not path.is_file():
            continue
        data = path.read_bytes()
        if b'\0' in data:
            continue
        try:
            content = data.decode('utf-8')
        except UnicodeDecodeError:
            continue
        checked += 1
        for number, line in enumerate(content.splitlines(), 1):
            findings.extend((name, number, category) for category in categories(line, forbidden))
    objects = 0
    if args.history:
        historical, objects = history_findings(forbidden)
        findings.extend(historical)
    # Report locations, never echo potentially private source text.
    for name, number, category in findings:
        print(f'{name}:{number}: {category}')
    print(f'Checked {checked} UTF-8 files and {objects} history objects; {len(findings)} findings.')
    return 1 if findings else 0


if __name__ == '__main__':
    raise SystemExit(main())
