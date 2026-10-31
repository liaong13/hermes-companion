#!/usr/bin/env python3
"""Read-only deployment status; does not print secrets or login QR URLs."""
import asyncio
import json
from pathlib import Path
import subprocess
import urllib.request


def main():
    root=Path.home()/'docker/napcat'
    subprocess.run(['docker','compose','-f',str(root/'compose.yaml'),'ps'],check=True)
    try:
        with urllib.request.urlopen('http://127.0.0.1:16099/webui/',timeout=5) as response:
            print('WebUI HTTP:',response.status)
    except Exception as exc:
        print('WebUI unavailable:',type(exc).__name__)
    try:
        import aiohttp
    except ImportError:
        print('OneBot probe unavailable: run with an interpreter providing aiohttp')
        return
    async def probe():
        cfg=json.loads((root/'data/config/onebot11.json').read_text())
        token=cfg['network']['websocketServers'][0]['token']
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
                async with session.ws_connect('http://127.0.0.1:13011',headers={'Authorization':f'Bearer {token}'}) as ws:
                    await ws.send_json({'action':'get_status','params':{},'echo':'deployment-status'})
                    async with asyncio.timeout(8):
                        async for msg in ws:
                            if msg.type!=aiohttp.WSMsgType.TEXT:
                                continue
                            data=json.loads(msg.data)
                            if data.get('echo')=='deployment-status':
                                status=data.get('data') or {}
                                print('OneBot status:',json.dumps({'retcode':data.get('retcode'),'online':status.get('online'),'good':status.get('good')}))
                                return
        except Exception as exc:
            print('OneBot not ready:',type(exc).__name__,'(before QQ login this is expected)')
    asyncio.run(probe())


if __name__=='__main__':
    main()
