# 开发约定

本文件面向在本仓库中协作的开发者与 AI 助手，记录必须遵守的约束与常用命令。

## 语言与风格

- 代码注释、日志、异常消息、界面文案、文档一律使用中文。
- 注释说明「为什么这样做」，不复述代码在做什么，也不记录修改过程（例如「此处改为……」）。变更记录写入 `CHANGELOG.md`。
- 遵循模块化、清晰、简洁、健壮、最小惊讶等原则；失败要尽早且明确，禁止静默兜底。

## 分层与依赖方向

```
routes → service → engine → {data_engine, executor, risk, ai_agent}
executor → broker 协议（binance_client / paper_broker 同构实现）
```

- `executor` 只依赖 Broker 协议，不区分模拟与实盘，两类 Broker 必须保持接口同构。
- `risk` 负责校验模型输出的工具批次；周期级门禁（待对账检查、账户熔断）属于 `engine`。
- `xml_parser` 是模型输出的唯一信任边界，只做白名单校验，不猜测模型意图。

## 资金安全红线

改动下列内容时必须同步补充回归测试：

- 任何写入交易所或模拟账本的路径。
- `ExecutionResult.status` 的取值与语义。
- `TradingEngine.RECONCILIATION_STATUSES`：出现其中任一状态时必须阻塞后续周期并暂停循环，不得放宽。
- `risk.py` 的任何上限判断。

其他约束：

- 结果未知（网络超时等）必须返回 `UNKNOWN` 并禁止自动重试，不得猜测成败。
- 缺失的外部状态（如杠杆倍数）不得用默认值代替，否则风控会静默失效。
- 客户端订单 ID 必须可复现，幂等重放依赖它命中既有订单。
- 新增配置项须在 `Config.validate` 中校验，并同步 `.env.example`、`docker-compose.yml`、README。

## 数据库

- 模型变更必须配套 Alembic 迁移，且 `downgrade` 可还原。
- 金额与数量统一使用 `Numeric(28, 12)`，禁止用浮点存账。
- 早期表使用无时区列存放 UTC 值，请使用 `naive_utc_now`；新表使用 `DateTime(timezone=True)` 与 `utc_now`。
- 对外输出时间戳统一走 `routes._format_timestamp`，避免前端按本地时区误读。

## 常用命令

```powershell
# 安装依赖
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt

# 测试与覆盖率门槛
.\.venv\Scripts\python.exe -m pytest --cov=app --cov=config --cov-report=term-missing --cov-fail-under=70

# 静态检查
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\python.exe -m pip check
node --check app/static/js/app.js

# 数据库迁移
$env:FLASK_APP="app:create_app"
.\.venv\Scripts\flask.exe db upgrade
```

测试全部离线运行，使用内存 SQLite 与假交易所，不得访问真实账户或发送订单。

## 前端

- 风格为黑白新粗野主义：黑边框、无圆角、高对比度、紧凑排列。
- CSS 按职责拆分到 `app/static/css/` 下的多个文件，避免单文件膨胀。
- 内容安全策略不允许内联脚本，请使用 `addEventListener` 或 `data-*` 属性配合事件委托，不要写 `onclick`。
- 所有插入 DOM 的模型输出与用户输入必须经过 `escapeHtml`。
- 前端依赖随仓库分发于 `app/static/js/vendor/`，不引入运行时 CDN 依赖。
- 提示与确认使用页面内自定义组件，不调用 `alert`、`confirm`。
