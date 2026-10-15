#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update_official_apps.py — 从 conversun/fnos-apps 每日同步官方应用清单

功能
----
拉取官方第三方应用商店仓库 conversun/fnos-apps 的 apps.json，
提取全部应用的 slug（小写标识），排序去重后写入仓库根目录 official_apps.txt。

official_apps.txt 在 aggregate.py 中作为「官方应用排除清单」使用：
聚合第三方源时跳过这些已在官方商店上架的应用，避免与官方重复。

用法
----
python scripts/update_official_apps.py          # 生成 official_apps.txt（无网络时保持原样退出）
python scripts/update_official_apps.py --dry-run  # 打印将写入的内容，不落盘
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

OFFICIAL_APPS_URL = "https://raw.githubusercontent.com/conversun/fnos-apps/main/apps.json"
OFFICIAL_APPS_PATH = Path(__file__).resolve().parent.parent / "official_apps.txt"
UA = {"User-Agent": "FnUnify-OfficialAppsSync/1.0"}


def fetch_apps_json() -> dict:
    """拉取官方 apps.json，失败抛异常。"""
    req = urllib.request.Request(OFFICIAL_APPS_URL, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def extract_slugs(data: dict) -> list:
    """提取 apps[].slug，去重、排序、统一小写。"""
    apps = data.get("apps", [])
    if not isinstance(apps, list):
        return []
    slugs = set()
    for app in apps:
        if isinstance(app, dict) and app.get("slug"):
            slugs.add(str(app["slug"]).strip().lower())
    return sorted(slugs)


def main() -> int:
    parser = argparse.ArgumentParser(description="同步官方应用清单 official_apps.txt")
    parser.add_argument("--dry-run", action="store_true", help="只打印结果，不写文件")
    args = parser.parse_args()

    print(f"拉取官方清单: {OFFICIAL_APPS_URL}")
    data = fetch_apps_json()
    slugs = extract_slugs(data)
    if not slugs:
        print("ERROR: apps.json 中未提取到任何 slug，保持官方清单不变")
        return 1

    content = "\n".join(slugs) + "\n"

    if args.dry_run:
        print(f"将写入 {len(slugs)} 行到 {OFFICIAL_APPS_PATH}")
        print(content)
        return 0

    old = OFFICIAL_APPS_PATH.read_text(encoding="utf-8") if OFFICIAL_APPS_PATH.exists() else ""
    if old == content:
        print(f"official_apps.txt 无变化（{len(slugs)} 行），跳过写入")
        return 0

    OFFICIAL_APPS_PATH.write_text(content, encoding="utf-8")
    print(f"official_apps.txt 已更新: {len(slugs)} 行 -> {OFFICIAL_APPS_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
