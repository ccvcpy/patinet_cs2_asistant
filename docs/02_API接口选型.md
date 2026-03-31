# CS2 理财助手接口选型说明 v1.0

## 1. 总体结论

v1.0 的接口选型不再追求大而全，而是专攻一个最赚钱、优先级最高的模块：**基于库存辅助挂刀倒余额 / 做 T 收益率 Top10**。

v1.0 最适合的分工如下：

1. `C5` 负责绑定账号识别、库存拉取、C5 最低售价、交易执行。
2. `SteamDT` 负责 `marketHashName` 映射、Steam 最低售价、批量/单品价格补齐。
3. `CSQAQ` 负责补充 `Steam` 价格、补缺诊断，以及后续的系列/指数分析。

因此 v1.0 的最稳方案不是三家一起重度接入，而是：**`C5` 负责库存和 C5 价，`SteamDT + CSQAQ` 负责 Steam 价补全与兼容降级。**

## 2. 功能到接口的映射

### 2.1 建立标的主表

推荐主接口：

1. `SteamDT - 获取steam饰品基础信息`
   - 接口：`GET /open/cs2/v1/base`
   - 作用：拿到 `中文名 / marketHashName / platformList.itemId`
   - 备注：每天只能调用一次，必须本地缓存。

补充接口：

1. `CSQAQ - 获取单件饰品详情`
   - 接口：`GET /api/v1/info/good`
   - 作用：补充 `market_hash_name`、平台价格、涨跌幅等分析字段。

为什么这样选：

1. `SteamDT /open/cs2/v1/base` 最适合做主映射表。
2. `CSQAQ` 更适合补市场分析字段，不适合当主映射源。
3. `C5` 多数交易接口默认要求你已经知道 `marketHashName` 或 `itemId`。

### 2.2 单个饰品价格监控

推荐组合：

1. `SteamDT - 通过marketHashName查询饰品价格`
   - 接口：`GET /open/cs2/v1/price/single`
   - 作用：拿多平台的实时价格快照，适合做轻量轮询。
2. `SteamDT - 通过marketHashName批量查询饰品价格`
   - 接口：`POST /open/cs2/v1/price/batch`
   - 作用：适合批量轮询观察池。
3. `SteamDT - 通过MarketHashName查询所有平台近7天均价`
   - 接口：`GET /open/cs2/v1/price/avg`
   - 作用：适合做偏离均价提醒。
4. `C5 - MarketHashNames批量查询在售最低价和数量`
   - 接口：`POST /merchant/product/price/batch`
   - 作用：直接拿 C5 最低价和在售数量，适合交易触发前判断。
5. `C5 - 求购最高价`
   - 接口：`GET merchant/purchase/v1/max-price`
   - 作用：补充买盘强弱和安全边际。
6. `CSQAQ - 批量获取饰品出售价格数据`
   - 接口：`POST /api/v1/goods/getPriceByMarketHashName`
   - 作用：补充批量价格快照，适合大观察池。

建议：

1. 日常轮询优先用 `C5 + SteamDT`。
2. 当 `SteamDT` 批量接口漏掉 `Steam` 价格时，立刻用 `CSQAQ` 按 `marketHashName` 补齐。
3. 做趋势判断时再叠加 `CSQAQ` 其它分析接口。

### 2.3 系列、武器箱、印花、篮子指数监控

如果是平台已有系列：

1. `CSQAQ - 获取首页相关数据`
   - 接口：`GET /api/v1/current_data`
   - 作用：直接拿官方指数和子指数。
2. `CSQAQ - 获取指数K线图`
   - 接口：`GET /api/v1/index_chart`
   - 作用：看指数历史走势。
3. `CSQAQ - 获取热门系列饰品列表`
   - 接口：`POST /api/v1/info/get_series_list`
   - 作用：拿系列列表。
4. `CSQAQ - 获取单件热门系列饰品详情`
   - 接口：`GET /api/v1/info/get_series_detail`
   - 作用：看某个系列的构成和走势。

如果是用户自定义篮子：

1. 用 `SteamDT price/batch` 或 `C5 price/batch` 采集篮子内每个饰品价格。
2. 在本地计算自定义指数，不依赖平台提供现成指数。

结论：

1. 官方系列或大盘指数，用 `CSQAQ` 最省事。
2. 自定义篮子指数，要自己在本地算。

### 2.4 单个饰品的趋势与辅助分析

推荐接口：

1. `CSQAQ - 获取单件饰品详情`
   - 接口：`GET /api/v1/info/good`
   - 作用：拿 1 日、7 日、15 日、30 日、90 日等涨跌幅。
2. `CSQAQ - 获取单件饰品存世量走势`
   - 接口：`GET /api/v1/info/good/statistic`
   - 作用：观察供给变化。
3. `CSQAQ - 获取成交量数据信息`
   - 接口：`POST /api/v1/info/vol_data_info`
   - 作用：看哪些品种成交更活跃。
4. `CSQAQ - 获取成交量图表/磨损信息`
   - 接口：`POST /api/v1/info/vol_data_detail`
   - 作用：适合分析某类武器磨损分布和成交价带。
5. `SteamDT - 通过MarketHashName查询所有平台近7天均价`
   - 接口：`GET /open/cs2/v1/price/avg`
   - 作用：补充 Steam 和其它平台的短期均值。

### 2.5 买入提醒、卖出提醒、多阈值提醒

提醒本身不是平台接口能力，而是本地规则引擎能力。

最适合的底层数据来源：

1. 单品价格：`C5 price/batch`、`SteamDT price/single`、`SteamDT price/batch`
2. 涨跌幅：`CSQAQ info/good`
3. 系列指数：`CSQAQ current_data`、`CSQAQ index_chart`
4. 挂刀比例：`CSQAQ exchange_detail`

因此提醒模块建议自己做，接口只负责供数。

通知渠道已确认：

1. 第一阶段使用 `企业微信 ServerChan` 推送提醒。

### 2.6 仓位和空仓记录

这个模块不建议依赖单个平台接口做主账本。

原因：

1. API 可以告诉你当前余额、在售、订单、库存。
2. API 不能完整表达你的主观策略，比如“卖飞后等 915 再接”。
3. 真实成本、加仓逻辑、分批减仓、手动场外交易也需要本地统一记录。

推荐接口只做辅助导入：

1. `C5 - 查询账号余额[new]`
2. `C5 - 查询用户 steam 信息`
3. `C5 - 库存列表`
4. `C5 - 查看在售列表`
5. `C5 - 订单详情`
6. `C5 - 买家订单状态列表`
7. `C5 - 卖家订单列表`

结论：

1. 仓位账本本地维护。
2. C5 接口只负责同步现状和校验。
3. 第一阶段成本由用户人工录入，系统不自动推算持仓成本。

### 2.7 C5 一键交易与自动执行

优先接口：

1. `C5 - 快速购买`
   - 接口：`POST /merchant/trade/v2/quick-buy`
   - 作用：最接近“一键成交”。
   - 优点：支持按 `itemId` 或 `marketHashName` 买，且可带 `maxPrice` 和 `delivery`。
2. `C5 - 在售列表查询〖new〗`
   - 接口：`POST /merchant/market/v2/products/list`
   - 作用：按 `itemId` 拿当前在售列表。
3. `C5 - 在售列表搜索〖new〗`
   - 接口：`POST /merchant/market/v2/products/search`
   - 作用：适合更复杂的检索。
4. `C5 - 某个hashName符合条件的在售列表`
   - 接口：`POST /merchant/market/v2/products/condition/hash/name`
   - 作用：可按 `marketHashName`、`maxPrice`、`delivery`、磨损等条件搜索。
   - 备注：文档标记为“将废弃”，实现时优先新接口。
5. `C5 - 在售饰品改价`
   - 接口：`POST /merchant/sale/v1/modify`
6. `C5 - 在售下架接口`
   - 接口：`POST /merchant/sale/v1/cancel`
7. `C5 - 查看在售列表`
   - 接口：`GET /merchant/sale/v1/search`

结论：

1. “提醒后手动确认再快速购买”是第一阶段最稳的方案。
2. 卖出同样采用“提醒后用户确认”的模式。
3. 真正全自动下单可以做，但应放到后面，并加预算、频率和余额风控。

### 2.8 基于库存的挂刀倒余额 / 做T收益率模块

这是 v1.0 的核心接口组合。

推荐主接口：

1. `C5 - 查询用户 steam 信息`
   - 接口：`GET /merchant/account/v1/steamInfo`
   - 作用：列出所有已绑定的 `steamId` 与对应昵称。
2. `C5 - 库存列表`
   - 接口：`GET /merchant/inventory/v2/{steamId}/{appId}`
   - 作用：逐个拉取每个绑定 `Steam` 账号库存，再在本地汇总。
3. `C5 - MarketHashNames批量查询在售最低价和数量`
   - 接口：`POST /merchant/product/price/batch`
   - 作用：获取候选饰品类型的 `C5` 最低售价。
   - 备注：库存做 T 模块里，优先使用库存接口自带 `price`，这里主要作为补充和通用监控价格源。
4. `CSQAQ - 批量获取饰品出售价格数据`
   - 接口：`POST /api/v1/goods/getPriceByMarketHashName`
   - 作用：按 `marketHashName` 批量补齐 `Steam` 最低售价。
5. `SteamDT - 通过marketHashName批量查询饰品价格`
   - 接口：`POST /open/cs2/v1/price/batch`
   - 作用：继续保留为批量主查询和兼容来源。
6. `SteamDT - 通过marketHashName查询单个饰品价格`
   - 接口：`GET /open/cs2/v1/price/single`
   - 作用：当批量接口限流、失败或单品补查时，作为兜底。

本模块在本地统一做这些计算：

1. `挂刀比例 = C5饰品最低售价 ÷ (0.869 × Steam饰品最低售价)`
2. `做T收益率 = 挂刀比例 × 0.869 - 0.73`

本模块统一输出字段：

1. 饰品名称。
2. `marketHashName`。
3. 对应 `steamId`。
4. 对应 `nickname`。
5. 做 T 收益率（百分比）。
6. 挂刀比例。
7. `C5` 最低售价。
8. `Steam` 最低售价。

实现建议：

1. 所有库存先在本地按 `marketHashName` 聚合，同类型饰品只算一个“饰品类型”。
2. `C5` 价格优先使用库存查询结果自带价格，避免为了做 T 再重复依赖其它价格源。
3. `Steam` 价格优先批量查询，并在缺失时自动切到补源。
4. 如果出现“有 C5 价格但没有 Steam 价格”，要把问题单独记录，并提供脚本查看。
5. v1.0 默认直接输出“做 T 收益率前十”，并支持 `Top N + C5价格 > xx` 的提醒过滤。
6. 挂刀倒余额候选和做 T Top10 共用同一批库存与价格数据。
7. 批量价格接口要配缓存与降级，避免被限流卡死主链路。

### 2.9 检视、磨损、花纹补充

如果未来你对高端品、磨损区间、花纹、贴纸溢价也要做辅助判断，最适合的还是 `SteamDT`：

1. `通过检视链接查询磨损度相关数据`
2. `通过ASMD参数查询磨损度相关数据`
3. `通过检视链接生成检视图`
4. `通过ASMD参数生成检视图`

这部分不是第一阶段必须，但对高客单品很有价值。

## 3. 接口侧约束和设计注意事项

### 3.1 C5

1. 所有接口都要在 query 中带 `app-key`。
2. 购买功能依赖账户余额。
3. 新在售接口目前需要设置 IP 白名单。
4. 新在售接口有独立限流，不能高频乱轮询。

### 3.2 SteamDT

1. 所有接口都要在 Header 中带 `Authorization: Bearer {API_KEY}`。
2. `base` 接口每天只能调用一次，必须落地缓存。
3. `price/batch` 权限不高，适合低频批量，不适合超高频扫盘。

### 3.3 CSQAQ

1. 使用 `ApiToken` 鉴权。
2. 文档说明需要绑定白名单 IP。
3. 本项目环境变量统一使用 `CSQAQ_API_KEY` 或 `CSQAQ_API_TOKEN` 来存这个 `ApiToken`。
4. 更适合做分析和指数，也适合在本项目里承担 `Steam` 价格补源，不适合做交易执行。

## 4. 推荐的第一阶段技术组合

v1.0 最推荐先接这些接口：

1. `C5 /merchant/account/v1/steamInfo`
2. `C5 /merchant/inventory/v2/{steamId}/{appId}`
3. `C5 /merchant/product/price/batch`
4. `CSQAQ /api/v1/goods/getPriceByMarketHashName`
5. `SteamDT /open/cs2/v1/base`
6. `SteamDT /open/cs2/v1/price/batch`
7. `SteamDT /open/cs2/v1/price/single`
8. `C5 /merchant/trade/v2/quick-buy`
9. `C5 /merchant/sale/v1/search`

原因很简单：

1. 先把“拉全账号库存 -> 聚合类型 -> 补齐 C5/Steam 最低价 -> 算挂刀比例 -> 算做 T 收益率 -> 输出前十”这条最赚钱的链路打通。
2. 这组接口最贴近你当前真实交易动作，也最直接服务库存辅助挂刀 / 做 T 模块。
3. `C5` 当前公开接口里没有方便直接拿 `Steam` 最低售价的能力，所以 `Steam` 价格补源要靠 `SteamDT + CSQAQ` 组合解决。
4. `CSQAQ` 已经进入 v1.0 主链路，但只承担 `Steam` 价格补源和缺价诊断，不参与交易执行。
