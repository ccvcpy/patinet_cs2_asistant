# Steam 接口与 SteamID 清单

更新时间：2026-04-01

## 1. Steam / Steam 价格相关接口清单

### 1.1 Steam 官方文档

1. Steamworks `IEconMarketService`
   - 文档地址：https://partner.steamgames.com/doc/webapi/IEconMarketService
   - 说明：官方市场相关 Web API 文档，但不适合作为本项目的 CS2 Community Market 最低售价来源。

2. Steamworks `ISteamInventory::RequestPrices`
   - 文档地址：https://partner.steamgames.com/doc/api/isteaminventory#RequestPrices
   - 说明：偏向 Steam Inventory Service 的开发者价格体系，不是本项目要的 Community Market 最低售价接口。

3. Steamworks `IInventoryService::GetPriceSheet`
   - 文档地址：https://partner.steamgames.com/doc/webapi/IInventoryService
   - 说明：同样属于 Steam Inventory Service 价格表方向，不适合作为当前 CS2 饰品 Steam 价格主来源。

### 1.2 当前项目实际使用的 Steam 价格来源

1. CSQAQ 批量价格接口
   - 文档地址：https://docs.csqaq.com/api-283470032
   - 接口：`POST /api/v1/goods/getPriceByMarketHashName`
   - 用途：当前项目的 Steam 价格主来源。

2. CSQAQ IP 绑定接口
   - 文档地址：https://docs.csqaq.com/api-283470024
   - 接口：`POST /api/v1/sys/bind_local_ip`
   - 用途：按文档要求绑定本机 IP。

3. SteamDT 批量价格接口
   - 文档参考：当前仓库客户端实现 `src/cs2_assistant/clients/steamdt.py`
   - 接口：`POST /open/cs2/v1/price/batch`
   - 用途：Steam 价格兜底。

4. SteamDT 单品价格接口
   - 文档参考：当前仓库客户端实现 `src/cs2_assistant/clients/steamdt.py`
   - 接口：`GET /open/cs2/v1/price/single`
   - 用途：单品兜底。

5. C5 库存接口
   - 文档参考：当前仓库客户端实现 `src/cs2_assistant/clients/c5game.py`
   - 接口：`GET /merchant/inventory/v2/{steamId}/{appId}`
   - 用途：拉取绑定 Steam 账号库存。

6. C5 批量价格接口
   - 文档参考：当前仓库客户端实现 `src/cs2_assistant/clients/c5game.py`
   - 接口：`POST /merchant/product/price/batch`
   - 用途：补充 C5 最低售价。

## 2. 当前结论

1. Steam 官方公开文档里，没有找到适合本项目直接拿 CS2 Community Market 最低售价的正式接口。
2. 当前项目的 Steam 价格主路径已经定为：

```text
CSQAQ 主 -> SteamDT 兜底 -> 缺价记录
```

3. C5 当前只承担库存和 C5 价格来源，不承担 Steam 价格来源。

## 3. Steam 账号与 ID 清单

当前从 C5 绑定信息读到的 Steam 账号如下：

1. `76561199876741579`
   - 昵称：`johnsonkeith8261`

2. `76561198279977505`
   - 昵称：`超脱因果，太上忘情。`

3. `76561198745843420`
   - 昵称：`garrettclark1693`

4. `76561199119018953`
   - 昵称：`115`

## 4. 其他固定 ID

1. CS2 / CSGO 应用 `appId`
   - `730`

## 5. 环境变量名备忘

```text
STEAMDT_API_KEY
CSQAQ_API_KEY
CSQAQ_API_TOKEN
C5GAME_API_KEY
SERVERCHAN_SENDKEY
```
