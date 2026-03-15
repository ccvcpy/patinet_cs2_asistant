import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_URL = "https://open.steamdt.com"
DEFAULT_TIMEOUT = 30
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "output"


class SteamDTError(RuntimeError):
    pass


def get_api_key() -> str:
    api_key = os.environ.get("STEAMDT_API_KEY")
    if api_key:
        return api_key
    raise SteamDTError("缺少系统变量 STEAMDT_API_KEY。")


def request_json(
    method: str,
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> Any:
    try:
        import requests
    except ModuleNotFoundError as exc:
        raise SteamDTError("未安装 requests，请先执行: pip install requests") from exc

    headers = {
        "Authorization": f"Bearer {get_api_key()}",
        "Accept": "application/json",
    }
    if json_body is not None:
        headers["Content-Type"] = "application/json"

    try:
        response = requests.request(
            method=method.upper(),
            url=f"{BASE_URL}{path}",
            headers=headers,
            params=params,
            json=json_body,
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise SteamDTError(f"请求失败: {exc}") from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise SteamDTError(f"响应不是合法 JSON: {response.text}") from exc

    if not response.ok:
        raise SteamDTError(f"HTTP {response.status_code}: {json.dumps(data, ensure_ascii=False)}")

    return data


def read_market_hash_names(args: argparse.Namespace) -> List[str]:
    names: List[str] = []
    if args.market_hash_name:
        names.extend(args.market_hash_name)

    if args.input_file:
        content = Path(args.input_file).read_text(encoding="utf-8")
        for line in content.splitlines():
            line = line.strip()
            if line:
                names.append(line)

    unique_names: List[str] = []
    seen = set()
    for name in names:
        if name not in seen:
            seen.add(name)
            unique_names.append(name)
    return unique_names


def write_output(result: Any, output_path: Optional[str]) -> None:
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if output_path:
        output_file = Path(output_path)
        if not output_file.is_absolute():
            output_file = DEFAULT_OUTPUT_DIR / output_file
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(text, encoding="utf-8")
        print(f"结果已写入: {output_file}")
        return
    print(text)


def cmd_base(args: argparse.Namespace) -> Any:
    return request_json("GET", "/open/cs2/v1/base", timeout=args.timeout)


def cmd_price_single(args: argparse.Namespace) -> Any:
    return request_json(
        "GET",
        "/open/cs2/v1/price/single",
        params={"marketHashName": args.market_hash_name},
        timeout=args.timeout,
    )


def cmd_price_batch(args: argparse.Namespace) -> Any:
    market_hash_names = read_market_hash_names(args)
    if not market_hash_names:
        raise SteamDTError("批量查询至少需要一个 marketHashName。")
    if len(market_hash_names) > 100:
        raise SteamDTError("批量查询一次最多允许 100 个 marketHashName。")

    return request_json(
        "POST",
        "/open/cs2/v1/price/batch",
        json_body={"marketHashNames": market_hash_names},
        timeout=args.timeout,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SteamDT CS2 饰品查询脚本。API Key 从系统变量 STEAMDT_API_KEY 读取。"
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"请求超时时间，默认 {DEFAULT_TIMEOUT} 秒。",
    )
    parser.add_argument(
        "--output",
        help="输出 JSON 文件路径。不传则直接打印到终端。",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    base_parser = subparsers.add_parser(
        "base",
        help="获取 Steam 饰品基础信息。",
    )
    base_parser.set_defaults(handler=cmd_base)

    single_parser = subparsers.add_parser(
        "price-single",
        help="通过 marketHashName 查询单个饰品价格。",
    )
    single_parser.add_argument(
        "--market-hash-name",
        required=True,
        help="饰品的 marketHashName。",
    )
    single_parser.set_defaults(handler=cmd_price_single)

    batch_parser = subparsers.add_parser(
        "price-batch",
        help="批量查询多个 marketHashName 的饰品价格。",
    )
    batch_parser.add_argument(
        "--market-hash-name",
        action="append",
        help="可重复传入多个 marketHashName。",
    )
    batch_parser.add_argument(
        "--input-file",
        help="从文本文件读取 marketHashName，每行一个。",
    )
    batch_parser.set_defaults(handler=cmd_price_batch)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        result = args.handler(args)
        write_output(result, args.output)
        return 0
    except SteamDTError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
