# patinet_cs2_asistant

CS2 理财助手第一版已经落下来了，当前是一个以 `Python + SQLite + CLI` 为核心的本地工具。

## 当前已实现

1. 从本地 `SteamDT` 基础数据导入饰品目录。
2. 搜索饰品并建立单品监控列表。
3. 创建自定义篮子，按“一篮子饰品价格直接相加”计算指数。
4. 手工记录仓位、成本、目标买价和目标卖价。
5. 为单品或篮子配置多条提醒规则。
6. 采集 `C5 + SteamDT + CSQAQ` 价格，计算挂刀比例并补齐 Steam 缺价。
7. 通过 `企业微信 ServerChan` 发送规则提醒和做 T 汇总提醒。
8. 提供 `C5` 快速购买、在售查询、库存查询、做 T 排行和做 T 提醒的 CLI 入口。

## 运行方式

建议先安装依赖：

```powershell
python -m pip install -e .
```

如果你不想先装包，也可以直接运行根目录脚本：

```powershell
python .\main.py --help
```

## 环境变量

至少准备这些变量中的一部分：

```powershell
$env:STEAMDT_API_KEY="..."
$env:C5GAME_API_KEY="..."
$env:CSQAQ_API_KEY="..."
$env:SERVERCHAN_SENDKEY="..."
```

说明：

1. `CSQAQ_API_KEY` 对应 `CSQAQ` 文档里的 `ApiToken` 接入令牌，也兼容读取 `CSQAQ_API_TOKEN`。
2. `CSQAQ` 官方文档在 [docs.csqaq.com](https://docs.csqaq.com/)。
3. 如果你要走 `CSQAQ`，先确认白名单 IP 已按文档绑定。

可选：

```powershell
$env:CS2_ASSISTANT_DB_PATH="C:\path\assistant.db"
$env:CS2_ASSISTANT_STEAMDT_BASE_PATH="C:\path\steamdt_cs2_base.json"
```

## 推荐的首次初始化流程

```powershell
python .\main.py init-db
python .\main.py import-catalog
python .\main.py search-item --keyword "二西莫夫"
python .\main.py watch-add --market-hash-name "AWP | Asiimov (Battle-Scarred)"
python .\main.py rule-add --target-type item --target-key "AWP | Asiimov (Battle-Scarred)" --metric c5_price --operator lte --threshold 500
python .\main.py rule-add --target-type item --target-key "AWP | Asiimov (Battle-Scarred)" --metric ratio --operator lte --threshold 0.92
python .\main.py check-market
```

## 规则说明

第一版支持这些 `metric`：

1. `c5_price`
2. `steam_price`
3. `c5_bid_price`
4. `ratio`
5. `basket_total`
6. `c5_change_pct`
7. `steam_change_pct`
8. `basket_change_pct`

`ratio` 使用固定公式：

```text
C5饰品价格 ÷ (0.869 × Steam饰品最低在售价)
```

涨跌幅类规则需要额外给 `--anchor-value`，例如：

```powershell
python .\main.py rule-add --target-type item --target-key "AWP | Asiimov (Battle-Scarred)" --metric c5_change_pct --operator lte --threshold -5 --anchor-value 580
```

## 常用命令

```powershell
python .\main.py watch-list
python .\main.py basket-add --name "上海贴纸篮子"
python .\main.py basket-add-item --basket-name "上海贴纸篮子" --market-hash-name "Sticker | Titan | Katowice 2015"
python .\main.py position-add --market-hash-name "AWP | Asiimov (Battle-Scarred)" --status holding --quantity 1 --manual-cost 560
python .\main.py notify-test
python .\main.py c5-sales
python .\main.py c5-inventory
python .\main.py c5-t-yield-top --top 10 --min-price 10
python .\main.py c5-t-yield-missing-steam
python .\main.py c5-t-yield-alert --top 10 --min-price 10
python .\main.py c5-quick-buy --market-hash-name "AWP | Asiimov (Battle-Scarred)" --max-price 500
```

## 做T提醒

当前做 T 提醒会做这些事：

1. 检查全部已绑定 `Steam` 账号的 `C5` 库存。
2. 按 `marketHashName` 聚合同类型饰品。
3. 优先使用库存里的 `C5` 价格，补齐 `Steam` 最低售价。
4. 按 `做T收益率` 排序，筛出 `Top N` 和 `C5价格 > xx` 的候选。
5. 通过 `ServerChan` 发一条汇总提醒。

建议把下面这条命令挂到计划任务里，每天早中晚各跑一次：

```powershell
python .\main.py c5-t-yield-alert --top 10 --min-price 10
```

如果运行时出现“有 C5 价格但缺少 Steam 价格”的情况，脚本会把问题写到 `data/c5_t_yield_missing_steam_prices.json`，也可以直接用命令查看：

```powershell
python .\main.py c5-t-yield-missing-steam
```

## 当前边界

第一版重点是把“监控、提醒、人工确认交易”这条链路跑通，所以暂时还没有做：

1. 无人值守自动买卖。
2. C5 自动改价策略。
3. 更复杂的 CSQAQ 系列/指数分析接入。
4. 图形界面。
