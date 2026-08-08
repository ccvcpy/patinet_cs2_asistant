# Steam 最高求购撮合与 Orderbook 延迟实验

- 生成时间：2026-07-22T17:08:13.428988+08:00
- 模式：真实执行
- 是否中止：是

## control — XM1014 | Hieroglyph (Well-Worn)

- 最终买家：xiaodigu11
- 结论分类：evidence_conflict_or_request_failure
- A 求购单：8576405384
- C 求购单：-
- B listing：494983200568005669
- 关键延迟：`{"aOrderToOrderbookBid": 0.686, "aMylistingsToOrderbookBid": 0.33, "bSubmitToOfficialPurchase": 1.958, "firstCrossToTerminal": null, "officialPurchaseToLastLowAsk": null, "officialPurchaseToLastABid": 2.695}`
- 错误：official purchase receipt exists but the new asset is not in winner inventory
