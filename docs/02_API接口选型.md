# CS2 理财助手 API 接口选型说明 v2.1

## 1. 当前总原则

接口选型只服务一个目标：

让做T扫描和提醒链路稳定拿到 C5 价格、尽可能稳定拿到 Steam 价格，并且在缺价时可诊断。

## 2. 最终价格源方案

当前最终方案：

```text
C5(库存/C5价) + CSQAQ(Steam主价) + SteamDT(Steam兜底) + ServerChan(推送)
```

## 3. Steam 官方 API 判断

当前没有找到一个适合本项目直接拿 CS2 Community Market 最低售价的 Steam 官方正式接口。

因此不再把 Steam 官方接口当作当前主线方案。

## 4. 当前各接口职责

### 4.1 C5

1. `GET /merchant/account/v1/steamInfo`
2. `GET /merchant/inventory/v2/{steamId}/{appId}`
3. `POST /merchant/product/price/batch`

职责：

1. 绑定账号识别。
2. 库存拉取。
3. C5 最低售价补源。

### 4.2 CSQAQ

1. `POST /api/v1/goods/getPriceByMarketHashName`
2. `POST /api/v1/sys/bind_local_ip`

职责：

1. Steam 价格主来源。
2. 价格补齐。

### 4.3 SteamDT

1. `POST /open/cs2/v1/price/batch`
2. `GET /open/cs2/v1/price/single`
3. `GET /open/cs2/v1/base`

职责：

1. Steam 价格兜底。
2. 单品补查。
3. 基础目录数据。

## 5. 当前事实结论

实测结果：

1. 在启用 `CSQAQ_API_KEY` 后，缺失 Steam 价格的饰品数量从 41 个降到了 3 个。
2. 剩余缺失项主要是特殊品类，如 `C4 Explosive`、部分 `Graffiti`。
3. 这些缺失项不是主扫描链路的大面积失败。
4. `CSQAQ` 确实存在 429 限流现象，但当前批量扫描链路可正常工作。

## 6. 当前缺价策略

如果出现“有 C5 价格但没有 Steam 价格”：

1. 不进入收益率结果。
2. 写入本地缺价文件。
3. 在输出或提醒里标明缺失数量。
