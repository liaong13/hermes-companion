# MoviePilot API 与排错

以下契约来自 2026-10-31 对 MoviePilot V3 源码及实际 HTTP 请求的核对。版本升级、端口和站点状态需要重新核对；历史验收不证明任意 MoviePilot 部署兼容。

## 已验证接口

以使用者显式配置的 `MOVIEPILOT_BASE_URL` 为前缀，例如同主机部署的 `http://127.0.0.1:13001/api/v1`；示例端口不是 MoviePilot 的通用默认值。认证放在 `X-API-KEY` 请求头。响应通常是 `{"success": true, "message": "", "data": ...}`；HTTP 200 不保证业务成功。

| 用途 | 方法与路径 | 参数 / 请求体 |
|---|---|---|
| 作品搜索 | `GET /media/search` | `title` |
| 作品详情 | `GET /media/{media_id}` | `media_source=themoviedb`、`type_name=电视剧` 或 `电影` |
| 季信息 | `GET /media/seasons` | `media_source`、`media_id`，可加 `season` |
| 精确资源搜索 | `GET /search/media/{media_id}` | `media_source`、`mtype=电视剧/电影`、剧集 `season`，可选 `sites` 为当前部署的站点 ID 逗号列表 |
| 规则语法诊断 | `GET /rule/groups` | 返回 `syntax_valid`、`syntax_error`、`usage`；不要在公共会话输出完整 usage |
| 正在下载 | `GET /download/` | 可选下载器 `name`；data 是任务列表 |
| 可用下载器 / 路径 | `GET /download/clients`、`GET /download/paths` | 只读，按需查看 |
| 添加所选下载 | `POST /download/` | `media_in`、`torrent_in`，可选 `downloader`、`save_path` |

下载体使用所选搜索上下文：

```python
payload = {
    "media_in": context["media_info"],
    "torrent_in": context["torrent_info"],
}
```

不要把整个 context 直接作为 body，也不要将 `enclosure` 当作普通公开链接打印出来。响应中的 `data.download_id` 用于与任务列表的 `hash` 对齐。

搜索结果中的 `torrent_info` 含 `site`、`site_name`、`title`、`description`、`size`（字节）、`seeders`、`page_url`、`downloadvolumefactor` 和 `uploadvolumefactor`，也可能含敏感 cookie 与签名下载地址。下载计量 `0` 表示免费，`0.5` 表示 50%，`1` 表示全量；未知值不要当免费。

## 搜索返回空列表

先查同次请求时间窗口，不把不同请求、订阅轮次或旧日志拼成一条链：

1. 认证/连通：401/403 是认证权限问题；404 可能是 API 路由差异。本机曾不开放 `/openapi.json`、`/docs`，不要假定文档端点存在。
2. 作品身份：确认媒体来源、原生 ID、类型与季号。
3. 站点访问：核对访问成功、超时、限流和返回数量；部分站点失败应标为覆盖不全。
4. 过滤：检查 `GET /rule/groups` 和日志中的 `filter_rules`、`filter_params`、`ParseException`。候选大于零但过滤出错，应报告“过滤失败”。
5. 精确匹配：过滤正常后再检查别名、IMDb/TMDB 匹配与季集条件。仅当整个请求完成且没有相关异常时，才能称“本次搜索未返回符合条件的资源”。

历史验证案例：手动 API 取回 499 个候选，`日番`第 6、7 层的 `B-Global` 规则 ID 导致 `Expected end of text, found '-'`，过滤后为零。改为字母数字 ID `BGlobal` 并同步两处引用后，同一搜索 API 返回 167 条。手动与订阅的配置键不同，但当时两者启用的规则组集合相同；“候选日志有条目”不证明订阅最终搜索成功。

若用户明确要求修规则，先重新读取规则配置并保存快照。已验证的 V3 接口 `PUT /rule/custom/{old_id}`、请求体 `{"new_rule_id":"BGlobal"}` 支持原子改名及重写引用；其他版本先核对当前契约。修复后核对其他规则不变、全部语法有效，再做只读搜索验收。这个维护入口不由脚本提供，普通搜索不得调用。

## 下载结果不明确

脚本在 POST 前先建立 `snapshot.<index>.submission.json` 回执，随后只提交一次。`submitted` 表示接口已返回 ID；`uncertain` 表示无法确认是否发生下载副作用，也包括服务返回添加失败。

- 先按选中的站点、标题、提交时间查询 `/download/`，再按需查看 MoviePilot 下载历史和实际下载器；只读核对，不再次添加。
- 正在下载列表不含所有已完成/做种任务，空列表不能证明没有提交。下载器可能把标题中的空格改成句点，比较时需要规范化。
- 查到真实任务后用其 hash 跟踪；无须改回执来宣称已提交。
- 仅在确认未创建任务、且用户原下载授权仍有效时，才可保留原回执并重新搜索产生新快照后重试一次；对同一不明确请求不要循环换快照绕过保护。
- `GET /download/` 不能满足全局查重时，以下载器和历史为准。脚本的同标题预检查及本地一次性回执是辅助，不是跨全部历史的幂等保证。

## Hermes 安装依据

本技能建议安装在当前 profile 的 `skills/media/moviepilot-torrents/`，入口及脚本作为一个完整目录维护；源码与安装副本分离。格式和发现机制依据 [Hermes Skills System](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)，实际发现仍用当前环境的 `skills_list` / `skill_view` 验证。不要为加载本技能改写身份、全局提示词或配置文件。
