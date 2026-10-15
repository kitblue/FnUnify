#!/usr/bin/env python3
"""
FnUnify 聚合脚本 v3
- 支持三种源格式：fnpack.json 顶层键式(V2)、fnpack.json 包装层({schema_version,source_info,apps})、apps.json(v1)
- releases 解析：从最新版 releases[ver].packages 提取 download_url / arch_diff / version
- 相对路径 icon_url/download_url/preview_urls 转 raw 绝对直链
- 开发者信息归一化：author/author_url 继承 maintainer/distributor/developer/source_info，缺失兜底仓库 owner
- 连接优先 API：设置了 GITHUB_TOKEN 时用 GitHub API 拉取（限流宽松），否则回退 raw 直链
- 去重（源订阅模式）：按 display_name 去重（同一 display_name 仅保留数据最完整的一个），若 display_name 相同且来源不同，额外加 _fork_of 溯源标记。注意：各 fork 源已用 appname 加 -N 后缀区分，按 appname 直接去重会误判，应改用 display_name 或规范化后包名去后缀比对。
- 重复源检测：同一 owner/repo 在 repo_list 中出现多次时，仅保留第一个出现，其余标记为重复源。
- 源健康监控：对 repo_list.txt 每个源做 HTTP HEAD/GET 轻量探测（fnpack.json 路径），
  不可达源在报告 source_health_report.json 中标记 unreachable，并附带 HTTP 状态码/失败原因/检查时间。
- 元数据质量：输出前统计 display_name/desc/version/download_url/icon_url 等关键字段覆盖率，
  不完整条目标记 incomplete，报告写入 metadata_quality_report.json。
- 输出官方 FnDepot V2 格式（schema_version 字符串 "2"、source_info、apps；
  categories 固定 9 类最多 2 个；releases.<version>.packages.<arch> 嵌套结构；
  install_type 归一化 ""/root；run_as 归一化 package/root；is_docker 布尔；size 非负整数）
"""
import json, re, os, sys, datetime, base64
from collections import defaultdict
import urllib.request, urllib.error, concurrent.futures

REPO_LIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "repo_list.txt")
OFFICIAL_LIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "official_apps.txt")
OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fnpack.json")
SOURCE_HEALTH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "source_health_report.json")
META_QUALITY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "metadata_quality_report.json")
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

# ========== V2 规范归一化辅助 ==========
FIXED_CATEGORIES = ("影音娱乐", "系统工具", "编程开发", "AI赋能", "生活服务",
                    "智能智控", "教育学习", "游戏地带", "硬件驱动")

# labels 自由文本 -> 固定分类映射（关键词包含匹配，顺序即优先级）
CATEGORY_RULES = [
    (("影音娱乐",), ("影音", "媒体", "音乐", "影视", "娱乐", "短剧", "漫画", "阅读", "直播", "IPTV", "多媒体", "云盘", "云存储")),
    (("游戏地带",), ("游戏", "多人", "自动化", "平台")),
    (("编程开发",), ("开发", "编程", "API", "数据库", "ARM", "代理")),
    (("AI赋能",), ("AI", "智能", "赋能")),
    (("硬件驱动",), ("硬件", "驱动", "蓝牙", "美化")),
    (("智能智控",), ("智控", "智能家居")),
    (("教育学习",), ("教育", "学习", "图书", "词典")),
    (("系统工具",), ("工具", "效率", "实用", "系统", "网络", "DNS", "反向代理", "负载均衡", "下载", "服务器", "原生", "Docker", "docker", "存储", "备份")),
]

def map_labels_to_categories(labels):
    """labels（字符串或列表）-> 最多 2 个固定分类"""
    if isinstance(labels, list):
        text = ",".join(str(x) for x in labels)
    else:
        text = str(labels or "")
    text = text.replace("/", ",").replace("、", ",")
    matched = []
    for cats, kws in CATEGORY_RULES:
        for kw in kws:
            if kw in text:
                for c in cats:
                    if c not in matched:
                        matched.append(c)
                break
        if len(matched) >= 2:
            break
    for token in re.split(r"[,，;；]", text):
        token = token.strip()
        if token in FIXED_CATEGORIES and token not in matched:
            matched.append(token)
    return matched[:2]

def normalize_install_type(it):
    """install_type 归一化：空字符串=存储空间，root=系统空间"""
    if isinstance(it, str):
        t = it.strip()
        if t in ("", "存储空间", "volume", "Agent", "工具", "音乐", "影视", "站长工具"):
            return ""
        if t in ("root", "系统空间"):
            return "root"
    return ""

def normalize_run_as(ra):
    return "root" if ra == "root" else "package"

def normalize_is_docker(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes")
    if isinstance(v, (int, float)):
        return bool(v)
    return False

def clean_size(v):
    """size 仅保留非负整数（Bytes）；字符串/浮点等来源不明的全部丢弃"""
    if isinstance(v, int) and v >= 0:
        return v
    return None

def normalize_version(v):
    """版本号规范化：去掉 v 前缀；不满足可比较版本的降级为 0.0.0"""
    if not isinstance(v, str):
        v = str(v)
    v = v.strip()
    if v.startswith("v") and len(v) > 1 and v[1].isdigit():
        v = v[1:]
    if re.match(r"^\d+(\.\d+)*([a-zA-Z0-9._-]*)?$", v):
        return v
    return "0.0.0"

def legalize_key(k):
    """键名合法化：只保留 [A-Za-z0-9._-]，非法字符转连字符"""
    k2 = re.sub(r"[^A-Za-z0-9._-]", "-", str(k))
    k2 = re.sub(r"-{2,}", "-", k2).strip("-")
    if k2 and k2[0].isalnum():
        return k2
    return "app-" + (k2 or "app")

def build_v2_releases(version, arch_diff):
    """由 (version, arch_diff) 构造 V2 releases.<version>.packages.<arch> 结构"""
    version = normalize_version(version)
    packages = {}
    if isinstance(arch_diff, dict) and arch_diff:
        for arch, pv in arch_diff.items():
            if not isinstance(pv, dict) or arch not in ("all", "x86", "arm"):
                continue
            dl = pv.get("download_url")
            if not dl:
                continue
            pkg = {"download_url": dl}
            if pv.get("sha256"):
                pkg["sha256"] = pv["sha256"]
            sz = clean_size(pv.get("size"))
            if sz is not None:
                pkg["size"] = sz
            packages[arch] = pkg
    if not packages:
        return None
    return {version: {"packages": packages}}

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
    # 客户端详情页"开发者/发布者"行分别读取 developer / publisher 字段（缺失则显示"未知开发者/未知发布者"）。
    # developer 兜底为归一化作者值（真实作者）；publisher 只保留源原始值，
    # 不兜底为源名/分销商（聚合源中"源名≠发布者"，前面几版即此行为，避免把源名或 kitblue 误显示为发布者）。
    e["developer"] = e.get("developer") or str(author) if author else e.get("developer", "")
    if not e.get("developer"):
        e["developer"] = ""
    e["publisher"] = e.get("publisher", "")
    e["distributor"] = e.get("distributor") or src_meta.get("name", "")
    e["distributor_url"] = e.get("distributor_url") or src_meta.get("homepage", f"https://github.com/{owner}")
    if not e.get("publisher_url"):
        e["publisher_url"] = e.get("distributor_url") or ""
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
    # V2 categories：优先取已有 categories，否则由 labels 映射；固定 9 类最多 2 个
    cats = e.get("categories")
    if isinstance(cats, str):
        cats = [cats]
    if isinstance(cats, list) and cats:
        cats = map_labels_to_categories(",".join(str(c) for c in cats))
    else:
        cats = map_labels_to_categories(e.get("labels"))
    e["categories"] = cats or ["系统工具"]
    e.pop("labels", None)
    # V2 字段归一化
    e["install_type"] = normalize_install_type(e.get("install_type"))
    e["run_as"] = normalize_run_as(e.get("run_as"))
    e["is_docker"] = normalize_is_docker(e.get("is_docker"))
    if "isdocker" in e:
        e.pop("isdocker", None)
    if not isinstance(e.get("size"), int):
        e.pop("size", None)
    # platform 数组
    plat = e.get("platform")
    if isinstance(plat, str):
        e["platform"] = [plat]
    elif not isinstance(plat, list) or not plat:
        e["platform"] = ["all"]
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
            "_src": "conversun/fnos-apps",
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
    txt, br = try_raw(owner, repo, "moo.json")
    if txt is not None:
        apps, src_meta = parse_fnpack(txt, owner, repo, br)
        for an, v in apps.items():
            v.setdefault("_src", f"{owner}/{repo}")
            v["_src_meta"] = src_meta
            v["_branch"] = br
        return apps
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

def load_official_excludes():
    """读取官方应用商店排除清单（conversun/fnos-apps 的 appname，小写归一化）"""
    try:
        with open(OFFICIAL_LIST, encoding="utf-8") as f:
            names = [l.strip().lower() for l in f if l.strip() and not l.startswith("#")]
        return set(names)
    except FileNotFoundError:
        return set()


# 源健康检查候选路径（fnpack.json 可能位于根或子目录）
HEALTH_PATHS = ("fnpack.json", "apps.json")


def probe_source(owner, repo):
    """轻量探测单个源：HEAD fnpack.json（优先）/ apps.json，失败时回退 GET。
    返回 dict：{owner/repo, reachable, http_status, probe_path, reason, checked_at}
    额外附带 GitHub 仓库元数据（pushed_at / archived），失败留空。
    """
    checked_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    result = {"owner/repo": f"{owner}/{repo}", "reachable": False,
              "http_status": None, "probe_path": None, "reason": "",
              "pushed_at": None, "archived": None, "checked_at": checked_at}
    # 轻量探测：优先 HEAD fnpack.json
    for path in HEALTH_PATHS:
        for br in ("main", "master"):
            url = f"{RAW}/{owner}/{repo}/{br}/{path}"
            try:
                req = urllib.request.Request(url, method="HEAD", headers=_headers())
                with urllib.request.urlopen(req, timeout=8) as r:
                    result["reachable"] = True
                    result["http_status"] = r.status
                    result["probe_path"] = f"{br}/{path}"
                    break
            except urllib.error.HTTPError as e:
                result["http_status"] = e.code
                if e.code != 404:
                    result["reason"] = f"HTTP {e.code}"
                    break  # 非 404 错误（403/5xx 等）视为失败，不再探测其它路径
                continue
            except Exception as e:
                result["reason"] = f"{type(e).__name__}: {e}"
                continue
        if result["reachable"] or result["http_status"] not in (None, 404):
            break
    if not result["reachable"]:
        # 均不可达：再尝试 GET 兜底一次（区分网络失败与真实不存在）
        if not result["reason"] and result["http_status"] == 404:
            result["reason"] = "404 Not Found（main/master 下均无 fnpack.json / apps.json）"
        elif not result["reason"]:
            result["reason"] = "网络请求失败或超时"
    # 附加仓库元数据（可选，失败不影响主结论）
    try:
        req = urllib.request.Request(f"{API}/repos/{owner}/{repo}", headers=_headers())
        with urllib.request.urlopen(req, timeout=8) as r:
            meta = json.loads(r.read().decode("utf-8", "replace"))
            result["pushed_at"] = meta.get("pushed_at")
            result["archived"] = meta.get("archived")
    except Exception:
        pass
    return result


def check_source_health(sources):
    """并发探测全部源，写 source_health_report.json。返回 (report, unreachable_list)"""
    print(f"源健康监控：探测 {len(sources)} 个源...")
    report = {"checked_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              "total": len(sources), "reachable": 0, "unreachable": 0,
              "sources": []}
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
        futs = {ex.submit(probe_source, *s.split("/", 1)): s for s in sources}
        for f in concurrent.futures.as_completed(futs):
            res = f.result()
            report["sources"].append(res)
            if res["reachable"]:
                report["reachable"] += 1
            else:
                report["unreachable"] += 1
    report["sources"].sort(key=lambda x: (x["reachable"], x["owner/repo"]))
    try:
        with open(SOURCE_HEALTH, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"源健康报告已写入: {SOURCE_HEALTH}")
    except Exception as e:
        print(f"WARNING: 写源健康报告失败: {e}")
    unreachable = [r["owner/repo"] for r in report["sources"] if not r["reachable"]]
    if unreachable:
        print(f"不可达源 {len(unreachable)} 个: {', '.join(unreachable[:20])}{' ...' if len(unreachable) > 20 else ''}")
    else:
        print("全部源可达")
    return report, unreachable


def _has_release_download(e):
    """V2：releases.<ver>.packages.<arch>.download_url 是否存在"""
    rel = e.get("releases")
    if not isinstance(rel, dict):
        return False
    for rv in rel.values():
        pkg = rv.get("packages") if isinstance(rv, dict) else None
        if isinstance(pkg, dict):
            for pv in pkg.values():
                if isinstance(pv, dict) and pv.get("download_url"):
                    return True
    return False

KEY_FIELDS = ("display_name", "desc", "version", "download_url", "icon_url", "author", "categories")

def _field_value(e, fld):
    if fld == "version":
        rel = e.get("releases")
        return bool(isinstance(rel, dict) and rel)
    if fld == "download_url":
        return _has_release_download(e)
    if fld == "categories":
        return bool(e.get("categories"))
    return e.get(fld)


def metadata_quality_report(apps_out):
    """统计关键字段覆盖率，标记 incomplete 条目，写 metadata_quality_report.json。
    返回 (report, incomplete_keys)
    """
    print("元数据质量检查：统计关键字段覆盖率...")
    total = len(apps_out)
    field_missing = {k: 0 for k in KEY_FIELDS}
    incomplete = []
    per_field_stats = {}
    for key, e in apps_out.items():
        missing = []
        for fld in KEY_FIELDS:
            v = _field_value(e, fld)
            if not v or (isinstance(v, str) and not v.strip()):
                field_missing[fld] += 1
                missing.append(fld)
        if missing:
            incomplete.append({"key": key, "missing": missing})
        for fld in KEY_FIELDS:
            per_field_stats.setdefault(fld, {"present": 0, "missing": 0})
            if fld not in missing:
                per_field_stats[fld]["present"] += 1
            else:
                per_field_stats[fld]["missing"] += 1
    coverage = {fld: (round(per_field_stats[fld]["present"] / total * 100, 1) if total else 0)
                for fld in KEY_FIELDS}
    report = {
        "checked_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_apps": total,
        "incomplete_count": len(incomplete),
        "coverage_percent": coverage,
        "incomplete_apps": incomplete,
    }
    try:
        with open(META_QUALITY, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"元数据质量报告已写入: {META_QUALITY}")
    except Exception as e:
        print(f"WARNING: 写元数据质量报告失败: {e}")
    print("覆盖率: " + ", ".join(f"{k}={v}%" for k, v in coverage.items()))
    return report, [x["key"] for x in incomplete]

# 输出精简：download_url/arch_diff/version/size 已由 releases 承载，输出前由 build_v2_releases 消费；
# maintainer* 并入 author/distributor；source_repo/changelog/history/bug_report_url 非必需
TRIM_FIELDS = ("maintainer", "maintainer_url", "source_repo",
               "changelog", "history", "bug_report_url", "details_url")

# icon_url 必填字段兜底：探测补全仍缺失时指向仓库占位图标（assets/app-placeholder.png）
PLACEHOLDER_ICON = "https://raw.githubusercontent.com/kitblue/FnUnify/main/assets/app-placeholder.png"

def trim_entry(e):
    """精简单个条目：删除已由 download_url/arch_diff 承载的冗余字段"""
    for k in TRIM_FIELDS:
        e.pop(k, None)
    return e


def dedup_apps(apps_out):
    """同名同包名去重：保留数据最完整的那个，其他标记为 fork 并保留 _fork_of 溯源。
    评分优先级：官方源(conversun/fnos-apps) > 有 maintainer/distributor/developer 字段 > icon 覆盖 > 源顺序
    """
    name_groups = defaultdict(list)
    for key, e in apps_out.items():
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
    return final_out


def main():
    with open(REPO_LIST, encoding="utf-8") as f:
        sources = [l.strip() for l in f if l.strip() and not l.startswith("#")]
    official_excludes = load_official_excludes()
    if official_excludes:
        print(f"官方应用排除清单: {len(official_excludes)} 个（fnOS 官方商店自带，不重复收录）")
    print(f"源订阅模式: 拉取 {len(sources)} 个源...")

    # 源健康监控：轻量探测每个源可达性，写 source_health_report.json
    health_report, unreachable_sources = check_source_health(sources)
    if unreachable_sources:
        print(f"[health] {len(unreachable_sources)} 个源不可达（详见 {SOURCE_HEALTH}），仍尝试拉取，结果以解析为准")

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
                if an in ("source_info", "apps", "fndepot.source"):
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
        out.setdefault("install_type", "")
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
    n_before_dedup = len(apps_out)
    apps_out = dedup_apps(apps_out)
    # 排除 fnOS 官方商店自带应用（不重复收录，官方由官方商店提供）
    # 兼容旧行为：默认排除；设置环境变量 FnUnify_INCLUDE_OFFICIAL=1 时收录官方（conversun/fnos-apps）
    include_official = os.environ.get("FnUnify_INCLUDE_OFFICIAL", "").strip().lower() in ("1", "true", "yes")
    if official_excludes and not include_official:
        dropped = [a for a in apps_out if a in official_excludes]
        for a in dropped:
            del apps_out[a]
        print(f"排除官方应用 {len(dropped)} 个 -> 剩余 {len(apps_out)} 个")
    else:
        print(f"官方应用排除已跳过 ({'收录官方 conversun/fnos-apps' if include_official else '无排除清单'})")
    print(f"去重后: {len(apps_out)} 唯一应用 (合并 {n_before_dedup} 条源条目)")

    # dedup/排除官方应用后，仅保留仍在 apps_out 中的缺失记录，避免旧 key KeyError
    missing_icons = [(k, o, r, b, a) for (k, o, r, b, a) in missing_icons if k in apps_out]
    missing_sizes = [(k, url) for (k, url) in missing_sizes if k in apps_out]
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
    # 并构造 V2 releases 结构、精简冗余字段，将 fnpack.json 控制在 jsDelivr 1024KB 限制内
    for e in apps_out.values():
        for k in ("_src_meta", "_orig", "_branch"):
            e.pop(k, None)
        # V2：平铺 download_url/arch_diff/version -> releases.<version>.packages.<arch>
        rel = build_v2_releases(e.get("version", ""), e.get("arch_diff"))
        if rel is None and e.get("download_url"):
            plat = e.get("platform") or []
            if isinstance(plat, str):
                plat = [plat]
            arch = "all" if ("all" in plat or len(plat) != 1 or plat[0] not in ("x86", "arm")) else plat[0]
            rel = {normalize_version(e.get("version", "")): {"packages": {arch: {"download_url": e["download_url"]}}}}
        if rel is not None:
            e["releases"] = rel
        for k in ("arch_diff", "download_url", "version", "size"):
            e.pop(k, None)
        # service_port 必须是字符串（FnDepot 客户端校验要求），数字一律转字符串
        if "service_port" in e and not isinstance(e["service_port"], str):
            e["service_port"] = str(e["service_port"])
        # icon_url 必填兜底
        if not e.get("icon_url"):
            e["icon_url"] = PLACEHOLDER_ICON
        trim_entry(e)

    # 元数据质量检查：统计关键字段覆盖率，标记 incomplete，写 metadata_quality_report.json
    quality_report, incomplete_keys = metadata_quality_report(apps_out)
    if incomplete_keys:
        print(f"[quality] {len(incomplete_keys)} 个应用缺少关键字段（详见 {META_QUALITY}）")

    # 输出：官方 FnDepot V2 三层结构（schema_version 字符串 "2" + source_info + apps），
    # 键名合法化（仅允许 [A-Za-z0-9._-]），每个应用一行紧凑输出
    source_info = {
        "name": "FnUnify",
        "author": "kitblue",
        "homepage": "https://github.com/kitblue/FnUnify",
        "description": f"飞牛 fnOS 第三方应用聚合源：聚合 {len(sources)} 个第三方源、去重后收录 {len(apps_out)} 个应用（含官方 conversun/fnos-apps 源收录）。",
    }
    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write('{"schema_version": "2", "source_info": '
                + json.dumps(source_info, ensure_ascii=False, separators=(",", ":"))
                + ', "apps": {\n')
        keys = list(apps_out.keys())
        for i, k in enumerate(keys):
            comma = "," if i < len(keys) - 1 else ""
            f.write(("  " if i == 0 else " ") + json.dumps(legalize_key(k), ensure_ascii=False) + ": "
                    + json.dumps(apps_out[k], ensure_ascii=False, separators=(",", ":"))
                    + comma + "\n")
        f.write("}}\n")
    print(f"完成: {len(apps_out)} 个应用 -> {OUTPUT} (官方 FnDepot V2 格式, schema_version '2', 每应用一行)")

if __name__ == "__main__":
    main()
