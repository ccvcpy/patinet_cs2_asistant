# Steam 最高求购撮合与 Orderbook 延迟实验

- 生成时间：2026-07-22T19:45:08.517194+08:00
- 模式：真实执行
- 是否中止：是

## late_buylisting — XM1014 | Mockingbird (Battle-Scarred)

- 最终买家：-
- 结论分类：evidence_conflict_or_request_failure
- A 求购单：8576592775
- C 求购单：-
- B listing：494983200569027205
- 关键延迟：`{"aOrderToOrderbookBid": 6.284, "aMylistingsToOrderbookBid": 5.891, "bSubmitToOfficialPurchase": null, "firstCrossToTerminal": null, "officialPurchaseToLastLowAsk": null, "officialPurchaseToLastABid": null}`
- 错误：donkzymeng:buylisting_c:"您不能购买此物品，因为其他人已经购买了此物品。"; c_action:"您不能购买此物品，因为其他人已经购买了此物品。"; no unambiguous terminal state within the bounded wait
