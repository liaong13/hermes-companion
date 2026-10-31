# MoviePilot Torrents Skill

通过已有 MoviePilot 服务搜索影视作品与种子，按平台、发布组、分辨率和做种人数比较资源，并在用户明确授权后下载选中的一份。该目录包含可独立安装的 Hermes Skill、Python 标准库脚本、API 排错参考和离线测试。

默认只搜索或展示；不会创建订阅。`download` 默认只生成所选资源摘要，只有 `--submit` 才调用添加下载接口。创建订阅、修改过滤规则、删除任务和物理文件整理不由本脚本执行。

## 前提与配置

- 已安装 Hermes，当前会话能够调用 `terminal`，执行环境具备 Python 3.10 或更高版本及 POSIX 文件权限接口；面向 Linux/macOS，Windows 尚未验证。
- 已有可访问的 MoviePilot 服务、可用站点及下载器；本 Skill 不安装 MoviePilot，也不附带站点账号或下载器配置。
- 当前实现按 MoviePilot V3 的接口验证；其他版本先核对 [API 契约](references/api.md)，不要假定 `/openapi.json` 或 `/docs` 一定开放。
- `terminal` 所在主机须能访问配置的 API；容器、SSH 后端和远程 Hermes 的 `127.0.0.1` 分别指向各自执行主机。

连接地址必须显式配置，包含 API 前缀；以下地址和端口只是同主机部署示例：

```sh
export MOVIEPILOT_BASE_URL='http://127.0.0.1:13001/api/v1'
export MOVIEPILOT_ENV_FILE='/absolute/path/to/moviepilot/app.env'
```

`MOVIEPILOT_ENV_FILE` 指向使用者自己的配置文件，脚本仅读取其中的 `API_TOKEN` 或 `API_KEY`，不执行文件内容。也可使用由当前进程安全注入的 `MOVIEPILOT_API_TOKEN` / `MOVIEPILOT_API_KEY`；它们优先于文件。不要把密钥字面量写进命令历史、仓库或聊天。

不设置默认服务或维护者的凭据路径。配置必须对实际运行 Hermes 的进程可见；交互 shell 的变量不自动进入 systemd 或容器中的 Hermes。配置目标进程的环境，不为本 Skill 改写身份或全局提示词。

## 安装到当前 profile

在仓库根目录执行，首次安装前确认目标不存在：

```sh
skill_dest="${HERMES_HOME:-$HOME/.hermes}/skills/media/moviepilot-torrents"
test ! -e "$skill_dest" && mkdir -p "$skill_dest" && cp -R skills/moviepilot-torrents/. "$skill_dest/"
```

如果目标已存在，先比较源码与安装副本，保留本地修改后再做定向更新，不直接覆盖整目录。复制整个目录，不能只复制 `SKILL.md` 而漏掉脚本、参考文档或许可证。

用 `skills_list` / `skill_view("moviepilot-torrents")` 核对发现与加载；也可用 `hermes skills list --source local` 查看状态。加载成功不代表 MoviePilot 已连接或已获准下载。仓库更新不会自动同步运行副本，安装也不自动改写 Hermes 服务环境。

## 使用

可在 Hermes 中输入：

```text
/moviepilot-torrents 搜索某部作品的种子资源，先只展示结果
```

脚本也可直接从仓库运行：

```sh
mp_script='skills/moviepilot-torrents/scripts/moviepilot.py'
python3 "$mp_script" media '作品名称'
python3 "$mp_script" resources --source themoviedb --id "$media_id" --type tv --season 1
python3 "$mp_script" rank "$snapshot" --contains CATCHPLAY --contains ADWeb --contains 1080p --packs
python3 "$mp_script" download "$snapshot" --index "$selected_index"
```

`media_id`、`snapshot`、`selected_index` 依次取自本次作品搜索、资源搜索和筛选结果；索引只属于对应快照。用户已明确授权后才加 `--submit`，随后用 `status --hash` 核对真实任务。完整流程与失败边界见 [SKILL.md](SKILL.md)。

缓存中含下载所需的 cookie 或签名地址，目录权限为 `0700`、文件为 `0600`。缓存放在 `$TMPDIR/moviepilot-torrents/`，未设置时放在当前 Hermes profile 的 `cache/scratch/moviepilot-torrents/`；不要提交快照、回执或原始 HTTP 响应。标准输出只提供脱敏摘要。

## 验证

在仓库根目录运行：

```sh
python3 -m unittest discover -s skills/moviepilot-torrents/tests -v
python3 skills/moviepilot-torrents/scripts/moviepilot.py --help
git diff --check -- skills/moviepilot-torrents README.md docs/README.md
```

离线测试覆盖筛选与原始索引、默认预览无网络请求、精确上下文提交、重复任务、结果不明时不重试、私有文件权限和显式连接配置。它们使用虚构数据，不连接服务或新增下载。

真实部署验证只需在自己的授权环境执行 `media`、`resources`、`rules` 和 `status`。2026-10-31 的维护环境已验证作品搜索、资源搜索、跨站比较、下载预览和任务状态；这不证明每个版本和站点均兼容。下载接口已有实际成功记录，但本脚本的提交分支仅做离线模拟验证，不为安装验收新增下载。

## 许可证与来源

本目录自有代码与文档采用 [MIT 许可证](LICENSE)。流程来自 MoviePilot V3 源码核对及 API 验证；未复制其实现代码。MoviePilot、Hermes 及使用者接入的站点遵循各自授权，本 Skill 不重新授权第三方服务或内容。
