# patinet_cs2_asistant

这是一个本地运行的 CS2 交易辅助项目。当前主线已经不是通用行情站，也不只是早期的 `t-profit` 扫描提醒，而是围绕 `C5 + Steam` 的底仓执行：

- `C5` 负责库存、补仓、部分买入执行。
- `Steam` 负责挂刀卖出、实时卖家挂单墙价格、挂单状态和 Steam Guard 确认。
- 执行器负责把扫描候选推进成可执行动作，并维护 `卖 Steam -> C5 补仓 -> 状态闭环`。

真实交易相关功能有风险。运行前先确认当前账号、配置、数据库状态和 `dryRun`。

## 先读这些文档

后续开发和运行时，优先级如下：

1. `AGENTS.md`
   项目开发硬规则，尤其是 Steam 实时取价、状态机、账号隔离、测试要求。

2. `docs/核心规则/CS赚钱核心知识总纲.md`
   当前赚钱模型、Steam/C5 价格源、求购、挂刀、补仓、ROI、证据链和风险边界的集中总纲。

3. `docs/核心规则/底仓执行口径.md`
   固定 `list / guadao` 和 `transfer` 的业务含义。

4. `data/strategy_config.json`
   运行时真实策略配置源，不是代码里的默认值。

5. `config/accounts.json`
   本地 Steam/C5 账号配置。里面可能含加密后的敏感信息，不要外发。

6. `docs/开发与操作/使用说明.md`
   本地操作便签，可能包含临时账号示例或个人记录，不作为公开规范文档。

`docs/产品与接口/` 主要记录需求和接口选型背景。它们和当前执行口径不一致时，以 `AGENTS.md`、`docs/核心规则/CS赚钱核心知识总纲.md`、`docs/核心规则/底仓执行口径.md` 和真实代码为准。

当前文档分类：

- `docs/核心规则/`：唯一规范入口和底仓状态机口径；
- `docs/产品与接口/`：产品需求、API 选型、Steam 接口与 ID 清单；
- `docs/实验与研究/`：Steam 撮合实验、Orderbook 延迟和导余额调查；
- `docs/测试与对账/`：真实卖出、补仓、余额与数量守恒的验收方法；
- `docs/开发与操作/`：本地使用、前端教程和库存工具；
- `docs/畜生学习法.md`：按用户要求独立保留在 `docs` 根目录。

## 安装与初始化

```powershell
pip install -e .
python .\main.py init-db
python .\main.py import-catalog
```

项目入口也可以用安装后的命令：

```powershell
cs2-assistant -h
```

但本文统一使用：

```powershell
python .\main.py ...
```

## 环境变量

常用环境变量：

```powershell
$env:C5GAME_API_KEY="..."
$env:STEAMDT_API_KEY="..."
$env:CSQAQ_API_KEY="..."
$env:SERVERCHAN_SENDKEY="..."
$env:CS2_MASTER_KEY="..."
```

说明：

- `C5GAME_API_KEY` 用于 C5 库存、价格、补仓和购买。
- `STEAMDT_API_KEY` / `CSQAQ_API_KEY` 用于扫描层 Steam 聚合价格补充。
- `SERVERCHAN_SENDKEY` 只在需要微信推送时使用。
- `CS2_MASTER_KEY` 影响本地账号敏感字段加解密。

## 账号与 Steam 登录

账号数据在 `config/accounts.json`。导入、切换和检查账号：

```powershell
python .\main.py account import-mafile "C:\path\to\account.maFile" --name "account-name" --username "steam-login"
python .\main.py account list
python .\main.py account use "account-name"
python .\main.py account status
```

登录并验证 Steam：

```powershell
python .\main.py steam login --account "account-name"
python .\main.py steam auth-check
python .\main.py steam cookie-refresh
python .\main.py steam confirm
```

读取所有本地已配置 Steam cookie 的钱包余额：

```powershell
python .\main.py account balance
```

这个命令读取的是 Steam 市场网页：

```text
GET https://steamcommunity.com/market/
```

页面里的 `g_rgWalletInfo` 包含：

- `wallet_balance`：当前可用余额，单位是分。
- `wallet_delayed_balance`：Steam 待入账/冻结余额，单位是分。
- `wallet_currency`：币种，例如 CNY 是 `23`。

注意：`account balance` 是独立查询功能，不应耦合执行器。

## 当前策略配置

运行时配置文件是：

```text
data/strategy_config.json
```

当前工作区里这个文件的关键值包括：

```json
{
  "common": {
    "executionEnabled": true,
    "dryRun": false,
    "balanceDiscount": 0.73
  },
  "guadaoBalance": {
    "guadaoItemScope": "crates_only",
    "guadaoMaxListingRatio": 0.69,
    "listingWallMinCount": 20,
    "caseListingPriceOffset": -0.01,
    "caseMaxOpenGuadaoCount": 100,
    "maxListPerCycle": 10
  },
  "profitTrade": {
    "enabled": true,
    "allowRealExecution": false,
    "balanceDiscount": 0.69,
    "minRoi": 0.08,
    "minItemValue": 5.0,
    "dailySteamBudget": 600.0,
    "scanMaxItems": 80
  },
  "legacyTransfer": {
    "transferMinRealRatio": 9999
  }
}
```

也就是说，当前挂刀公共执行配置是非 dry-run；但 `profitTrade.allowRealExecution` 仍是 `false`，新做T执行器不会直接触发真实 Steam 买入或 C5 上架。正式运行前必须先确认这些值，尤其是：

- `executionEnabled`
- `dryRun`
- `autoListEnabled`
- `autoRebuyEnabled`
- `guadaoItemScope`
- `guadaoMaxListingRatio`
- `maxListPerCycle`
- `profitTrade.enabled`
- `profitTrade.allowRealExecution`
- `profitTrade.balanceDiscount`
- `profitTrade.minRoi`

`guadaoItemScope` 当前只按以下两类理解：

- `crates_only`：只允许 CSGO-API `crates` 广义箱子进入挂刀候选，包括 Case、Capsule、Package、Container 等；旧 `case_only` 只作为兼容输入，读取后归一为 `crates_only`。
- `non_case_only`：只允许非箱子进入挂刀候选。

旧配置如果写 `all`，按当前约定应视作 `crates_only` 处理，不要重新扩展为全品类，除非重新确认交易口径。

## 真实挂刀定价口径

真实上架时，Steam 实时价格只认 `orderbook`：

```text
GET /market/orderbook?q=Load&qp=[730,"market_hash_name"]
```

执行口径：

- 只读取 `rgCompactSellOrders`。
- 只看卖家挂单墙。
- 不参考买家数据。
- 不重新使用 `item_nameid`。
- 不重新走 `itemordershistogram`。
- `market_hash_name` 必须动态传入，不能写死某个饰品名。

真实执行时：

- 拿不到 Steam 实时价格，可以跳过。
- 不能 fallback 到 CSQAQ/SteamDT 聚合价继续真实上架。
- `dry-run` 才允许用扫描阶段已有价格模拟流程。

新开挂刀的比例选择口径：

- `guadaoMaxListingRatio` 是硬上限，只负责把 `listing_ratio <= 上限` 的品种纳入可导候选池。
- 执行器真正上架前，会对每个候选重新读取一次 Steam `orderbook`，按当前执行挂价计算一个真实 `listing_ratio`。
- 进入候选池后，不再计算额外的全局动态阈值；每个品种直接使用自己本轮实时算出来的 `listing_ratio`。
- 本轮开新挂刀时，按每个品种自己的实时 `listing_ratio` 从低到高选择；比例相同时，再优先选择最近卖出更快的品种。
- 不做盘口分桶，也不按 Steam 墙里有多少个卖单来“吃深度”；每个候选只看当前规则取出的一个执行挂价和一个比例。
- 每笔新挂刀都会在流水 `note` 里冻结 `listingRatioAtOpen`、`maxRebuyRatioAtOpen`、`guadaoMaxListingRatioAtOpen` 和 `steamNetFactorAtOpen`，后续补仓优先按这笔流水自己的冻结口径闭环。

箱子模式当前固定口径：

- `listingWallMinCount = 20`
- 按卖家累计墙取价
- 再应用 `caseListingPriceOffset = -0.01`
- 当前实现效果等价于最终挂价表现为“累计墙价格 + 0.01”

不要擅自改成“第一档最低卖家价 + 0.01”，也不要把箱子逻辑和非箱子统一。

## 执行器状态机

执行器核心表有三层，不要混用：

- `inventory_pool`
  品类级底仓状态和粗粒度展示。

- `inventory_assets`
  单资产级可交易、已挂单、已卖出等状态。

- `pool_operations`
  真实动作流水和闭环推进链路。

关键链路：

```text
sell_on_steam.listed
sell_on_steam.sold
rebuy_on_c5.pending
rebuy_on_c5.completed / failed
```

默认规则：

- 上一轮挂刀循环未闭环时，先推进旧状态，默认不开新挂单。
- 仅针对箱子/广义箱子，`sell_on_steam.listed` 和 `rebuy_on_c5.pending` 低于 `caseMaxOpenGuadaoCount` 时，可以继续开启下一轮。
- 达到 `caseMaxOpenGuadaoCount` 后，只暂停新开挂刀并提醒，执行器不能停止，还要继续扫描、推进卖出和补仓。
- 这个例外不适用于 `listing_pending`、`rebuy_failed` 等需要人工确认或失败处理的状态。

候选过滤也要注意：

- `scan_strategies()` 来自 C5 聚合库存视角。
- 真实上架还必须过滤到当前 executor 账号和当前账号本地可交易资产。
- 有候选不代表当前 Steam 账号能上架。

## 运行执行器

先做一次 dry-run 观察：

```powershell
python .\main.py executor start --once --dry-run
```

真实执行一次：

```powershell
python .\main.py executor start --once --no-dry-run --enable
```

常驻执行：

```powershell
python .\main.py executor start --no-dry-run --enable
```

常用限制参数：

```powershell
python .\main.py executor start --once --dry-run --max-list 3
python .\main.py executor start --once --dry-run --max-transfer-buy 0
```

Steam Guard 待确认时：

```powershell
python .\main.py steam confirm
```

安全边界：`steam confirm` 只会读取本地数据库里本程序 `sell_on_steam` 待确认流水的 `asset_id`，并通过 Steam 待确认挂单列表映射到对应 `listing_id` 后确认。它不会全量确认当前账号 mobile confirmations；映射不到本程序 asset 的确认会保持未确认。

测试单个资产能否被 Steam `sellitem` 接受：

```powershell
python .\main.py steam test-list --asset-id "asset-id" --price 999
```

这个测试默认高价上架后撤单。真实执行前仍要确认当前账号、资产和 Steam Guard 状态。

## 补仓口径

真实补仓时不重新读取 Steam 实时挂价。

补仓判断使用对应卖出流水记录的 Steam 已卖出税后到手价：

```text
Steam已卖出税后到手 = steamListPrice * steamNetFactor
补仓比例 = C5补仓价 / Steam已卖出税后到手
```

新流水优先使用开单时冻结的最高补仓比例：

```text
补仓比例 <= maxRebuyRatioAtOpen
```

旧流水没有 `maxRebuyRatioAtOpen` 时，才兼容使用当前 `guadaoMaxListingRatio`。C5 网络临时错误应延迟重试，不应误判成永久失败。

注意：卖出流水里可能同时保存 `steamSellerNetPrice`、`steamSellerNetPriceSource=steam_history`、`steamSoldAt` 等来自 Steam 历史成交页的字段。这些字段用于日志、报表和钱包对账；真实补仓判断仍不要在补仓时重新读取 Steam 实时挂价。

## 报表与对账

挂刀余额折扣报表：

```powershell
python .\main.py pool guadao-report --from 2026-06-16T22 --to 2026-06-22T23
python .\main.py pool guadao-report --from 2026-06-22T13:06 --to 2026-06-22T23:00
python .\main.py pool guadao-report --from 2026-06-16 --to 2026-06-22 --detail
```

当前时间边界规则：

- `2026-06-16` 作为开始表示 `00:00:00`。
- `2026-06-16` 作为结束表示 `23:59:59`。
- `2026-06-16T22` 作为开始表示 `22:00:00`。
- `2026-06-16T22` 作为结束表示 `22:59:59`。
- `2026-06-16T22:15` 作为开始表示 `22:15:00`。
- `2026-06-16T22:15` 作为结束表示 `22:15:59`。
- 带秒的完整 ISO 时间会按传入值解析。
- 日期必须补齐两位月/日，例如 `2026-06-02T23:00`，不要写成 `2026-06-2T23:00`。

对账时要区分：

- `总览`：按 C5 补仓完成时间统计闭环。它回答的是“这段时间补仓闭环了多少”，不是“这段时间 Steam 钱包入账了多少”。
- `Steam入账口径(按卖出时间)`：按 Steam 官方历史成交时间统计。代码只把 `note.steamSoldAt` / `note.timeSold` 视为钱包入账时间，不再把 `pool_operations.completed_at` 当作卖出时间。
- `卖出时间对账`：把本时间段 Steam 卖出拆成已闭环、未闭环和因余额不足忽略的流水，用于解释为什么卖出入账和 C5 补仓闭环不是同一个集合。
- 报表里的 `C5金额` / `总折比` 列：闭环栏使用实际 C5 补仓现金；未闭环栏使用当前数据库能归集到的待补/后续补仓金额，仅供分析，最终仍以 C5 成功成交记录为准。
- `成交时间缺失`：说明这批 sold 流水只有程序确认时间，缺少 Steam 官方成交时间，不能直接拿来做钱包时间窗口对账。需要从 Steam market history 回填后再比较钱包差额。
- `对账提示`：说明 `总览` 里有多少是“历史卖出、本期补仓”，这部分不应计入本时间段 Steam 钱包入账。
- `当前未闭环存量`：当前仍未成功补仓闭环的卖出流水，不按报表日期过滤。
- `account balance`：读取 Steam 钱包当前可用余额和待入账/冻结余额。拿钱包差额对账时，要用多个账号合计，并排除同时间段内非本项目产生的 Steam 充值、购买、退款或人工交易。

这些集合不能随意相加，否则容易重复计算同一笔 Steam 卖出余额。

## 做T扫描与提醒

早期扫描提醒仍保留，但它是扫描/提醒层，不是执行层。

扫描：

```powershell
python .\main.py t-profit scan --top 20 --min-price 10
python .\main.py t-profit scan --bottom 20 --min-price 10
python .\main.py t-profit missing-steam
```

提醒：

```powershell
python .\main.py notify t-profit --configure
python .\main.py notify t-profit --show-config
python .\main.py notify t-profit --once
python .\main.py notify t-profit
```

旧命令 `t-yield` / `notify t-yield` 仍兼容，但不作为主入口。

做T扫描的聚合价格源是：

```text
C5 库存/C5价 + CSQAQ Steam主价 + SteamDT Steam兜底
```

这个价格源只适合扫描和提醒，不可替代真实执行时的 Steam `orderbook`。

## 搬砖做T执行器

新搬砖做T入口是 `profit-trade`，不要和旧 `t-profit` 扫描提醒混用。

查看或切换后端开关：

```powershell
python .\main.py profit-trade config
python .\main.py profit-trade config --enable
python .\main.py profit-trade config --disable
python .\main.py profit-trade config --allow-real-execution
python .\main.py profit-trade config --disallow-real-execution
```

扫描机会：

```powershell
python .\main.py profit-trade scan --limit 20
python .\main.py profit-trade scan --limit 20 --record
python .\main.py profit-trade scan --limit 1 --dump-json
python .\main.py profit-trade run-once
```

扫描默认只读取数据，不买、不卖、不锁。`--record` 会把机会写入 `profit_trades` 候选流水；再次记录扫描会先取消旧的买 B 前候选，再写入新候选，避免复用已经过期的 ROI。`--lock` 会写入流水并短时间锁定 A 资产，但仍然不会买 B 或上架 C5。

扫描会先按可交易状态、保护规则和 `profitTrade.minItemValue` 做前置过滤，随后对所有剩余品类读取 Steam 官方 `orderbook`、计算 ROI 并执行 C5 风控。`profitTrade.scanMaxItems` 和 `--scan-max-items` 暂时只为旧配置、CLI 和 API 调用兼容保留，不再截断实际参与评估的品类；`limit` 只限制最终返回或写入多少个通过条件的执行机会。

手动锁定一笔候选流水：

```powershell
python .\main.py profit-trade lock 123
python .\main.py profit-trade buy 123
python .\main.py profit-trade list-c5 123
python .\main.py profit-trade refresh-sales
```

`buy` 只处理 `locked` 流水：执行前重新读取 Steam `orderbook` 并按实际付款价复算 ROI。普通非 commodity 饰品会先查询具体 listing，再用 `buylisting` 购买；箱子等 commodity 可以使用 `createbuyorder`，但提交求购请求不等于已经买到，必须结合求购单、钱包、库存和资产变化确认真实成交。买 B 前如果 A 锁已过期、价格移动超出容忍范围，或 ROI 低于 `profitTrade.minRoi`，不会使用旧价格继续购买。

profitTrade 的 ROI 口径和 notify 做T提醒保持一致：

```text
面折比 = C5挂价 / Steam买入价
C5预计到手折比 = 面折比 * 0.99
ROI = C5预计到手折比 - profitTrade.balanceDiscount
真实成本 = Steam买入价 * profitTrade.balanceDiscount
预计收益 = C5挂价 * 0.99 - 真实成本
```

Profit Trade 只使用自己独立的 `profitTrade.balanceDiscount`，不读取 `common.balanceDiscount`，也不自动跟随挂刀执行器的 `guadaoBalance.guadaoMaxListingRatio`。

买 B 的 Steam 账号选择规则是：优先用 A 资产所属 Steam 账号；如果这个账号余额不足，再从其他本地账号里选择“可用余额足够且余额最小”的账号。余额是否足够按 Steam 实际付款价判断，收益核算按该笔冻结的 `profitTrade.balanceDiscount`。

`list-c5` 只处理 `steam_bought` 流水：C5 `sale_create` 成功后才把 A 资产锁从 `active` 转成 `consumed`。这意味着 A 已经被 profitTrade 长期占用，不能再被挂刀或旧 `legacyTransfer` 选中。

`run-once` 是后端执行器的一轮运行入口：

- `profitTrade.allowRealExecution=false` 时，只扫描并写入候选，不会锁 A、买 B 或上架 C5。
- `profitTrade.allowRealExecution=true` 时，会先推进已买入待上架的流水；然后取消旧候选，重新扫描新鲜机会，并在同一轮连续完成 `发现机会 -> 审计通过 -> 锁定A -> 买入B -> C5上架`。`C5售出 -> 收益结算` 依赖 C5 后续售出，需要之后轮次刷新。

`refresh-sales` 会读取 C5 当前在售列表；已过检查窗口且 productId 不再出现在在售列表里的 `c5_listed` 流水，会按本程序上架价与 `profitTrade.c5CurrentSaleNetFactor` 结算到 `completed`。没有 C5 成交详情 API 前，`c5SoldNetPriceSource` 会标记为 `estimated_from_listing_price`。

启动前端可调用的本地 API：

```powershell
python .\main.py profit-trade serve-api
```

再启动前端：

```powershell
cd frontend
npm run dev
```

前端通过 Vue Router 4 的 hash 路由拆成三个 Profit Trade 子页：

- `#/profit-trade/overview`（S1）：保留执行控制、进行中流水和已完结收益，并增加 `ROI > 0` 观察池。
- `#/profit-trade/interruptions`（S2）：查看取消、失败和需人工处理的中断流水及七步时间线。
- `#/profit-trade/logs`（S3）：查看 Profit Trade 实时结构化日志。

旧地址 `#profit-trade` 会兼容跳转到 S1。页面通过 `/api/profit-trade/dashboard` 和 `/api/profit-trade/config` 读取、修改真实后端状态；如果 API 没启动，只显示离线和空状态，不会读取 `frontend/public/profit_trade_dashboard.json` 或其他静态快照冒充当前运营数据。

S1 的观察池和执行机会不是一回事：只要最新完整评估的 ROI 大于 0，就可以保存到观察池并查看价格/ROI 历史；低于 `profitTrade.minRoi`、C5 风控失败或超过人工审核阈值时只能观察，不能创建执行流水、锁 A、买 B 或上架 C5。最新评估不再满足观察条件时会退出当前池，但历史仍保留；整轮扫描失败不会清空旧观察结果。

S2 默认展示 `cancelled`、`failed` 和 `manual_required`。点击“知晓并隐藏”只会从默认问题列表折叠该记录，不会删除 `profit_trades` 流水、状态时间线或日志，并且可以恢复。如果记录关联未确认终态的 Steam buy order 或存在不确定成交证据，后端会先要求安全解决远端订单；无法确认时返回冲突，不能直接隐藏。

S3 只展示明确标记为 `source=profit_trade` 的事件，包括 Profit Trade 自己发起的 Steam、C5 和 local 扫描/状态机活动。它不读取挂刀执行器、C5 扫货或其他执行器日志。结构化日志按 UTC 自然日写入：

```text
logs/profit_trade/YYYY-MM-DD.jsonl
```

闭日文件压缩为 `.jsonl.gz`，默认保留 90 天；支持时间和交易字段筛选、SSE 实时显示、JSONL 导出和可读 `.log` 下载。Cookie、sessionid、密码、API key、Steam Guard/identity/device secret、C5 app-key、trade URL、token/styleToken 和完整认证请求体不会落盘。

日志会记录 Profit Trade Steam 请求的 operation、账号、request ID、状态码、耗时、`Retry-After` 和近期请求频率，因此能帮助分析 429。但这次可观测性更新没有修改 Steam 原有请求重试、relogin 或买入重试规则；429 也不等于 cookie 失效。Profit Trade 日志只能证明自身发起了哪些请求，不能单独证明 429 是挂刀执行器造成的；需要将来独立的挂刀日志按 UTC 时间和账号交叉对照。

当前安全边界：

- `profitTrade.enabled` 已可从后端和前端切换，它只表示新做T执行器允许工作。
- `profitTrade.allowRealExecution` 默认是 `false`。只有它为 `true`，且单笔流水已经进入对应步骤时，`buy` / `list-c5` 或前端按钮才会触发真实 Steam 买入 / C5 上架。
- 数据库已新增 `asset_reservations` 和 `profit_trades`，用于记录做T流水和单资产锁。
- profitTrade 锁住的 A 资产会被挂刀和旧 `legacyTransfer` 跳过，避免两个执行器抢同一个 `asset_id`。
- 单笔进度固定为：`发现机会 -> 审计通过 -> 锁定A -> 买入B -> C5上架 -> C5售出 -> 收益结算`。
- 当前 C5 在售价估算到手按 `price * 0.99`；C5 最近成交价按“已扣手续费”处理，不再二次乘 `0.99`。
- ServerChan 日报只通过前端按钮或命令手动触发：

```powershell
python .\main.py profit-trade daily-report --send
```

## 开发与测试

涉及执行器、Steam、C5、补仓、状态机的改动，先读：

```text
AGENTS.md
docs/核心规则/CS赚钱核心知识总纲.md
docs/核心规则/底仓执行口径.md
```

最低测试要求：

```powershell
python -m pytest tests\test_executor_engine.py -q
```

如果涉及 CLI 或展示：

```powershell
python -m pytest tests\test_cli.py -q
```

如果涉及 Steam 市场客户端：

```powershell
python -m pytest tests\test_steam_market.py -q
```

如果涉及真实交易口径，必须补最小回归测试。不能只改日志、不补测试。

## 常用命令速查

```powershell
python .\main.py -h
python .\main.py account -h
python .\main.py steam -h
python .\main.py executor -h
python .\main.py pool -h
python .\main.py t-profit -h
python .\main.py notify t-profit -h
```

查看当前 Git 工作区时注意：本项目经常会有本地配置、缓存、账号和数据文件变化。不要为了“干净”回退不理解的状态文件，更不要重置数据库状态来掩盖执行器推进问题。
