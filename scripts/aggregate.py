#!/usr/bin/env python3
"""
FnUnify 聚合脚本 v3
- 支持三种源格式：fnpack.json 顶层键式(V2)、fnpack.json 包装层({schema_version,source_info,apps})、apps.json(v1)
- releases 解析：从最新版 releases[ver].packages 提取 download_url / arch_diff / version
- 相对路径 icon_url/download_url/preview_urls 转 raw 绝对直链
- 开发者信息归一化：author/author_url 继承 maintainer/distributor/developer/source_info，缺失兜底仓库 owner；
  官方 conversun/fnos-apps 作者留空（避免把仓库维护者当应用作者）；fnos-apps 镜像源(fork)作者从 source_info.author 或镜像 owner 继承
- 连接优先 API：设置了 GITHUB_TOKEN 时用 GitHub API 拉取（限流宽松），否则回退 raw 直链
- 去重（源订阅模式）：按 display_name 去重（同一 display_name 仅保留数据最完整的一个），若 display_name 相同且来源不同，额外加 _fork_of 溯源标记。注意：各 fork 源已用 appname 加 -N 后缀区分，按 appname 直接去重会误判，应改用 display_name 或规范化后包名去后缀比对。
- 重复源检测：同一 owner/repo 在 repo_list 中出现多次时，仅保留第一个出现，其余标记为重复源。
- 源健康监控：对 repo_list.txt 每个源做 HTTP HEAD/GET 轻量探测（fnpack.json 路径），
  不可达源在报告 source_health_report.json 中标记 unreachable，并附带 HTTP 状态码/失败原因/检查时间；
  可达源追加 GET 结构验证（structure_ok），识别 200 但内容非合法应用清单的伪健康源。
- 元数据质量：输出前统计 display_name/desc/version/download_url/icon_url 等关键字段覆盖率，
  不完整条目标记 incomplete（severity=warning，非阻断），报告写入 metadata_quality_report.json。
- 去重加权：fnos-apps 类源（官方 conversun/fnos-apps 及其 fork 镜像）统一按 fnos-apps 加权；
  分组键优先 display_name 规范化，回退 appname；winner 缺图标时组内同名继承。
- 输出官方 FnDepot V2 格式（schema_version 字符串 "2"、source_info、apps；
  categories 固定 9 类最多 2 个；releases.<version>.packages.<arch> 嵌套结构；
  install_type 归一化 ""/root；run_as 归一化 package/service（root 视为非法，统一映射为 service）；
  is_docker 布尔；size 非负整数；categories 非法旧分类映射到合法 9 类（见 LEGACY_CATEGORY_MAP））
"""
import json, re, os, sys, datetime, base64, hashlib, time
from collections import defaultdict
import urllib.request, urllib.error, concurrent.futures

REPO_LIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "repo_list.txt")
OFFICIAL_LIST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "official_apps.txt")
OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fnpack.json")
SOURCE_HEALTH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "source_health_report.json")
META_QUALITY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "metadata_quality_report.json")
CHANGELOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "CHANGELOG.md")
RAW = "https://raw.githubusercontent.com"
API = "https://api.github.com"
TOKEN = os.environ.get("GITHUB_TOKEN", "")

# 增量拉取缓存（可选增强）：内存缓存 + 可选磁盘缓存
_mem_cache = {}
_CACHE_DIR = os.environ.get("FnUnify_CACHE_DIR", "").strip()
_CACHE_TTL = 6 * 3600  # 磁盘缓存有效期 6 小时

def _cache_path(url):
    if not _CACHE_DIR:
        return None
    h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    d = os.path.join(_CACHE_DIR, ".fnunify_cache")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        return None
    return os.path.join(d, h + ".txt")

def _disk_cache_get(url):
    p = _cache_path(url)
    if not p or not os.path.exists(p):
        return None
    try:
        if time.time() - os.path.getmtime(p) > _CACHE_TTL:
            return None
        with open(p, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None

def _disk_cache_set(url, txt):
    p = _cache_path(url)
    if not p:
        return
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write(txt)
    except Exception:
        pass

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
    """优先 API（带 token 时），失败回退 raw 直链。
    增量拉取缓存（可选增强）：设置 FnUnify_CACHE_DIR 时启用磁盘缓存（缓存目录外的
    .fnunify_cache 子目录，按 URL 哈希存储，6 小时内复用，避免重复拉取同一源文件）；
    未设置时退化为进程内内存缓存（同一次运行中相同 URL 只请求一次）。"""
    url = f"{RAW}/{owner}/{repo}/{branch}/{path}"
    if TOKEN:
        api_url = f"{API}/repos/{owner}/{repo}/contents/{path}?ref={branch}"
    else:
        api_url = None

    cache_key = url
    mem_val = _mem_cache.get(cache_key)
    if mem_val is not None:
        return mem_val or None
    disk_hit = _disk_cache_get(url)
    if disk_hit is not None:
        _mem_cache[cache_key] = disk_hit
        return disk_hit or None

    txt = None
    if api_url:
        atxt = _http(api_url)
        if atxt:
            try:
                j = json.loads(atxt)
                if isinstance(j, dict) and j.get("content"):
                    txt = base64.b64decode(j["content"]).decode("utf-8", "replace")
            except Exception:
                pass
    if txt is None:
        for _ in range(2):
            txt = _http(url)
            if txt:
                break
    if txt is None:
        # 直连 GitHub 超时/被墙时回退 gh-proxy 代理（国内网络场景）
        for _ in range(2):
            txt = _http(f"https://gh-proxy.com/{url}")
            if txt:
                break
    if txt:
        _mem_cache[cache_key] = txt
        _disk_cache_set(url, txt)
    return txt

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
# fnOS 应用商店 / FnDepot V2 校验认可的固定分类（官方 9 类，最多 2 个）
VALID_CATEGORIES = ("影音娱乐", "系统工具", "编程开发", "AI赋能", "生活服务",
                    "智能智控", "教育学习", "游戏地带", "硬件驱动")
# 历史源/其它体系分类 -> 官方 9 类映射（输出前统一转换）
LEGACY_CATEGORY_MAP = {
    "编程开发": "编程开发",
    "游戏地带": "游戏地带",
    "影音娱乐": "影音娱乐",
    "生活服务": "生活服务",
    "AI赋能": "AI赋能",
    "硬件驱动": "硬件驱动",
    "智能智控": "智能智控",
    "教育学习": "教育学习",
    "系统工具": "系统工具",
    "开发工具": "编程开发",
    "实用工具": "系统工具",
    "媒体工具": "影音娱乐",
    "安全工具": "系统工具",
    "数据库": "系统工具",
    "文件系统": "系统工具",
    "网络服务": "系统工具",
    "其他": "系统工具",
    "其它": "系统工具",
    "工具": "系统工具",
    "办公": "生活服务",
    "效率": "系统工具",
    "运维": "系统工具",
    "监控": "系统工具",
    "下载": "系统工具",
    "网盘": "影音娱乐",
    "阅读": "影音娱乐",
    "音乐": "影音娱乐",
    "影视": "影音娱乐",
    "直播": "影音娱乐",
    "游戏": "游戏地带",
    "NAS": "系统工具",
    "Docker": "系统工具",
    "docker": "系统工具",
    "AI": "AI赋能",
    "智能": "AI赋能",
    "教育": "教育学习",
    "学习": "教育学习",
}
FIXED_CATEGORIES = VALID_CATEGORIES

def legalize_categories(cats):
    """categories 列表 -> 官方 9 类：非官方分类经 LEGACY_CATEGORY_MAP 映射，
    仍不合法则丢弃；去重保序，最多 2 个。"""
    out = []
    for c in cats or []:
        c2 = LEGACY_CATEGORY_MAP.get(c, c)
        if c2 in VALID_CATEGORIES and c2 not in out:
            out.append(c2)
        if len(out) >= 2:
            break
    return out

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
    """run_as 归一化：V2 合法值为 package/root，其余（如 service）映射为
    package；root 保留。"""
    if not isinstance(ra, str):
        return "package"
    t = ra.strip().lower()
    if t in ("package", "root"):
        return t
    return "package"

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

GITHUB_PROJECTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "github_projects.json")
_gh_projects = {}
try:
    with open(GITHUB_PROJECTS_FILE, "r", encoding="utf-8") as f:
        _gh_projects = {str(k).lower(): v for k, v in json.load(f).items() if not str(k).startswith("_")}
except Exception:
    _gh_projects = {}
_gh_cache = {}

def _gh_owner_from_download(e):
    """从条目字段中提取 GitHub owner 字符串（不经过 API 查询）。
    字段优先级：homepage > download_url > readme_url。
    返回 owner 字符串，若无法解析则返回 None。"""
    for fld in ("homepage", "download_url", "readme_url"):
        v = e.get(fld)
        if not isinstance(v, str) or not v:
            continue
        m = re.search(r"https://github\.com/([^/\s]+)/([^/\s]+)", v)
        if m:
            return m.group(1)
        m = re.search(r"https://raw\.githubusercontent\.com/([^/\s]+)/([^/\s]+)", v)
        if m:
            return m.group(1)
    return None


def _extract_github_owner(url: str) -> str | None:
    """从单个 GitHub URL 中提取 owner 字符串。"""
    if not isinstance(url, str) or not url:
        return None
    m = re.search(r"https://github\.com/([^/\s]+)/([^/\s]+)", url)
    if m:
        return m.group(1)
    m = re.search(r"https://raw\.githubusercontent\.com/([^/\s]+)/([^/\s]+)", url)
    if m:
        return m.group(1)
    return None


def _extract_owner_from_releases(releases: dict, src_owner: str) -> str | None:
    """从 releases 包下载链接中提取 GitHub owner，跳过指向源仓库的链接。
    遍历每个 release 版本、每个架构包，收集所有 download_url。
    如果任一 URL 的 owner 不等于当前源 owner，返回该 owner（表示真实项目）。
    若全部 URL 都指向源仓库则返回 None。"""
    if not isinstance(releases, dict):
        return None
    for ver, ver_data in releases.items():
        pkgs = ver_data.get("packages", {}) if isinstance(ver_data, dict) else {}
        for plat, pkg_data in pkgs.items():
            url = pkg_data.get("download_url") if isinstance(pkg_data, dict) else None
            if not url:
                continue
            owner = _extract_github_owner(url)
            if owner is None:
                continue
            if owner.lower() == src_owner.lower():
                continue  # 指向源仓库，跳过
            return owner  # 找到真实项目 owner
    return None


def fill_from_github_project(e, owner, repo, extra_urls=None, appname=None):
    """developer/publisher 兜底：从条目字段（homepage/download_url/readme_url）、
    extra_urls（已绝对化的下载链接）、releases 包下载链接及 github_projects.json
    映射表中提取 GitHub owner 字符串，直接作为 developer/publisher 填充。
    不经过 API 查询，无需 PAT，快速高效。
    源有 developer/publisher 原始值时保留，缺失才兜底；源搜集者（源仓库 owner）不是
    开发者/发布者，绝不写入。"""
    src_owner = (owner or "").lower()

    def extract_from_fields(entry):
        """字段级提取：homepage > download_url > readme_url > extra_urls"""
        for fld in ("homepage", "download_url", "readme_url"):
            v = entry.get(fld)
            if not isinstance(v, str) or not v:
                continue
            ow = _gh_owner_from_download({fld: v})
            if ow:
                return ow
        for u in (extra_urls or []):
            ow = _extract_github_owner(u)
            if ow:
                return ow
        return None

    def extract_from_archdiff(entry):
        """arch_diff 包下载链接提取（绝对化后）"""
        ad = entry.get("arch_diff")
        if not isinstance(ad, dict):
            return None
        for arch, av in ad.items():
            if isinstance(av, dict) and av.get("download_url"):
                ow = _extract_github_owner(av["download_url"])
                if ow and ow.lower() != src_owner:
                    return ow
        return None

    # 提取一次 owner 供 developer/publisher 复用
    owner_found = None
    if not e.get("publisher") or not e.get("developer"):
        owner_found = extract_from_fields(e) or extract_from_archdiff(e) or \
                      _extract_owner_from_releases(e.get("releases", {}), src_owner)
        # 映射表兜底：字段/链接提取不到时，按 appname（去 -N 后缀小写）查表
        if not owner_found and appname:
            mapped = _gh_projects.get(_strip_num_suffix(str(appname).lower()))
            if mapped and "/" in str(mapped):
                m_owner = str(mapped).split("/", 1)[0]
                if m_owner.lower() != src_owner:
                    owner_found = m_owner
    if owner_found and owner_found.lower() != src_owner:
        if not e.get("developer"):
            e["developer"] = owner_found
            if not e.get("developer_url"):
                e["developer_url"] = f"https://github.com/{owner_found}"
        if not e.get("publisher"):
            e["publisher"] = owner_found
            if not e.get("publisher_url"):
                e["publisher_url"] = f"https://github.com/{owner_found}"

def normalize_entry(entry, owner, repo, branch, src_meta=None):
    """源订阅模式：原样保留源字段，不做作者改写/兜底（避免显示错误作者）"""
    e = dict(entry)
    src_meta = src_meta or {}
    # 作者：按源字段原样取值；fnos-apps 类源（官方及其 fork）maintainer 实为仓库维护者，
    # 不等于应用作者，若等于 conversun/源作者则留空（与官方直加一致），避免显示错误作者；
    # 镜像源（非官方 conversun 的 fnos-apps fork）作者从 source_info.author > distributor > 镜像仓库 owner 继承
    src_is_fnos_apps = "fnos-apps" in (entry.get("_src") or "").lower()
    src_owner = (entry.get("_src") or "").split("/", 1)[0]
    author = (e.get("maintainer") or e.get("distributor") or e.get("developer"))
    if src_is_fnos_apps:
        if author in ("conversun", src_meta.get("author"), src_meta.get("distributor")):
            author = ""
        if not author and src_owner != "conversun":
            author = src_meta.get("author") or src_meta.get("distributor") or src_owner
    else:
        author = author or src_meta.get("author") or src_meta.get("distributor")
    e["author"] = str(author) if author else ""
    e["author_url"] = (e.get("maintainer_url") or e.get("distributor_url")
                       or e.get("developer_url") or (f"https://github.com/{owner}" if author else ""))
    # 客户端详情页"开发者/发布者"行分别读取 developer / publisher 字段（缺失则显示"未知开发者/未知发布者"）。
    # developer 兜底为归一化作者值（真实作者）；publisher 只保留源原始值，
    # 不兜底为源名/分销商（聚合源中"源名≠发布者"，前面几版即此行为，避免把源名或 kitblue 误显示为发布者）。
    # developer / publisher：不做数据兜底，源条目有原始值才保留，没有就留空
    # （客户端对应显示"未知开发者/未知发布者"）。严禁用源名/分销商/归一化作者乱填，
    # 乱填会导致字段与离线安装包元数据不一致，进而被客户端识别为不同应用。
    e["developer"] = (e.get("developer") or "").strip()
    e["publisher"] = (e.get("publisher") or "").strip()
    # GitHub 项目补全（轻量级兜底）：developer/publisher 缺失时，先尝试从 download_url
    # 自动解析真实 GitHub 项目抓取开发者；解析失败/指向源仓库再查映射表。
    # 源搜集者（源仓库 owner/源名）不是应用开发者，严禁写入 developer/publisher。
    fill_from_github_project(e, owner, repo)
    e["distributor"] = (e.get("distributor") or "").strip()
    e["distributor_url"] = e.get("distributor_url") or ""
    # publisher_url 兜底：优先 distributor_url，缺失时用 developer_url（详情页链接可点击）
    if not e.get("publisher_url"):
        e["publisher_url"] = e.get("distributor_url") or e.get("developer_url") or ""
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
    # V2 categories：优先取已有 categories，否则由 labels 映射；统一映射为 V2 合法 9 类，最多 2 个
    cats = e.get("categories")
    if isinstance(cats, str):
        cats = [cats]
    if isinstance(cats, list) and cats:
        cats = map_labels_to_categories(",".join(str(c) for c in cats))
    else:
        cats = map_labels_to_categories(e.get("labels"))
    e["categories"] = legalize_categories(cats) or ["系统工具"]
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

def parse_apps_json(txt, src="conversun/fnos-apps"):
    """fnos-apps apps.json v1 -> {appname: entry}；src 为所属源（fork 传 fork 仓库，官方默认 conversun）"""
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
            "_src": src,
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
            if f"{owner}/{repo}" in DIRECT_SOURCES:
                # 直引源：保持上游原始 _src/_upstream 溯源不变，不做强制改写；无 _src 时补本源归属
                v["_direct"] = True
                if not v.get("_src"):
                    v["_src"] = f"{owner}/{repo}"
            else:
                # _src 强制为本源仓库；manifest 中自带的真上游溯源（如子源条目）挪到 _upstream 保留
                if v.get("_src") and str(v["_src"]) != f"{owner}/{repo}":
                    v["_upstream"] = v["_src"]
                v["_src"] = f"{owner}/{repo}"
            v["_src_meta"] = src_meta
            v["_branch"] = br
        return apps
    txt, br = try_raw(owner, repo, "apps.json")
    if txt is not None:
        apps = parse_apps_json(txt, f"{owner}/{repo}")
        if f"{owner}/{repo}" in DIRECT_SOURCES:
            for an, v in apps.items():
                v["_direct"] = True
                v["_branch"] = br
        return apps
    txt, br = try_raw(owner, repo, "moo.json")
    if txt is not None:
        apps, src_meta = parse_fnpack(txt, owner, repo, br)
        for an, v in apps.items():
            if f"{owner}/{repo}" in DIRECT_SOURCES:
                v["_direct"] = True
                if not v.get("_src"):
                    v["_src"] = f"{owner}/{repo}"
            else:
                if v.get("_src") and str(v["_src"]) != f"{owner}/{repo}":
                    v["_upstream"] = v["_src"]
                v["_src"] = f"{owner}/{repo}"
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
    """读取官方应用商店排除清单（conversun/fnos-apps）。

    返回 dict：
      names        官方 appname 集合（小写）
      names_norm   appname 规范化集合（去空白/连字符/下划线/点号）
      displays     display_name 规范化集合（在线拉 conversun/fnos-apps apps.json 补充；
                   拉取失败时回退为空集，仅按 appname 通道排除）
    """
    try:
        with open(OFFICIAL_LIST, encoding="utf-8") as f:
            names = {l.strip().lower() for l in f if l.strip() and not l.startswith("#")}
    except FileNotFoundError:
        names = set()
    displays = set()
    try:
        txt = _http("https://raw.githubusercontent.com/conversun/fnos-apps/main/apps.json", timeout=15)
        j = json.loads(txt)
        apps = j["apps"] if isinstance(j, dict) and "apps" in j else j
        if isinstance(apps, list):
            for a in apps:
                an = (a.get("appname") or "").lower()
                if an:
                    names.add(an)
                dn = (a.get("display_name") or a.get("name") or "")
                if dn:
                    displays.add(_norm_dedup_name(dn))
        elif isinstance(apps, dict):
            for k, a in apps.items():
                an = str(k).lower()
                names.add(an)
                if isinstance(a, dict):
                    dn = (a.get("display_name") or a.get("name") or "")
                    if dn:
                        displays.add(_norm_dedup_name(dn))
    except Exception:
        pass
    return {"names": names, "names_norm": {_norm_dedup_name(x) for x in names},
            "displays": displays}


# 源健康检查候选路径（fnpack.json 可能位于根或子目录）
HEALTH_PATHS = ("fnpack.json", "apps.json")


def probe_source(owner, repo):
    """轻量探测单个源：HEAD fnpack.json（优先）/ apps.json，失败时回退 GET。
    返回 dict：{owner/repo, reachable, http_status, probe_path, reason, checked_at}
    HEAD 成功后追加 GET 结构验证（structure_ok / structure_note）：内容须为合法 JSON 且含有效应用清单结构。
    额外附带 GitHub 仓库元数据（pushed_at / archived），失败留空。
    """
    checked_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    result = {"owner/repo": f"{owner}/{repo}", "reachable": False,
              "http_status": None, "probe_path": None, "reason": "",
              "structure_ok": None, "structure_note": "",
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
                    # 结构验证：GET 内容必须是合法 JSON 且含有效应用清单结构
                    txt = _http(f"{RAW}/{owner}/{repo}/{br}/{path}", timeout=12)
                    if txt is None:
                        result["structure_ok"] = False
                        result["structure_note"] = "GET 拉取内容失败"
                    else:
                        try:
                            j = json.loads(txt)
                            if path == "fnpack.json":
                                ok = (isinstance(j, dict) and
                                      (("apps" in j and isinstance(j["apps"], dict))
                                       or any(isinstance(v, dict) and ("display_name" in v or "download_url" in v or "releases" in v)
                                              for v in j.values())))
                            else:  # apps.json
                                ok = isinstance(j, dict) and isinstance(j.get("apps"), list) and bool(j["apps"])
                            result["structure_ok"] = ok
                            result["structure_note"] = "ok" if ok else "结构异常：无法解析为有效应用清单"
                        except Exception:
                            result["structure_ok"] = False
                            result["structure_note"] = "非合法 JSON"
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
    # 结构异常统计：可达但内容非有效应用清单的源（伪健康源）
    structure_bad = [r["owner/repo"] for r in report["sources"]
                     if r["reachable"] and r.get("structure_ok") is False]
    if structure_bad:
        print(f"结构异常源 {len(structure_bad)} 个（200 但内容非有效应用清单）: {', '.join(structure_bad[:20])}{' ...' if len(structure_bad) > 20 else ''}")
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


HEALTH_STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "source_health_state.json")
PRUNE_THRESHOLD = 3  # 连续不可达天数阈值，达到后自动剔除
# 用户决策：以下源即使长期不可达也保留在 repo_list.txt（不移除、不剔除）
# 注：keep_sources.txt 机制已废弃；KITblue/fantune 不再作为独立源，其条目由 fnthird 收录承接。
PRUNE_EXEMPT = {"Blue-Mink/moo", "kci-lnk/fn-knock-turborepo"}
# 直引源模式（原样保留）：以下源引入的 app 条目完全保留上游原始信息，
# 跳过 normalize_entry 的全部信息改写（developer/publisher 兜底、author/distributor 继承、
# display_name/desc/categories 归一化等），且不强制改写 _src/_upstream 溯源。
# 覆盖 11 个优质聚合源 + conversun/fnos-apps 官方源；自有子源 kitblue/fnthird 仍按自有逻辑处理。
# （KITblue/fantune 已不再作为独立源收录，其条目经 fnthird 承接。）
DIRECT_SOURCES = {
    "shuangji66/FnDepot", "bekafka/FnDepot", "rockchild2332/fnos-apps-fndepot",
    "RROrg/fn-apps", "Brian099/FnDepot", "yuexps/FnDepot", "cmbya/FnDepot",
    "magiccode1412/FnDepot", "Kasbuky-sudo/FnDepot", "Hxido-RXM/FnDepot",
    "conversun/fnos-apps", "EWEDLCM/FnDepot",
}

def prune_unreachable_sources(sources, unreachable_sources):
    """不可达源自动剔除（#7）：维护连续失败计数状态文件，
    连续 PRUNE_THRESHOLD 天不可达的源自动从 repo_list.txt 移除（注释保留痕迹）。
    单次抖动不会误删；剔除记录追加到源健康报告。返回本次剔除列表。"""
    if not unreachable_sources:
        return []
    unreachable_sources = [s for s in unreachable_sources if s not in PRUNE_EXEMPT]
    state = {}
    try:
        if os.path.exists(HEALTH_STATE):
            with open(HEALTH_STATE, encoding="utf-8") as f:
                state = json.load(f)
    except Exception:
        state = {}
    today = datetime.date.today().isoformat()
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    pruned = []
    for src in unreachable_sources:
        st = state.get(src, {"count": 0, "last": ""})
        if st.get("last") == today:
            continue  # 同日重复探测不累加
        st["count"] = st["count"] + 1 if st.get("last") == yesterday else 1
        st["last"] = today
        state[src] = st
        if st["count"] >= PRUNE_THRESHOLD:
            pruned.append(src)
    try:
        with open(HEALTH_STATE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"WARNING: 写源健康状态失败: {e}")
    if not pruned:
        return []
    # 从 repo_list.txt 移除（保留注释行，可追溯）
    removed = []
    try:
        with open(REPO_LIST, encoding="utf-8") as f:
            lines = f.readlines()
        new_lines = []
        for ln in lines:
            s = ln.strip().lstrip("\ufeff")
            if s in pruned:
                removed.append(s)
                new_lines.append(f"# {ln.rstrip(chr(10))}  # pruned {today} (unreachable x{PRUNE_THRESHOLD})\n")
            else:
                new_lines.append(ln)
        with open(REPO_LIST, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        print(f"[prune] 自动剔除连续 {PRUNE_THRESHOLD} 天不可达源 {len(removed)} 个: {', '.join(removed)}")
    except Exception as e:
        print(f"WARNING: 剔除不可达源失败: {e}")
    return removed


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
            incomplete.append({"key": key, "missing": missing, "severity": "warning"})
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
        "quality_level": "warning",
        "note": "缺失关键字段的应用标记为 warning 级别（非阻断）：商店仍可安装，仅详情页字段不完整；author/desc 缺失常见于官方直加与精简 fork 源。",
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


def _norm_dedup_name(s):
    """去重规范化：小写并去除空白/连字符/下划线/点号，用于 display_name / appname 比对"""
    return re.sub(r"[\s_.\-]+", "", str(s or "").lower())

def _strip_num_suffix(name):
    """去掉末尾 -N 数字后缀（fancontrolserver-2 -> fancontrolserver）"""
    m = re.match(r'^(.+)-\d+$', name)
    return m.group(1) if m else name

def _is_fnos_apps_like(src):
    """fnos-apps 类源判定：官方 conversun/fnos-apps 及其 fork 镜像统一识别（用于去重加权）。
    仅当仓库名恰为 'fnos-apps'（官方仓库或 fork 保留原名）时判定为官方镜像；
    RROrg/fn-apps、rockchild2332/fnos-apps-fndepot 等派生名不算官方镜像，避免整源误排除。
    排除 kitblue/fnthird：它是 FnUnify 自己的原生 manifest 子源，非官方镜像。"""
    def _check(p):
        p = str(p or "").lower()
        if p in ("kitblue/fnthird", "fnthird"):
            return False
        if "/" not in p:
            return False
        return p.split("/", 1)[1] == "fnos-apps"
    if isinstance(src, (list, tuple, set)):
        parts = [str(x or "").lower() for x in src]
        # 子源豁免：列表里仅含 kitblue 子源 + 普通第三方源时不算官方镜像
        others = [p for p in parts if p not in ("kitblue/fnthird", "fnthird")]
        if not others:
            return False
        return any(_check(p) for p in others)
    return _check(src)

def dedup_apps(apps_out):
    """同应用去重：分组键优先 display_name 规范化（兼容 Jellyfin / jellyfin 等写法差异），
    回退 appname；跨源同名但 display_name 不同的条目，按 appname 去 -N 后缀归并（#2）。
    保留数据最完整的那个，其他标记为 fork 并保留 _fork_of 溯源。
    评分优先级：fnos-apps 类源(官方与镜像统一加权) > maintainer/distributor/developer 字段 > icon 覆盖 > 源顺序。
    winner 缺失 icon_url 时从组内其它条目同名继承（上游 > 同名继承 > 探测 > 占位）。
    winner appname 统一标准化：组内优先选不带数字后缀的版本，消除 fancontrolserver/fancontrolserver-2 等跨源不一致问题。
    """
    name_groups = defaultdict(list)   # display_name 规范化 -> keys
    appname_groups = defaultdict(list)  # appname 去后缀 -> keys
    for key, e in apps_out.items():
        g = _norm_dedup_name(e.get("display_name")) or _norm_dedup_name(e.get("appname"))
        name_groups[g].append(key)
        ag = _strip_num_suffix(e.get("appname") or key)
        appname_groups[ag].append(key)

    # 跨 display_name 归并：同一 appname 去后缀出现在多个 display_name 组时合并（#2）
    # 例：FanTune（display=FanTune, appname=fancontrolserver）与
    #     Fan Control Server（display=Fan Control Server, appname=fancontrolserver-2）→ 同一应用
    merged = []            # 最终组（每个组是 key 列表）
    consumed_groups = set()
    # 先按 display_name 组遍历；遇到跨组 appname 时把涉及的组全合并
    gnames = list(name_groups.keys())
    for gi, gname in enumerate(gnames):
        if gi in consumed_groups:
            continue
        group_keys = list(name_groups[gname])
        # 找与该组共享 appname canonical 的其他组
        share = set()
        for k in group_keys:
            ag = _strip_num_suffix(apps_out[k].get("appname") or k)
            for k2 in appname_groups.get(ag, []):
                k2_group = _norm_dedup_name(apps_out[k2].get("display_name")) or _norm_dedup_name(apps_out[k2].get("appname"))
                share.add(k2_group)
        share.discard(gname)
        for other in share:
            if other in name_groups and gnames.index(other) not in consumed_groups:
                group_keys.extend(name_groups[other])
                consumed_groups.add(gnames.index(other))
        merged.append(group_keys)

    final_out = {}
    for keys in merged:
        if not keys:
            continue
        if len(keys) == 1:
            k0 = keys[0]
            final_out[apps_out[k0].get("appname") or k0] = apps_out[k0]
            continue
        # 评分：fnos-apps 类源(+10) > maintainer(+3) > distributor(+2) > developer(+1) > icon_url(+1)
        def score(k):
            e = apps_out[k]
            s = 0
            src = e.get("_src", "")
            if _is_fnos_apps_like(src):
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
        # 同名图标继承：winner 缺图标时从组内其它条目补 icon_url
        # 直引源条目跳过继承，保持上游原始信息完全不变
        if not e.get("icon_url") and not e.get("_direct"):
            for k2 in keys:
                if k2 == winner:
                    continue
                ic = apps_out[k2].get("icon_url")
                if ic:
                    e["icon_url"] = ic
                    break
        # appname 标准化：组内优先选不带 -N 后缀的版本，消除跨源不一致
        all_appnames = [apps_out[k2].get("appname") or k2 for k2 in keys]
        canonical_names = [_strip_num_suffix(n) for n in all_appnames]
        # 优先选 canonical（去掉后缀后）且最短的
        canonical_set = set(canonical_names)
        if len(canonical_set) == 1:
            # 所有条目规范化后相同，直接用
            best_name = canonical_names[0]
        else:
            # 部分带后缀部分不带，选不带后缀的（canonical_set 中不带后缀的）
            non_suffix_names = [n for n in canonical_names if n == all_appnames[canonical_names.index(n)]]
            if non_suffix_names:
                best_name = min(non_suffix_names, key=len)
            else:
                best_name = canonical_names[0]
        e["appname"] = best_name
        # 溯源：全部来源仓库列表并入 _src（不修改原始字段值）
        srcs = [apps_out[k].get("_src", "") for k in keys]
        e["_src"] = srcs if len(srcs) > 1 else srcs[0]
        e["_fork_of"] = winner
        final_out[e.get("appname") or winner] = e
        if len(keys) > 1:
            print(f"  {keys[0]}: {len(keys)} 源 -> {winner} (+{len(keys)-1} 合并)")
    return final_out


def main():
    with open(REPO_LIST, encoding="utf-8") as f:
        sources = [l.strip().lstrip("\ufeff") for l in f if l.strip() and not l.startswith("#")]
    official_excludes = load_official_excludes()
    if official_excludes.get("names"):
        print(f"官方应用排除清单: {len(official_excludes['names'])} 个（fnOS 官方商店自带，不重复收录）")
    print(f"源订阅模式: 拉取 {len(sources)} 个源...")

    # 源健康监控：轻量探测每个源可达性，写 source_health_report.json
    health_report, unreachable_sources = check_source_health(sources)
    # 不可达源自动剔除（#7）：连续不可达达阈值从 repo_list.txt 移除
    pruned = prune_unreachable_sources(sources, unreachable_sources)
    if pruned:
        try:
            health_report["pruned_sources"] = pruned
            with open(SOURCE_HEALTH, "w", encoding="utf-8") as f:
                json.dump(health_report, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
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

    # 自家子源兜底：并发拉取可能因 GitHub 限流/抖动漏掉 kitblue/fnthird，
    # 漏了会导致独立应用（hermes-studio/qwenpaw/deepseek.harness 及承接的 FanTune 等）被转发源低质条目顶掉。
    # 顺序重拉一次，保证子源条目一定进入去重池。
    if not any((e.get("_src") or "").startswith("kitblue/fnthird") for _, e in entries):
        print("[fallback] kitblue/fnthird 未收录，顺序重拉兜底...")
        fb = load_source("kitblue/fnthird")
        print(f"  [fallback] 重拉得到 {len(fb) if fb else 0} 条")
        if fb:
            for an, entry in fb.items():
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
        is_direct = bool(e.get("_direct"))
        if is_direct:
            # 直引源模式：完全原样保留上游条目，跳过 normalize_entry 全部信息改写
            out = dict(e)
            # 个别直引源（如 EWEDLCM/FnDepot）条目 URL 使用相对路径 ./xx/...，
            # 输出前按本源绝对化，保证 icon/readme/preview 可被商店客户端解析（仅路径转换，不改信息）
            for _fld in ("icon_url", "readme_url"):
                _v = out.get(_fld)
                if isinstance(_v, str) and _v and not _v.startswith(("http://", "https://")):
                    out[_fld] = abs_url(_v, owner, repo, branch)
            _pv = out.get("preview_urls")
            if isinstance(_pv, list):
                out["preview_urls"] = [abs_url(u, owner, repo, branch)
                                       if isinstance(u, str) and u and not u.startswith(("http://", "https://")) else u
                                       for u in _pv]
        else:
            out = normalize_entry(e, owner, repo, branch, src_meta)
        v, dl, ad = extract_download(out, owner, repo, branch)
        if not dl:
            continue  # 无可下载地址 -> 过滤
        out["version"] = v or out.get("version", "")
        out["download_url"] = dl
        if not is_direct:
            # 二次补全：此时 download_url 已绝对化，字段级提取命中率更高（仅非直引源）
            fill_from_github_project(out, owner, repo, extra_urls=[dl] if dl else None, appname=an)
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
        # key 唯一化：先标准化 appname（去掉 -N 后缀），再判断是否重复
        # 同源冲突由 dict 天然消化；跨源重复：标准化后相同则共享 key，不同则各保留原始 appname
        raw_appname = (out.get("appname") or an).lower()
        canonical = re.sub(r'-(\d+)$', '', raw_appname)  # 去掉末尾的 -N 后缀
        if canonical in used_keys:
            # 标准化后已存在，直接用原始 appname 覆盖（带后缀的版本让路）
            key = canonical
        else:
            key = raw_appname
            used_keys.add(key)
        out["appname"] = raw_appname
        if key in apps_out:
            apps_out[key] = out  # 后写覆盖（keep_sources 优先机制已废弃）
        else:
            apps_out[key] = out
        if not is_direct and not out.get("icon_url"):
            missing_icons.append((key, owner, repo, branch, raw_appname))
        if not is_direct and not out.get("size") and dl:
            missing_sizes.append((key, dl))

    # 同名同包名去重：保留数据最完整的那个，其他标记为 fork 并保留 _fork_of 溯源
    n_before_dedup = len(apps_out)
    apps_out = dedup_apps(apps_out)
    # 去重后映射表兜底：winner 可能来自 appname 与映射表键不一致的源条目（如 adguardhome vs adguard），
    # 按最终 key/appname 再查一次 github_projects.json 补全 developer/publisher（#9）
    # 直引源条目跳过该兜底，保持上游原始信息完全不变。
    for k, e in apps_out.items():
        if e.get("_direct"):
            continue
        if e.get("developer") or e.get("publisher"):
            continue
        m_key = _strip_num_suffix(str(k).lower())
        mapped = _gh_projects.get(m_key)
        if not mapped and e.get("appname"):
            mapped = _gh_projects.get(_strip_num_suffix(str(e.get("appname")).lower()))
        if not mapped or "/" not in str(mapped):
            continue
        m_owner = str(mapped).split("/", 1)[0]
        _srcv = e.get("_src") or ""
        if isinstance(_srcv, list):
            _srcv = _srcv[0] if _srcv else ""
        src_owner = str(_srcv).split("/", 1)[0].lower()
        if m_owner.lower() == src_owner:
            continue
        if not e.get("developer"):
            e["developer"] = m_owner
            e.setdefault("developer_url", f"https://github.com/{m_owner}")
        if not e.get("publisher"):
            e["publisher"] = m_owner
            e.setdefault("publisher_url", f"https://github.com/{m_owner}")
    # 排除 fnOS 官方商店自带应用（不重复收录，官方由官方商店提供）
    # 兼容旧行为：默认排除；设置环境变量 FnUnify_INCLUDE_OFFICIAL=1 时收录官方（conversun/fnos-apps）
    include_official = os.environ.get("FnUnify_INCLUDE_OFFICIAL", "").strip().lower() in ("1", "true", "yes")
    if official_excludes.get("names") and not include_official:
        off_names = official_excludes["names"]
        off_names_norm = official_excludes["names_norm"]
        off_displays = official_excludes["displays"]
        dropped = []
        for a in list(apps_out.keys()):
            e = apps_out[a]
            hit = (a in off_names
                   or _norm_dedup_name(a) in off_names_norm
                   or _norm_dedup_name(e.get("display_name")) in off_displays
                   or _is_fnos_apps_like(e.get("_src", "")))
            if hit:
                dropped.append(a)
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

    # 输出前统一清理内部字段（_src 为溯源字段保留；_src_meta/_orig/_branch/_direct 已用完）
    # 并构造 V2 releases 结构、精简冗余字段，将 fnpack.json 控制在 jsDelivr 1024KB 限制内
    for e in apps_out.values():
        # publisher_url 最终兜底：所有 developer_url 补全（normalize_entry / 二次 fill /
        # dedup 后映射表兜底）均已完成后执行；优先 distributor_url，缺失用 developer_url
        # 直引源条目跳过此兜底，保持上游原始信息完全不变
        if not e.get("_direct") and not e.get("publisher_url"):
            e["publisher_url"] = e.get("distributor_url") or e.get("developer_url") or ""
        for k in ("_src_meta", "_orig", "_branch", "_direct"):
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

    # 源贡献统计（可选增强）：按 _src 溯源字段统计去重后应用归属
    source_contrib = defaultdict(int)
    for e in apps_out.values():
        sr = e.get("_src", [])
        if isinstance(sr, str):
            sr = [sr]
        for s in sr:
            if s:
                source_contrib[s] += 1
    top_contrib = sorted(source_contrib.items(), key=lambda x: -x[1])[:10]

    # 应用变更 changelog（可选增强）：对比上次 fnpack.json，生成变更摘要写 CHANGELOG.md
    changelog_lines = []
    old_keys = set()
    try:
        with open(OUTPUT, encoding="utf-8") as f:
            old_pack = json.load(f)
        old_keys = set(old_pack.get("apps", {}).keys())
    except Exception:
        pass
    new_keys = set(apps_out.keys())
    added = new_keys - old_keys
    removed = old_keys - new_keys
    if old_keys:
        changelog_lines.append(f"## {datetime.date.today().isoformat()} 聚合更新")
        changelog_lines.append(f"- 应用总数: {len(old_keys)} -> {len(new_keys)}")
        if added:
            changelog_lines.append(f"- 新增 {len(added)}: " + ", ".join(sorted(added)[:20]) + (" ..." if len(added) > 20 else ""))
        if removed:
            changelog_lines.append(f"- 移除 {len(removed)}: " + ", ".join(sorted(removed)[:20]) + (" ..." if len(removed) > 20 else ""))
        if not added and not removed:
            changelog_lines.append("- 应用集合无变化（仅字段/版本更新）")
        try:
            with open(CHANGELOG, "w", encoding="utf-8") as f:
                f.write("\n".join(changelog_lines) + "\n")
        except Exception:
            pass

    # 输出：官方 FnDepot V2 三层结构（schema_version 字符串 "2" + source_info + apps），
    # 键名合法化（仅允许 [A-Za-z0-9._-]），每个应用一行紧凑输出
    source_info = {
        "name": "FnUnify",
        "author": "kitblue",
        "homepage": "https://github.com/kitblue/FnUnify",
        "description": f"飞牛 fnOS 第三方应用聚合源：聚合 {len(sources)} 个第三方源（含自有子源 kitblue/fnthird，承接 FanTune 等独立应用收录），去重后收录 {len(apps_out)} 个应用（默认排除 fnOS 官方商店已收录应用）。",
        "source_contrib": {k: v for k, v in top_contrib},
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
