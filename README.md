# patinet_cs2_asistant

当前主线只聚焦一个核心模块：基于全部 `C5` 绑定库存，计算做T收益率，并通过独立提醒模块输出高收益快报和 `15:30` 固定提醒。

## 当前推荐入口

```powershell
python .\main.py t-profit scan --top 10 --min-price 10
python .\main.py t-profit scan --bottom 10 --min-price 10
python .\main.py t-profit missing-steam

python .\main.py notify t-profit --show-config
python .\main.py notify t-profit --configure
python .\main.py notify t-profit --once
python .\main.py notify t-profit
```

旧命令 `t-yield` 和 `notify t-yield` 仍兼容，但不再作为主入口展示。

## 做T公式

```text
折算比 = C5最低售价 / (0.869 * Steam最低售价)
做T收益率 = 折算比 * 0.869 - 0.73
```

`--top` 按收益率从高到低输出，`--bottom` 按收益率从低到高输出，方便快速查看低收益区间。

扫描命令的库存筛选只保留三类：

1. `all`：全部
2. `all_cooldown`：全冷却
3. `has_tradable`：存在不冷却

## 价格源策略

```text
C5 负责库存和 C5 价格
CSQAQ 负责 Steam 价格主来源
SteamDT 负责 Steam 价格兜底
```

当前没有合适的 Steam 官方公开接口可直接拿到任意 CS2 饰品的最新社区最低售价，所以仍以第三方聚合为主。

## 提醒策略

当前只保留两类提醒：

1. 高收益快报
2. `15:30` 固定提醒

库存范围只分三类：

1. `all`：全部
2. `all_cooldown`：全冷却
3. `not_all_cooldown`：存在不冷却

ServerChan 推送内容已压缩成更适合手机阅读的两行结构。

## 环境变量

```powershell
$env:C5GAME_API_KEY="..."
$env:STEAMDT_API_KEY="..."
$env:CSQAQ_API_KEY="..."
$env:SERVERCHAN_SENDKEY="..."
```

## 缺失 Steam 价格

缺价记录会写入：

```text
data/c5_t_yield_missing_steam_prices.json
```

查看命令：

```powershell
python .\main.py t-profit missing-steam
```
