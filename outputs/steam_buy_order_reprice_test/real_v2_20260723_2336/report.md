# Steam 求购原单改价接口实测

- 状态：`completed`
- 账号：`ropzx55x`
- 物品：`Glove Case`
- 初始价：`21 CNY 分`
- 提高价：`22 CNY 分`
- 普通重复 createbuyorder：`original_order_unchanged`
- createbuyorder + 旧 buy_orderid：`original_order_unchanged`
- 最终判断：`no_in_place_reprice_behavior_observed`
- 清理确认：`True`

## 证据边界

本报告只根据 createbuyorder 响应、mylistings 中的远端订单 ID/价格/数量、官方 market history 与钱包快照判断；不把 orderbook 当作订单终态证据。
