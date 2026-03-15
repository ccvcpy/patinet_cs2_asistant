import html
import http.client
import json
import os
import sys
from pathlib import Path


API_HOST = "open.steamdt.com"
API_PATH = "/open/cs2/v1/base"
OUTPUT_DIR = Path(__file__).resolve().parent / "output"
JSON_FILE = OUTPUT_DIR / "steamdt_cs2_base.json"
HTML_FILE = OUTPUT_DIR / "steamdt_cs2_base.html"


def get_api_key() -> str:
    api_key = os.environ.get("STEAMDT_API_KEY")
    if api_key:
        return api_key

    for scope in ("User", "Machine"):
        api_key = os.environ.get("STEAMDT_API_KEY")
        if api_key:
            return api_key

        if sys.platform.startswith("win"):
            api_key = os.popen(
                f'powershell -NoProfile -Command "[Environment]::GetEnvironmentVariable(\'STEAMDT_API_KEY\', \'{scope}\')"'
            ).read().strip()
            if api_key:
                return api_key

    raise RuntimeError("Missing environment variable: STEAMDT_API_KEY")


def fetch_api_data() -> str:
    api_key = get_api_key()

    conn = http.client.HTTPSConnection(API_HOST, timeout=30)
    try:
        headers = {
            "Authorization": f"Bearer {api_key}",
        }
        conn.request("GET", API_PATH, "", headers)
        response = conn.getresponse()
        body = response.read().decode("utf-8")
    finally:
        conn.close()
    return body


def build_html(pretty_text: str) -> str:
    source_url = f"https://{API_HOST}{API_PATH}"
    escaped = html.escape(pretty_text)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SteamDT CS2 Base API</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f5f1e8;
      --panel: #fffdf8;
      --text: #1f2937;
      --muted: #6b7280;
      --border: #d6d3d1;
      --accent: #1d4ed8;
    }}
    * {{
      box-sizing: border-box;
    }}
    body {{
      margin: 0;
      font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
      background: radial-gradient(circle at top, #fff7ed 0, var(--bg) 48%, #efe7da 100%);
      color: var(--text);
    }}
    .wrap {{
      max-width: 1100px;
      margin: 40px auto;
      padding: 0 20px;
    }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 16px;
      box-shadow: 0 12px 40px rgba(15, 23, 42, 0.08);
      overflow: hidden;
    }}
    .header {{
      padding: 20px 24px 12px;
      border-bottom: 1px solid var(--border);
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 24px;
    }}
    p {{
      margin: 0;
      color: var(--muted);
    }}
    a {{
      color: var(--accent);
    }}
    pre {{
      margin: 0;
      padding: 24px;
      overflow: auto;
      white-space: pre-wrap;
      word-break: break-word;
      line-height: 1.55;
      font-size: 14px;
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <div class="header">
        <h1>SteamDT CS2 基础接口返回结果</h1>
        <p>接口地址：<a href="{source_url}">{source_url}</a></p>
      </div>
      <pre>{escaped}</pre>
    </div>
  </div>
</body>
</html>
"""


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    raw_text = fetch_api_data()

    try:
        parsed = json.loads(raw_text)
        pretty_text = json.dumps(parsed, ensure_ascii=False, indent=2)
    except json.JSONDecodeError:
        pretty_text = raw_text

    JSON_FILE.write_text(pretty_text, encoding="utf-8")
    HTML_FILE.write_text(build_html(pretty_text), encoding="utf-8")

    print(f"JSON 文件: {JSON_FILE}")
    print(f"HTML 文件: {HTML_FILE}")


if __name__ == "__main__":
    main()
