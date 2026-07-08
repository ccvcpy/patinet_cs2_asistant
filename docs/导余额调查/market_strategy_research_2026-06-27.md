# CS2 市场与导余额方向研究（2026-06-27）

本文是一次方向性研究，不是执行参数变更。结论基于当前仓库、当前本地数据库、Steam 实时 orderbook、C5/ECO 公开 OpenAPI 文档、Steam/Valve 官方资料和部分社区/专业数据源。

## 0. 结论先行

最适合当前项目的主攻方向不是继续只卷箱子，而是：

1. 保持现有箱子执行器稳定，但承认 `0.69` 以内当前箱子机会不足。
2. 新增一个只读的“全品类机会雷达”，优先扫描贴纸、胶囊、高流动低价皮肤。
3. 在只读验证 24-48 小时后，做“非箱子白名单执行”，不要直接全品类自动跑。
4. C5 自动求购值得接入，尤其适合普通饰品和贴纸补仓；箱子不优先用求购。
5. ECO API 可以实现自动购买、自动求购、上架、订单处理，但要先完成签名/权限/余额/风控验证，小仓测试后再接生产。
6. 贴纸“求购 -> Steam 卖”有机会，但不能按“买来立刻卖”理解；Steam 交易保护/冷却意味着必须用底仓置换模型。
7. Dota2、TF2、Rust 等跨游戏先做只读雷达，不要直接复用 CS2 执行状态机。
8. 额度卡/汇率类因素只适合做成本参数，不适合作为项目主线。

## 1. 当前本地项目事实

### 1.1 当前配置

来自 `data/strategy_config.json`：

- `steamNetFactor = 0.869`
- `guadaoMaxListingRatio = 0.69`
- `guadaoItemScope = case_only`
- `listingWallMinCount = 20`
- `caseListingPriceOffset = -0.01`
- `listingPriceOffset = 0.01`
- `minPrice = 1.1`
- `caseMaxOpenGuadaoCount = 100`

当前配置意味着：生产执行仍只允许箱子进入挂刀候选；`0.69` 是硬上限，不是动态全局阈值。

### 1.2 API 与账号状态

本地账号配置只读检查结果：

- Steam 账号：5 个
- 带 Steam cookies 的账号：5 个
- 带 C5 API key 的账号：0 个

因此，本次无法用 C5 实时 API 拉当前 C5 价格，只能使用本地 C5 库存缓存和数据库历史。Steam 价格使用本地 cookie 调 Steam 官方 `orderbook` 实时读取。

### 1.3 当前库存最低挂刀比例

只读扫描：

- C5 价格源：`data/c5_inventory_all_cache.json`
- 缓存时间：`2026-06-27T12:59:46+00:00`
- Steam 价格源：Steam 官方 `orderbook`
- 扫描 pool 品种：209 个
- 因 `minPrice=1.1` 导致的假低比例过滤：108 个
- 剩余真实可评估项：101 个

当前真实可评估低比例主要来自非箱子：

| 品种 | 比例 | C5价 | Steam挂价 | 本地可交易 |
|---|---:|---:|---:|---:|
| Sticker \| w0nderful (Holo) \| Austin 2025 | 62.17% | 3.29 | 6.09 | 11 |
| Sticker \| Ancient Beast (Foil) | 63.29% | 13.81 | 25.11 | 6 |
| MP5-SD \| Gold Leaf (Minimal Wear) | 65.41% | 1.33 | 2.34 | 1 |
| Glock-18 \| Green Line (Well-Worn) | 66.39% | 1.95 | 3.38 | 1 |
| Antwerp 2022 Legends Sticker Capsule | 66.91% | 2.89 | 4.97 | 1 |
| Sticker \| Bolt Strike | 67.39% | 0.65 | 1.11 | 3 |

当前纯武器箱最低比例：

| 品种 | 比例 | C5价 | Steam挂价 | 本地可交易 |
|---|---:|---:|---:|---:|
| Fever Case | 71.88% | 3.86 | 6.18 | 52 |
| Revolution Case | 73.85% | 1.72 | 2.68 | 36 |
| Kilowatt Case | 78.81% | 1.13 | 1.65 | 339 |
| Dreams & Nightmares Case | 80.26% | 8.16 | 11.70 | 3 |
| Glove Case | 85.55% | 108.00 | 145.28 | 1 |

判断：当前 `case_only + 0.69` 下，箱子基本开不出新单。加速的有效空间在非箱子，尤其贴纸、胶囊、少量高流动低价皮肤。

### 1.4 历史成交速度

本地 `pool_operations` 统计：

- `sell_on_steam.sold`: 3611 条
- `rebuy_on_c5.completed`: 3612 条

卖出完成最多的品种：

- Kilowatt Case：3019 笔
- Revolution Case：344 笔
- Rio 2022 Legends Sticker Capsule：59 笔
- Fever Case：58 笔
- Copenhagen 2024 Champions Autograph Capsule：51 笔
- Antwerp 2022 Legends Sticker Capsule：50 笔

近 14 天平均卖出耗时：

| 品种 | 笔数 | 平均耗时 |
|---|---:|---:|
| Fever Case | 52 | 约 1.8 分钟 |
| Austin 2025 Legends Sticker Capsule | 4 | 约 35.5 分钟 |
| Revolution Case | 110 | 约 83.4 分钟 |
| Kilowatt Case | 839 | 约 157.9 分钟 |
| Rio 2022 Legends Sticker Capsule | 30 | 约 531.2 分钟 |
| Antwerp 2022 Legends Sticker Capsule | 37 | 约 561.5 分钟 |
| Copenhagen 2024 Champions Autograph Capsule | 39 | 约 1028.3 分钟 |

判断：

- Fever Case 很快，但当前比例约 0.72，超过 `0.69`。
- Kilowatt 历史量大，但当前比例偏高。
- 胶囊/贴纸有低比例机会，但卖出速度比热门箱子更分化，必须加流动性评分。

## 2. CS2 大盘与品类判断

### 2.1 大盘不是稳定套利市场，而是政策驱动市场

最近影响 CS2 市场结构的变化主要不是普通供需，而是 Valve 规则变动。

重要外部事实：

- Steam 社区市场正在改版，增加更好的资产数据、筛选和单个 listing 展示。这会提高 Steam 市场的信息效率，也会让低价好货更难被简单脚本捡漏。来源：Steam Community Market Updates。
- 2026 Cologne Major Shop 改成直接购买指定贴纸，价格按需求变动。这打破旧的 Major 贴纸胶囊供给模型。来源：Counter-Strike 官方 IEM Cologne 2026 新闻。
- Steam Trade Protection 使新交易物品在 7 天内不能转移/修改/消耗。对任何“买来马上转卖”的策略都是硬约束。来源：Steam Support Trade Protected Items。
- Steam 市场有 Steam Transaction Fee 和游戏特定费用，项目里的 `steamNetFactor=0.869` 仍然是必要口径。来源：Steam Community Market FAQ。

判断：后续项目不能只追“当前最低比例”，必须把 Valve 更新风险当成第一类风险。

社区资料口径：

- B站、贴吧、抖音、Reddit 都能看到 CS2 饰品、箱子、贴纸、大盘讨论，但这些更像情绪和叙事源，不是可执行价格源。
- B站和抖音内容偏短线观点、行情复盘、热点叙事，适合观察市场情绪是否过热。
- 贴吧内容噪声大，搜索结果和有效讨论混杂，只能辅助发现话题，不适合直接作为策略证据。
- Reddit `r/csgomarketforum` 适合观察海外玩家对 Valve 更新、Major 贴纸、箱子供应的预期变化，但仍不能替代 Steam orderbook、C5/ECO 实时价格和本地成交数据。

项目里应把这些社区源定位为“舆情标签”，例如：热点升温、恐慌、更新预期、争议品类，而不是直接触发买卖。

### 2.2 箱子

优点：

- 流动性强。
- 名称标准化，无磨损/模板复杂度。
- 适合自动执行。
- C5 补仓成功率通常好于冷门皮肤。

问题：

- 当前 `0.69` 内基本没有新机会。
- 如果为了速度放宽到 `0.72` 左右，Fever Case 这类高速品种会有机会，但导余额损耗会明显上升。

建议：

- 箱子执行器继续保留。
- 增加一个明确的 `speedModeMaxRatio`，不要偷偷改 `guadaoMaxListingRatio`。
- 速度模式只允许高流动箱子，并在日志里显示额外损耗。

### 2.3 胶囊

优点：

- 比箱子更容易出现低比例。
- 名称标准化，执行复杂度低。
- 历史中 Antwerp/Rio/Austin 胶囊已经有成交记录。

问题：

- 卖出速度比热门箱子慢。
- Major 贴纸系统变化后，新胶囊供应逻辑可能被削弱或改变。
- 部分胶囊/纪念包监控历史里出现过极端异常比例，必须重新用实时 Steam orderbook 校验。

建议：

- 胶囊应进入机会雷达。
- 自动执行只允许白名单。
- 每品种设置并发上限，不能和箱子共用 100 个大池。

### 2.4 贴纸

优点：

- 当前最低比例机会主要在贴纸。
- 单价低，适合小仓试错。
- Steam 买卖墙可提供较清晰的流动性信号。
- C5/ECO 求购可能把补仓成本压低。

问题：

- 低价贴纸很容易被 `minPrice=1.1` 造出假低比例。
- 新 Major 贴纸受需求定价影响，波动极大。
- 冷门贴纸卖单少，容易挂很久。
- 贴纸名、赛事、战队、选手热度变化快，不适合无脑全自动。

建议：

- 主攻“老 Major 高流动贴纸/稳定贴纸”，不追刚上线的极端热度贴纸。
- 做贴纸白名单，并引入：
  - Steam 卖单总量
  - Steam 求购总量
  - 买一/卖一价差
  - 最近成交速度
  - C5/ECO 补仓深度
  - 是否被 `minPrice` 钳制

### 2.5 普通皮肤

优点：

- 会出现很低比例。
- C5/ECO API 都支持按 hashName 或商品编号购买。
- 某些低价高流动皮肤适合做小额导余额。

问题：

- 磨损、模板、款式、贴纸附加价值会引入复杂性。
- 单件机会多，持续补仓困难。
- 高价皮肤不适合导余额，资金占用和波动风险过大。

建议：

- 只做低价、高流动、无特殊模板溢价的普通皮肤。
- 不碰高价刀/手套/稀有模板。
- 初期只读观察，不接自动执行。

### 2.6 刀、手套、高端皮肤

不建议作为导余额主线。

原因：

- 流动性不稳定。
- 个体差异大。
- 价格受模板、磨损、贴纸、收藏偏好影响。
- 2025 红转金/刀手炼金机制改变后，高端品类更受规则风险影响。

项目可以监控，但不应自动执行。

## 3. C5 API 可行性

### 3.1 当前项目已经接入的 C5 能力

`src/cs2_assistant/clients/c5game.py` 当前已有：

- `price_batch`
- `purchase_max_price`
- `goods_search`
- `normal_buy`
- `quick_buy`
- `buyer_order_status`
- `buyer_order_detail`
- `sale_create`
- `sale_cancel`
- `sale_modify`

`executor_buy.py` 已经用 `quick_buy` 做 C5 补仓：

- `max_price = steamListPrice * steamNetFactor * guadao_max_listing_ratio`
- 通过 `quick_buy` 按 hashName 和最高价买入。

判断：C5 普通自动买已经具备，不是新能力。

### 3.2 C5 自动求购

C5 官方 OpenAPI 文档明确有：

- 发起求购：`/merchant/purchase/v1/create`
- 取消求购
- 求购列表
- 求购详情
- 求购最高价

当前项目只接了“求购最高价”，没有接“发起求购/管理求购”。

判断：C5 自动求购技术上可实现，但需要新增一条独立状态机，不能塞进现有 `rebuy_on_c5`。

建议用途：

- 普通贴纸/普通皮肤补仓。
- 用于提前建低价补仓池。
- 不优先用于箱子，因为箱子快速购买/现货深度通常够用。

不建议：

- 不要让自动求购和自动补仓同时抢同一品种，必须做资金/库存限额。
- 不要在没有 C5 API key 和小仓验证前接生产。

## 4. ECO API 可行性

ECO Swagger 公开定义可访问：

- `/swagger-resources`
- `/swagger/docs/v1`

接口分组包括：

- Buy：购买相关接口
- Purchase：求购相关接口
- Selling：出售相关
- Order：订单相关接口
- Market：市场相关

已确认存在的关键端点：

- `/Api/open/buy/BuyByGoodsNum`：指定商品购买
- `/Api/open/buy/BuyByHashName`：指定类别购买
- `/Api/open/buy/P2PBuyOrder`：P2P 购买订单
- `/Api/open/purchase/PurchasePublish`：发布求购信息
- `/Api/open/purchase/PurchaseEdit`：编辑求购信息
- `/Api/open/purchase/PurchaseStop`：终止求购信息
- `/Api/Selling/PublishStock`：上架库存
- `/Api/open/order/UserAcceptOffer`：用户接受报价

ECO 请求模型里明确支持：

- `GameId`: CS2=730, Dota2=570, TF2=440, Rust=252490
- `HashName`
- `Price`
- `MaxCount`
- `TradeLink`
- `Timestamp`
- `Sign`
- `PartnerId`

判断：

- ECO 自动买饰品在接口层可实现。
- ECO 自动求购也可实现。
- ECO 不是“没有 API”的问题，而是签名、商户/Partner 权限、余额、风控、订单状态、报价确认的接入问题。

建议：

- 先做 ECO 只读市场价/在售/求购价同步。
- 再接 `BuyByHashName` 小仓购买。
- 最后再接求购和上架。
- ECO 不要直接接进当前 C5/Steam 执行状态机；先做独立 `external_market_orders`。

## 5. 贴纸求购战法

### 5.1 可行，但不能按“买来马上卖”设计

Steam 交易保护/冷却会限制买入后立刻转移或再次处理。对项目来说，正确模型不是：

- C5/ECO 求购买入
- 立刻 Steam 卖出

正确模型应该是：

- 先有一批可交易底仓贴纸
- Steam 先卖出旧底仓，形成余额
- C5/ECO 求购补回 replacement
- replacement 冷却/保护期结束后，回到底仓

这和当前 `list/guadao` 的设计一致。

### 5.2 策略核心

贴纸策略应该按“库存循环”做：

1. 建白名单：只选高流动、低价差、补仓深度足够的贴纸。
2. 先小仓持有可交易底仓。
3. Steam 按实时 orderbook 上架。
4. C5/ECO 用自动求购或快速买补仓。
5. 补仓价低于卖出税后到手的 `0.69` 或白名单阈值才执行。
6. 每品种限额，避免单个贴纸被市场变化套住。

### 5.3 不适合的贴纸

- 新 Major 动态需求定价刚上线的热门贴纸。
- 买卖墙薄、卖单少、求购少的选手贴纸。
- 被 `minPrice` 钳制造成假低比例的低价贴纸。
- 单价太高、单笔资金占用大的稀有贴纸。

## 6. 导余额还有哪些办法

### 6.1 当前最可行：非箱子白名单挂刀

这是当前最适合项目的方向。

原因：

- 和现有状态机最接近。
- 不需要改变“先卖 Steam、后补 C5”的安全闭环。
- 当前本地扫描已证明非箱子有低比例机会。
- 可以小仓逐步扩大。

### 6.2 速度模式箱子

可行，但成本更高。

例如 Fever Case 当前比例约 0.72，但历史卖出非常快。若允许速度模式：

- `guadaoMaxListingRatio = 0.69` 仍作为正常模式硬上限。
- `speedModeMaxRatio = 0.72` 只给高流动白名单品种。
- 日志必须显示本单额外损耗。

这适合“要速度，不惜多损耗一点”的场景。

### 6.3 Steam 低价余额买入再外部卖出

这就是当前项目里的 `transfer` 思路。

限制：

- Steam 新买资产不能立即作为外部卖出对象。
- 必须卖旧底仓，拿新买资产补回底仓。
- 只适合已有可交易底仓的品种。

这条路可以继续优化，但它不是“导余额”，而是赚差价/低价余额变现。

### 6.4 C5/ECO 自动求购补仓

这是中期最值得做的增强。

它不会直接加快 Steam 卖出，但会降低补仓成本，提高闭环成功率。

### 6.5 额度卡/汇率

不建议作为项目主线。

可以把它抽象成一个参数：

- `effectiveSteamBalanceCost`
- `balanceDiscount`
- `capitalCost`

但不要让项目围绕支付通道、额度、汇率漏洞设计。原因：

- 工程优势弱。
- 合规/账号风控风险高。
- 不可复现。
- 一旦规则变化，系统价值归零。

项目应该做的是识别市场价差和执行闭环，不是做支付通道套利。

## 7. 其他游戏方向

### 7.1 Dota2

优点：

- Steam 市场品类多。
- ECO OpenAPI 明确支持 `GameId=570`。
- 可能存在跨平台价差。

问题：

- 低价长尾极多，手续费吞噬严重。
- 部分物品交易/市场限制不同。
- C5/ECO 深度需要验证。
- 品类元数据和 CS2 不同，不能直接复用 CS2 分类器。

建议：只读雷达，不接自动执行。

### 7.2 TF2

优点：

- TF2 Key 是老牌交易货币。
- Steam 市场支持。
- ECO OpenAPI 明确支持 `GameId=440`。

问题：

- 真正套利多发生在第三方现金市场/交易社区，不一定适合 C5 + Steam 模型。
- Steam 市场费用仍然高。
- 价格稳定意味着价差也小。

建议：把 TF2 Key 当作“Steam 余额折价参考指标”，不作为第一阶段执行品种。

### 7.3 Rust

优点：

- ECO OpenAPI 明确支持 `GameId=252490`。
- Rust 皮肤有 Steam 市场和第三方市场。

问题：

- 国内平台深度、交易限制、市场节奏都要重新验证。
- 项目没有 Rust 元数据、分类、执行测试。

建议：放进跨游戏机会雷达，等 CS2 非箱子跑通后再研究。

## 8. 项目优化路线

### 阶段 1：全品类机会雷达

只读，不交易。

数据源：

- Steam official orderbook
- Steam pricehistory
- C5 price/list/purchase APIs
- ECO market/buy/purchase APIs
- 本地 `pool_operations`
- 本地 `inventory_assets`

输出字段：

- 当前挂刀比例
- Steam 卖单总量
- Steam 求购总量
- 买一/卖一价差
- 24h/7d 本地卖出速度
- C5 可买价和数量
- C5 求购最高价
- ECO 可买价和数量
- ECO 求购最高价
- 当前账号可交易库存
- 是否被 `minPrice` 钳制
- 是否高价值/低流动风险
- 推荐动作：执行 / 观察 / 排除

### 阶段 2：白名单非箱子执行

先只开放 3-5 个品种。

要求：

- 每品种并发上限。
- 总资金上限。
- 只允许有可交易底仓的品种。
- 不允许 `minPrice` 钳制项。
- 不允许 C5/ECO 价格过期。
- 不允许高价单件。
- 继续冻结每笔开单时的比例字段。

### 阶段 3：C5 自动求购

新增状态机，不要混入 `rebuy_on_c5`。

建议状态：

- `purchase_bid.open`
- `purchase_bid.filled`
- `purchase_bid.partial`
- `purchase_bid.cancelled`
- `purchase_bid.failed`

用途：

- 给贴纸/普通皮肤建低价补仓池。
- 降低 C5 补仓成本。
- 作为未来 `rebuy_on_c5` 的供应源。

### 阶段 4：ECO 接入

先只读，后小仓。

顺序：

1. ECO 签名与鉴权。
2. 市场价格读取。
3. 求购/在售读取。
4. 小仓 `BuyByHashName`。
5. 订单状态查询。
6. 求购发布/编辑/停止。
7. 上架与报价处理。

### 阶段 5：跨游戏雷达

不要直接执行。

先做：

- Dota2/TF2/Rust 价差扫描
- 成交量观察
- 平台支持深度
- 交易限制差异
- 小仓人工验证

## 9. 最终建议

当前最合适的项目方向：

1. 继续让箱子执行器跑，但不要指望 `0.69` 下显著提速。
2. 把研发主力转向“非箱子机会雷达 + 白名单执行”。
3. 优先试贴纸和胶囊，不优先试高价皮肤。
4. C5 自动求购值得做，尤其适合普通饰品和贴纸。
5. ECO API 值得接，但先只读和小仓，不能直接上生产。
6. 贴纸求购战法可行，但要用底仓循环，不要买来立即卖。
7. 跨游戏是第二阶段，不是当前主攻。
8. 额度卡/汇率只做参数，不做主线。

一句话：下一步最该造的是“跨平台、跨品类的低比例机会雷达”，再用白名单把少数高质量机会接入当前安全闭环。

## 10. 主要来源

- Steam Community Market FAQ: https://help.steampowered.com/en/faqs/view/61F0-72B7-9A18-C70B
- Steam Trade Protected Items: https://help.steampowered.com/en/faqs/view/365F-4BEE-2AE2-7BDD
- Steam Trade and Market Holds: https://help.steampowered.com/en/faqs/view/34A1-EA3F-83ED-54AB
- Steam Community Market Updates: https://steamcommunity.com/games/593110/announcements/detail/673994309884707671
- Counter-Strike IEM Cologne 2026: https://www.counter-strike.net/newsentry/672869045073084948
- C5 OpenAPI llms: https://opendoc.c5game.com/llms.txt
- C5 发起求购: https://opendoc.c5game.com/api-181496279.md
- C5 快速购买: https://opendoc.c5game.com/api-354375293.md
- C5 普通购买: https://opendoc.c5game.com/api-354331322.md
- C5 求购最高价: https://opendoc.c5game.com/api-221209660.md
- ECO Swagger resources: https://openapi.ecosteam.cn/swagger-resources
- ECO OpenAPI definition: https://openapi.ecosteam.cn/swagger/docs/v1
- Reddit r/csgomarketforum: https://www.reddit.com/r/csgomarketforum/
- Bilibili CSGO/CS2 饰品价格搜索: https://search.bilibili.com/all?keyword=CSGO%E9%A5%B0%E5%93%81%E4%BB%B7%E6%A0%BC
- Bilibili CS2 市场分析示例: https://www.bilibili.com/video/BV1MKPYzAEgv/
- Baidu Tieba csgo吧示例搜索结果: https://tieba.baidu.com/p/10805301479
- EsportFire CS2 indexes: https://esportfire.com/indexes
- CSGOStocks: https://csgostocks.de/
- cs2.sh price API: https://cs2.sh/
- CSFloat API docs: https://docs.csfloat.com/
- Douyin CS2 market search examples: https://www.douyin.com/search/cs2%E9%A5%B0%E5%93%81%E4%B8%80%E5%B9%B4%E8%B5%B0%E5%8A%BF
