# NapCat 公共群回合与持久化准入修复

本材料与 [NapCat 插件](../../plugins/hermes-napcat/README.md) 的 `runtime.mode: social_agent / contract: 5` 配套。默认 `host_llm / contract: 4` 仍是无工具单次问答，不因插件升级自动改变。这里的宿主补丁与 [llm-facade.patch](llm-facade.patch) 的记忆提供者绑定能力是独立交付。

## 故障与修复

真实网关第一次接收群消息时，adapter 为精确的原始 source 发出进程内 permit。正式 gateway 会将消息持久化，并经过 `copy(source)`、source codec 和第二次 `_handle_message`。旧实现只认 Python 对象身份，导致重建后的合法消息被判为 `Unauthorized user`，队列却记录为 completed，没有模型调用和回复。

[napcat-public-turn.patch](napcat-public-turn.patch) 包含本模式依赖的完整宿主接线：

- 以 adapter 的来源证明准入群消息，按群与真实成员隔离会话；群控制命令和敏感审批不进入群。
- 默认不加载私人 SOUL、USER、MEMORY、prefill 或本地文件引用；群正文的 `@file:`、`@folder:` 和插件引用保持字面文本。显式启用[共享人格](../../plugins/hermes-napcat/docs/共享人格与日常记忆.md)时，只由可信通道提供同源 SOUL 与筛选审核后的非敏感日常摘要，仍不开放私人原文或文件读取工具。
- 每次实际工具派发核验 turn authority；工具定义可见不代表拥有私人资料或主机权限。
- 公共群 native validation 与快照恢复接续仍有效的原始 source。恢复必须完整匹配唯一的 live permit，并复核策略 revision、连接代数、撤回及插件寿命；不序列化可复用凭证，也不以 `_napcat_public_turn` 或队列状态代替授权。
- permit 查找只在 ambient permit 精确属于当前 source 时优先使用它，否则查 source ledger，防止 A 回合的上下文干扰 B。

重启后没有 live permit 的旧快照失败关闭，不会自动重新授权或重投历史消息。本补丁不是持久出站能力或可靠投递保障。

## 基线与应用

唯一核对的基线为 `5894e750c3dba980d43b25aa29e3958149ba2c61`。其他版本须重新审阅和测试。补丁不含运行配置、凭据、账号、群号、数据库、其他 profile 文件或旧 qqbot 本地修改。

```sh
export COMPANION_SOURCE="$HOME/tools/hermes-companion"
export HERMES_SOURCE="/path/to/hermes-agent"
git -C "$HERMES_SOURCE" rev-parse HEAD
git -C "$HERMES_SOURCE" status --short
git -C "$HERMES_SOURCE" apply --check \
  "$COMPANION_SOURCE/integration/hermes-host/napcat-public-turn.patch"
```

先在受控副本中审阅、应用和测试，再经授权部署；已有修改不得整目录覆盖。若目标已经应用该补丁，可用 `git apply --reverse --check` 辅助核对，不能再次叠加。补丁不提供自动安装器，不修改任何配置，也不会重启网关。应用时须一起部署同版 `host_runtime.py` 及兄弟 `hermes-scoped-memory/isolated_worker.py`；只安装一端不能工作。配置继续通过 Hermes 受支持的 CLI 修改。

## 验证证据与限制

2026-10-29 的实际结果：

- 插件套件执行 168 项：165 通过，3 项旧 standalone agent 的本地测试跳过。跳过的本地草案不作为本次交付；仓库回归对应 165 项。
- 部署测试 5 项通过。
- 发布前从精确 Git 暂存区导出的干净副本复跑：插件 165 项通过、0 跳过，部署 5 项通过。配套宿主补丁的两个测试文件另以 canonical runner 复跑 8 项通过；补丁在固定基线的独立 index 上应用成功，14 个目标文件逐字节匹配当前已验收宿主改动。
- 新增实际 GatewayRunner、正式 callback wrapper、SQLite authority、codec 和第二次准入测试，确认回复只发送一次。
- 实际 AIAgent 调用 loopback HTTP 模型：模型请求一次、回答发送一次，私人 memory canary 未进入模型请求。它是本地替身，不是云模型或真实 OneBot 验收。
- 变异验证把恢复结果改回新 source 对象后，完整模型回合重现 Unauthorized，模型请求为零，回归按预期失败。
- 覆盖身份／群／消息篡改、无 live permit、撤回、断线和 ambient A 下恢复 B。
- 宿主定向回归：10 项通过、1 项跳过；另 4 项 native ingress trust 子进程用例受测试夹具导入错误及 isolated runtime 准备超时阻断，未计作通过。没有完成宿主全套测试。
- 插件套件保留 SQLite ResourceWarning 和临时 fixture 的 logging error；不声称无警告。
- 真实网关重启后已观察到群内收到新消息、调用模型并发送回复，用户确认“成功了”。结论限于本次群文本回复。连续引用机器人文字的部分场景仍缺少引用内容，不能声称引用、工具、图像发送或所有隐私边界已全面验收。

测试命令：

```sh
cd "$COMPANION_SOURCE/plugins/hermes-napcat"
HERMES_SOURCE="$HERMES_SOURCE" hermes --run-module unittest discover -s tests -p 'test_*.py' -v
hermes --run-module unittest discover -s deploy -p 'test_*.py' -v
cd "$HERMES_SOURCE"
scripts/run_tests.sh tests/agent/test_tool_authority.py tests/gateway/test_napcat_public_admission.py
```

选择实际 managed Hermes 解释器，普通临时数据使用部署者许可的 scratch；不要把系统 Python 缺依赖当作业务回归失败。源码通过、文件落位、进程加载、OneBot 回执与用户客户端确认是不同层级。
