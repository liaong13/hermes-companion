# NapCat 纯本地上下文增量

本阶段对应 [life-memory 0.2.0](../../plugins/hermes-life-memory/README.md)、NapCat `0.4.0` 与 social `0.1.2`。源码补丁为 [napcat-local-memory-context.patch](napcat-local-memory-context.patch)，新增 `TurnRunner.public_memory_context_contract = 1`。它是在现有[公共群回合](napcat-public-turn.md)和[公开交流风格](napcat-social-participation.md)接线上的增量，不替代它们。

核对宿主基线为 `5894e750c3dba980d43b25aa29e3958149ba2c61` 加已有本地接线差异。其他版本须先检查实际目标源码与接口；未修改或部署真实宿主。

## 实际边界

当 NapCat 显式配置 `platforms.napcat.extra.local_conversation_context=true` 时，通道为当前精确 source 提供 `public_memory_history_policy`。宿主在构造 Agent 和载入历史前复验：

- 返回 `local_events / contract 1` 的公共群回合创建新 Agent，历史参数为空；缓存 Agent、旧 transcript、旧压缩结果不自动重放。
- 本轮记忆由通道临时上下文提供，来自当前成员、当前群且来源／依赖仍有效的本地视图。
- 模型或正文标志不能签发该策略；source 过期或策略不支持时拒绝。
- 未配置本地上下文的路径、私聊和其他通道保留原行为。

该开关作用于此适配器全部公共群，独立于 life-memory 的采集开关与群范围。记忆插件停用／卸载后仍不回落旧历史；没有本地记忆时只使用当轮合法输入及原有公开表达。关闭通道策略属于独立配置变更，旧历史仍在，应先审阅目标会话与历史迁移。

旧宿主缺少 marker 时，新通道拒绝开启该模式；只修改配置不会获得合同。原始历史数据库、已经发出的 QQ 消息、模型服务端记录和外部备份不会被本补丁删除。新建 Agent 存在构造开销，本轮未测量真实模型的费用、缓存收益和延迟。

## 在一次性源码覆盖层复验

使用已准备完整 Hermes 依赖及 pytest 的实际解释器，指定可写 `TMPDIR`：

```sh
export HERMES_SOURCE=/path/to/hermes-agent
export HERMES_PYTHON=/path/to/actual/hermes/python
export TMPDIR=/path/to/reviewed/writable/scratch
"$HERMES_PYTHON" integration/hermes-host/verify_local_memory_offline.py \
  --host-source "$HERMES_SOURCE" --python "$HERMES_PYTHON"
```

工具只复制四个 gateway 源文件到临时目录，先 `git apply --check` 再应用增量；其余宿主模块及测试 helper 从指定源码只读导入。覆盖层和路径扩展仅供测试，不进入产品补丁。不会复制 profile、认证或历史，不安装依赖、不调用模型、不连接真实 QQ，不改写目标宿主。

复验包括实际修补后的新 Agent／历史加载方法，以及匹配的真实 NapCat 类型、worker 探测和捕获 OneBot 的人物接线。当前结果见[阶段验收](../../docs/纯本地记忆与交流续接.md)；源码测试不等于网关加载或真实客户端验收。
