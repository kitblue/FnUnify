#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sync_fndepot.py — FnDepot 定制版（fndepot_unify）自动发版流水线

项目角色
--------
FnUnify 是一个 fnOS 第三方应用源聚合项目，同时维护一个 FnDepot 客户端的定制发行版
（fndepot_unify）：在原版客户端之上不修改任何二进制/前端，仅在 fpk 的 cmd 安装钩子
中注入 FnUnify 订阅源，让用户在安装定制版后打开即用。

本脚本是「定制版自动发版流水线」：

1. 查询唯一上游 Blue-Mink/FnDepot 的最新 release（tag + fpk 资产）
2. 与本地记录版本（scripts/sync_state.json）比对；无新版 → 退出，不随编译递增版本
3. 下载原版 fpk → 解包（gzip + tar）
4. 定制：新增 cmd/seed_fnunify.sh，并在 cmd/common 的 install/upgrade 回调中
   幂等注入「预置 FnUnify 源」调用（重复运行不重复注入）
5. 重打包 fndepot_unify_v<ver>.fpk，manifest 版本跟随原版
6. 可选发布（--publish）：GitHub API 创建 release 并上传 fpk
7. 更新本地版本记录 scripts/sync_state.json

版本策略
--------
发版版本 = 上游（Blue-Mink/FnDepot）release tag 中的版本号 **降一位**：
- 上游 v0.6.6 → 本项目发版 0.6.5（tag=fndepot-v0.6.5，manifest.version=0.6.5，
  fpk=fndepot_unify_v0.6.5.fpk）
- 上游发 0.6.7 → 本项目发 0.6.6，依此类推。

降一位用于测试客户端升级功能：定制版客户端检查本项目（kitblue/FnUnify）的 release，
发版版本低于已装版本时即可走升级流程验证。原版不发新版就不重新发版，不随编译次数递增。

参数行为
--------
python scripts/sync_fndepot.py            # 检查 + 构建，不发布
python scripts/sync_fndepot.py --check-only  # 仅检查版本，输出 CHECK_ONLY 行（CI 友好，
                                             # 不下载/不构建/不写文件）；0=无需构建, 2=有新版本
python scripts/sync_fndepot.py --force    # 忽略版本记录强制构建
python scripts/sync_fndepot.py --publish  # 检查 + 构建 + 发布到 GitHub Releases
python scripts/sync_fndepot.py --dry-run  # 构建 + 打印发布计划，不真正发布
python scripts/sync_fndepot.py --token ghp_xxx  # 显式提供 GitHub Token

安全说明
--------
- 只读操作（--check-only）不要求 Token（GitHub 公开 API 匿名可用）；
- 构建与发布涉及写网络，需 Token：--token 或环境变量 GITHUB_TOKEN。
"""

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ================================================================ 常量

# 唯一上游：Blue-Mink/FnDepot（原版 FnDepot 客户端，定制版 fpk 的原始来源，自动检查其 release 更新）
UPSTREAM_REPO = "Blue-Mink/FnDepot"
# 本项目发布仓库（定制版 fpk 发布目标；定制版客户端检查更新即检查本仓库 release）
PUBLISH_REPO = "kitblue/FnUnify"
# FnUnify 聚合源地址（seed 脚本写入客户端 sources 表的订阅源）
SOURCE_URL = "https://raw.githubusercontent.com/kitblue/FnUnify/main/fnpack.json"
SOURCE_NAME = "FnUnify"

# 路径
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent                          # fnunify_work
STATE_FILE = SCRIPT_DIR / "sync_state.json"       # {"last_version": "0.6.5", ...}
BUILD_DIR = ROOT / "build_work"
WORK_DIR = BUILD_DIR / "src"                      # 原版 fpk 解包 + 定制的工作目录
TMP_DIR = ROOT / ".tmp_sync"                      # 原版 fpk 下载缓存

# GitHub API
API = "https://api.github.com"
USER_AGENT = "marvis-sync-fndepot"
HEADERS_BASE = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}

# fpk tar 成员顺序（重打包时保持与原包一致）
FPK_MEMBER_ORDER = ["i18n", "config", "manifest", "app.tgz", "cmd", "ICON.PNG", "wizard", "ICON_256.PNG"]

# 注入标记：cmd/common 中已存在该文件名则视为已注入（幂等）
SEED_MARKER = "seed_fnunify.sh"


# ================================================================ SEED 脚本模板
# __SOURCE_URL__ / __SOURCE_NAME__ 在 inject_customization 时替换为实际值。
#
# 设计要点：
# - 幂等：以 sources.url 唯一约束为依托，重复运行不产生重复记录
# - 只增不删：远程 fnpack 中不存在的本地外部源（用户手动添加）一律保留
# - 单后端：python3 直连 sqlite3 写入（fnOS 内置 python3）
SEED_SCRIPT = r"""#!/bin/bash
# FnUnify 源预置脚本（定制版，随 fpk 安装/升级时由 common 调用）
# 功能：将 FnUnify 聚合源（fnpack.json 直链）幂等写入 ${TRIM_PKGVAR}/fndepot.db
#       的 sources 表（source_type='external'），客户端原生同步逻辑负责后续拉取/解析/展示。
# 原则：只新增，绝不删除用户手动添加的本地源；以 sources.url 唯一约束保证幂等。
set -u

DB="${TRIM_PKGVAR:-}/fndepot.db"
URL="__SOURCE_URL__"
NAME="__SOURCE_NAME__"

[ -f "${DB}" ] || { echo "fndepot.db 不存在，跳过源预置"; exit 0; }

sync_via_python() {
python3 - "${DB}" "${URL}" "${NAME}" <<'PY'
import sqlite3, sys, time

db_path, source_url, source_name = sys.argv[1], sys.argv[2], sys.argv[3]

def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

con = sqlite3.connect(db_path)
cur = con.cursor()

# 已存在同 url 的外部源则跳过（幂等，不覆盖用户对同名源的手动修改）
cur.execute("SELECT id, url FROM sources WHERE url = ? AND source_type = 'external'", (source_url,))
row = cur.fetchone()
if row:
    log(f"FnUnify 源已存在（id={row[0]}），跳过插入")
    con.close()
    sys.exit(0)

# 计算下一个 source_order，避免与其他外部源冲突
cur.execute("SELECT COALESCE(MAX(source_order), -1) FROM sources WHERE is_builtin = 0")
next_order = (cur.fetchone()[0] or -1) + 1
now = time.strftime("%Y-%m-%d %H:%M:%S")

try:
    cur.execute(
        "INSERT INTO sources(url, name, is_default, is_builtin, priority, sync_status, "
        "sync_enabled, is_fork, overlap_risk, source_order, created_at, updated_at, source_type) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (source_url, source_name, 0, 0, 100, 'pending', 1, 0, 'none',
         next_order, now, now, 'external'),
    )
    con.commit()
    log(f"已预置 FnUnify 源: {source_url}")
except Exception as e:
    log(f"插入失败: {e}")
    con.rollback()
finally:
    con.close()
PY
}

sync_via_python || { echo "python3 不可用，跳过源预置（可稍后在应用内手动添加 FnUnify 源）"; exit 0; }
exit $?
"""


# ================================================================ 基础工具

def log(msg: str):
    """统一时间戳日志。"""
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def sha256_hex(path: Path) -> str:
    """计算文件 sha256（分块读取，适配大文件）。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def get_custom_version(upstream_version: str) -> str:
    """定制版版本号跟随上游 FnDepot，直接返回上游版本号。

    用户明确要求：定制版版本号 = 上游版本号（不再降一位）。
    """
    return upstream_version


def http_get(url: str, headers: dict | None = None, binary: bool = False):
    """GET 请求；binary=True 时返回字节，否则解码为 utf-8 文本。"""
    req = urllib.request.Request(url, headers={**HEADERS_BASE, **(headers or {})})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    return data if binary else data.decode("utf-8", errors="replace")


def http_post_json(url: str, payload: dict, headers: dict | None = None) -> dict:
    """POST JSON，返回响应 JSON。"""
    body = json.dumps(payload).encode("utf-8")
    req_headers = {**HEADERS_BASE, "Content-Type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=body, method="POST", headers=req_headers)
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def http_delete(url: str, headers: dict | None = None):
    """DELETE 请求；2xx 返回 HTTP 状态码，404 返回 404（供上层判断存在性）。"""
    req = urllib.request.Request(url, method="DELETE", headers={**HEADERS_BASE, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return 404
        raise


def load_token(args) -> str:
    """获取 GitHub Token：--token > 环境变量 GITHUB_TOKEN > 本地历史 token 文件。"""
    tok = (args.token or os.environ.get("GITHUB_TOKEN", "")).strip()
    if tok:
        return tok
    candidates = [
        ROOT / ".tmp_sync" / "github_token.txt",
        ROOT.parent / "temp" / "github_token.txt",
    ]
    for cand in candidates:
        if cand.exists():
            t = cand.read_text(encoding="utf-8", errors="ignore").strip()
            if t:
                return t
    return ""  # 允许匿名调用（check-only / 公开 API）


def auth_headers(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"} if tok else {}


# ================================================================ 上游查询

def query_upstream_release(tok: str = "") -> dict:
    """
    查询 Blue-Mink/FnDepot 的最新 release（tag + 原版 fpk 资产）。

    返回：
        {"tag": 上游 tag, "version": 上游规范化版本号, "asset_name": 原版 fpk 资产名,
         "asset_url": 资产下载地址, "published_at": 发布时间}
    """
    data = json.loads(
        http_get(f"{API}/repos/{UPSTREAM_REPO}/releases/latest", auth_headers(tok))
    )
    # 上游 tag 形如 fndepot-v0.6.6，统一提取首个语义化版本号
    m = re.search(r"(\d+\.\d+(?:\.\d+)?)", data["tag_name"])
    version = m.group(1) if m else data["tag_name"].lstrip("v")

    asset = None
    for a in data.get("assets", []):
        if a["name"].lower().endswith(".fpk"):
            asset = a
            break
    if not asset:
        raise SystemExit(f"原版 release 未找到 fpk 资产: tag={data['tag_name']}")

    return {
        "tag": data["tag_name"],
        "version": version,
        "asset_name": asset["name"],
        "asset_url": asset["browser_download_url"],
        "published_at": data.get("published_at", ""),
    }


# ================================================================ 本地状态

def load_state() -> dict:
    """读取 scripts/sync_state.json；不存在或损坏时返回空 dict。"""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(state: dict):
    """写入 scripts/sync_state.json。"""
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ================================================================ 构建管线

def download_asset(url: str, dest: Path):
    """下载原版 fpk 到缓存目录；已存在且非空则复用（避免重复下载）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        log(f"缓存命中，跳过下载: {dest} ({dest.stat().st_size} bytes)")
        return
    log(f"下载原版 fpk: {url}")
    data = http_get(url, binary=True)
    dest.write_bytes(data)
    log(f"已保存: {dest} ({len(data)} bytes)")


def extract_fpk(fpk: Path, out_dir: Path):
    """解包 gzip+tar 的 fpk 到工作目录（先清空旧工作目录）。"""
    log(f"解包: {fpk.name}")
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with gzip.open(fpk, "rb") as gz:
        raw = gz.read()
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as tf:
        tf.extractall(out_dir)


def inject_customization(work_dir: Path):
    """
    注入定制内容：
    1. 新增 cmd/seed_fnunify.sh（内置 FnUnify 源，幂等写 sources 表）
    2. 在 cmd/common 的 install_callback / upgrade_callback 中追加预置调用
    """
    cmd_dir = work_dir / "cmd"
    common_path = cmd_dir / "common"
    seed_path = cmd_dir / SEED_MARKER

    # 1) 写入 seed 脚本（模板变量替换）
    seed_text = (
        SEED_SCRIPT
        .replace("__SOURCE_URL__", SOURCE_URL)
        .replace("__SOURCE_NAME__", SOURCE_NAME)
    )
    seed_path.write_text(seed_text, encoding="utf-8")
    os.chmod(seed_path, 0o755)
    log(f"已写入 {SEED_MARKER}")

    # 2) common 注入（幂等：已含标记则跳过）
    common_text = common_path.read_text(encoding="utf-8")
    if SEED_MARKER in common_text:
        log("cmd/common 已含预置调用，跳过注入")
        return

    hook_block = (
        f"\n    # FnUnify 源预置（定制版）\n"
        f"    if [ -x \"${{COMMON_SCRIPT_DIR}}/{SEED_MARKER}\" ]; then\n"
        f"        \"${{COMMON_SCRIPT_DIR}}/{SEED_MARKER}\" || log_msg \"WARNING: FnUnify 源预置失败，可手动添加\"\n"
        f"    fi\n"
    )
    # 锚点：install 与 upgrade 回调中持久化数据重建之后
    anchor = "rebuild_persistent_data_if_needed || return 1"
    count = common_text.count(anchor)
    if count != 2:
        log(f"WARNING: cmd/common 中 rebuild 锚点出现 {count} 次（预期 2），注入可能不完整")
    common_text = common_text.replace(anchor, anchor + hook_block)
    common_path.write_text(common_text, encoding="utf-8")
    log("已注入 install_callback / upgrade_callback 预置调用")


def rewrite_manifest_version(work_dir: Path, release_version: str):
    """将解包目录 manifest 的 version 改写为发版版本（降一位后的版本）。"""
    manifest_path = work_dir / "manifest"
    text = manifest_path.read_text(encoding="utf-8")
    new_text = re.sub(r'^version="[^"]*"', f'version="{release_version}"', text,
                      count=1, flags=re.MULTILINE)
    if new_text == text:
        log(f"WARNING: manifest 未找到 version 行，跳过改写")
        return
    manifest_path.write_text(new_text, encoding="utf-8")
    log(f"manifest version 已改写为 {release_version}")


def repack_fpk(work_dir: Path, dest: Path):
    """按原 fpk 的 tar 成员顺序重打包为 gzip tar。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name in FPK_MEMBER_ORDER:
            p = work_dir / name
            if not p.exists():
                log(f"WARNING: 成员缺失 {name}")
                continue
            if p.is_dir():
                tf.add(p, arcname=name, recursive=True)
            else:
                tf.add(p, arcname=name, recursive=False)
    dest.write_bytes(buf.getvalue())
    log(f"重打包完成: {dest} ({dest.stat().st_size} bytes)")


# ================================================================ 发布

def publish_release(tok: str, version: str, fpk_path: Path,
                    upstream_version: str = "", dry_run: bool = False):
    """
    创建 GitHub release（tag=fndepot-v<version>）并上传 fpk。
    发布校验：同名 tag/release 已存在时先删除旧 release（连带资产）与 tag，再重建，
    确保发布成功且资产唯一。
    dry_run=True 时仅打印计划，不做任何网络写操作。
    """
    tag = f"fndepot-v{version}"
    title = f"FnDepot 定制版 v{version}（内置 FnUnify 源）"
    body = (
        f"FnDepot 定制版 v{version}\n\n"
        f"- 上游版本：{UPSTREAM_REPO} v{upstream_version or version}\n"
        f"- 发版版本：v{version}（跟随上游 {UPSTREAM_REPO} 版本号，不再降位）\n"
        f"- 客户端检查更新：本仓库（{PUBLISH_REPO}）release\n"
        f"- 内置 FnUnify 订阅源（安装/升级时自动写入，打开即用）\n"
        f"- fpk sha256: {sha256_hex(fpk_path)}\n\n"
        f"安装方式：fnOS 应用中心手动安装 fpk，或在 FnDepot 中添加 FnUnify 源后安装。"
    )
    if dry_run:
        log(f"[DRY-RUN] 将创建 release tag={tag} title={title}")
        log(f"[DRY-RUN] 将上传 {fpk_path.name} ({fpk_path.stat().st_size} bytes)")
        return

    # 发布校验：检查同名 tag/release 是否已存在
    existing = None
    try:
        resp = http_get(f"{API}/repos/{PUBLISH_REPO}/releases/tags/{tag}", auth_headers(tok))
        existing = json.loads(resp)
        log(f"检测到已存在 release: id={existing['id']} tag={tag}，先删除旧 release 与 tag 再重建")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise

    if existing:
        # 1) 删除旧 release（GitHub 会连带移除其 release 资产）
        http_delete(f"{API}/repos/{PUBLISH_REPO}/releases/{existing['id']}", auth_headers(tok))
        log("已删除旧 release（连带资产）")
        # 2) 删除旧 tag，确保新 release 重新打 tag 到默认分支 head
        status = http_delete(f"{API}/repos/{PUBLISH_REPO}/git/refs/tags/{tag}", auth_headers(tok))
        log(f"已删除旧 tag（DELETE 返回 {status}）")

    log(f"创建 release: {tag}")
    rel = http_post_json(
        f"{API}/repos/{PUBLISH_REPO}/releases",
        {"tag_name": tag, "name": title, "body": body,
         "draft": False, "prerelease": False},
        auth_headers(tok),
    )
    upload_url = rel["upload_url"].split("{")[0]
    log(f"上传资产: {fpk_path.name}")
    req = urllib.request.Request(
        f"{upload_url}?name={fpk_path.name}",
        data=fpk_path.read_bytes(),
        method="POST",
        headers={**HEADERS_BASE, "Content-Type": "application/octet-stream",
                 **auth_headers(tok)},
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        json.loads(resp.read().decode("utf-8", errors="replace"))
    log("发布完成")


# ================================================================ CLI

def main():
    ap = argparse.ArgumentParser(description="FnDepot 定制版自动发版流水线")
    ap.add_argument("--publish", action="store_true", help="构建后发布到 GitHub Releases")
    ap.add_argument("--dry-run", action="store_true", help="发布环节仅打印计划")
    ap.add_argument("--force", action="store_true", help="忽略版本记录强制构建")
    ap.add_argument("--check-only", action="store_true",
                    help="仅检查版本：输出 CHECK_ONLY 行（不下载/不构建/不改文件），"
                         "返回 0=无需构建, 2=有新版本待构建")
    ap.add_argument("--token", default="", help="GitHub Token（默认读 GITHUB_TOKEN）")
    args = ap.parse_args()

    tok = load_token(args)

    # 1) 查询上游
    latest = query_upstream_release(tok)
    log(f"上游 {UPSTREAM_REPO} 最新版本: v{latest['version']} (release {latest['published_at']})")
    release_version = get_custom_version(latest["version"])
    log(f"发版版本（跟随上游）: v{release_version}")

    # 2) 版本比对（以发版版本为准）
    state = load_state()
    last = state.get("last_version", "")
    need_build = last != release_version
    log(f"本地记录发版版本: v{last or '(无记录)'} -> {'需要构建' if need_build else '无需构建'}")

    # 3) check-only：CI 友好输出，不写任何文件
    if args.check_only:
        print(f"CHECK_ONLY upstream={latest['version']} local={last or 'none'} "
              f"release={release_version} need_build={'yes' if need_build else 'no'} "
              f"published_at={latest['published_at']}")
        return 0 if not need_build else 2

    # 4) 无需构建则退出
    if not args.force and not need_build:
        log(f"本地记录发版版本 {last} 与当前发版版本一致，无需发版（版本降一位策略，不随编译递增）")
        return 0

    # 5) 下载 + 解包 + 定制 + 重打包
    orig_fpk = TMP_DIR / latest["asset_name"]
    download_asset(latest["asset_url"], orig_fpk)
    extract_fpk(orig_fpk, WORK_DIR)
    inject_customization(WORK_DIR)
    rewrite_manifest_version(WORK_DIR, release_version)

    out_fpk = ROOT / f"fndepot_unify_v{release_version}.fpk"
    repack_fpk(WORK_DIR, out_fpk)

    # 6) 记录版本（发布前记录，--publish 失败可重跑 --force）
    save_state({
        "last_version": release_version,
        "upstream_version": latest["version"],
        "last_built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })

    # 7) 发布（可选）
    if args.publish or args.dry_run:
        publish_release(tok, release_version, out_fpk,
                        upstream_version=latest["version"], dry_run=args.dry_run)

    log(f"完成。产物: {out_fpk}")
    log(f"sha256: {sha256_hex(out_fpk)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
