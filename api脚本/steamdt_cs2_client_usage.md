# SteamDT CS2 查询脚本说明

## 脚本位置

`C:\Users\dmm\Desktop\patinet_cs2_asistant\api脚本\steamdt_cs2_client.py`

## 功能概览

这个脚本封装了 SteamDT 开放平台里和 CS2 饰品相关的 3 个接口，并且按你的要求使用 `requests` 发请求：

1. 获取 Steam 饰品基础信息
2. 通过 `marketHashName` 查询单个饰品价格
3. 通过 `marketHashName` 批量查询饰品价格

脚本支持命令行参数执行。运行时你只需要提供 API Key 和你要查的参数。

## 通用要求

### 依赖安装

脚本使用 `requests`，首次使用前请安装：

```bash
pip install requests
```

### 服务地址

`https://open.steamdt.com`

### 认证方式

所有接口都需要在请求头里带：

`Authorization: Bearer {YOUR_API_KEY}`

脚本固定从系统变量读取 API Key：

- 系统变量名：`STEAMDT_API_KEY`

脚本运行时，你只需要提供接口本身需要的业务参数，不需要再传 `--api-key`。

### 通用参数

- `--timeout`
  - 含义：请求超时时间，单位秒
  - 必填：否
  - 默认值：`30`
- `--output`
  - 含义：输出 JSON 文件路径
  - 必填：否
  - 不传时：直接打印到终端
  - 传相对路径时：会写到 `api脚本\output\` 目录下

## 接口 1：获取 Steam 饰品基础信息

### 对应接口

- 方法：`GET`
- 路径：`/open/cs2/v1/base`

### 作用

获取 Steam 饰品基础信息列表，返回每个饰品的名称、`marketHashName`，以及各个平台对应的商品 ID。

### 频率限制

文档标注：`每日 1 次`

建议：

- 拿到结果后保存
- 不要高频重复调用

### 返回重点字段

- `data[].name`
  - 饰品名称
- `data[].marketHashName`
  - 饰品 Hash 名称
- `data[].platformList[].name`
  - 平台名称
- `data[].platformList[].itemId`
  - 平台饰品 ID

### 示例命令

```bash
python steamdt_cs2_client.py base --output steamdt_base.json
```

## 接口 2：通过 marketHashName 查询单个饰品价格

### 对应接口

- 方法：`GET`
- 路径：`/open/cs2/v1/price/single`

### 请求参数

- `marketHashName`
  - 位置：Query 参数
  - 类型：`string`
  - 必填：是
  - 作用：指定要查询价格的饰品

### 作用

根据一个 `marketHashName` 查询该饰品在各个平台上的价格信息。

### 频率限制

文档标注：`每分钟 60 次`

### 返回重点字段

- `data[].platform`
  - 平台名称
- `data[].platformItemId`
  - 平台商品 ID
- `data[].sellPrice`
  - 在售价格
- `data[].sellCount`
  - 在售数量
- `data[].biddingPrice`
  - 求购价格
- `data[].biddingCount`
  - 求购数量
- `data[].updateTime`
  - 更新时间

### 示例命令

```bash
python steamdt_cs2_client.py price-single --market-hash-name "AK-47 | Redline (Field-Tested)"
```

## 接口 3：通过 marketHashName 批量查询饰品价格

### 对应接口

- 方法：`POST`
- 路径：`/open/cs2/v1/price/batch`
- 请求体：`application/json`

### 请求参数

- `marketHashNames`
  - 位置：JSON Body
  - 类型：`string[]`
  - 必填：是
  - 数量限制：`1` 到 `100`
  - 作用：批量传入要查询的饰品 `marketHashName`

### 作用

一次查询多个饰品在各个平台上的价格。

### 频率限制

文档标注：`每分钟 1 次`

### 返回重点字段

- `data[].marketHashName`
  - 当前饰品的 `marketHashName`
- `data[].dataList[]`
  - 当前饰品对应的平台价格列表
- `data[].dataList[].platform`
  - 平台名称
- `data[].dataList[].platformItemId`
  - 平台商品 ID
- `data[].dataList[].sellPrice`
  - 在售价格
- `data[].dataList[].sellCount`
  - 在售数量
- `data[].dataList[].biddingPrice`
  - 求购价格
- `data[].dataList[].biddingCount`
  - 求购数量
- `data[].dataList[].updateTime`
  - 更新时间

### 支持的脚本传参方式

方式 1：重复传多个 `--market-hash-name`

```bash
python steamdt_cs2_client.py price-batch ^
  --market-hash-name "AK-47 | Redline (Field-Tested)" ^
  --market-hash-name "AWP | Asiimov (Battle-Scarred)"
```

方式 2：从文本文件读取，每行一个

```bash
python steamdt_cs2_client.py price-batch --input-file market_hash_names.txt
```

文本文件示例：

```text
AK-47 | Redline (Field-Tested)
AWP | Asiimov (Battle-Scarred)
M4A1-S | Hyper Beast (Factory New)
```

## 命令帮助

查看总帮助：

```bash
python steamdt_cs2_client.py --help
```

查看子命令帮助：

```bash
python steamdt_cs2_client.py base --help
python steamdt_cs2_client.py price-single --help
python steamdt_cs2_client.py price-batch --help
```

## 脚本内部请求方式

脚本内部使用的是 `requests.request(...)`，不是 `http.client`。

批量查询对应的请求逻辑等价于：

```python
import requests

url = "https://open.steamdt.com/open/cs2/v1/price/batch"
payload = {
    "marketHashNames": [
        "AK-47 | Redline (Field-Tested)"
    ]
}
headers = {
    "Authorization": f"Bearer {STEAMDT_API_KEY}",
    "Content-Type": "application/json"
}

response = requests.request("POST", url, headers=headers, json=payload, timeout=30)
print(response.text)
```

## 返回结构说明

根据离线文档，接口统一返回类似结构：

```json
{
  "success": true,
  "data": [],
  "errorCode": 0,
  "errorMsg": "",
  "errorData": {},
  "errorCodeStr": ""
}
```

重点字段说明：

- `success`
  - 是否调用成功
- `data`
  - 业务数据
- `errorCode`
  - 错误码
- `errorMsg`
  - 错误信息
- `errorData`
  - 错误附加数据
- `errorCodeStr`
  - 错误码字符串描述

## 离线文档依据

本脚本和本文档基于仓库内离线文档整理，主要对应以下页面：

- `获取steam饰品基础信息`
- `通过marketHashName查询饰品价格`
- `通过marketHashName批量查询饰品价格`
- `接口权限列表(请优先查看)`
- `一分钟接入SteamDT开放平台`
