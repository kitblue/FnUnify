#!/usr/bin/env python3
"""
FnUnify 聚合脚本 v3
- 支持三种源格式：fnpack.json 顶层键式(V2)、fnpack.json 包装层({schema_version,source_info,apps})、apps.json(v1)
- releases 解析：从最新版 releases[ver].packages 提取 download_url / arch_diff / version
- 相对路径 icon_url/download_url/preview_urls 转 raw 绝对直链
- 开发者信息归一化：author/author_url 继承 maintainer/distributor/developer/source_info，缺失兜底仓库 owner
- 连接优先 API：设置了 GITHUB_TOKEN 时用 GitHub API 拉取（限流宽松），否则回退 raw 直链
- 去重：appname 小写，fnos-apps 优先，版本最高者胜
- 输出 FnDepot V2 顶层键式 fnpack.json（platform 数组、arch_diff 规范）
"""
import json, re, os, sys, datetime, base64
import urllib.request, concurrent.futures

REPO_LIST = "repo_list.txt"
OUTPUT = "fnpack.json"
RAW = "https://raw.githubusercontent.com"
API = "https://api.github.com"
TOKEN = os.environ.get("GITHUB_TOKEN", "")

def _headers():
    h = {"User-Agent": "FnUnify-Aggregator/3.0"}
    if TOKEN:
        h["Authorization"] = f"Bearer {TOKEN}"
    return h

def _http(url, timeout=30):
    req = urllib.request.Request(url, headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8")
    except Exception:
        return None

def fetch_file(owner, repo, path, branch):
    """优先 API（带 token 时），失败回退 raw 直链"""
    if TOKEN:
        txt = _http(f"{API}/repos/{owner}/{repo}/contents/{path}?ref={branch}")
        if txt:
            try:
                j = json.loads(txt)
                if isinstance(j, dict) and j.get("content"):
                    return base64.b64decode(j["content"]).decode("utf-8", "replace")
            except Exception:
                pass
    return _http(f"{RAW}/{owner}/{repo}/{branch}/{path}")

def try_raw(owner, repo, path, branches=("main", "master")):
    for br in branches:
        txt = fetch_file(owner, repo, path, br)
        if txt is not None:
            return txt, br
    return None, None

def version_key(v):
    if not v:
        return (0, 0, 0, 0)
    parts = re.findall(r"\d+", str(v))
    nums = [int(x) for x in parts[:4]]
    while len(nums) < 4:
        nums.append(0)
    return tuple(nums)

def abs_url(u, owner, repo, branch):
    if not u:
        return ""
    u = str(u).strip()
    if u.startswith(("http://", "https://")):
        return u
    u2 = u[2:] if u.startswith("./") else u
    u2 = u2.lstrip("/")
    return f"{RAW}/{owner}/{repo}/{branch}/{u2}"

def latest_release_version(entry):
    rel = entry.get("releases")
    if not isinstance(rel, dict):
        return None
    vers = [v for v in rel.keys() if isinstance(rel[v], dict)]
    if not vers:
        return None
    return max(vers, key=version_key)

def extract_download(entry, owner, repo, branch):
    """从 releases 提取最新版下载信息 -> (version, download_url, arch_diff)"""
    lv = latest_release_version(entry)
    ad = None
    if lv is not None:
        rel_v = entry["releases"][lv]
        pkg = rel_v.get("packages") if isinstance(rel_v, dict) else None
        if isinstance(pkg, dict):
            ad = {}
            for arch, pv in pkg.items():
                if isinstance(pv, dict) and pv.get("download_url"):
                    ad[arch] = {"download_url": abs_url(pv["download_url"], owner, repo, branch)}
                    if pv.get("sha256"):
                        ad[arch]["sha256"] = pv["sha256"]
                    if pv.get("size"):
                        ad[arch]["size"] = pv["size"]
    dl = ""
    if isinstance(entry.get("download_url"), str) and entry["download_url"]:
        dl = abs_url(entry["download_url"], owner, repo, branch)
    if not dl and ad:
        for arch in ("x86", "arm", "all"):
            if arch in ad:
                dl = ad[arch]["download_url"]
                break
        if not dl and ad:
            dl = next(iter(ad.values()))["download_url"]
    return (lv or entry.get("version", "")), dl, ad

def normalize_entry(entry, owner, repo, branch, src_meta=None):
    e = dict(entry)
    src_meta = src_meta or {}
    # 开发者信息归一化：author = maintainer > distributor > developer > source_info.author > owner
    author = (e.get("maintainer") or e.get("distributor") or e.get("developer")
              or src_meta.get("author") or src_meta.get("distributor") or owner)
    e["author"] = str(author) if author else owner
    e["author_url"] = (e.get("maintainer_url") or e.get("distributor_url")
                       or e.get("developer_url") or f"https://github.com/{owner}")
    e["distributor"] = e.get("distributor") or src_meta.get("name", "")
    e["distributor_url"] = e.get("distributor_url") or src_meta.get("homepage", f"https://github.com/{owner}")
    # 图标：icon_url > icon > logo，相对路径转绝对
    icon = e.get("icon_url") or e.get("icon") or e.get("logo")
    if icon:
        e["icon_url"] = abs_url(icon, owner, repo, branch)
    e.pop("icon", None)
    e.pop("logo", None)
    # preview_urls 绝对化
    pv = e.get("preview_urls")
    if isinstance(pv, list):
        e["preview_urls"] = [abs_url(p, owner, repo, branch) for p in pv]
    # categories -> labels
    if not e.get("labels") and e.get("categories"):
        e["labels"] = ",".join(e["categories"]) if isinstance(e["categories"], list) else str(e["categories"])
    # platform 数组
    plat = e.get("platform")
    if isinstance(plat, str):
        e["platform"] = [plat]
    elif not isinstance(plat, list) or not plat:
        e["platform"] = ["all"]
    e.pop("categories", None)
    return e

def parse_fnpack(txt, owner, repo, branch):
    """fnpack.json：包装层 or 顶层键式 -> ({appname: entry}, src_meta)"""
    try:
        data = json.loads(txt)
    except Exception:
        return {}, {}
    if not isinstance(data, dict):
        return {}, {}
    src_meta = {}
    raw_apps = None
    if "apps" in data:
        si = data.get("source_info")
        src_meta = si if isinstance(si, dict) else {}
        raw_apps = data["apps"]
    else:
        raw_apps = data
    out = {}
    if isinstance(raw_apps, dict):
        for k, v in raw_apps.items():
            if isinstance(v, dict) and ("display_name" in v or "download_url" in v or "releases" in v):
                v["appname"] = v.get("appname", k)
                out[k.lower()] = v
    elif isinstance(raw_apps, list):
        for a in raw_apps:
            if isinstance(a, dict) and a.get("appname"):
                out[str(a["appname"]).lower()] = a
    return out, src_meta

def parse_apps_json(txt):
    """fnos-apps apps.json v1 -> {appname: entry}"""
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
            "icon_url": a.get("icon_url", ""),
            "_src": "fnos-apps",
            "_orig": a,
        }
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
    txt, br = try_raw(owner, repo, "fnpack.json")
    if txt is not None:
        apps, src_meta = parse_fnpack(txt, owner, repo, br)
        for an, v in apps.items():
            v.setdefault("_src", f"{owner}/{repo}")
            v["_src_meta"] = src_meta
            v["_branch"] = br
        return apps
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
            try:
                apps = f.result()
            except Exception:
                apps = {}
            for an, entry in apps.items():
                if an in ("source_info", "apps", "moo", "fndepot.source", "fnos-apps-store"):
                    continue
                if an not in merged:
                    merged[an] = entry
                    continue
                cur = merged[an]
                if entry.get("_src") == "fnos-apps" and cur.get("_src") != "fnos-apps":
                    merged[an] = entry
                elif cur.get("_src") == "fnos-apps" and entry.get("_src") != "fnos-apps":
                    pass
                elif version_key(entry.get("version")) > version_key(cur.get("version")):
                    merged[an] = entry

    apps_out = {}
    for an, e in sorted(merged.items()):
        src = e.get("_src", "")
        owner, repo = (src.split("/", 1) + ["", ""])[:2]
        branch = e.get("_branch", "main")
        src_meta = e.get("_src_meta", {})
        out = normalize_entry(e, owner, repo, branch, src_meta)
        v, dl, ad = extract_download(out, owner, repo, branch)
        if not dl:
            continue  # 无可下载地址 -> 过滤
        out["version"] = v or out.get("version", "")
        out["download_url"] = dl
        if ad:
            out["arch_diff"] = ad
        elif isinstance(out.get("arch_diff"), dict):
            ad2 = {}
            for arch, av in out["arch_diff"].items():
                if isinstance(av, dict) and av.get("download_url"):
                    ad2[arch] = {"download_url": abs_url(av["download_url"], owner, repo, branch)}
            if ad2:
                out["arch_diff"] = ad2
        if not out.get("size") and ad:
            for arch in ("x86", "arm", "all"):
                if arch in ad and ad[arch].get("size"):
                    out["size"] = ad[arch]["size"]
                    break
        out.setdefault("labels", "")
        out.setdefault("changelog", "")
        out.setdefault("history", {})
        out.setdefault("install_type", "存储空间")
        out.setdefault("platform", ["x86", "arm"])
        for k in ("_src", "_src_meta", "_orig", "_branch"):
            out.pop(k, None)
        out.setdefault("source_repo", src)
        key = (out.get("appname") or an).lower()
        apps_out[key] = out

    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(apps_out, f, ensure_ascii=False, indent=2)
    print(f"完成: {len(apps_out)} 个应用 -> {OUTPUT} (FnDepot V2 顶层键式)")

if __name__ == "__main__":
    main()
