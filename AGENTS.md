# AGENTS.md

本文件面向两类读者：

- 人类维护者
- 后续参与本项目的 Codex / AI 代理

目标不是介绍项目功能，而是固定“后续开发时不能踩的坑”和“默认必须遵守的实现口径”。

## 1. 项目目标与高风险区域

本项目的核心不是通用行情站，而是围绕 `C5 + Steam` 的挂刀 / 补仓 / 底仓执行。

默认理解：

- `C5` 负责库存、补仓、部分成交执行
- `Steam` 负责挂刀卖出、实时挂价墙价格、挂单状态确认
- 执行器的真实职责是把“扫描候选”推进成“可执行动作”，并安全地维护状态闭环

高风险改动区：

- `src/cs2_assistant/services/executor_engine.py`
- `src/cs2_assistant/services/executor_buy.py`
- `src/cs2_assistant/services/pricing.py`
- `src/cs2_assistant/clients/steam_market.py`
- `data/strategy_config.json`
- `config/accounts.json`
- 数据库中的 `inventory_pool` / `inventory_assets` / `pool_operations`

硬规则：

- 任何会触发真实上架、真实补仓、Steam Guard 确认、卖出推进的改动，必须先理解当前状态机再改。
- 不允许为了“先跑起来”绕过状态推进逻辑。
- 不允许在不理解真实执行口径时，把扫描层逻辑直接挪进执行层。

## 2. Steam 执行口径

### 2.1 实时价格来源

真实执行时，Steam 实时价格来源只认 `orderbook`。

当前约定：

- 使用 Steam `orderbook`
- 只读取 `rgCompactSellOrders`
- 只看卖家挂单墙，不参考买家数据

不要做的事：

- 不要重新引入 `item_nameid`
- 不要重新走 `itemordershistogram`
- 不要把第三方聚合价格当作真实执行价
- 不要写死某个饰品名；`market_hash_name` 必须是动态传入参数

### 2.2 失败处理

真实执行时：

- 取不到 Steam 实时价格，可以跳过
- 不能 fallback 到第三方聚合价继续执行

`dry-run` 时：

- 允许使用扫描阶段已有价格做兜底，仅用于模拟流程

### 2.3 Cookie 与 relogin 判断

`/market/mylistings` 超时，默认按网络 / Steam 波动处理，不默认认定为 cookie 失效。

只有认证失败才应触发自动 relogin 判断，例如：

- `400`
- `401`

不要把以下情况直接当作 cookie 失效：

- `ReadTimeout`
- `ConnectTimeout`
- `SSLEOF`
- 其他短时网络波动

## 3. 执行器与状态机规则

### 3.1 三层状态职责

这三层状态不要混用：

- `inventory_pool`
  负责品类级底仓状态
- `inventory_assets`
  负责单资产级别可交易/已卖/已挂单状态
- `pool_operations`
  负责动作流水与推进链路

后续改动时，先判断你要改的是：

- 品类状态
- 单件资产状态
- 动作流水状态

### 3.2 候选不等于可执行

`scan_strategies()` 给出的候选，来自 C5 聚合库存视角。

但真实挂刀时还必须再过一层过滤：

- 当前 executor 账号
- 当前账号本地可交易资产

因此：

- “有候选”不代表“当前账号能上架”
- 不能把跨账号候选直接当成当前 Steam 账号可执行动作

### 3.3 挂刀闭环优先级

上一轮挂刀循环未闭环时：

- 先推进旧状态
- 不开新挂单

不要破坏这条链路：

- `sell_on_steam.listed`
- `sell_on_steam.sold`
- `rebuy_on_c5.pending`
- `rebuy_on_c5.completed` / `failed`

挂单消失不应被简单视为异常；它可能意味着：

- 已卖出
- 状态待推进
- 当轮 `mylistings` 暂时不可读

因此推进逻辑必须结合：

- Steam 活跃挂单读取结果
- 本地 `pool_operations`
- 当前等待时间窗口

### 3.4 日志口径

日志必须对用户可读，不允许只有内部术语。

至少保证：

- 上架时显示 `Steam挂价`
- 上架时显示 `预计到手`
- 卖出推进时显示 `Steam售价`
- 卖出推进时显示 `税后到手`
- 补仓时显示实际 `C5` 买入价

如果改动了金额口径，必须同步检查上架日志、卖出日志、补仓日志是否仍一致。

## 4. 配置与默认值

### 4.1 配置来源

运行时策略配置源是：

- `data/strategy_config.json`

不是硬编码常量。

开发时不要假设用户使用默认值；先看真实配置文件。

### 4.2 核心定价旋钮

当前影响挂刀定价的主要配置是：

- `listingWallMinCount`
- `listingPriceOffset`
- `caseListingPriceOffset`

修改定价逻辑时，优先确认是：

- 统一规则变更
- 箱子专用规则变更
- 仅某条执行路径的局部修正

### 4.3 箱子模式当前约定

当前箱子模式已经明确，不要擅自改口径：

- `listingWallMinCount = 20`
- 按卖家累计墙取价
- 再应用 `caseListingPriceOffset = -0.01`

在当前实现里，这等价于：

- 取卖家累计墙价格
- 最终挂价表现为“累计墙价格 + 0.01”

禁止事项：

- 不要擅自改成“第一档最低卖家价 + 0.01”
- 不要擅自把箱子逻辑改成和非箱子统一
- 除非用户再次明确确认，否则不要改这条约定

### 4.4 账号与敏感数据

账号数据在：

- `config/accounts.json`

敏感字段受本地环境和加密影响，尤其注意：

- `CS2_MASTER_KEY`
- 当前 `account use` 切换结果
- `trade_url`
- `cookies`

改账户逻辑前必须先确认：

- 当前执行账号是谁
- `trade_url` 是否和当前账号匹配
- 本地加密/解密链路是否受影响

## 5. 开发与测试守则

### 5.1 最低测试要求

修改以下区域后，至少运行：

- `tests/test_executor_engine.py`

如果涉及 CLI 或配置展示，还要看：

- `tests/test_cli.py`

如果涉及真实交易口径：

- 必须补最小回归测试
- 不允许只改日志、不补测试

### 5.2 可接受的保护性设计

可以接受：

- 网络超时保护
- GET 请求短重试
- 跳过执行而不误成交
- 延迟推进状态而不误判卖出

不可以接受：

- 为了“稳定” silently fallback 到非 Steam 实时价
- 为了避免超时直接跳过状态机
- 用粗暴重置数据库状态掩盖推进问题

### 5.3 新接口接入原则

新增接口时优先遵守：

- 可跳过
- 不误成交
- 不误推进状态
- 日志可解释
- 有最小测试覆盖

如果一个新接口更“方便”，但会削弱真实执行边界，默认不要接入。

## 6. Repo Facts 与 Policy 的区分

后续开发时，必须区分三类信息：

### 6.1 Discoverable repo facts

可以从仓库直接确认的事实，例如：

- 当前取价实现在哪个文件
- 当前状态机怎么推进
- 当前配置项叫什么

这类问题先读代码，不要问用户。

### 6.2 Current agreed trading policy

当前已经确认、必须遵守的交易口径，例如：

- 真实执行只认 Steam 实时价格
- 使用 `orderbook`
- 只看 `rgCompactSellOrders`
- 箱子按 `listingWallMinCount=20` 的卖家累计墙，再 `+0.01`

这类口径不能擅自更改。

### 6.3 Changes requiring explicit reconfirmation

以下改动必须重新得到用户明确确认：

- 箱子模式定价规则变化
- 从 Steam 实时价退回到第三方聚合价
- 自动补仓边界变化
- 状态机推进语义变化
- 账号隔离与 trade URL 匹配规则变化

没有明确确认时，默认保持现有口径。
