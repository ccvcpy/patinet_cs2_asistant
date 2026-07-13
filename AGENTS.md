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

真实上架定价时，Steam 实时价格来源只认 `orderbook`。

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

真实上架时：

- 取不到 Steam 实时价格，可以跳过
- 不能 fallback 到第三方聚合价继续执行

真实补仓时：

- 不需要重新读取 Steam 实时挂价
- 不需要按补仓时的实时 Steam 挂价重新计算挂刀比例
- 只按对应卖出流水记录的 `steamListPrice * steamNetFactor` 作为 Steam 已卖出税后到手价
- 补仓判断口径是 `C5补仓价 / Steam已卖出税后到手价 <= guadaoMaxListingRatio`

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
- 默认不开新挂单

箱子有一个明确例外：

- 仅针对箱子，`sell_on_steam.listed` / `rebuy_on_c5.pending` 低于 `caseMaxOpenGuadaoCount` 时，可以继续开启下一轮挂刀
- 默认上限是 `caseMaxOpenGuadaoCount = 100`
- 达到上限后必须暂停开启新一轮并提醒用户，但执行器不能停止；后续每轮仍要继续扫描、推进 Steam 卖出和 C5 补仓
- 这个例外不适用于非箱子，也不适用于 `listing_pending`、`rebuy_failed` 等需要人工确认或失败处理的状态

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
- 补仓等待时显示实际 `C5` 价、最高补仓价、`Steam卖出税后到手`、补仓比例
- 补仓成功时显示实际 `C5` 买入价

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
- `caseMaxOpenGuadaoCount`
- `guadaoItemScope`

修改定价逻辑时，优先确认是：

- 统一规则变更
- 箱子专用规则变更
- 仅某条执行路径的局部修正

`guadaoItemScope` 控制挂刀品类范围：

- `case_only`：只允许箱子进入挂刀候选
- `non_case_only`：只允许非箱子进入挂刀候选

当前不再支持 `all`。旧配置里如果仍写着 `all`，应按 `case_only` 处理。

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

调用 C5 API 时必须显式支持压缩传输，例如发送 `Accept-Encoding: gzip, deflate, br` 或使用客户端默认压缩能力；新增 C5 请求封装时要确认响应解压链路可用，避免库存、行情、成交记录等高频接口用未压缩明文放大网络开销。

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

- 真实上架定价只认 Steam 实时价格
- 使用 `orderbook`
- 只看 `rgCompactSellOrders`
- 箱子按 `listingWallMinCount=20` 的卖家累计墙，再 `+0.01`
- 真实补仓只按对应卖出流水里的 Steam 税后到手价判断，不读取补仓时 Steam 实时挂价

这类口径不能擅自更改。

### 6.3 Changes requiring explicit reconfirmation

以下改动必须重新得到用户明确确认：

- 箱子模式定价规则变化
- 从 Steam 实时价退回到第三方聚合价
- 自动补仓边界变化
- 状态机推进语义变化
- 账号隔离与 trade URL 匹配规则变化

没有明确确认时，默认保持现有口径。

## 7. 历史会话复盘固定规则

本节来自 `dev_re_temp`、`dev_re`、`重构`、`开发 Vue TypeScript 前端`、`构建箱子挂刀监控系统` 等会话的复盘。后续代理必须把这些当作项目规则，而不是临时聊天结论。

### 7.1 先确认用户当前真正要做什么

如果用户要求的是：

- 总结教训
- 修改文档
- 解释代码
- 只讨论方案

就不要顺手改业务代码、前端代码或 CLI 输出。

如果用户明确说“不要改代码”“只整理教训”“只解释”，本轮只允许读代码、读日志、改指定文档，不能把发现的问题直接实现掉。

### 7.2 历史上下文必须真实导入

用户提到旧线程、截图里的线程名、`dev` / `dev_re` / `dev_re_temp` 时，不要只凭当前窗口记忆总结。

默认流程：

- 先读 `AGENTS.md`、`README.md`、核心 `docs/*.md`
- 再查本地 session log 或线程摘要
- 最后再改当前文档

如果涉及功能“丢失”，先确认当前运行目录、真实分支、Codex worktree 和用户 Desktop 仓库是否一致。不要在 Codex worktree 里修完，却让用户在 `C:\Users\dmm\Desktop\patinet_cs2_asistant` 继续失败。

### 7.3 用户纠正时不能只附和

用户纠正设计或口径时，必须回到代码、数据和状态机验证。

处理方式：

- 如果用户说法和当前实现一致，明确说明证据来自哪里
- 如果用户说法会引入风险，直接指出风险和可替代方案
- 如果之前是代理理解错了，要改文档或方案中的误导表达

不要用“你说得对”替代技术判断。

### 7.4 动态挂刀比例的当前口径

`guadaoMaxListingRatio` 是候选池硬上限，不是后续再动态调节的全局阈值。

当前挂刀开新单口径：

- 先按 `guadaoMaxListingRatio` 把明显不可导的候选排除
- 进入候选池后，每个饰品使用自己本轮实时 Steam orderbook 算出的 `listing_ratio`
- 本轮新开挂刀按每个饰品自己的实时 `listing_ratio` 从低到高执行
- 不做盘口比例分桶
- 不按“某一档有多少个”来吃深度
- 不再额外套一层“低比例不够再放宽到高比例”的动态阈值

每笔新开挂刀必须冻结当时口径：

- `listingRatioAtOpen`
- `maxRebuyRatioAtOpen`
- `guadaoMaxListingRatioAtOpen`
- `steamNetFactorAtOpen`

后续补仓优先使用该流水自己的 `maxRebuyRatioAtOpen` 和 `steamNetFactorAtOpen`。老流水没有冻结字段时，才兼容回退到当前配置。

C5 补仓订单最终交付失败后创建替换补仓时：

- 禁止再按当前 C5 实时价格强制追价。
- 替换单最高补仓价冻结为原失败补仓单的 `actual_price`，缺失时回退 `expected_price`。
- 替换单仍必须使用本单冻结的 `maxRebuyRatioAtOpen`；它是硬上限内的本单动态挂刀比例，不是当前全局 `guadaoMaxListingRatio`。
- 原失败单价格上限和本单动态比例上限必须同时满足；任一不满足都保持 `pending`，等待后续价格回落。
- 历史遗留的 `forceRebuyReplacement=true` 替换单执行时要按原 `expected_price` 安全迁移，不能继续强制追当前价。

### 7.4.1 profitTrade / notify 做T收益公式

`profitTrade` 和 `notify t-profit` 的收益率必须使用同一套做T公式结构；其中 Profit Trade 使用自己独立配置的 `profitTrade.balanceDiscount` 作为余额折扣：

- 面折比 = `C5挂价 / Steam买入价`
- C5预计到手折比 = `面折比 * 0.99`
- ROI / transfer_real_ratio = `C5预计到手折比 - profitTrade.balanceDiscount`
- 真实成本金额 = `Steam买入价 * profitTrade.balanceDiscount`
- 预计收益金额 = `C5挂价 * 0.99 - 真实成本金额`

不要再用 `expected_profit / steam_real_cost` 当做T ROI；它会比 notify 口径偏高。不要把 `common.balanceDiscount` 当作 Profit Trade 成本比例，也不要让 Profit Trade 自动跟随挂刀执行器的 `guadaoBalance.guadaoMaxListingRatio`。这三个配置项彼此独立；Profit Trade 只认 `profitTrade.balanceDiscount`，除非用户再次明确要求变更口径。

做T买 B 的 Steam 账号选择规则：

- 优先用 A 资产所属 Steam 账号买 B。
- 如果该账号 Steam 钱包可用余额不足，再从其他本地 Steam 账号中选择“可用余额足够且余额最小”的账号。
- 判断余额是否足够时按 Steam 实际付款价判断，不按折扣后真实成本判断。
- A 卖 C5 不要求和买 B 是同一个 Steam 账号。

做T `createbuyorder` 的失败重试规则是硬约束：

- `STEAM_BUY_LISTING_RETRY_ATTEMPTS = 3` 表示最多三次求购尝试；除非用户再次明确修改，否则后续修复不得删除、缩减或绕过这三次重试。
- 每次尝试内部必须先等待并确认成交；确认未成交后才允许撤单；只有远端求购单已经确认消失且没有成交证据时，才允许进入下一次重试。
- 撤单与成交发生竞态时，必须按成交成功继续 C5 链路，禁止再次购买。
- 撤单后订单仍活跃、Steam 状态不可读或证据冲突时，保留 `manual_required` 和 A 锁，不继续重试；这是“不确定终态保护”，不等于减少正常失败重试次数。
- “知晓并隐藏”遇到关联 `steamBuyOrderId` 时不是纯本地 UI 操作，必须先安全撤单并确认终态；无法确认时拒绝隐藏。
- 已确认撤销的流水必须记录确认字段，后续循环直接跳过，避免每轮重复访问 Steam 拖慢执行器。

profitTrade 已上架 C5 后的改价规则：

- 首次上架可以按当前 C5 最低在售价做竞争性定价。
- 已上架后的降价不能每轮执行；默认必须距离上次 C5 上架或上次改价超过 `profitTrade.repriceCooldownHours`，当前运行配置为 3 小时。
- 每次降价前仍要重新读取 C5 当前最低在售价、检查在售深度，并重新计算降价后的 ROI。
- 降价后的 ROI 低于 `profitTrade.minRoi` 时不能自动改价，要转人工处理。
- ROI 超过 `profitTrade.manualReviewRoi` 时视为价格源异常，必须 ServerChan 提醒并停止自动改价。

### 7.4.2 Profit Trade 全量评估与 ROI 观察池

`profitTrade.scanMaxItems` 已经不再是实际扫描上限：

- 配置、CLI 和 API 暂时保留该字段，只用于兼容旧调用方。
- 通过可交易、保护规则和 `profitTrade.minItemValue` 等前置过滤的所有品类，都必须读取 Steam orderbook、计算 ROI 并执行 C5 风控。
- 禁止在读取 Steam orderbook 前按 C5 参考价截取前 N 个品类，否则会漏掉低价格但高 ROI 的机会。
- `limit` 只控制最终返回或写入多少个通过条件的执行机会，不得减少参与评估的品类。

`ROI > 0` 观察池和可执行机会必须分开：

- 观察池用于保存最新行情和价格/ROI 历史，不创建正常 `profit_trades` 流水，不锁 A，不买 B，也不上架 C5。
- `ROI > 0` 但低于 `profitTrade.minRoi`、C5 风控失败或 ROI 超过人工审核阈值的品类，可以显示在观察池，但必须明确标记为仅观察或已阻断。
- 真正执行仍必须通过最低 ROI、C5 风控、保护规则、审计和资产锁等全部门槛。
- 最新完整扫描确认 ROI 不再大于 0 或不再满足观察条件时，可以退出当前观察池；历史观察不能删除。
- 整轮扫描或行情读取异常时，不能把所有旧观察项误判为退出。

### 7.4.3 Profit Trade 前端、中断追踪与独立日志

Profit Trade 前端使用 Vue Router 4 和 hash history，固定三个子路由：

- `#/profit-trade/overview`：S1 总览，保留原执行控制、进行中流水和已完结收益，并展示 ROI 观察池。
- `#/profit-trade/interruptions`：S2 中断追踪，展示取消、失败和需人工处理的未完整结算尝试。
- `#/profit-trade/logs`：S3 实时日志。

旧 `#profit-trade` 必须兼容跳转到总览。页面业务状态只认后端 API 和持久化数据；API 离线时显示离线/空状态，禁止把 `frontend/public/profit_trade_dashboard.json` 或其他静态快照当成当前运营状态兜底。

中断追踪规则：

- 复用 `profit_trades` 的真实状态和步骤，并用状态事件补充时间线，不另造交易状态机。
- “知晓并隐藏”只改变默认问题列表显示；不得删除流水、状态事件或日志，并且必须支持恢复到默认列表。
- 存在未确认终态的 Steam buy order、无订单 ID 的不确定买入或其他成交证据冲突时，不能直接知晓隐藏；必须先走安全撤单/终态确认，无法确认时返回冲突并保留记录。

Profit Trade 日志规则：

- 日志只接收明确标记为 `source=profit_trade` 的事件；不能根据 Steam/C5 endpoint 事后猜测 caller。
- 日志范围包括 Profit Trade 自己发起的 Steam、C5 和 local 状态机/扫描活动，不包含挂刀执行器、C5 扫货或其他执行器日志。
- 挂刀执行器以后使用独立日志；Profit Trade 页面不得读取或混合展示挂刀日志。
- 结构化日志按 UTC 自然日写入 `logs/profit_trade/YYYY-MM-DD.jsonl`，闭日压缩为 `.jsonl.gz`，默认保留 90 天。
- Cookie、sessionid、密码、API key、Steam Guard/identity/device secret、C5 app-key、trade URL、token/styleToken 和完整认证请求体不得落盘；错误响应只能保存脱敏且限长的摘要。
- 日志写入、压缩、查询或实时广播失败不得改变真实交易结果。
- 429 日志要记录请求来源、账号、operation、request ID、状态码、耗时、`Retry-After` 和近期 Profit Trade Steam 请求频率；这只增强可观测性，不改变现有请求重试、relogin 或买入状态机。
- `429` 不等于 cookie 失效，也不能仅凭 Profit Trade 自身日志证明是挂刀执行器造成的。只有以后拿到独立挂刀日志，才能按 UTC 时间、账号和请求频率进行交叉分析。

### 7.5 Steam Guard 确认与撤单安全

生产路径不能使用宽泛的 `confirm_all()` 去确认所有手机确认。

Steam Guard 确认必须限定在本程序可证明拥有的对象上：

- 本地 `pool_operations`
- `sell_on_steam`
- 对应 `asset_id`
- 能映射到本程序记录的 `listing_id` 或 confirmation

如果无法把 Steam confirmation 可靠映射到本程序刚上架的 asset，就跳过并保持人工确认状态。不要为了自动化确认掉账号里可能存在的人工交易报价或其他无关确认。

撤销未卖出挂单时也一样：

- 先备份数据库
- 按 Steam 账号读取活跃挂单
- 只处理本程序 `pool_operations` 里的 `listingId/assetId`
- 远端 Steam 撤单成功后，才恢复本地 asset/pool/operation 状态
- `deferred` 这种未真实上架状态可以本地释放
- 查不到远端状态、疑似已成交、孤立 `inventory_assets.status=listed`，默认跳过，不粗暴重置

### 7.6 报表与钱包对账口径

`pool guadao-report` 里至少有三种时间口径，不能混用：

- C5 补仓完成时间：用于统计闭环补仓折扣
- Steam 官方成交时间：用于对账 Steam 钱包入账
- 程序确认/推进时间：只能说明程序什么时候发现状态变化，不能当成 Steam 钱包入账时间

禁止再把 `pool_operations.completed_at` 当作 Steam 卖出时间做钱包对账。

报表解释必须明确：

- 历史卖出、本期补仓，不应计入本期 Steam 钱包入账
- 本期卖出但未补仓，应计入 Steam 入账但不是 C5 闭环
- 缺少 `steamSoldAt` / `timeSold` 的流水，不能用于严格时间窗口钱包对账
- 当前未闭环存量是不按日期过滤的当前状态

如果用户拿钱包余额质疑报表，先按 Steam 官方 market history / 钱包记录查入账，不要先假设用户时间记错。

### 7.7 CLI 输出与中文终端格式

PowerShell 里中文、英文饰品名、`CNY` 金额混排时，普通 f-string 宽度不可靠。

如果修改 CLI 报表输出：

- 长口径说明拆成短行
- 表格对齐要按终端显示宽度，而不是 Python 字符数
- 在窄窗口下仍要能读
- 必须用用户实际命令跑一次肉眼检查
- 涉及 CLI 输出时补 `tests/test_cli.py`

但如果用户当前要求是“整理教训/写文档”，不要借这个理由继续改 CLI 实现。

### 7.8 箱子挂刀监控系统口径

`case-monitor` 是独立监控/报告链路，不是执行器状态机的一部分。

它应当：

- 采集 C5 价格和 Steam 官方 orderbook / pricehistory
- 写独立快照表
- 生成 24h / 7d / 30d 统计
- 给执行策略提供参考
- 不触发真实上架、补仓、卖出推进
- 不改 `pool_operations` 状态机

价格与品类坑点：

- Steam compact orderbook 价格是最小货币单位，CNY 下 `89` 是 `0.89` 元，不是 `89.00` 元
- “武器箱”要严格区分 Sticker Capsule、Souvenir Package 等 crates 子类
- 报告可以展示胶囊/纪念包，但执行器 `case_only` 例外不能被它们误触发
- 低流动性高价物品不能只看 20 墙稳定性，要结合最高求购价、24h/7d 成交量和采用价格源
- 终端、JSON、Markdown、前端页面的推荐类别和字段口径必须一致

默认报告生成应一次生成全量 crates 数据，网页再筛选类别。`--recommendation-type` 只能作为临时聚焦筛选，不应让用户以为要分多次生成。

### 7.9 前端学习与前端改动节奏

这个项目的前端不只是交付页面，也承担用户学习 Vue / TypeScript / AI 协作的目标。

当用户处于学习模式时：

- 不要一次堆几百行代码
- 每次只改用户指定的小点
- 不做无关重构
- 改完说明改了哪里、为什么这样改
- 用户要求解释时，停止写代码，按初学者能懂的方式逐段解释
- 对 `computed`、`ref.value`、`filter`、`return` 这类概念，要拆开外层函数和内层回调讲

如果用户明确说“不要回复”或“不要继续写代码”，必须停止输出或停止改动，不能继续推进自认为合理的下一步。

### 7.10 重构与文档同步规则

README 和核心文档必须反映当前真实项目，而不是早期做 T / 提醒工具的旧主线。

更新文档前必须确认：

- 当前代码实际入口
- 当前配置实际值
- 当前 CLI 是否存在
- 当前执行器真实状态机
- `AGENTS.md` 和 `docs/05_底仓执行口径.md` 的最新口径

不要把 `docs/使用说明.md`、账号示例、cookie、trade URL、密码、密钥等敏感内容搬进 README 或提交。

### 7.11 Git、分支和工作区纪律

用户说“某提交记录是 beta2.0”不等于要创建 `beta2.0` 分支。

涉及 Git 时必须先确认：

- 用户说的是分支名、提交说明、tag、PR 名，还是版本标签
- 当前所在分支
- 本地分支和远程分支指向
- 是否存在 Codex worktree 与 Desktop 仓库不一致
- 工作区有哪些未提交改动，哪些不是本轮产生的

不要擅自 force-push、重写 `main` 历史、删除用户分支或 stage 无关文件。敏感文档和无关脏文件默认不提交。

### 7.12 验证规则

每次修复都要用用户实际失败的命令验证一次。

示例：

- 用户报 `pool case-monitor` 不存在，就必须在用户实际仓库运行 `python .\main.py pool case-monitor ...`
- 用户报前端页面空白，就必须确认入口组件实际渲染并跑前端构建
- 用户报钱包余额对不上，就必须按 Steam 官方成交时间/market history 对账

测试只证明代码路径没有回归，不等于用户场景已经恢复。最后必须回到用户原命令或原页面验证。
