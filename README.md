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

2. `docs/05_底仓执行口径.md`
   固定 `list / guadao` 和 `transfer` 的业务含义。

3. `data/strategy_config.json`
   运行时真实策略配置源，不是代码里的默认值。

4. `config/accounts.json`
   本地 Steam/C5 账号配置。里面可能含加密后的敏感信息，不要外发。

5. `docs/使用说明.md`
   本地操作便签，可能包含临时账号示例或个人记录，不作为公开规范文档。

`docs/01_*` 到 `docs/04_*` 主要记录早期需求和接口选型背景。它们和当前执行口径不一致时，以 `AGENTS.md`、`docs/05_底仓执行口径.md` 和真实代码为准。

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
  "executionEnabled": true,
  "autoListEnabled": true,
  "autoRebuyEnabled": true,
  "dryRun": false,
  "guadaoItemScope": "case_only",
  "guadaoMaxListingRatio": 0.69,
  "listingWallMinCount": 20,
  "listingPriceOffset": 0.01,
  "caseListingPriceOffset": -0.01,
  "caseMaxOpenGuadaoCount": 100,
  "maxListPerCycle": 10
}
```

也就是说，当前本地配置默认是真实执行态。正式运行前必须先确认这些值，尤其是：

- `executionEnabled`
- `dryRun`
- `autoListEnabled`
- `autoRebuyEnabled`
- `guadaoItemScope`
- `guadaoMaxListingRatio`
- `maxListPerCycle`

`guadaoItemScope` 当前只按以下两类理解：

- `case_only`：只允许广义箱子进入挂刀候选。
- `non_case_only`：只允许非箱子进入挂刀候选。

旧配置如果写 `all`，按当前约定应视作 `case_only` 处理，不要重新扩展为全品类，除非重新确认交易口径。

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

只有当：

```text
补仓比例 <= guadaoMaxListingRatio
```

才允许继续补仓。C5 网络临时错误应延迟重试，不应误判成永久失败。

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

## 开发与测试

涉及执行器、Steam、C5、补仓、状态机的改动，先读：

```text
AGENTS.md
docs/05_底仓执行口径.md
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
