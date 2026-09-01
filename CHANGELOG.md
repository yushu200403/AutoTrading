# 变更记录

本文件记录对交易安全、配置契约与对外接口有影响的变更。

## 未发布

### 控制台访客界面

- 删除网页顶部的访客说明条；访客与登录用户继续查看相同的行情、账户、持仓、净值、模型输出和交易记录。
- 未登录时隐藏设置标签及设置内容，通过导航栏控制台登录入口验证密码；登录后显示设置并开放控制操作，退出或会话过期时自动关闭设置并返回数据页。
- 所有控制与修改接口继续由服务端 Session、CSRF 和密码认证保护，前端隐藏不替代服务端权限校验。
- 恢复可选的访客只读认证配置；显式禁止访客读取时显示顶部登录提示并隐藏数据区，右上角运行状态同时标注模拟盘或实盘。

## v3.1 - 2026-08-31

### 模型循环持续运行

- 删除日亏损、峰值回撤、24 小时模型 token 预算和连续失败次数熔断；普通周期失败后仍按 `TRADING_INTERVAL_MINUTES` 继续调用模型。
- 删除失败指数退避，模型调用间隔不再因连续失败扩大；单次请求的超时、重试和协议校验仍然保留。
- 保留 `PENDING`、`UNKNOWN`、`PARTIAL`、`CRITICAL` 待对账门禁，订单执行状态无法确认时仍会立即停止自动循环，避免重复交易。
- 移除 `AI_MAX_DAILY_TOKENS`、`MAX_CONSECUTIVE_CYCLE_FAILURES`、`RISK_MAX_DAILY_LOSS_PERCENT`、`RISK_MAX_DRAWDOWN_PERCENT` 配置。

### 访客只读访问

- 余额、持仓、净值、交易记录、模型推理、自定义指令与 AI 记忆等只读接口改为无需密码即可访问，并移除 `CONSOLE_READONLY_AUTH_ENABLED` 配置。
- 设置面板对访客完整展示数据，但启动、停止、单次运行、一键全平、实盘切换和保存指令等控件保持禁用；输入正确控制台密码后才恢复操作。
- 所有控制与修改接口继续由服务端 Session、CSRF 和密码认证保护，访客直接调用接口仍会被拒绝。

## 2026-08-14 安全审计整改

### 资金安全

- 待对账门禁从只检查 `PENDING` 扩展到 `PENDING`、`UNKNOWN`、`PARTIAL`、`CRITICAL`。此前仓位裸奔或订单结果不明时，下一周期仍会照常开始决策。触发时通过 `halt_required` 暂停后台循环并要求人工核对。
- 新增账户级熔断：24 小时净值回落上限、相对峰值回撤上限、24 小时模型 token 预算。任一触发即暂停交易循环。
- 保护单新增最小间距校验（`RISK_MIN_PROTECTIVE_DISTANCE_PERCENT`）。此前只校验方向，止损可贴在现价旁通过风控，导致开仓即被扫损。
- 启用强制保护单时，会让持仓失去止损保护的撤单批次被整体拒绝；单独撤止盈不受限。按 ID 撤单无法在风控层判断类型，改由周期末的失保核对告警兜底。
- 交易所杠杆信息获取失败时不再降级为 1 倍，而是标记为未知并拒绝开仓，避免杠杆上限校验静默失效。
- 撤单遇到网络异常时返回 `UNKNOWN` 而非改用 algoId 重试。此前网络超时会被误判为订单类型不匹配，可能撤销无关订单。
- `_child_order_id` 在缺少父级订单 ID 时直接报错，避免所有回补单共用同一固定 ID 而被幂等检查误命中。

### 可用性

- `KLINE_DISPLAY_LIMIT` 默认值由 100 下调为 30。原默认配置下 K 线段约 15.7 万字符，叠加重试后超出常用模型的上下文窗口。启动时按 `AI_MAX_PROMPT_CHARS` 做静态估算校验。
- 模型响应 token 上限改为可配置（`AI_MAX_RESPONSE_TOKENS`，默认 8000），并与 `AI_MAX_MEMORY_CHARS` 交叉校验。此前硬编码 2000，与默认 20000 字符的记忆上限互相矛盾，模型写长记忆时会被截断并导致周期反复失败。`AI_MAX_MEMORY_CHARS` 默认值相应调整为 4000。
- 后台循环不再因单个周期异常而永久退出，改为记录并指数退避后继续；连续失败达到 `MAX_CONSECUTIVE_CYCLE_FAILURES` 时暂停并记录原因。
- `stop()` 返回线程是否已在等待窗口内结束，控制台据此给出准确提示。
- Gunicorn `--timeout` 由 180 秒调整为 600 秒，`stop_grace_period` 由 45 秒调整为 150 秒。一次同步周期可能耗时数分钟，worker 被杀会在下单与状态回写之间中断并留下待对账记录。
- `synchronize_time()` 修正为写入 ccxt 使用的 `options['timeDifference']`。此前赋值给不存在的属性，函数完全无效。

### 安全

- 只读接口默认要求会话认证（`CONSOLE_READONLY_AUTH_ENABLED`）。此前余额、持仓、模型完整推理链、记忆白板与自定义指令均可匿名读取。
- 新增无鉴权的 `/healthz` 存活探针，容器健康检查不再依赖 `/api/status`。
- `CONSOLE_PASSWORD` 默认值由 `admin` 改为空并强制校验，任何环境都无法以默认弱口令启动。
- `DevelopmentConfig.DEBUG` 不再硬编码覆盖 `FLASK_DEBUG`；生产环境 `SESSION_COOKIE_SECURE` 默认为 true。
- 新增内容安全策略与 1 MB 请求体上限。为配合不含 `unsafe-inline` 的策略，内联 `onclick` 改为事件监听与事件委托。
- Chart.js v4.5.0 改为随仓库分发，移除运行时 CDN 依赖。

### 数据与可追溯性

- 新增迁移 `20260814_03`：`trading_cycle.tokens_used` 与 `started_at` 索引、`trade_decision.execution_status` 索引、`equity_snapshot` 的 `(trading_mode, timestamp)` 复合索引、`paper_account` 单行约束，并删除无任何读写方的遗留表 `pending_order`。
- `ToolCall.raw_json` 改为保留模型原始输出。此前存的是参数规范化后重新序列化的结果，无法追溯模型实际写了什么。
- `/api/memory`、`/api/instructions`、`/api/equity-history` 的时间戳改用统一格式化函数。此前直接输出无时区值，浏览器按本地时区解读，东八区显示时间早 8 小时。
- 移除从未写入的 `market_snapshot.btc_dominance` 字段；`datetime.utcnow` 替换为等价的非废弃实现。

### 模型引导

- `count_usdt` 在提示词中明确标注为仓位名义价值并给出杠杆换算示例，此前表述为「USDT 金额」，易被理解为保证金。
- 移除「收益率持续不为正将被解雇」等措辞，改为按风险调整后收益评价，避免系统性推高风险偏好。
- 提示词补充最小保护单间距与撤单纪律说明，减少模型反复触发风控拒绝。

### 其他

- `paper_broker` 的 `fetch_balance` 由提交改为刷新，不再顺带提交调用方未完成的事务；`fetch_positions` 与 `get_position_size` 改为持锁单次查询，消除双重查询竞态。
- 低价币价格按数量级选择小数位，此前固定两位会把低价币显示为 `$0.00`。
- 账户净值解读逻辑合并为单一实现，消除路由层与数据层的重复。
- 新增 `tests/test_executor.py`；补充熔断、撤单失保、工具分派、协议拒绝路径与循环暂停语义的测试。测试数量由 82 增至 115，分支覆盖率由 74.3% 提升至 77.0%。
