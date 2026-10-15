#!/usr/bin/env python3
"""FnUnify 聚合源 V2 规范转换脚本
将旧版平铺结构（顶层 download_url/version/arch_diff + 自由文本 labels）
转换为官方 V2 格式：
  schema_version 字符串 "2"；categories 固定 9 类（最多 2 个）；
  releases.<version>.packages.<arch> 嵌套结构；size 非负整数 Bytes；
  键名仅允许 [A-Za-z0-9._-] 且以字母或数字开头。
"""
import json, re, sys, os

V2_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "fnpack.json")

# icon_url 必填字段兜底：源数据缺失时指向仓库占位图标（assets/app-placeholder.png）
PLACEHOLDER_ICON = "https://raw.githubusercontent.com/kitblue/FnUnify/main/assets/app-placeholder.png"

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
    # 兜底：文本本身就是合法分类
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
    """版本号规范化：去掉 v 前缀；不满足可比较版本的降级为 0.0.0 并保留原值告警"""
    if not isinstance(v, str):
        v = str(v)
    v = v.strip()
    if v.startswith("v") and len(v) > 1 and v[1].isdigit():
        v = v[1:]
    if re.match(r"^\d+(\.\d+)*([a-zA-Z0-9._-]*)?$", v):
        return v
    return "0.0.0"

def legalize_key(k, entry=None):
    """键名合法化：只保留 [A-Za-z0-9._-]，非法字符转连字符；
    中文等无法映射时尝试从 download_url 文件名推断真实包名"""
    k2 = re.sub(r"[^A-Za-z0-9._-]", "-", k)
    k2 = re.sub(r"-{2,}", "-", k2).strip("-")
    if k2 and k2[0].isalnum():
        return k2
    # 无法合法化：尝试从下载文件名提取包名前缀
    if entry:
        for url_field in ("download_url",):
            dl = entry.get(url_field) or ""
            # 取 basename，去掉 -version[-arch] 后缀与 .fpk
            base = dl.rstrip("/").split("/")[-1]
            base = re.sub(r"\.fpk$", "", base)
            base = re.sub(r"[-_]?\d+\.\d+.*$", "", base)
            base = re.sub(r"[-_]?(x86|arm|all|amd64|aarch64)$", "", base)
            base = re.sub(r"[^A-Za-z0-9._-]", "-", base).strip("-")
            if base and base[0].isalnum():
                return base[:60]
    return "app-" + (k2 or "app")

def build_releases(entry):
    """从平铺字段构造 releases.<version>.packages.<arch> 结构"""
    version = normalize_version(entry.get("version") or "0.0.0")
    packages = {}
    arch_diff = entry.get("arch_diff")
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
    else:
        dl = entry.get("download_url")
        if dl:
            plat = entry.get("platform") or []
            if isinstance(plat, str):
                plat = [plat]
            if "all" in plat or len(plat) != 1 or plat[0] not in ("x86", "arm"):
                arch = "all"
            else:
                arch = plat[0]
            pkg = {"download_url": dl}
            sz = clean_size(entry.get("size"))
            if sz is not None:
                pkg["size"] = sz
            packages[arch] = pkg
    if not packages:
        return None
    return {version: {"packages": packages}}

def transform_app(key, entry):
    e = dict(entry)
    e.pop("arch_diff", None)
    e.pop("labels", None)
    e.pop("isdocker", None)
    # icon_url 必填：缺失时兜底仓库占位图标（README 已声明）
    if not e.get("icon_url"):
        e["icon_url"] = PLACEHOLDER_ICON
    # categories
    cats = map_labels_to_categories(entry.get("labels"))
    if not cats:
        cats = ["系统工具"]
    e["categories"] = cats[:2]
    # install_type / run_as / is_docker 归一化
    e["install_type"] = normalize_install_type(entry.get("install_type"))
    e["run_as"] = normalize_run_as(entry.get("run_as"))
    e["is_docker"] = normalize_is_docker(entry.get("is_docker"))
    # size 清洗（顶层 size 已被 releases 承载，删除避免脏类型）
    e.pop("size", None)
    # 版本与 releases
    rel = build_releases(entry)
    if rel is None:
        return None
    e["releases"] = rel
    e.pop("version", None)
    e.pop("download_url", None)
    # 键名与 appname 一致性
    e["appname"] = key
    # 可选字段保留
    for opt in ("service_port", "readme_url", "preview_urls", "i18n",
                "maintainer", "maintainer_url", "distributor", "distributor_url",
                "developer", "developer_url", "details_updated_at"):
        if opt in e:
            e[opt] = entry.get(opt)
    # 溯源字段
    if "_src" in entry:
        e["_src"] = entry["_src"]
    if "_fork_of" in entry:
        e["_fork_of"] = entry["_fork_of"]
    return e

def main():
    src_path = sys.argv[1] if len(sys.argv) > 1 else V2_ROOT
    out_path = sys.argv[2] if len(sys.argv) > 2 else V2_ROOT
    with open(src_path, encoding="utf-8") as f:
        data = json.load(f)
    src_meta = data.get("source_info") or {}
    apps_in = data.get("apps") or data
    apps_out = {}
    skipped = []
    for key, entry in apps_in.items():
        new_key = legalize_key(key, entry)
        # 同名键冲突（合法化后），保留原顺序第一个
        if new_key in apps_out:
            skipped.append((key, "键名合法化冲突"))
            continue
        te = transform_app(new_key, entry)
        if te is None:
            skipped.append((key, "无可用安装包"))
            continue
        apps_out[new_key] = te
    source_info = {
        "name": src_meta.get("name") or "FnUnify",
        "author": src_meta.get("author") or "kitblue",
    }
    for k in ("homepage", "description"):
        if src_meta.get(k):
            source_info[k] = src_meta[k]
    if "i18n" in src_meta:
        source_info["i18n"] = src_meta["i18n"]
    output = {
        "schema_version": "2",
        "source_info": source_info,
        "apps": apps_out,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"完成: {len(apps_out)} 个应用 -> {out_path}")
    print(f"跳过: {len(skipped)}")
    for s in skipped[:20]:
        print(" ", s)

if __name__ == "__main__":
    main()
