# NapCat 未点名候选的安静交付补丁

该补丁是[主动聊天正式功能](../../plugins/hermes-napcat/docs/主动聊天.md)的补充，不替代 [NapCat 合同 5 补丁](napcat-public-turn.md)。源基线为 `5894e750c3dba980d43b25aa29e3958149ba2c61`；其他版本先查实际源码及本地差异，不盲目覆盖。

## 为什么需要宿主改动

`reply_expected=False` 已允许最终 `NO_REPLY` 静默，但仅抑制最终结果不够：若工具进度、流式草稿或中途回复先发出，群友仍会被打扰。

[napcat-ambient-quiet.patch](napcat-ambient-quiet.patch)只对 NapCat 的未寻址候选关闭常规工具进度、思考／中途回复、文本增量、流式 TTS 和长任务存活提示。保留最后选择发送的文本、审批和失败路径；明确寻址 NapCat 及其他平台的普通回合不变。

它不设置 `scheduled_heartbeat` 或 `internal`，不修改用户、消息 ID、codec、source permit 或工具授权。真实未点名群消息仍走原合同 5，不冒充自动化或主人请求。空闲主动发言仍由插件独立的受限文本能力完成。

## 最小检查

```sh
# 仅检查，不应用；HERMES_SOURCE 指向实际宿主源码
# 已应用时用 --reverse --check 验证当前目标包含这份增量

git -C "$HERMES_SOURCE" apply --check /path/to/napcat-ambient-quiet.patch

# HERMES_TEST_PYTHON 必须指向已有 pytest 的隔离测试环境，不是应用运行时
HERMES_SOURCE="$HERMES_SOURCE" "$HERMES_TEST_PYTHON" -m pytest \
  /path/to/hermes-companion/integration/hermes-host/test_napcat_ambient_quiet.py \
  -q -p no:cacheprovider --basetemp "$TMPDIR/napcat-ambient-pytest"
```

应用运行时可能没有 pytest；当前维护环境的 `hermes --run-module pytest` 实测缺少该模块，不能把这个入口写成已验证命令，也不要用 pip 修改应用依赖。使用已准备的 PM test-environment；宿主自身测试仍使用其 canonical runner，注意该 runner 的 `/var/tmp` 临时目录前提与本地临时目录政策是否兼容。

本独立集成测试在导入宿主前隔离 profile／凭据，使用真实 GatewayRunner 路径及离线模型／平台夹具，不连接 QQ 或云模型，不能代替网关重载与 QQ 客户端确认。补丁源码已写入不代表活动 Python 进程已加载；从网关进程树内不得绕过自身重启保护。
