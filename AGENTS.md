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

### 2.1.1 Orderbook 快照与 Steam 求购撮合铁则

“真实执行价格只认 Steam orderbook”表示只能使用 Steam 官方价格源，不表示 orderbook 是无延迟的成交终态：

- `rgCompactSellOrders` 和 `rgCompactBuyOrders` 都是聚合快照，买卖两侧都可能短暂滞后。
- 2026-07-22 实验测得：卖单远端撤销后，卖盘档位仍残留 4.247 秒；求购已经成交后，旧最高求购仍残留约 6.632 秒。
- 被现有求购价格覆盖的新卖单，可能在确认生效时直接完成撮合，完全不进入公开 `rgCompactSellOrders`。
- orderbook 可以作为候选扫描、执行估价和购买前价格保护，但不能单独证明某个 listing 仍存在，也不能作为成交终态证据。

Steam 官方求购撮合规则必须按以下口径理解：

- 新卖单出现时，所有最大愿付价不低于卖价的求购都具备匹配资格。
- 多个求购都能匹配时，选择创建时间最早的匹配求购，不是选择价格最高的求购。
- 价格决定是否有资格匹配；在合格求购中，时间决定谁先成交。
- 新建最高求购因为创建时间晚，不保证拿到瞬时低价卖单。
- 当求购可以选择多个卖单时，Steam 先找能满足求购的最便宜物品，再选择其中创建时间最早的卖单。

实现约束：

- 不能假设提高 orderbook 轮询频率就一定能抓到交叉低价；这类卖单可能从未公开到聚合卖盘，提高频率只会增加 429 和多执行器请求压力。
- 普通非 commodity 饰品取得 listingId 后，listingId 只用于标识，不提供预留；`buylisting` 仍可能返回卖单已被其他人购买。
- `createbuyorder` 成功只代表求购已创建；新求购没有时间优先权，仍必须按钱包、活跃求购、库存和官方 history 确认真实成交。
- 所有活跃求购的名义总额最多为当前钱包余额的 10 倍；这不是十倍资金。单笔求购实际最大付款额不得超过当前账号可用余额，余额不足时程序不得提交或继续把它当作健康求购继续等待。
- 2026-08-08 的真实实验修正了一个容易误解的点：钱包余额后来下降时，Steam 不保证立即自动删除原有求购。小嘀咕账号在一笔较低价求购成交、余额降到约 ¥286 后，另一张 ¥465 求购仍显示在榜单上。因此“求购仍在榜上”不等于“当前钱包足够成交”，也不等于资金已经完整预留；真正撮合时才会再次面临付款能力检查。
- 余额变化后必须把旧求购重新标记为资金健康度未知/不足，禁止仅凭远端仍显示就继续新增求购或假设它一定能成交；需要通过钱包、活跃求购、库存和官方 history 重新确认终态。Steam 旧求购不会因本条项目规则自动被当作已撤单。
- 人民币执行前必须验证钱包货币为 CNY（Steam `currencyId=23`）；跨币种盘口必须先转换，不能把美元最小单位当成人民币分值。
- orderbook 连续显示明显交叉盘口时要视为可能存在缓存、节点不同步或币种/筛选差异；购买前仍要重新查价，具体卖单失效时安全退出，不能使用旧价格强买。

官方依据：`https://help.steampowered.com/en/faqs/view/61F0-72B7-9A18-C70B`。

### 2.1.2 长期求购链路当前保守口径

- 长期求购不是对所有可交易饰品无差别铺开；先排除明显没有希望的 ROI，候选数量、单笔价格和账号钱包都必须受限。
- Profit 扫描和原来的直接购买链路保持最高优先级。中间增加的求购链路先检查旧长期求购是否已经成交：有真实成交证据就锁定成交资产并推进 C5 上架。
- 有旧长期求购且确认仍活跃时，若当前卖盘已经达到正常 ROI，当前保守策略是不撤单、不改价、不新建，保留旧单的时间优势；若卖盘未达到正常 ROI，也不为了交叉盘口擅自改动旧单。
- 没有旧长期求购时，卖盘达到正常 ROI 走原直接购买链路；卖盘不达标则暂不操作，不因为盘口交叉就强行创建求购。
- 求购改价采用“撤旧、确认旧单消失且未成交、再创建新单”的终态闭环；不能假设 Steam 支持原单原位改价，也不能让同一账号的旧单和新单形成未记录的重复资金承诺。
- 每次求购成功创建只代表订单创建，不代表 B 已买到。实际成交必须继续核对实际付款、钱包变化、库存新资产、远端活跃求购和 Steam 官方 market history；最高愿付价不等于实际成交价。

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

Profit Trade 的普通扫描和行情服务构建必须直接复用账号库中已保存的 Cookie：

- 禁止在每轮扫描、每次构建 `MarketService` 时主动校验全部账号 Cookie。
- 真实认证失败时，只允许由发生失败的具体账号按请求链路刷新 Cookie。
- `search_listings` 的 HTTP 400 已有安全 `createbuyorder` 兜底时，不得为了进入兜底先启动全账号 Cookie 校验或登录。

不要把以下情况直接当作 cookie 失效：

- `ReadTimeout`
- `ConnectTimeout`
- `SSLEOF`
- 其他短时网络波动

Profit Trade 买 B 前的本地 Steam 队列超时表示本次 HTTP 回调没有执行，不是远端购买失败：

- 首次队列超时允许立即从头重跑一次完整买前检查。
- 再次超时时，只有流水仍为 `locked`、没有任何购买请求/订单/资产证据、A 预留仍明确属于该流水时，才保留 A 锁到下一个 Profit Trade 周期后继续复查。
- 价格、ROI、余额、C5 风控等确定性失败仍按原规则取消；不得用队列超时保护绕过。
- 一旦存在购买请求可能已发出的证据，禁止自动重买，必须走既有终态确认。

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

- 本节所说的“箱子”始终是广义箱子，即 CSGO-API `crates` 分类；包括 Weapon Case、Sticker/Autograph Capsule、Souvenir/Collection Package、Container 等子类，不能只按英文名是否以 `Case` 结尾判断
- 仅针对上述广义箱子，Steam 当前挂单槽低于 `caseMaxOpenGuadaoCount` 时，可以继续开启下一轮挂刀
- 新上架使用“广义箱子风险占用槽”：`sell_on_steam.listed` 加上 `sell_on_steam.listing_pending` 且 `note.confirmationStatus` 为 `confirm_sent_waiting_active_listing` 或 `listing_missing_unverified` 的数量；合计低于 `caseMaxOpenGuadaoCount` 时必须允许继续扫描和新上架，达到上限才暂停新上架
- `confirm_sent_waiting_active_listing` 与 `listing_missing_unverified` 是 `listing_pending` 的容量例外：二者按数量占风险槽但不形成整轮硬阻断，必须和新上架并行推进；前者只能在 Steam 活跃挂单真实出现后转为 `listed`，不能仅凭 Steam Guard 返回确认数量伪造 `listed`。其他 `listing_pending`、找不到对应 operation 证据的孤立品类 `listing_pending`、`rebuy_failed` 等仍按原边界硬阻断
- 已经卖出但等待 C5 补仓的 `rebuy_on_c5.pending` / `failed` 不占新上架风险槽，也不能参与随机撤单
- 默认上限是 `caseMaxOpenGuadaoCount = 100`
- 达到上限后必须暂停开启新一轮并提醒用户，但执行器不能停止；后续每轮仍要继续扫描、推进 Steam 卖出和 C5 补仓
- 3 小时满载释放使用独立的“远端确认活跃槽”口径，只统计 Steam 远端确认仍活跃的 `sell_on_steam.listed`；`confirm_sent_waiting_active_listing` 与 `listing_missing_unverified` 都不是活跃挂单证据，即使风险占用槽已满也不得启动满载计时或随机撤单
- 远端确认活跃槽连续满载 `caseFullReleaseAfterHours = 3` 小时后，只从 Steam 远端确认仍活跃的箱子挂单中随机撤销 `caseFullReleaseFraction = 0.125`（12.5%）；不得按挂单年龄选择该批释放对象
- 满载随机撤单必须等 Steam 远端撤单成功后，才能把本地 operation 改为 `canceled` 并恢复 asset；低于上限后，下一次重新满载要重新开始计时
- `caseFullReleaseAfterHours` 必须按“连续、有效的 Steam 活跃挂单快照”计时，不能按老挂单创建时间反推满载开始时间。每次确认不满都要清空计时；后端重启、Steam 快照不可读、调度长时间中断或两次观测间隔超过正常容忍窗口，都必须打断连续性，并从下一次完整确认满载时重新计时。系统关闭和状态未知的时间不得计入连续满载时间。
- 原有单笔挂单超过 48 小时自动撤单恢复的规则继续保留，它与满载随机释放是两条独立规则
- 单笔挂单超过 48 小时后，如果本单挂价仍处于 Steam 当前 `rgCompactSellOrders` 最低价，并且当前 `C5补仓价 / 本单Steam税后到手` 不超过本单冻结的最大挂刀比例加 `staleListedMaxRatioTolerancePct = 1.5` 个百分点，则不得撤单；使用 `staleListedRecheckHours = 24` 每天复查一次
- 超过 48 小时的挂单已经不在当前最低价，或者复查比例超过上述容忍上限时，才走原有远端撤单成功后恢复本地资产的流程；盘口或 C5 价格无法安全读取时保留挂单，不能误撤
- 这个例外不适用于非箱子，也不适用于除 `confirm_sent_waiting_active_listing`、`listing_missing_unverified` 以外的 `listing_pending`、`rebuy_failed` 等需要人工确认或失败处理的状态
- `sell_on_steam.listed`、`confirm_sent_waiting_active_listing` 和 `listing_missing_unverified` 的广义箱子按数量占风险槽；风险槽未满时不得阻断后续新挂单
- 扫描结果里的 `blockedByOpenCycle` / `waiting_existing_cycle` 是整轮全局判断，不代表被标记的候选品类自己存在旧流水。API、日志和前端解释时必须同时给出真正的阻断流水，禁止把全局原因错误归因到千瓦、变革等候选卡片本身

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

### 3.5 C5 发货确认期限与终态证据

C5 卖家发货期限按 12 小时处理，但 12 小时只是“必须复查订单详情”的时间边界，不是本地自动判失败的证据：

- 只有真实 C5 订单号存在时才开始 12 小时发货确认时钟。
- 超过 12 小时后仍必须查询 `buyer_order_detail`。
- 详情明确成功才推进 `completed`。
- 详情明确失败才推进 `c5_failed` 并创建唯一替换补仓。
- 详情仍在发货中、暂时不可读或接口异常时，保持 `delivery_pending` 并继续复查，禁止仅凭经过时间重复补仓。
- 后端重启后，发货确认任务必须经过短暂启动缓冲再查询详情；不能在客户端和调度器尚未恢复时，根据旧时间戳立即判失败。

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
- `caseFullReleaseAfterHours`
- `caseFullReleaseFraction`
- `staleListedRecheckHours`
- `staleListedMaxRatioTolerancePct`
- `guadaoItemScope`

修改定价逻辑时，优先确认是：

- 统一规则变更
- 箱子专用规则变更
- 仅某条执行路径的局部修正

`guadaoItemScope` 控制挂刀品类范围：

- `crates_only`：唯一规范配置值，语义是仅允许 CSGO-API `crates` 广义箱子进入挂刀候选，包括 Weapon Case、Sticker/Autograph Capsule、Souvenir/Collection Package、Container 等
- `non_case_only`：只允许不属于上述 CSGO-API `crates` 分类的物品进入挂刀候选

`case_only` 只作为历史配置/API 输入别名，读取后必须立即归一成 `crates_only`；任何新配置、API 返回、CLI、日志、测试和文档禁止再把 `case_only` 当作规范值。扫描候选、活跃挂单槽、未闭环阻断、满载计时、随机撤单、报表和前端必须复用同一套广义箱子分类语义。

当前不再支持 `all`。旧配置里如果仍写着 `all`，应按 `crates_only` 处理。

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

- 先读 `AGENTS.md`、`README.md`、`docs/核心规则/*.md`
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

人工批量重设补仓口径是一个明确的例外，只允许作用于“Steam 已卖出且唯一当前补仓子流水仍为 `rebuy_on_c5.pending`”的流水：

- 用户填写的新补仓价会替换该笔流水当前生效的冻结补仓价，不是额外叠加一个临时批准上限。
- 当前冻结补仓比例必须按 `新补仓价 / 该笔 Steam 实际税后到手` 重新计算，并与新冻结价格一起供后续补仓判断使用。
- 原冻结价格和原冻结比例只保留在追加式审计历史中，不再继续限制该笔流水。
- `guadaoMaxListingRatioAtOpen`、`listingRatioAtOpen` 等开单历史事实不得被伪造；全局挂刀策略也不随人工重设而改变。
- 已存在 C5 未决订单证据、发货确认中或正在执行补仓任务的流水必须拒绝人工重设，避免重复补仓。
- 用户在其他平台已经真实补回底仓时，可以对同样处于“已卖出待补仓”的流水手动完结；保存实际补仓价、平台、完成时间和审计记录，但不得伪造 C5 订单或新的 Steam 资产。

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

- C5 最低价唯一使用 `merchant/product/price/batch`（`price_batch`）。Profit Trade 扫描、Steam `search_listings` 400/429 求购兜底、真正调用 `sale_create` 前的复查和自动改价必须复用同一口径；禁止让 `goods_search`、`market/v2/products/search`、统计价或逐单分页结果覆盖它。
- `price_batch` 是品类聚合结果，不返回逐张挂单的 `productId`，因此不能假装从聚合值中精确删除自己的挂单。自动改价必须读取数据库中全部已知的同饰品自家活跃 Profit Trade 挂价：只要 `price_batch` 最低价命中其中任一自家挂价，就按“最低价可能是自己”保持全部相关挂单；只有 `price_batch` 最低价严格低于全部已知自家挂价时，才允许按竞争规则追价。不得让多张自家挂单互相追着降价。
- `market/v2/products/search` 仅可用于需要逐张商品信息的购买路径，且无论定价还是“寻找适合程序直接批量购买的商品”，请求都不得传 `delivery` 或 `acceptBargain`。任一字段都会把可按标价购买的完整市场切成子集并漏掉真实最低价。
- 当前不识别稳定价格墙，也不延迟确认所谓独立孤价。`price_batch` 返回的最低价直接参与首次定价和后续改价，但后续改价必须先通过上述自家挂价保护。
- C5 行情在扫描机会时读取，并在真正调用 `sale_create` 前重新读取；普通买 B 前不增加第三次重复读取。Steam `search_listings` 400/429 等异常回退中的既有重新校验仍必须保留。
- ROI 门槛必须服从不可逆状态边界：Steam 买 B 之前，自动开单或人工批准继续按开单/批准时冻结的 ROI 门槛判断，并统一把 ROI 四舍五入到四位小数比较；一旦真实 Steam 购买已经完成并进入 `steam_bought`，开单最低 ROI 和高 ROI 人工审核门槛都不得再阻止 C5 上架。此时只在四位小数归一化后的 ROI 小于 0 时停止，其他 C5 价格可读性、市场深度和订单安全风控继续保留。错误文案至少显示四位百分比小数，禁止再出现 `2.30% < 2.30%`。
- 首次上架价为竞争参考价乘以 `0.9967`（降低 `initialListingDiscountPct = 0.33%`），再向下取到人民币分；不设置固定金额的最小或最大折扣。`sale_create` 前市场变化时，真实挂价可以不同于扫描估算价，但必须重新计算净到手、利润和 ROI；Steam 已买后的上架门槛按上一条“买后四位小数 ROI 非负”执行，不再要求满足开单时冻结的完整最低 ROI。
- 挂单年龄从首次真实 C5 上架成功时间开始计算。0～12 小时内遵守 `repriceCooldownHours = 3` 小时冷却；只有 `price_batch` 最低价严格低于自己的挂价且未命中任一已知自家挂价时，才把目标价降为该最低价乘以 `0.99` 后向下取分。等于自己的挂价时按“可能是自己”保持原价，禁止对自己的现价连续乘以 `0.99`。
- 0～12 小时的改价必须满足开单时冻结的完整最低 ROI；不满足时保持 `c5_listed`，不能因此提前永久转人工。
- 12～24 小时内每个 Profit Trade 执行周期都检查一次，不再遵守普通 3 小时冷却；满足上述自家挂价保护后，目标价仍为最新 `price_batch` 最低价乘以 `0.99` 后向下取分。
- 12～24 小时的最低 ROI 为开单时冻结最低 ROI 的 `staleMinRoiFactor = 0.5` 倍。低于该底线时不改价，保持 `c5_listed` 且不发送触底通知；如果后续市场恢复到可接受范围，仍可继续自动追价。不得为了触及 ROI 底线而改到一个仍不能击败竞争者的价格。
- 达到 24 小时后，必须在 C5 深度读取和风险检查前停止自动改价，转为 `manual_required`，但保持 `step_key=c5_listed` 继续确认真实成交，并且只发送一次 ServerChan 通知；通知失败不得阻止状态落库，后续循环不得再次调用 `sale_modify`。
- 挂单消失或活跃列表暂时未见商品都不是结算证据；只有匹配到真实 C5 卖家成交订单，才能进入 `completed`。
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

Profit Trade 的 Steam `search_listings` 429 熔断规则：

- Profit Trade 全量扫描、库存 ROI 观察池和全市场选品观察池只允许消费本轮已经取得的 Steam `orderbook` 响应；扫描阶段禁止调用 `search_listings`、禁止抓取 `listingId`，即使发现交叉盘口也只能保存并展示同次 orderbook 证据。具体 `listingId` 的发现只属于已经创建真实流水后的购买执行路径，不能为了诊断交叉盘口增加请求、触发 relogin 或 listings 熔断。
- 普通非 commodity 饰品每次执行只允许一次 `search_listings`；首次 HTTP 429 后不要在同账号连续做 2 秒、4 秒短重试，立即打开 Profit Trade 全账号共享的 listings 路由熔断。该规则只改变 listings 查询，不得削弱 `STEAM_BUY_LISTING_RETRY_ATTEMPTS = 3` 的具体购买/求购失败重试。
- 箱子、胶囊、印花等本来就应使用 `createbuyorder` 的 commodity 品类必须直接跳过 `search_listings`，避免无意义增加 listings 路由压力。
- listings 首次 429 或路由处于冷却时，必须重新读取 Steam orderbook、C5 行情和深度，重新计算价格容差、ROI、余额和 C5 风控；仍符合条件时按当前 orderbook 最低卖价创建 `quantity=1` 的求购单，并复用既有的成交确认、撤单终态和三次购买重试状态机。不再符合时安全取消，不得使用旧价格购买。
- listings 路由首次冷却 10 分钟；冷却期间继续 ROI 观察、Steam orderbook、C5 同步和收益结算，也允许通过上述安全求购路径创建并推进新流水、锁定 A，但禁止新的 `search_listings`。纯 listings 429 即使涉及多个账号或短时间多次发生，也只能隔离 listings 路由，不能升级为 Steam 全局熔断，更不能封死整条买 B 链路；只有同时出现其他 Steam 路由异常时，才按全局异常处理。
- 冷却到期直接恢复为正常状态，不进入 `half_open`，也不发送独立的 listings 恢复探测。下一笔真实且仍符合条件的普通饰品机会按正常流程查询 `search_listings`；如果该真实查询再次返回 429，就重新冷却 10 分钟，并让当前机会在重新校验后改走安全求购。
- 禁止为了判断 listings 是否恢复而额外消耗一次查询；实际业务查询本身就是恢复后的首次验证。查询成功就继续指定卖单购买，查询 429 就重新熔断，不能在同一轮连续短重试。
- 熔断状态必须持久化并在总览、观察卡片、执行区、中断追踪和实时日志中展示触发账号、连续 429、最后 429、剩余时间和冷却结束时间。刷新前端或重启后端不能丢失；冷却时间一到，页面必须恢复正常状态，不再显示“等待恢复探测”。
- 历史上因 listings 连续 429 已经取消的中断流水永久保留，不能在路由恢复后改成成功。新流水首次 429 后优先改走安全求购；只有重新校验价格、ROI、余额或 C5 风控不再符合，或者求购终态无法安全确认时，才按对应状态机安全取消或转人工。

### 7.4.4 Profit Trade 已完结收益报表不得复用运营列表上限

Profit Trade 的“已完结收益”“全部累计”和日期筛选属于会计统计视图，不能复用总览页为了控制响应体积而设置的最近流水 `limit`。

2026-07-27 已发生严重事故：`/api/profit-trade/dashboard` 先读取最近 100 条原始流水，再由页面隐藏 `cancelled`，导致数据库真实存在的 65 笔 `completed` 只显示 34 笔；页面把这 34 笔错误标成“全部”，把真实累计利润 `CNY 493.66` 错报为 `CNY 302.39`。旧流水没有删除，只是被活动/取消流水挤出了有界运营列表。

以后必须遵守：

- 运营列表和统计报表必须分离。最近任务、进行中流水可以有界；“全部”“累计”“按日期统计”不能以该有界列表作为数据源。
- 用户选择日期后，必须由后端按照完整数据库范围和统一时间字段重新查询；禁止先取最近 N 条，再在浏览器里做日期过滤。
- Profit Trade 已完结收益按真实 Steam 购买时间 `steamBoughtAt` 归属日期；前端本地日期必须先转换成准确的 UTC 起止边界再传给后端。不得用 `created_at`、`updated_at` 或 C5 完成时间替代购买时间。
- “全部”表示所有符合状态条件的历史记录，不得存在未向用户说明的 `LIMIT 100`、`LIMIT 500` 或其他隐式截断。
- 分页只能限制本页 `items`，不能限制 `count`、Steam 买入总额、实际利润总额等汇总；汇总必须在完整筛选结果上计算。
- 状态过滤必须在分页/limit 之前完成。`cancelled`、`failed`、进行中流水不得占用 `completed` 报表的名额。
- API 返回的明细、笔数和金额汇总必须使用同一套状态与时间谓词，不能分别从不同快照或不同范围计算。
- 没有可靠 `steamBoughtAt` 的历史记录只能在“全部”中保留并明确时间缺失；带日期筛选时不得伪造归属日期。
- 前端只有在拿到完整范围的后端统计时才能显示“全部”“累计”；如果接口有截断，必须显示“最近 N 条”及截断状态，禁止误导性命名。
- 回归测试必须制造“有效 completed 位于普通列表上限之外，且中间夹有大量 cancelled”的数据，验证旧记录不会被挤出；同时覆盖北京时间日期边界、单边日期、全部范围、空结果和分页不影响汇总。
- 上线验收不能只看页面当前几笔。必须对比数据库 `completed` 总数、逐笔 `realized_profit` 之和、Steam 买入总额与 API 汇总；三者完全一致才算通过。

核心纪律：

> 有界运营列表不是会计账本。先截断、后过滤，会让统计随着无关流水进入而漂移；任何写着“全部/累计/日期范围”的数字，都必须从完整筛选集合计算。

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
- Steam 旧版 `/market/pricehistory/` 返回的日期字符串（例如 `Jul 21 2026 20: +0`）不能把末尾 `+0` 机械解释为真实 UTC。逐点对照新版市场页面的 Unix 时间戳后，2016-11 至 2026-07 的 3762/3762 个点在冬季和夏季都固定快 1 小时；这是 Steam 旧标签格式的固定校正，不是“夏令时才减 1 小时”。
- `pricehistory` 时间口径优先使用 Steam 新版市场页面预载数据中的 Unix `time`，再从 UTC 按 IANA 时区转换到 `Asia/Shanghai`；禁止对时间字符串手工固定加 8 小时。美国夏令时与非夏令时切换期间也必须使用时区感知转换，不能写死偏移量。
- 当前解析器对这个经过全量交叉验证的 Steam 旧格式固定减 1 小时，并同时支持 Unix 秒和毫秒；其他未知文本格式不得套用该修正。如果无法取得或验证时间语义，应把小时级时间视为不确定。时间相关回归必须覆盖夏令时、非夏令时、切换边界和普通日期。
- 报告展示时要区分 Weapon Case、Sticker Capsule、Souvenir Package 等 `crates` 子类，不能把中文类别名称混成同一种具体物品
- 执行器 `crates_only`、活跃挂单槽和箱子未闭环例外使用的是广义箱子口径；胶囊、纪念包等 `crates` 子类必须和 Weapon Case 使用同一分类结果，不能一处计入、一处排除
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
- `AGENTS.md`、`docs/核心规则/CS赚钱核心知识总纲.md` 和 `docs/核心规则/底仓执行口径.md` 的最新口径

不要把 `docs/开发与操作/使用说明.md`、账号示例、cookie、trade URL、密码、密钥等敏感内容搬进 README 或提交。

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

### 7.13 饰品目录搜索不能把首批上限冒充完整结果

所有从本地 `items` 目录选择标准饰品的入口必须共享同一搜索语义，包括：Profit Trade 保护品类、手工流水、全市场选品、挂刀特殊比例和 C5 扫货。

固定规则：

- 用户输入的空格分隔词必须按“全部关键词均匹配”处理，不能把整句当作一个连续子串。
- 搜索排序必须按相关性和饰品本体归组；普通版与 StatTrak 同款要相邻展示，禁止让某一版本的上百条记录占满首批结果。
- `普通版` / `普通款` 表示非 StatTrak；`暗金` / `StatTrak` 表示 StatTrak。目录里没有字面“普通版”也必须能正确搜索。
- 已确认的常见中文别名应归一化，例如用户输入“伽马多普勒”必须能命中目录中的“伽玛多普勒”。
- 后端必须返回 `offset / limit / total / hasMore / nextOffset`；前端必须提供继续加载，并按 `marketHashName` 去重。
- `limit` 只限制当前返回批次，不能被解释成全部匹配结果。搜索框没有继续加载能力时，不得静默截断候选。
- 只有“从标准物品目录选项”的搜索适用上述分页；日志、流水和当前页面筛选应继续使用各自完整数据集或服务端业务分页，不能混成目录搜索。
- 回归测试必须构造超过首批上限的数据，并证明目标位于旧排序上限之外时仍可在首屏合理出现或通过后续分页取得，且翻页无重复、无遗漏。

核心纪律：

> 搜索首批结果不是完整目录。相关性排序解决“先看到什么”，分页元数据和继续加载解决“最终能否找到全部”。

### 7.14 与用户沟通一律使用北京时间

所有向用户汇报的时间（买入/成交时间、扫描时间、日志时间、过期时间、重启时间等）一律使用北京时间（`Asia/Shanghai`，UTC+8），并明确标注“北京时间”。

- 数据库和日志内部继续使用 UTC，禁止为了展示而改动存储口径。
- 引用日志或数据库的原始时间时，必须先换算成北京时间再给用户，禁止把未标注时区的 UTC 数字（如 `17:40:32`）直接丢给用户。
- 换算示例：UTC `2026-08-12T17:40:32Z` = 北京时间 `2026-08-13 01:40:32`。
- 前端页面本身的本地时间展示按浏览器时区即可；本条主要约束对话、日志说明、报告和诊断结论中的时间口径。
