# OpenNOF1

OpenNOF1 是一个面向可行性验证的 AI 交易工作流：系统持续读取 Binance USDT-M 永续合约的真实公共行情，聚合技术指标、账户状态和 AI 记忆，再通过严格的 XML + JSON 工具协议生成并执行交易决策。

项目默认运行在持久化模拟交易模式。模拟模式与实盘共用行情、风控和订单编排，仅替换交易执行与账户账本，不会向 Binance 提交订单。

## 当前能力

- 真实行情：1m、15m、1h、4h、1d K 线、盘口、资金费率、多空比和市场宽度。
- 技术分析：EMA、MACD、RSI、VWAP、ATR、布林带、支撑阻力和局部峰谷背离。
- 模拟交易：持久化钱包、双向 LONG/SHORT 仓位、手续费、杠杆、保证金、已实现/未实现盈亏。
- 订单生命周期：市价开平仓、部分平仓、止损、止盈、撤单、保护单数量同步和客户端订单 ID 幂等。
- 实盘执行：CCXT 精度、客户端订单 ID 对账、未知结果隔离、保护单失败回补和算法订单兼容层。
- AI 协议：整批严格解析、主备提供商故障转移、响应完整性检查和执行前风控。
- 持续调度：普通周期失败后仍按固定配置间隔继续调用模型，保持对市场和持仓变化的响应。
- 可追溯性：市场快照、交易意图（保留模型原始输出）、执行结果、周期状态、token 用量和分模式净值历史。
- 控制台：访客可只读查看全部业务数据；控制与修改接口使用服务端 Session、CSRF 和密码认证，并提供 CSP 与防嵌套等响应头。
- 回归测试：离线假交易所与内存数据库，不访问真实账户；启用全项目分支覆盖率门槛。

## 交易模式

### 模拟交易（默认）

```env
TRADING_MODE=paper
PAPER_INITIAL_BALANCE_USDT=10000
PAPER_TAKER_FEE_RATE=0.0004
PAPER_DEFAULT_LEVERAGE=1
```

模拟成交使用 Binance 实时公共行情。余额、仓位、条件单和成交写入数据库，进程重启后仍可恢复。API Key 不是模拟模式的必要条件。

### 实盘交易

实盘必须同时满足以下配置：

```env
TRADING_MODE=live
LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_REAL_ORDERS
BINANCE_API_KEY=你的_API_Key
BINANCE_API_SECRET=你的_API_Secret
```

账户需启用 USDT-M 合约和双向持仓模式。建议先在 `paper` 或 Binance 测试网中完成完整回归，确认交易对、杠杆、权限和保护单行为后再启用实盘。

运行中的后台循环不允许切换模式；必须先停止服务，防止一个周期跨越两个账户环境。

## 快速开始

### 1. 准备配置

复制 `.env.example` 为 `.env`，至少填写 AI 主提供商和控制台安全配置：

```env
AI_1_API_KEY=你的模型密钥
AI_1_BASE_URL=https://api.deepseek.com/v1
AI_1_MODEL=deepseek-chat

FLASK_SECRET_KEY=请替换为高强度随机值
CONSOLE_PASSWORD=请替换为强密码
CONSOLE_AUTH_ENABLED=true
TRADING_MODE=paper
```

完整配置及默认值见 [`.env.example`](./.env.example)。生产环境会拒绝默认 Flask 密钥和默认控制台密码。

控制台的余额、持仓、净值、交易记录、模型推理、自定义指令和 AI 记忆均允许访客直接查看。启动、停止、单次运行、一键全平、实盘切换和保存指令等状态变更必须先输入控制台密码；服务端仍会校验会话与 CSRF，前端按钮禁用不是唯一安全边界。若将站点公开到互联网，请确认可以接受上述业务数据公开。

### 2. 本地运行

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:FLASK_APP="app:create_app"
.\.venv\Scripts\flask.exe db upgrade
.\.venv\Scripts\python.exe run.py
```

访问 <http://127.0.0.1:5000>。

### 3. Docker Compose

```bash
docker compose up -d --build
```

容器启动时会先执行数据库迁移，再使用单进程、多线程 Gunicorn 启动应用，避免创建多个交易调度器。默认只绑定 `127.0.0.1:5000`。

当前 Compose 会把 `POSTGRES_PASSWORD` 嵌入数据库连接 URL，因此密码仅应包含字母、数字、下划线、点号和短横线；如需使用其他字符，应改为预先编码且与数据库凭证一致的独立 `DATABASE_URL` 管理方案。

```bash
docker compose down
```

不要把删除数据库卷作为常规停止步骤；卷中包含模拟账本、AI 记忆和交易审计记录。

## 数据库迁移

新数据库直接执行：

```powershell
$env:FLASK_APP="app:create_app"
.\.venv\Scripts\flask.exe db upgrade
```

从未使用迁移管理的旧版数据库升级时：

1. 先备份数据库。
2. 将现有旧表标记为旧版基线：

```powershell
.\.venv\Scripts\flask.exe db stamp 20260811_01
```

3. 应用模拟账本与安全升级：

```powershell
.\.venv\Scripts\flask.exe db upgrade
```

升级前已有的交易决策和净值快照会标记为 `live`，因为旧版引擎默认直接连接实盘执行器；新增记录始终由当前周期显式写入 `paper` 或 `live`。

详细说明见 [`migrations/README.md`](./migrations/README.md)。

## 风控配置

风控边界全部由环境变量控制，不固化为策略常量：

| 配置 | 默认值 | 作用 |
|---|---:|---|
| `TRADING_SYMBOLS` | BTC、ETH、BNB、SOL、DOGE | 允许交易的交易对 |
| `RISK_MAX_LEVERAGE` | 20 | AI 可请求的最大杠杆 |
| `RISK_MAX_SINGLE_TRADE_USDT` | 1000 | 单次开仓名义价值上限 |
| `RISK_MAX_POSITION_NOTIONAL_USDT` | 3000 | 单方向仓位名义价值上限 |
| `RISK_MAX_TOTAL_NOTIONAL_USDT` | 5000 | 账户总名义敞口上限 |
| `RISK_MIN_FREE_BALANCE_USDT` | 0 | 下单后最低可用余额 |
| `RISK_REQUIRE_PROTECTIVE_ORDER` | true | 开仓是否必须携带止损或止盈 |
| `RISK_MIN_PROTECTIVE_DISTANCE_PERCENT` | 0.3 | 保护单触发价与现价的最小间距，0 表示关闭 |
| `AI_MAX_TOOL_CALLS` | 10 | 单周期最大工具调用数 |

单笔与仓位上限均按**名义价值**计算（名义价值 = 保证金 × 杠杆），工具参数 `count_usdt` 同样是名义价值而非保证金。

`RISK_REQUIRE_PROTECTIVE_ORDER` 启用时，会让持仓失去止损保护的撤单批次将被整体拒绝；需要调整止盈止损应使用 `modify_position` 而非先撤后建。单独撤销止盈单不受限制。

这些配置是工作流验证参数，不构成收益保证。交易所自身的最小名义价值、数量精度和杠杆能力仍以实时市场元数据为准。

### 提示词体积

`KLINE_DISPLAY_LIMIT` 直接决定提示词大小：每个交易对每周期输出 5 个时间周期的 K 线。默认 5 个交易对、每周期 30 根时约 4.9 万字符。启动时会按 `AI_MAX_PROMPT_CHARS` 做静态估算校验，超出即拒绝启动，避免运行期才发现超出模型上下文窗口。

`AI_MAX_MEMORY_CHARS` 与 `AI_MAX_RESPONSE_TOKENS` 存在交叉校验：记忆白板上限不得超过响应 token 预算所能容纳的字符数，否则模型写入长记忆时会被截断并导致周期反复失败。

## 回归测试与质量检查

安装开发依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

运行全量测试和分支覆盖率门槛：

```powershell
.\.venv\Scripts\python.exe -m pytest --cov=app --cov-report=term-missing --cov-fail-under=70
```

运行静态检查与依赖一致性检查：

```powershell
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\python.exe -m pip check
node --check app/static/js/app.js
```

前端依赖（Chart.js）随仓库分发于 `app/static/js/vendor/`，运行时不请求外部 CDN，因此内容安全策略中的 `script-src` 仅允许同源脚本。

测试使用内存 SQLite、假行情和假 Exchange，不需要联网，也不会向 Binance 发送订单。

## 关键执行保证

- 每个交易周期使用互斥锁和唯一 `cycle_id`。
- 工具批次只在模型响应和风控全部通过后执行一次。
- 每项外部动作执行前先保存 `PENDING` 决策意图，执行后再更新结果。
- 工具执行失败不会触发模型重发已执行交易。
- 前序失败后，后续开仓、加仓、修改杠杆等增险动作会被跳过；减险和记忆更新仍可继续。
- 市价单超时后按客户端订单 ID 对账；无法确认时返回 `UNKNOWN`，禁止自动重试。
- 撤单遇到网络异常同样返回 `UNKNOWN`，不会改用另一套订单 ID 空间重试。
- 新增仓位的保护单创建失败时自动反向回补；补偿失败标记为 `CRITICAL`。
- 存在 `PENDING`、`UNKNOWN`、`PARTIAL` 或 `CRITICAL` 状态的决策时，后续周期一律拒绝启动，并暂停交易循环等待人工核对。
- 交易所杠杆信息无法确认时不会按 1 倍处理，而是直接拒绝开仓。
- 单个周期内部异常或失败结果不会终止后台循环，也不会扩大调用间隔；下一周期仍按 `TRADING_INTERVAL_MINUTES` 调度。
- 自动循环只会因用户主动停止或存在 `PENDING`、`UNKNOWN`、`PARTIAL`、`CRITICAL` 待对账状态而结束，不再因亏损、回撤、token 用量或连续失败自动暂停。
- 模拟模式每周期用最新真实价格触发止损和止盈。
- 每个周期结束后核对持仓保护单，发现失保持仓会记录告警。

## 项目结构

```text
AutoTrading/
├── app/
│   ├── bot/
│   │   ├── ai_agent.py       # AI 主备提供商与严格响应协议
│   │   ├── binance_client.py # Binance 行情与实盘 Broker
│   │   ├── data_engine.py    # 行情、账户和提示词聚合
│   │   ├── engine.py         # 交易周期协调、待对账门禁与审计
│   │   ├── exceptions.py     # 领域异常与失败语义
│   │   ├── executor.py       # Broker 无关的订单编排
│   │   ├── indicators.py     # 技术指标与提示词格式化
│   │   ├── macro_data.py     # 宏观指标表述
│   │   ├── paper_broker.py   # 持久化模拟交易 Broker
│   │   ├── prompts.py        # 系统提示词与工具定义
│   │   ├── risk.py           # 配置化执行前风控
│   │   ├── service.py        # 后台循环生命周期与退避
│   │   ├── tz_utils.py       # 时区与时间戳工具
│   │   └── xml_parser.py     # 工具协议解析与校验
│   ├── models.py             # 业务与模拟账本模型
│   ├── routes.py             # 仪表盘 API、认证与 CSRF
│   ├── static/js/vendor/     # 随仓库分发的前端依赖
│   └── runtime.py            # 唯一应用与交易服务装配
├── migrations/               # Flask-Migrate 迁移
├── tests/                    # 离线回归测试
├── config.py                 # 环境配置与启动校验
├── wsgi.py                   # 生产 WSGI 入口
├── run.py                    # 本地开发入口
└── docker-compose.yml
```

## 已知边界

- 模拟成交按最新行情价执行，不模拟盘口冲击、滑点、资金费扣划和强平撮合。模拟盘的浮亏不会被强制平仓，因此其收益曲线相对实盘偏乐观，切换实盘前应据此打折评估。
- 模拟条件单只在每个交易周期开始时按最新价检查，周期内的价格穿刺不可见；实盘止损由交易所实时执行。
- 当前运行模型是单应用进程；横向扩容前必须引入跨进程锁或独立调度服务。模拟账本的行级锁在 SQLite 上不生效，多进程部署必须改用 PostgreSQL。
- 外部订单后若审计回写中断，系统会因遗留待对账意图失败关闭；恢复交易前必须先与执行端人工对账并修正决策状态。事务发件箱自动对账与恢复流程尚待下一迭代。
- 按订单 ID 撤单时，风控层无法判断该订单是止损还是止盈，因此该路径依赖周期末的失保核对告警而非事前拦截。
- 行情聚合为串行请求（约 45 次往返），决策所依据的行情可能有数十秒延迟；缩短 `TRADING_INTERVAL_MINUTES` 前应先评估该延迟。
- 外部行情、AI 或数据库不可用时，周期会失败并保留状态，不会静默继续开仓。
- `ruff` 配置未启用 E501，因此 `line-length` 声明不参与检查，仓库中存在少量超长行。

## 免责声明

本项目用于研究 AI 嵌入交易工作流的可行性，不构成投资建议。加密资产和杠杆交易风险极高，使用者应自行承担模拟偏差、模型错误、软件故障和实盘损失。

## 许可证

Apache 2.0
