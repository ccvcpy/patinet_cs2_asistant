# 独立老挂单检查真实验收记录

## 运行

- 任务：`stale_listing_recheck`
- 一次性维护授权：`stale-maint-e6cfeba3593a`
- 实际运行：`2026-08-07T01:20:28.659493Z` ～ `2026-08-07T01:20:52.297Z`
- 汇总：账号 2 个、到期 22 笔、实查 2 笔、撤单尝试 2 笔、撤单成功 2 笔、撤单失败 0 笔、活跃挂单未匹配 20 笔

## 真实撤单对象

| operation | market | assetId | listingId | 撤单原因 | 结果 |
|---:|---|---:|---:|---|---|
| 24943 | Kilowatt Case | 53071696184 | 554657164592522803 | listed more than 48 hours and no longer at market floor | canceled；asset locked |
| 24944 | Kilowatt Case | 53071696185 | 554657164592524819 | listed more than 48 hours and no longer at market floor | canceled；asset locked |

撤单前，本地流水记录了同一账号的有效 Steam 活跃挂单证据（`activeVerifiedAt=2026-08-06T19:04:22Z`）。本轮重新读取到 Steam CNY 卖一 `1.64`、C5 `price_batch` `0.97`、当前比例 `0.6644199682`、允许上限 `0.705`，挂价 `1.68` 不在最低价保护内，因此才进入撤单。

## Steam 与通知证据

- Steam `market/removelisting`：两次请求均 HTTP 200。
  - `steamreq_159650c2bba44c218165d43b332ef9e1`
  - `steamreq_d0f61d8be0754f14b9cfbba3b2c73ae0`
- ServerChan：`2026-08-07T01:20:54.045Z` 日志为 `operation=serverchan_notify`、`message=ServerChan 通知已发送`，事件键为 `stale-listing-recheck:stale-8c8c1964d0224b9a`。
- 撤单后对账号 `fabc498d` 做只读 Steam `mylistings` 完整快照：`complete=true`、`pagesScanned=1`、`actualActiveCount=0`、目标两个 listing 均无匹配。

## 截图边界

当前 Windows 环境无法取得可用的 Steam 页面截图：Steam 客户端 `mylistings` 内容区黑屏，Edge 页面显示 `ERR_BLOCKED_BY_CLIENT`。因此目录中的 `steam-before-*.png`、`steam-after-*.png`（若存在）只记录取证阻塞，不能冒充有效的“我的在售”前后截图。日志、HTTP 200、数据库终态和撤单后 Steam 快照均已保留；UI 截图需要解除浏览器拦截或由用户在正常 Steam/Edge 页面手动刷新后再补。
