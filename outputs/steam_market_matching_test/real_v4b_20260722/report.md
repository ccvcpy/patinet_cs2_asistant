# Steam 最高求购撮合与 Orderbook 延迟实验

- 生成时间：2026-07-22T19:30:33.927826+08:00
- 模式：真实执行
- 是否中止：是

## late_higher_buy_order — MAC-10 | Storm Camo (Factory New)

- 最终买家：-
- 结论分类：evidence_conflict_or_request_failure
- A 求购单：8576572177
- C 求购单：-
- B listing：-
- 关键延迟：`{"aOrderToOrderbookBid": 2.045, "aMylistingsToOrderbookBid": 1.661, "bSubmitToOfficialPurchase": null, "firstCrossToTerminal": null, "officialPurchaseToLastLowAsk": null, "officialPurchaseToLastABid": null}`
- 错误：donkzymeng:history_c:Steam request failed: GET /market/myhistory/render/: 429 Client Error: Too Many Requests for url: https://steamcommunity.com/market/myhistory/render/?query=&start=100&count=100&norender=1; Steam request failed: GET /market/myhistory/render/: 429 Client Error: Too Many Requests for url: https://steamcommunity.com/market/myhistory/render/?query=&start=100&count=100&norender=1
