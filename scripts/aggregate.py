#!/usr/bin/env python3
"""
FnUnify 聚合脚本 v3
- 支持三种源格式：fnpack.json 顶层键式(V2)、fnpack.json 包装层({schema_version,source_info,apps})、apps.json(v1)
- releases 解析：从最新版 releases[ver].packages 提取 download_url / arch_diff / version
- 相对路径 icon_url/download_url/preview_urls 转 raw 绝对直链
- 开发者信息归一化：author/author_url 继承 maintainer/distributor/developer/source_info，缺失兜底仓库 owner
- 连接优先 API：设置了 GITHUB_TOKEN 时用 GitHub API 拉取（限流宽松），否则回退 raw 直链
- 去重（源订阅模式）：appname 同名同包名，保留数据最完整的一个，其他标记 _fork_of 溯源
- 输出 FnDepot V2 顶层键式 fnpack.json（platform 数组、arch_diff 规范）
"""
import json, re, os, sys, datetime, base64
from collections import defaultdict
import urllib.request, concurrent.futures

REPO_LIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "repo_list.txt")
OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fnpack.json")
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
    """提取下载信息 -> (version, download_url, arch_diff)
    优先级：download_url 字段 > 条目自带 arch_diff > releases 最新版"""
    lv = latest_release_version(entry)
    ad = None
    # 条目自带 arch_diff（部分源直接写 arch_diff 而无 releases）
    own_ad = entry.get("arch_diff")
    if isinstance(own_ad, dict):
        ad = {}
        for arch, pv in own_ad.items():
            if isinstance(pv, dict) and pv.get("download_url"):
                ad[arch] = {"download_url": abs_url(pv["download_url"], owner, repo, branch)}
                if pv.get("sha256"):
                    ad[arch]["sha256"] = pv["sha256"]
                if pv.get("size"):
                    ad[arch]["size"] = pv["size"]
    # releases 最新版（优先级更高，覆盖自身 arch_diff 中同名架构）
    if lv is not None:
        rel_v = entry["releases"][lv]
        pkg = rel_v.get("packages") if isinstance(rel_v, dict) else None
        if isinstance(pkg, dict):
            ad = ad or {}
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
    """源订阅模式：原样保留源字段，不做作者改写/兜底（避免显示错误作者）"""
    e = dict(entry)
    src_meta = src_meta or {}
    # 作者：按源字段原样取值；fnos-apps 类源（官方及其 fork）maintainer 实为仓库维护者，
    # 不等于应用作者，若等于 conversun/源作者则留空（与官方直加一致），避免显示错误作者
    src_is_fnos_apps = "fnos-apps" in (entry.get("_src") or "").lower()
    author = (e.get("maintainer") or e.get("distributor") or e.get("developer"))
    if src_is_fnos_apps and author in ("conversun", src_meta.get("author"), src_meta.get("distributor")):
        author = ""
    if not src_is_fnos_apps:
        author = author or src_meta.get("author") or src_meta.get("distributor")
    e["author"] = str(author) if author else ""
    e["author_url"] = (e.get("maintainer_url") or e.get("distributor_url")
                       or e.get("developer_url") or (f"https://github.com/{owner}" if author else ""))
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
            if isinstance(v, dict) and ("display_name" in v or "download_url" in v or "releases" in v or "details_url" in v):
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
        # 懒加载详情：新版 FnDepot 模板 apps 仅含 details_url，详情在 apps/{app}.json
        for an, v in list(apps.items()):
            du = v.get("details_url")
            if not du or v.get("download_url") or v.get("releases") or v.get("arch_diff"):
                continue
            dtxt = fetch_file(owner, repo, du.lstrip("/"), br)
            if not dtxt:
                continue
            try:
                dj = json.loads(dtxt)
            except Exception:
                continue
            if not isinstance(dj, dict):
                continue
            detail = dj.get("app") if isinstance(dj.get("app"), dict) else dj
            detail = dict(detail)
            detail.pop("appname", None)
            detail.pop("app_name", None)
            detail.setdefault("appname", an)
            merged = dict(v)
            merged.update(detail)
            apps[an] = merged
        for an, v in apps.items():
            v.setdefault("_src", f"{owner}/{repo}")
            v["_src_meta"] = src_meta
            v["_branch"] = br
        return apps
    txt, br = try_raw(owner, repo, "apps.json")
    if txt is not None:
        return parse_apps_json(txt)
    return {}

ICON_CANDIDATES = [
    lambda an: f"{an}/icon.png",
    lambda an: f"{an}/ICON.PNG",
    lambda an: f"{an}/ICON_256.PNG",
    lambda an: f"{an}/icon_256.png",
    lambda an: f"{an}/logo.png",
    lambda an: f"apps/{an}/icon.png",
    lambda an: f"apps/{an}/ICON.PNG",
    lambda an: f"apps/{an}/ICON_256.PNG",
    lambda an: f"assets/icons/{an}.png",
    lambda an: f"icon/{an}.png",
    lambda an: f"icons/{an}.png",
]

def probe_icon(owner, repo, branch, appname):
    """HEAD 探测常见图标路径，命中即返回 raw 直链；超过 512KB 视为非图标"""
    base = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}"
    for fn in ICON_CANDIDATES:
        url = f"{base}/{fn(appname)}"
        try:
            req = urllib.request.Request(url, method="HEAD")
            with urllib.request.urlopen(req, timeout=6) as r:
                if r.status == 200:
                    cl = r.headers.get("Content-Length")
                    if cl and int(cl) <= 524288:
                        return url
        except Exception:
            continue
    return ""

def probe_size(url):
    """HEAD download_url 取 Content-Length 作为包体积；>=100KB 才采信"""
    try:
        req = urllib.request.Request(url, method="HEAD")
        req.add_header("User-Agent", "FnUnify/1.0")
        with urllib.request.urlopen(req, timeout=8) as r:
            cl = r.headers.get("Content-Length")
            if cl and int(cl) >= 102400:
                return int(cl)
    except Exception:
        pass
    return None

def main():
    with open(REPO_LIST, encoding="utf-8") as f:
        sources = [l.strip() for l in f if l.strip() and not l.startswith("#")]
    print(f"源订阅模式: 拉取 {len(sources)} 个源...")

    # 全量收录：不去重、不按版本择优，保留每个源的全部条目（字段原样）
    entries = []
    seen_sources = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
        futures = {ex.submit(load_source, s): s for s in sources}
        for f in concurrent.futures.as_completed(futures):
            try:
                apps = f.result()
            except Exception:
                apps = {}
            if apps:
                seen_sources += 1
            for an, entry in apps.items():
                if an in ("source_info", "apps", "moo", "fndepot.source", "fnos-apps-store"):
                    continue
                entries.append((an, entry))

    print(f"解析成功 {seen_sources}/{len(sources)} 个源, 全量条目 {len(entries)} 个 (未去重)")

    apps_out = {}
    used_keys = set()
    missing_icons = []  # (key, owner, repo, branch, appname)
    missing_sizes = []  # (key, url)
    for an, e in entries:
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
        out.setdefault("source_repo", src)
        # 注意：_src/_src_meta/_orig/_branch 保留到去重评分之后，输出前统一清理
        # key 唯一化：同源冲突由 dict 天然消化；跨源重复保留全部条目，key 加 -2/-3 后缀
        # appname 保留源原始名（不带后缀），后缀只用于 dict key，供去重阶段正确分组
        base = (out.get("appname") or an).lower()
        key = base
        i = 2
        while key in used_keys:
            key = f"{base}-{i}"
            i += 1
        used_keys.add(key)
        out["appname"] = base
        apps_out[key] = out
        if not out.get("icon_url"):
            missing_icons.append((base, owner, repo, branch, base))
        if not out.get("size") and dl:
            missing_sizes.append((base, dl))

    # 同名同包名去重：保留数据最完整的那个，其他标记为 fork 并保留 _fork_of 溯源
    # 优先级：官方源(conversun/fnos-apps) > 有 maintainer/distributor/developer 字段 > icon 覆盖 > 源顺序
    name_groups = defaultdict(list)
    for key, e in apps_out.items():
        e.get("_src", "")  # 确保存在
        name_groups[e.get("appname")].append(key)

    final_out = {}
    for appname, keys in name_groups.items():
        if len(keys) == 1:
            final_out[appname] = apps_out[keys[0]]
            continue
        # 评分：官方源(+10) > maintainer(+3) > distributor(+2) > developer(+1) > icon_url(+1)
        def score(k):
            e = apps_out[k]
            s = 0
            src = e.get("_src", "")
            if "conversun/fnos-apps" in src:
                s += 10
            if e.get("maintainer"):
                s += 3
            if e.get("distributor"):
                s += 2
            if e.get("developer"):
                s += 1
            if e.get("icon_url"):
                s += 1
            return s
        keys.sort(key=score, reverse=True)
        winner = keys[0]
        e = dict(apps_out[winner])
        # 溯源：全部来源仓库列表并入 _src（不修改原始字段值）
        srcs = [apps_out[k].get("_src", "") for k in keys]
        e["_src"] = srcs if len(srcs) > 1 else srcs[0]
        e["_fork_of"] = winner
        final_out[appname] = e
        if len(keys) > 1:
            print(f"  {appname}: {len(keys)} 源 -> {winner} (+{len(keys)-1} 合并)")

    apps_out = final_out
    print(f"去重后: {len(apps_out)} 唯一应用 (合并 {sum(len(v) for v in name_groups.values())} 条源条目)")

    if missing_icons:
        still_missing = [(k, o, r, b, a) for (k, o, r, b, a) in missing_icons if not apps_out[k].get("icon_url")]
        print(f"探测 {len(still_missing)} 个仍缺失图标的条目...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
            futs = {ex.submit(probe_icon, o, r, b, a): (k, o, r) for k, o, r, b, a in still_missing}
            for f in concurrent.futures.as_completed(futs):
                k, o, r = futs[f]
                try:
                    url = f.result()
                except Exception:
                    url = ""
                if url:
                    apps_out[k]["icon_url"] = url
        got = sum(1 for k, *_ in still_missing if apps_out[k].get("icon_url"))
        print(f"图标探测补全 {got}/{len(still_missing)} 个")

    if missing_sizes:
        print(f"探测 {len(missing_sizes)} 个缺失 size 的下载地址...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as ex:
            futs = {ex.submit(probe_size, url): k for k, url in missing_sizes}
            for f in concurrent.futures.as_completed(futs):
                k = futs[f]
                try:
                    sz = f.result()
                except Exception:
                    sz = None
                if sz:
                    apps_out[k]["size"] = sz
        got = sum(1 for k, _ in missing_sizes if apps_out[k].get("size"))
        print(f"size 探测补全 {got}/{len(missing_sizes)} 个")

    # 输出前统一清理内部字段（_src 为溯源字段保留；_src_meta/_orig/_branch 已用完）
    for e in apps_out.values():
        for k in ("_src_meta", "_orig", "_branch"):
            e.pop(k, None)

    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(apps_out, f, ensure_ascii=False, indent=2)
    print(f"完成: {len(apps_out)} 个应用 -> {OUTPUT} (FnDepot V2 顶层键式)")

if __name__ == "__main__":
    main()
