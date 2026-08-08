# Steam 最高求购撮合与 Orderbook 延迟实验

- 生成时间：2026-07-22T17:27:44.108449+08:00
- 模式：真实执行
- 是否中止：是（第一组安全完成；后两组未下单）
- 中止原因：`donkzymeng` 的 orderbook 会话返回美元盘口，而其他账号返回人民币盘口；随后用于核对该会话的 `/market/` 请求收到 HTTP 429。按实验安全规则停止后续组。

## control — Five-SeveN | Desert Seal (Field-Tested)

- 最终买家：vnuzl692
- 结论分类：existing_high_bid_matched_before_public_listing
- A 求购单：8576414496
- C 求购单：-
- B listing：508493999443938217
- 官方 purchaseId：508493999443938218
- 关键延迟：`{"aOrderToOrderbookBid": 9.378, "aMylistingsToOrderbookBid": 8.921, "bSubmitToOfficialPurchase": 1.609, "firstCrossToTerminal": null, "officialPurchaseToLastLowAsk": null, "officialPurchaseToLastABid": 6.632}`
- 错误：-

## 当前能得出的结论

- 已稳定公开的最高求购 A 确实优先吃掉了价格不高于 A 的新卖单 B。
- B 的卖单获得了 listingId，但人民币观察线程没有在公开 orderbook 中看到这笔 ¥0.40 低价卖单，说明撮合快于公开卖盘传播。
- A 的旧最高求购在官方成交时间后仍被观察到约 6.632 秒，证明 orderbook 的买盘也可能短暂滞后。
- 因后两组未执行，目前还不能判断后来更高求购或 buylisting 能否插队。
