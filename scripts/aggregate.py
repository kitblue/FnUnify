#!/usr/bin/env python3
"""
FnUnify 聚合脚本
聚合 FnDepot 生态（fnpack.json V2）与 fnos-apps（apps.json v1），
按 appname 去重、保留最高版本、fnos-apps 优先，输出统一 fnpack.json（FnDepot V2 规范）。
"""
import json, re, urllib.request, concurrent.futures, os, sys, datetime

REPO_LIST = "repo_list.txt"
OUTPUT = "fnpack.json"

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # 脚本位于 scripts/，切到仓库根
RAW = "https://raw.githubusercontent.com"

def fetch(url, timeout=30):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "FnUnify-Aggregator/1.0")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8")
    except Exception:
        return None

def try_raw(owner, repo, path, branches=("main", "master")):
    for br in branches:
        txt = fetch(f"{RAW}/{owner}/{repo}/{br}/{path}")
        if txt is not None:
            return txt, br
    return None, None

def version_key(v):
    """把版本字符串拆成可比较数字元组"""
    if not v:
        return (0, 0, 0, 0)
    parts = re.findall(r"\d+", str(v))
    nums = [int(x) for x in parts[:4]]
    while len(nums) < 4:
        nums.append(0)
    return tuple(nums)

def parse_fnpack(txt):
    """fnpack.json V2 键式 -> {appname: entry}"""
    try:
        data = json.loads(txt)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for k, v in data.items():
        if isinstance(v, dict) and "display_name" in v:
            v["appname"] = v.get("appname", k)
            out[k.lower()] = v
    return out

def parse_apps_json(txt):
    """fnos-apps apps.json v1 -> {appname: entry(转 V2)}"""
    try:
        data = json.loads(txt)
    except Exception:
        return {}
    out = {}
    for a in data.get("apps", []):
        if not isinstance(a, dict) or not a.get("appname"):
            continue
        an = a["appname"].lower()
        entry = {
            "appname": an,
            "display_name": a.get("display_name", an),
            "desc": a.get("description", ""),
            "version": a.get("fpk_version") or a.get("version", ""),
            "author": a.get("maintainer", ""),
            "homepage": a.get("homepage_url", ""),
            "labels": a.get("category", ""),
            "platform": a.get("platforms", ["x86", "arm"]),
            "_src": "fnos-apps",
            "_orig": a,
        }
        # 构造 download_url（fnos-apps release 资产命名: {file_prefix}_{fpk_version}_{arch}.fpk）
        tag = a.get("release_tag", "")
        prefix = a.get("file_prefix") or a.get("appname")
        fv = a.get("fpk_version") or a.get("version", "")
        if tag and prefix and fv:
            base = f"https://github.com/conversun/fnos-apps/releases/download/{tag}/{prefix}_{fv}"
            entry["download_url"] = base + "_x86.fpk"
            ad = {}
            for arch in a.get("platforms", ["x86", "arm"]):
                if arch in ("x86", "arm"):
                    ad[arch] = {"download_url": f"{base}_{arch}.fpk"}
            if ad:
                entry["arch_diff"] = ad
        out[an] = entry
    return out

def load_source(owner_repo):
    owner, repo = owner_repo.split("/", 1)
    # 优先 fnpack.json
    txt, br = try_raw(owner, repo, "fnpack.json")
    if txt is not None:
        apps = parse_fnpack(txt)
        for v in apps.values():
            v.setdefault("_src", f"{owner}/{repo}")
        return apps
    # 其次 apps.json
    txt, br = try_raw(owner, repo, "apps.json")
    if txt is not None:
        return parse_apps_json(txt)
    return {}

def main():
    with open(REPO_LIST, encoding="utf-8") as f:
        sources = [l.strip() for l in f if l.strip() and not l.startswith("#")]
    print(f"聚合 {len(sources)} 个源...")

    merged = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
        futures = {ex.submit(load_source, s): s for s in sources}
        for f in concurrent.futures.as_completed(futures):
            src = futures[f]
            try:
                apps = f.result()
            except Exception:
                apps = {}
            for an, entry in apps.items():
                if an in ("source_info", "apps", "moo", "fndepot.source", "fnos-apps-store"):
                    continue  # 跳过非应用条目
                if an not in merged:
                    merged[an] = entry
                    continue
                cur = merged[an]
                # fnos-apps 优先（官方维护、持续更新）
                if entry.get("_src") == "fnos-apps" and cur.get("_src") != "fnos-apps":
                    merged[an] = entry
                elif cur.get("_src") == "fnos-apps" and entry.get("_src") != "fnos-apps":
                    pass
                elif version_key(entry.get("version")) > version_key(cur.get("version")):
                    merged[an] = entry

    # 清理内部字段并规范化输出
    apps_out = {}
    for an, e in sorted(merged.items()):
        # 规范化 arch_diff：每个 arch 必须是 {"download_url": url}，否则剔除该 arch
        ad = {}
        raw_ad = e.get("arch_diff")
        if isinstance(raw_ad, dict):
            for arch, av in raw_ad.items():
                if isinstance(av, dict) and av.get("download_url"):
                    ad[arch] = {"download_url": av["download_url"]}
        # 顶层 download_url 强制补齐：缺失时从 arch_diff 回填（优先 x86，其次 arm，再其次任意）
        dl = e.get("download_url")
        if not dl:
            for arch in ("x86", "arm"):
                if arch in ad:
                    dl = ad[arch]["download_url"]
                    break
            if not dl and ad:
                dl = next(iter(ad.values()))["download_url"]
        if not dl:
            continue  # 无下载地址的条目对用户不可安装，过滤
        out = {}
        src_repo = e.get("_src", "")
        for k, v in e.items():
            if k in ("_src", "_orig", "arch_diff", "download_url"):
                continue
            out[k] = v
        out["download_url"] = dl
        if ad:
            out["arch_diff"] = ad
        # 补默认字段（install_type 缺失时按 FnDepot 主流取值兜底）
        out.setdefault("labels", "")
        out.setdefault("changelog", "")
        out.setdefault("history", {})
        out.setdefault("install_type", "存储空间")
        out.setdefault("size", "")
        # size 缺失时从 arch_diff 回填（若来源条目在 arch 级带了 size）
        if not out.get("size"):
            for arch in ("x86", "arm"):
                if arch in ad and raw_ad.get(arch, {}).get("size"):
                    out["size"] = raw_ad[arch]["size"]
                    break
        if src_repo:
            out["source_repo"] = src_repo  # 标注来源仓库便于追溯
        # platform 统一为数组（兼容客户端解析）；缺失时从 arch_diff 推导，再退回默认双架构
        plat = out.get("platform")
        if isinstance(plat, str):
            out["platform"] = [plat]
        elif not isinstance(plat, list) or not plat:
            out["platform"] = list(ad.keys()) if ad else ["x86", "arm"]
        # V2 键统一用小写 appname，无 appname 时退回源键小写
        key = (e.get("appname") or an).lower()
        apps_out[key] = out

    # FnDepot V2 规范：顶层直接是 应用键 -> 详情 的映射，不允许任何包装层
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(apps_out, f, ensure_ascii=False, indent=2)
    print(f"完成: {len(apps_out)} 个应用 -> {OUTPUT} (FnDepot V2 顶层键式)")

if __name__ == "__main__":
    main()
