#!/usr/bin/env python3
"""Prepare an isolated deployment. Never touches Hermes config or QQ Bot."""
import argparse
import json
import os
from pathlib import Path
import secrets
import re
import subprocess


def write_new(path, content, mode=0o600):
    if path.exists():
        raise SystemExit(f'Refusing to overwrite existing file: {path}')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, 'w') as stream:
        stream.write(content)


def prepare(root, image):
    if not re.fullmatch(r'mlikiowa/napcat-docker@sha256:[0-9a-f]{64}', image):
        raise SystemExit('Require an inspected, pinned upstream image digest')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if any((root / p).exists() or (root / p).is_symlink() for p in ['compose.yaml', '.env', 'adapter.env', 'data/config/webui.json', 'data/config/onebot11.json']):
        raise SystemExit('Deployment exists; refusing to regenerate credentials/config')
    for name in ['data/qq', 'data/config', 'data/plugins']:
        (root / name).mkdir(parents=True, exist_ok=True, mode=0o700)
    web = secrets.token_urlsafe(36)
    onebot = secrets.token_urlsafe(36)
    write_new(root / '.env', f'NAPCAT_IMAGE={image}\nNAPCAT_UID={os.getuid()}\nNAPCAT_GID={os.getgid()}\n')
    write_new(root / 'adapter.env', f'NAPCAT_ACCESS_TOKEN={onebot}\n')
    write_new(root / 'data/config/webui.json', json.dumps({'host':'0.0.0.0','port':6099,'token':web,'loginRate':3}, indent=2)+'\n')
    config = {'network': {'httpServers': [], 'httpClients': [], 'websocketClients': [], 'websocketServers': [{'name':'hermes-forward','enable':True,'host':'0.0.0.0','port':3001,'messagePostFormat':'array','reportSelfMessage':False,'token':onebot,'debug':False,'heartInterval':30000}]},'musicSignUrl':'','enableLocalFile2Url':False,'parseMultMsg':False}
    write_new(root / 'data/config/onebot11.json', json.dumps(config, indent=2)+'\n')
    write_new(root / 'compose.yaml', '''name: hermes-napcat
services:
  napcat:
    image: ${NAPCAT_IMAGE:?pinned image required}
    hostname: qqbridge
    restart: unless-stopped
    init: true
    environment:
      NAPCAT_UID: ${NAPCAT_UID:?required}
      NAPCAT_GID: ${NAPCAT_GID:?required}
    ports:
      - "127.0.0.1:16099:6099"
      - "127.0.0.1:13011:3001"
    volumes:
      - ./data/qq:/app/.config/QQ
      - ./data/config:/app/napcat/config
      - ./data/plugins:/app/napcat/plugins
    security_opt:
      - no-new-privileges:true
    shm_size: 256mb
    mem_limit: 2g
    pids_limit: 256
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
''')
    subprocess.run(['docker','compose','-f',str(root/'compose.yaml'),'--env-file',str(root/'.env'),'config','--quiet'],check=True)
    print(f'Prepared and validated {root}; credentials were not printed. Official qqbot unchanged.')


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,default=Path.home()/'docker/napcat')
    parser.add_argument('--image',required=True)
    args=parser.parse_args()
    prepare(args.directory.expanduser().resolve(),args.image)
