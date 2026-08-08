# Steam 求购原单改价接口实测

- 状态：`dry_run`
- 账号：`ropzx55x`
- 物品：`Glove Case`
- 初始价：`21 CNY 分`
- 提高价：`22 CNY 分`
- 普通重复 createbuyorder：`未执行`
- createbuyorder + 旧 buy_orderid：`未执行`
- 最终判断：`no Steam request sent`
- 清理确认：`False`

## 证据边界

本报告只根据 createbuyorder 响应、mylistings 中的远端订单 ID/价格/数量、官方 market history 与钱包快照判断；不把 orderbook 当作订单终态证据。
