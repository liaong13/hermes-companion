#!/usr/bin/env python3
"""MoviePilot read-only discovery and explicit single-resource download."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler, Request


class MoviePilotError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def env_credentials(path):
    values = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, sep, value = line.partition("=")
            if sep and key.strip() in {"API_TOKEN", "API_KEY"}:
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                values[key.strip()] = value
    return values


class Client:
    def __init__(self):
        self.base = os.environ.get("MOVIEPILOT_BASE_URL", "").rstrip("/")
        if not self.base:
            raise MoviePilotError("未设置 MOVIEPILOT_BASE_URL，请指定自己的 MoviePilot API 地址")
        parts = urlsplit(self.base)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            raise MoviePilotError("MOVIEPILOT_BASE_URL 必须是无内嵌凭据的 HTTP(S) API 地址")
        self.key = os.environ.get("MOVIEPILOT_API_TOKEN") or os.environ.get("MOVIEPILOT_API_KEY")
        if not self.key and os.environ.get("MOVIEPILOT_ENV_FILE"):
            path = Path(os.environ["MOVIEPILOT_ENV_FILE"]).expanduser()
            config = env_credentials(path)
            self.key = config.get("API_TOKEN") or config.get("API_KEY")
        if not self.key:
            raise MoviePilotError("未找到 MoviePilot API 凭据；请核对服务主机和配置路径")
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def api(self, path, params=None, payload=None):
        url = self.base + path
        if params:
            url += "?" + urlencode(params)
        request = Request(url, data=json.dumps(payload).encode() if payload is not None else None,
                          headers={"X-API-KEY": self.key, "Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=240) as response:
                result = json.load(response)
        except HTTPError as error:
            raise MoviePilotError(f"MoviePilot HTTP {error.code}；未自动重试") from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise MoviePilotError("MoviePilot 请求失败或响应无效；未自动重试，请核对服务与任务状态") from None
        if not isinstance(result, dict) or "success" not in result:
            raise MoviePilotError("MoviePilot 响应契约不符；请核对当前版本的接口")
        return result


def data_or_error(result):
    if not result.get("success"):
        raise MoviePilotError("MoviePilot 业务返回失败；请检查本次请求日志与规则诊断")
    return result.get("data")


def safe_url(value):
    parts = urlsplit(value or "")
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if k in {"id", "hit"}])
    return urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, query, ""))


def is_pack(context):
    meta = context.get("meta_info") or {}
    title = (context.get("torrent_info") or {}).get("title") or ""
    return bool(meta.get("begin_season") is not None and not meta.get("begin_episode")
                and not meta.get("episode_list") and not re.search(r"S\d+\s*E\d", title, re.I))


def summary(context, index):
    torrent = context.get("torrent_info") or {}
    meta = context.get("meta_info") or {}
    size = torrent.get("size")
    return {"index": index, "site": torrent.get("site_name"), "title": torrent.get("title"),
            "season_episode": meta.get("season_episode"), "pack_candidate": is_pack(context),
            "size_gib": round(float(size) / 2**30, 2) if size is not None else None,
            "seeders": torrent.get("seeders"), "download_factor": torrent.get("downloadvolumefactor"),
            "description": torrent.get("description"), "page_url": safe_url(torrent.get("page_url"))}


def private_write(path, value, exclusive=False):
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def save_snapshot(result, query, base):
    home = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
    root = Path(os.environ["TMPDIR"]) if os.environ.get("TMPDIR") else home / "cache/scratch"
    folder = root / "moviepilot-torrents"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder.chmod(0o700)
    path = folder / ("search-" + uuid.uuid4().hex + ".json")
    snapshot = {"version": 1, "created_at": now(), "base_url": base, "query": query,
                "success": result.get("success"), "contexts": result.get("data") or []}
    private_write(path, snapshot, exclusive=True)
    return path, snapshot


def load_snapshot(path):
    snapshot = json.loads(Path(path).read_text(encoding="utf-8"))
    if snapshot.get("version") != 1 or not isinstance(snapshot.get("contexts"), list):
        raise MoviePilotError("快照格式不符，请重新执行 resources")
    return snapshot


def ranked(snapshot, contains=(), site=None, packs=False):
    rows = []
    for index, context in enumerate(snapshot["contexts"]):
        torrent = context.get("torrent_info") or {}
        if site and torrent.get("site_name") != site:
            continue
        title = (torrent.get("title") or "").casefold()
        if not all(term.casefold() in title for term in contains) or (packs and not is_pack(context)):
            continue
        rows.append(summary(context, index))
    return sorted(rows, key=lambda row: int(row["seeders"]) if row["seeders"] is not None else -1, reverse=True)


def normalize_title(value):
    return re.sub(r"[\W_]+", "", value or "").casefold()


def task_summary(task):
    return {key: task.get(key) for key in ["hash", "title", "site_name", "downloader", "state",
                                           "progress", "dlspeed", "save_path", "size"]}


def download(path, index, submit=False, downloader=None, save_path=None, client=None):
    snapshot = load_snapshot(path)
    if not 0 <= index < len(snapshot["contexts"]):
        raise MoviePilotError("索引超出当前快照范围")
    context = snapshot["contexts"][index]
    torrent, media = context.get("torrent_info"), context.get("media_info")
    if not torrent or not media or not torrent.get("enclosure"):
        raise MoviePilotError("所选上下文缺少媒体信息或下载链接，请重新搜索")
    plan = {"submitted": False, "snapshot_time": snapshot["created_at"], "selection": summary(context, index)}
    if not submit:
        return plan
    client = client or Client()
    if snapshot["base_url"] != client.base:
        raise MoviePilotError("当前 API 地址与快照不同，请在目标服务上重新搜索")
    receipt_path = Path(str(path) + f".{index}.submission.json")
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        return {**plan, "already_attempted": True, "receipt": receipt,
                "note": "未重复 POST；先按回执核对实际下载器"}
    tasks = data_or_error(client.api("/download/")) or []
    duplicates = [task_summary(task) for task in tasks
                  if normalize_title(task.get("title")) == normalize_title(torrent.get("title"))]
    if duplicates:
        return {**plan, "existing_tasks": duplicates, "note": "已存在同标题任务，未重复提交"}
    receipt = {"state": "uncertain", "attempted_at": now(), "index": index,
               "site": torrent.get("site_name"), "title": torrent.get("title")}
    try:
        private_write(receipt_path, receipt, exclusive=True)
    except FileExistsError:
        raise MoviePilotError("该资源已由另一次调用开始提交，请先核对回执") from None
    payload = {"media_in": media, "torrent_in": torrent}
    if downloader:
        payload["downloader"] = downloader
    if save_path:
        payload["save_path"] = save_path
    result = client.api("/download/", payload=payload)
    data = data_or_error(result) or {}
    if not data.get("download_id"):
        raise MoviePilotError("接口未返回 download_id，提交结果不明；请先核对下载器")
    receipt.update(state="submitted", download_id=data["download_id"])
    private_write(receipt_path, receipt)
    return {**plan, "submitted": True, "receipt": receipt}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("media", help="只读作品搜索")
    p.add_argument("title")
    p = sub.add_parser("resources", help="只搜索种子，保存私有快照")
    p.add_argument("--source", default="themoviedb")
    p.add_argument("--id", required=True)
    p.add_argument("--type", choices=["tv", "movie"], required=True)
    p.add_argument("--season", type=int)
    p.add_argument("--sites")
    p.add_argument("--limit", type=int, default=10)
    p = sub.add_parser("rank", help="离线筛选并按做种人数排序")
    p.add_argument("snapshot")
    p.add_argument("--contains", action="append", default=[])
    p.add_argument("--site")
    p.add_argument("--packs", action="store_true")
    p.add_argument("--limit", type=int, default=10)
    p = sub.add_parser("download", help="默认只生成摘要；--submit 才添加下载")
    p.add_argument("snapshot")
    p.add_argument("--index", type=int, required=True)
    p.add_argument("--submit", action="store_true")
    p.add_argument("--downloader")
    p.add_argument("--save-path")
    p = sub.add_parser("status", help="只读查询正在下载任务")
    p.add_argument("--hash", required=True)
    sub.add_parser("rules", help="只读查询规则语法")
    args = parser.parse_args(argv)
    if args.command == "rank":
        snapshot = load_snapshot(args.snapshot)
        rows = ranked(snapshot, args.contains, args.site, args.packs)
        result = {"snapshot_time": snapshot["created_at"], "count": len(rows), "results": rows[:max(0, args.limit)]}
    elif args.command == "download":
        result = download(args.snapshot, args.index, args.submit, args.downloader, args.save_path)
    else:
        client = Client()
        if args.command == "media":
            rows = data_or_error(client.api("/media/search", {"title": args.title})) or []
            result = {"results": [{key: row.get(key) for key in ["title", "year", "type", "media_source", "media_id", "vote_average", "overview"]} for row in rows]}
        elif args.command == "resources":
            if args.type == "tv" and args.season is None:
                parser.error("电视剧搜索需明确 --season")
            query = {"media_source": args.source, "mtype": "电视剧" if args.type == "tv" else "电影"}
            if args.season is not None:
                query["season"] = args.season
            if args.sites:
                query["sites"] = args.sites
            raw = client.api("/search/media/" + quote(args.id, safe=""), query)
            path, snapshot = save_snapshot(raw, {"media_id": args.id, **query}, client.base)
            rows = ranked(snapshot)
            result = {"success": snapshot["success"], "snapshot": str(path), "snapshot_time": snapshot["created_at"],
                      "count": len(rows), "results": rows[:max(0, args.limit)]}
            if not raw.get("success"):
                result["note"] = "API 未返回成功结果；先检查 rules 与本次搜索日志，不直接判断无资源"
        elif args.command == "status":
            rows = data_or_error(client.api("/download/")) or []
            result = {"tasks": [task_summary(row) for row in rows if row.get("hash") == args.hash],
                      "scope": "正在下载列表，缺失不代表任务不存在或已完成"}
        else:
            groups = (data_or_error(client.api("/rule/groups")) or {}).get("rule_groups", [])
            result = {"groups": [{key: row.get(key) for key in ["name", "syntax_valid", "syntax_error"]} for row in groups]}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (MoviePilotError, OSError, ValueError, KeyError, TypeError) as error:
        # 不输出 traceback、请求头、原始响应或缓存内容。
        message = str(error) if isinstance(error, MoviePilotError) else "本地文件或数据格式异常，请检查配置与私有快照"
        print(json.dumps({"error": message}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
