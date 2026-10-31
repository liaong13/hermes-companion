# Hermes Companion · NapCat Docker 部署辅助

本目录为 [NapCat 接入组件](../README.md)准备独立 Docker 服务，源码来自 [Hermes Companion](../../../README.md)。它**不安装 Hermes 插件、不修改 Hermes 配置、不停用官方 `qqbot`，也不自动切换现有入口**。

适用于已有 Docker Engine／Compose、可登录专用测试 QQ 的 Linux 主机。容器服务运行、WebUI 可访问、QQ 在线、模型回复和隔离验收是不同层级，不能相互替代。

已有 NapCat 时直接使用仓库 [INSTALL.md](../../../INSTALL.md)完成 Hermes 侧成套接入。该安装器不创建本目录的容器、不代登录 QQ；准备新服务仍按本页单独执行。

## 源码与运行数据分开

- 源码示例：`$HOME/tools/hermes-companion/plugins/hermes-napcat/deploy/`。
- 默认部署目录：`$HOME/docker/napcat/`，由 `prepare.py --directory` 改为其他目录。
- `compose.yaml` 与 `.env`：镜像摘要、UID/GID 和容器运行设置。
- `adapter.env`：供 Hermes 网关安全注入的 `NAPCAT_ACCESS_TOKEN`，不会自动加载进网关。
- `data/config/`：WebUI 与 OneBot 配置，含不同的密钥。
- `data/qq/`：真实 QQ 登录数据；`data/plugins/`：NapCat 插件数据。

真实配置、密钥、账号数据和日志不要提交到源码仓库；备份应加密。源码仓库改名不表示需要迁移既有部署、runtime 或 backups 目录。

## 准备新的部署

在仓库根目录执行。先拉取并检查镜像，再把命令中的占位摘要替换成实际核实的 64 位十六进制摘要；不要直接用浮动 `latest` 生成部署：

```sh
export COMPANION_SOURCE="$HOME/tools/hermes-companion"
export NAPCAT_DEPLOY="$HOME/docker/napcat"
cd "$COMPANION_SOURCE"
docker pull mlikiowa/napcat-docker:latest
docker image inspect mlikiowa/napcat-docker:latest --format '{{json .RepoDigests}}'
python plugins/hermes-napcat/deploy/prepare.py \
  --directory "$NAPCAT_DEPLOY" \
  --image 'mlikiowa/napcat-docker@sha256:<已核实摘要>'
docker compose -f "$NAPCAT_DEPLOY/compose.yaml" \
  --env-file "$NAPCAT_DEPLOY/.env" up -d
```

`prepare.py` 只接受 `mlikiowa/napcat-docker@sha256:` 固定摘要，创建新文件并执行 `docker compose config --quiet`。它拒绝覆盖已有 compose、凭据及关键配置，不是配置升级／迁移工具；若中途失败，不要直接删除已有账号数据后重跑。

脚本生成彼此独立的 WebUI／OneBot token，配置与密钥文件权限 `0600`，新建数据目录权限 `0700`。不设置 `MODE` 环境变量，避免上游入口复制模板覆盖预先配置的鉴权。

## 登录与连接

| 用途 | 主机回环地址 | 容器端口 |
| --- | --- | --- |
| WebUI | `http://127.0.0.1:16099/webui/` | 6099 |
| OneBot11 正向 WS | `ws://127.0.0.1:13011` | 3001 |

WebUI 密钥在 `$NAPCAT_DEPLOY/data/config/webui.json`，只在本机受保护的编辑器或密码管理流程中查看，不要粘贴进聊天或 Git。登录后 WebUI 可能刷新 token，按界面要求操作。使用专用 QQ 扫码并在手机确认；OneBot 通常在 QQ 登录后才就绪。

OneBot 配置使用 `messagePostFormat: array`、关闭 self-message 上报，并将正向 WS token 与 `adapter.env` 中的值保持一致。**WebUI token 不能替代 OneBot token。** 按[插件配置说明](../README.md#配置先保持关闭再逐项启用)将 `NAPCAT_ACCESS_TOKEN` 安全注入实际网关进程；`adapter.env` 的存在不证明服务已经读到它。

远程管理只使用 SSH 本地转发，不开放公网端口：

```sh
ssh -L 16099:127.0.0.1:16099 <实际主机>
```

## 容器权限与数据风险

Compose 仅将端口绑定主机回环，不使用 `privileged`，不挂载 Docker socket，启用 `no-new-privileges`；设置 256 MiB shared memory、2 GiB 内存、256 PID 上限及日志轮转（10 MiB × 3）。

镜像入口仍以初始 root 执行用户映射，再降到当前主机 UID/GID；容器不是绝对隔离。上游存在尝试 bind-mount 运行环境文件的代码，本配置没有授予 `SYS_ADMIN` 或 privileged，**不要为消除这类报错而增加权限**。

日志可能含二维码登录 URL 和 WebUI token，不要直接粘贴原始日志或作为公开附件。Docker 日志轮转不覆盖 `data/` 中的应用日志和媒体缓存，它们仍需独立保留／清理策略。数据备份应覆盖账号与配置并加密保存，不要把源码更新当成运行数据备份。

## 状态检查与停止

```sh
docker compose -f "$NAPCAT_DEPLOY/compose.yaml" ps
# 仅适用于默认 $HOME/docker/napcat，且解释器已提供 aiohttp
python plugins/hermes-napcat/deploy/status.py
```

`status.py` 是只读检查：输出容器状态、WebUI HTTP 状态以及带鉴权的 OneBot `get_status`（retcode／online／good），不输出 token 或登录二维码 URL。**它目前硬编码默认部署目录和默认端口，没有 `--directory` 参数**；自定义目录不要将它的结果当成目标部署状态，也不要为检查而再启动第二个消息接收网关。

普通回退先停止本容器，保留数据与旧入口：

```sh
docker compose -f "$NAPCAT_DEPLOY/compose.yaml" stop
```

此命令不影响官方 `qqbot`。不要用 `down -v` 或删除 `data/` 回退普通配置。

## 切换与验收门槛

自动化工具测试可从插件目录运行：

```sh
cd "$COMPANION_SOURCE/plugins/hermes-napcat"
python -m unittest discover -s deploy -p 'test_*.py' -v
```

上线前须另记真实结果：专用账号登录、好友私聊收发、指定群真实 @ 或显式名称触发、非触发拒绝、未授权用户／群拒绝、昵称／群名片、重连与去重、私人／公共边界、媒体实际能力，以及旧 `qqbot` 仍可用。基础通道或某个 TXT 场景通过，不代表所有文件／语音、长期记忆或供应商数据删除已经验收。

当前已有事实以[开发状态顶部记录](../docs/开发状态.md)和[权限部署验收](../docs/权限部署验收.md)为准；历史“待部署”只说明当时阶段。目标环境全部必要条目通过后，再由操作者明确确认切换，保留旧配置与本地补丁。宿主、隔离及媒体的额外准备见[测试部署接线](../docs/测试部署接线.md)。

如需发布自己的部署仓库，不要上传上述数据，建议默认私有。Git SSH 可访问某仓库不等于有创建仓库的 API 权限；使用已授权 GitHub CLI 或预建仓库，不在聊天索取 token。
