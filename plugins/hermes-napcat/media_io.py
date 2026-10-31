"""Bounded media I/O. No arbitrary NapCat paths, proxy inheritance or unsandboxed parsing."""
from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
from urllib.parse import urlsplit

import aiohttp

from .process_owner import ProcessCleanupError, ProcessOwner


class MediaError(ValueError):
    """Only fixed reason codes are exposed. Never include URLs or provider errors."""


def validate_download_policy(value):
    if type(value) is not str or value not in {'allowlist', 'public_https'}:
        raise ValueError('download_policy must explicitly be allowlist or public_https')
    return value


def public_ip(value):
    if '%' in value:
        return False
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address):
        # Reject mapped/transition addresses even when their embedded IPv4 is public.
        if (address.ipv4_mapped or address.sixtofour or address.teredo
                or address not in ipaddress.ip_network('2000::/3')):
            return False
    return address.is_global and not (address.is_multicast or address.is_reserved)


class PublicResolver(aiohttp.abc.AbstractResolver):
    def __init__(self):
        self.resolver = aiohttp.resolver.DefaultResolver()

    async def resolve(self, host, port=0, family=0):
        rows = await self.resolver.resolve(host, port, family)
        try:
            permitted = bool(rows) and all(public_ip(row['host']) for row in rows)
        except (ValueError, KeyError, TypeError):
            permitted = False
        if not permitted:
            raise MediaError('dns_denied')
        # These exact vetted addresses, not a second DNS lookup, feed the connector.
        return rows

    async def close(self):
        await self.resolver.close()


class MediaFetcher:
    def __init__(self, hosts=(), *, download_policy='allowlist', max_bytes=10 * 1024**2, test_loopback=False):
        self.download_policy = validate_download_policy(download_policy)
        self.hosts, self.max_bytes = frozenset(hosts), max_bytes
        self.test_loopback = test_loopback  # Code-injected test seam, absent from config schema.

    def validate_url(self, url):
        if (not isinstance(url, str) or len(url) > 8192
                or any(ord(c) < 33 or 127 <= ord(c) <= 159 for c in url)):
            raise MediaError('url_denied')
        try:
            parsed = urlsplit(url)
            host, port = parsed.hostname, parsed.port
            if (parsed.username is not None or parsed.password is not None or '#' in url
                    or not host or '%' in host or '\\' in url):
                raise ValueError
            if self.test_loopback:
                if parsed.scheme != 'http' or host != '127.0.0.1' or not port:
                    raise ValueError
            else:
                authority = f'[{host}]' if ':' in host else host
                if (parsed.scheme != 'https' or port not in (None, 443)
                        or parsed.netloc.lower() not in (authority, authority+':443')
                        or self.download_policy == 'allowlist' and host not in self.hosts):
                    raise ValueError
                try:
                    address = ipaddress.ip_address(host)
                except ValueError:
                    # Reject alternative numeric forms, IDNA/escaped hosts and
                    # ambiguous labels before aiohttp can normalize the authority.
                    if (len(host) > 253 or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                                                 for label in host.split('.'))
                            or not re.fullmatch(r'[a-z][a-z0-9-]*', host.split('.')[-1])
                            or re.fullmatch(r'0x[0-9a-f]+', host.split('.')[-1])):
                        raise MediaError('url_denied') from None
                else:
                    if not public_ip(str(address)):
                        raise MediaError('url_denied')
        except (ValueError, TypeError):
            raise MediaError('url_denied') from None
        return url

    async def fetch(self, url):
        self.validate_url(url)
        resolver = None if self.test_loopback else PublicResolver()
        connector = aiohttp.TCPConnector(resolver=resolver, use_dns_cache=False, limit=2)
        try:
            async with aiohttp.ClientSession(connector=connector, trust_env=False, auto_decompress=False,
                        cookie_jar=aiohttp.DummyCookieJar(),
                        timeout=aiohttp.ClientTimeout(total=20, connect=5, sock_read=5)) as session:
                async with session.get(url, allow_redirects=False, headers={'Accept-Encoding': 'identity'}) as response:
                    if response.status != 200:
                        raise MediaError('download_rejected')
                    if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                        raise MediaError('encoding_denied')
                    if response.content_length is not None and response.content_length > self.max_bytes:
                        raise MediaError('size_limit')
                    data = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        if len(data) + len(chunk) > self.max_bytes:
                            raise MediaError('size_limit')
                        data.extend(chunk)
                    if not data:
                        raise MediaError('empty_media')
                    return bytes(data), response.headers.get('Content-Type', '').split(';', 1)[0].lower()
        except MediaError:
            raise
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError):
            raise MediaError('download_failed') from None
        finally:
            if resolver:
                await resolver.close()


class ExportReader:
    """Dedicated owner-only shared export; path walk uses pinned dirfds and O_NOFOLLOW."""
    def __init__(self, root, prefix):
        self.root = Path(root)
        self.prefix = Path(prefix)
        if (not self.root.is_absolute() or not self.prefix.is_absolute()
                or self.root == Path('/') or self.prefix == Path('/') or '..' in self.prefix.parts):
            raise ValueError('invalid media export')
        current = Path('/')
        for part in self.root.parts[1:]:
            current /= part
            if current.is_symlink():
                raise ValueError('media export symlinks forbidden')
        info = self.root.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('media export must be owner-only')

    def read(self, path, max_bytes, cancelled=None):
        try:
            if not isinstance(path, str) or '\\' in path or '\x00' in path:
                raise ValueError
            relative = Path(path).relative_to(self.prefix)
            if not relative.parts or any(p in ('..', '.') for p in relative.parts):
                raise ValueError
            fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                # Rewalk the configured absolute root by dirfd too: checking
                # parent symlinks only at startup would leave a swap race.
                for part in self.root.parts[1:]:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    os.close(fd)
                    fd = child
                root_info = os.fstat(fd)
                if root_info.st_uid != os.getuid() or root_info.st_mode & 0o077:
                    raise ValueError
                for part in relative.parts[:-1]:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    os.close(fd)
                    fd = child
                file_fd = os.open(relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                try:
                    before = os.fstat(file_fd)
                    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > max_bytes:
                        raise ValueError
                    chunks, size = [], 0
                    while True:
                        if cancelled is not None and cancelled.is_set():
                            raise MediaError('cancelled')
                        chunk = os.read(file_fd, min(65536, max_bytes + 1 - size))
                        size += len(chunk)
                        if size > max_bytes:
                            raise MediaError('size_limit')
                        if not chunk:
                            break
                        chunks.append(chunk)
                    after = os.fstat(file_fd)
                    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                        raise ValueError
                    return b''.join(chunks), ''
                finally:
                    os.close(file_fd)
            finally:
                os.close(fd)
        except MediaError:
            raise
        except (ValueError, OSError):
            raise MediaError('export_path_denied') from None


class MediaSandbox:
    """Ephemeral namespace per attachment; bytes via pipes, no cache or home mounts.

    Optional reviewed runtime mounts allow an already installed faster-whisper
    plus local weights. Nothing downloads packages or model weights.
    """
    def __init__(self, *, bwrap=None, python='/usr/bin/python3', runtime_mounts=(), models=None, timeout=30):
        self.bwrap = bwrap or shutil.which('bwrap')
        self.python, self.runtime_mounts, self.models = python, tuple(runtime_mounts), models
        self.timeout = timeout
        self._cleanup_failed = False

    def command(self):
        if not self.bwrap:
            raise MediaError('sandbox_unavailable')
        cmd = [self.bwrap, '--unshare-all', '--die-with-parent', '--new-session', '--clearenv',
               '--ro-bind', '/usr', '/usr', '--ro-bind', '/lib', '/lib', '--proc', '/proc',
               '--dev', '/dev', '--tmpfs', '/tmp', '--tmpfs', '/work', '--chdir', '/work',
               '--setenv', 'HOME', '/work', '--setenv', 'PATH', '/usr/bin:/bin',
               '--setenv', 'OPENBLAS_NUM_THREADS', '1', '--setenv', 'OMP_NUM_THREADS', '1']
        if Path('/lib64').exists():
            cmd += ['--ro-bind', '/lib64', '/lib64']
        for mount in self.runtime_mounts:
            # Runtime mounts are code-supplied deployment capabilities, never inbound data.
            cmd += ['--ro-bind', str(mount), str(mount)]
        if self.models:
            cmd += ['--ro-bind', str(self.models), '/models']
        cmd += ['--ro-bind', str(Path(__file__).with_name('media_worker.py')), '/parser.py',
                '--', self.python, '-I', '-B', '/parser.py']
        return cmd

    async def process(self, kind, data, suffix=''):
        if self._cleanup_failed:
            raise MediaError('sandbox_shutdown_unconfirmed')
        if len(data) > 10 * 1024**2:
            raise MediaError('size_limit')
        payload = json.dumps({'kind': kind, 'data': base64.b64encode(data).decode(), 'suffix': suffix}).encode()
        owner = ProcessOwner(self.command(), process_group=True)
        try:
            process = await owner.start()
        except asyncio.CancelledError:
            await self._close_owner(owner)
            raise
        except OSError:
            await self._close_owner(owner)
            raise MediaError('sandbox_unavailable') from None
        async def exchange():
            async def write():
                process.stdin.write(payload)
                await process.stdin.drain()
                process.stdin.close()
            writer = asyncio.create_task(write())
            try:
                output = await process.stdout.read(14 * 1024**2 + 1)
                # StreamReader.read(n) need not return all n bytes.
                chunks, size = [output], len(output)
                while output and size <= 14 * 1024**2:
                    output = await process.stdout.read(min(65536, 14 * 1024**2 + 1 - size))
                    chunks.append(output)
                    size += len(output)
                if size > 14 * 1024**2:
                    raise MediaError('parser_output_limit')
                await writer
                await process.wait()
                if process.returncode:
                    raise MediaError('sandbox_failed')
                result = json.loads(b''.join(chunks))
                if result.get('status') != 'ok':
                    raise MediaError(result.get('error', 'parse_failed'))
                return result
            finally:
                if not writer.done():
                    writer.cancel()
                await asyncio.gather(writer, return_exceptions=True)
        try:
            operation = owner.track(exchange())
            return await asyncio.wait_for(asyncio.shield(operation), self.timeout if kind != 'whisper' else 90)
        except asyncio.TimeoutError:
            raise MediaError('parser_timeout') from None
        except (OSError, json.JSONDecodeError, BrokenPipeError):
            raise MediaError('sandbox_failed') from None
        finally:
            await self._close_owner(owner)

    async def _close_owner(self, owner):
        try:
            await owner.close()
        except ProcessCleanupError:
            self._cleanup_failed = True
            raise
