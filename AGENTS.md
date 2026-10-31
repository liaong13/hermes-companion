# Hermes Companion

安装、升级或接入请求先读 [INSTALL.md](INSTALL.md)，使用 `scripts/install.py` 成套处理插件、宿主补丁、Skills、来源记录和配置，不把仓库根目录交给 `hermes plugins install`。

开发时保留已有修改。安装器默认只读计划；只有用户要求实际安装时执行 `--apply`，需要激活时加 `--restart`。只收到仓库链接而没有安装意图时先解释用途。QQ 账号、主人与群范围由用户或既有配置提供；密钥使用环境或仅用户可读的 token 文件，不回显、不写入仓库。

新安装默认包含本地人物候选和自然接话；原始对话记录、共同经历与约定需明确选择 `--features local-memory`，主动约定联系另需 `--proactive-agreements --idle-groups`。保留已有私人 memory provider，不把 scoped-memory 数据层的分发当作已启用 provider。

修改安装器后运行 `python3 -m unittest discover -s tests -v`、`python3 scripts/check_portability.py` 和 `git diff --check`；组件改动另做相关回归。用合成 home 验证写入，不用开发者的活动 profile 做安装测试。安装、网关连接与 QQ 客户端送达分别报告；不绕过 Hermes 自重启保护，不自动提交或推送。
