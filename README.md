# patinet_cs2_asistant

当前主线只聚焦一个核心模块：基于全部 `C5` 绑定库存，计算做T收益率，并通过独立提醒模块输出高收益快报和 `15:30` 固定提醒。

## 快速开始

### 1. 安装依赖

```powershell
pip install -e .
```

如果你不想用可编辑安装，也可以先安装项目依赖后直接运行 `python .\main.py ...`。

### 2. 配置环境变量

至少准备这些变量中的一部分：

```powershell
$env:C5GAME_API_KEY="..."
$env:STEAMDT_API_KEY="..."
$env:CSQAQ_API_KEY="..."
$env:SERVERCHAN_SENDKEY="..."
```

说明：

1. `C5GAME_API_KEY` 用于拉取绑定 Steam 账号和库存。
2. `CSQAQ_API_KEY` 用于批量查询 Steam 价格主来源。
3. `STEAMDT_API_KEY` 用于 Steam 价格兜底。
4. `SERVERCHAN_SENDKEY` 只有在你要推送提醒时才需要。

### 3. 初始化本地数据

第一次使用建议先跑这两步：

```powershell
python .\main.py init-db
python .\main.py import-catalog
```

作用：

1. `init-db` 会创建本地 SQLite 数据库。
2. `import-catalog` 会把本地 SteamDT 基础数据导入数据库，方便后续搜索和辅助映射。

### 4. 直接开始扫描

```powershell
python .\main.py t-profit scan --top 10 --min-price 10
python .\main.py t-profit scan --bottom 10 --min-price 10
python .\main.py t-profit missing-steam
```

### 5. 配置并运行提醒

```powershell
python .\main.py notify t-profit --configure
python .\main.py notify t-profit --show-config
python .\main.py notify t-profit --once
python .\main.py notify t-profit
```

旧命令 `t-yield` 和 `notify t-yield` 仍兼容，但不再作为主入口展示。

## `data` 文件夹怎么来

不需要手工创建完整内容，程序会在运行时自动生成它需要的文件。

常见情况：

1. 第一次执行扫描或提醒时，如果缺少 `data/`，程序会自动创建。
2. 库存缓存会自动写到 `data` 相关文件里。
3. 缺失 Steam 价格的记录会自动写入 `data/c5_t_yield_missing_steam_prices.json`。
4. 提醒配置和提醒状态也会自动写入 `data/`。

也就是说，正常使用时你只需要：

```powershell
python .\main.py init-db
python .\main.py t-profit scan --top 10
```

后续相关本地数据会自动落盘。

## 做T公式

```text
折算比 = C5最低售价 / (0.869 * Steam最低售价)
做T收益率 = 折算比 * 0.869 - 0.73
```

`--top` 按收益率从高到低输出，`--bottom` 按收益率从低到高输出，方便快速查看低收益区间。

## 扫描命令的库存筛选

扫描命令的 `--inventory-filter` 只保留三类：

1. `all`：全部
2. `all_cooldown`：全冷却
3. `has_tradable`：存在不冷却

兼容说明：

1. 旧值 `mixed_only`
2. 旧值 `tradable_only`
3. 旧值 `cooldown_only`

现在仍然能用，但 help 不再主推。

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

提醒里的库存范围只分三类：

1. `all`：全部
2. `all_cooldown`：全冷却
3. `not_all_cooldown`：存在不冷却

ServerChan 推送内容已压缩成更适合手机阅读的格式，同时保留账号来源摘要。

## 常用命令

```powershell
python .\main.py -h
python .\main.py t-profit -h
python .\main.py t-profit scan -h
python .\main.py notify -h
python .\main.py notify t-profit -h
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
