#!/bin/bash
# FnUnify 源同步脚本（定制版，随 fpk 安装/升级时由 common 调用）
# 功能：从 FnUnify 聚合源拉取 fnpack.json，将其中所有应用 download_url
#       幂等写入 ${TRIM_PKGVAR}/fndepot.db 的 sources 表（external 类型）。
# 原则：只新增/更新，绝不删除用户手动添加的本地源。
set -u

DB="${TRIM_PKGVAR:-}/fndepot.db"
URL="https://raw.githubusercontent.com/kitblue/FnUnify/main/fnpack.json"
NAME="FnUnify"

[ -f "${DB}" ] || { echo "fndepot.db 不存在，跳过源同步"; exit 0; }

sync_via_python() {
python3 - "${DB}" "${URL}" "${NAME}" <<'PY'
import sqlite3, sys, json, time
from urllib.request import urlopen, Request

db_path, remote_url, source_name = sys.argv[1], sys.argv[2], sys.argv[3]

def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

log("开始源同步...")
try:
    req = Request(remote_url, headers={"User-Agent": "FnUnify-Sync/1.0"})
    with urlopen(req, timeout=30) as resp:
        remote = json.loads(resp.read().decode("utf-8"))
except Exception as e:
    log(f"下载远程 fnpack.json 失败: {e}")
    sys.exit(0)

if not isinstance(remote, dict):
    log("远程 fnpack.json 格式异常，跳过同步")
    sys.exit(0)

apps = remote.get("apps", {})
if not isinstance(apps, dict):
    log("远程 fnpack.json 缺少 apps 对象，跳过同步")
    sys.exit(0)

# 1) 收集远程所有 download_url（去重；兼容 V1 顶层字段与 V2 releases 嵌套）
remote_urls = set()
for app_key, app_data in apps.items():
    if not isinstance(app_data, dict):
        continue
    if app_data.get("download_url"):
        remote_urls.add(app_data["download_url"])
    rel = app_data.get("releases")
    if not isinstance(rel, dict):
        continue
    for rv in rel.values():
        pkg = rv.get("packages") if isinstance(rv, dict) else None
        if not isinstance(pkg, dict):
            continue
        for pv in pkg.values():
            if isinstance(pv, dict) and pv.get("download_url"):
                remote_urls.add(pv["download_url"])

con = sqlite3.connect(db_path)
cur = con.cursor()

# 2) 遍历本地外部源：在远程中 → 跳过；不在 → 用户手动源，保留
cur.execute("SELECT id, url FROM sources WHERE is_builtin = 0 AND source_type = 'external'")
existing = dict(cur.fetchall())

skip_count = 0
not_found_count = 0
for sid, url in existing.items():
    if url in remote_urls:
        skip_count += 1
    else:
        not_found_count += 1
        log(f"本地源 {url} 不在远程 fnpack.json 中，保留不动（疑似用户手动添加）")

# 3) 将远程中尚未存在的 URL 幂等插入
now = time.strftime("%Y-%m-%d %H:%M:%S")
cur.execute("SELECT COALESCE(MAX(source_order), -1) FROM sources WHERE is_builtin = 0")
next_order = (cur.fetchone()[0] or -1) + 1

new_count = 0
update_count = 0
for dl in remote_urls:
    if dl in existing.values():
        update_count += 1
        cur.execute(
            "UPDATE sources SET name=?, updated_at=? WHERE url=? AND is_builtin=0 AND source_type='external'",
            (source_name, now, dl),
        )
        continue
    try:
        cur.execute(
            "INSERT INTO sources(url, name, is_default, is_builtin, priority, sync_status, "
            "sync_enabled, is_fork, overlap_risk, source_order, created_at, updated_at, source_type) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (dl, source_name, 0, 0, 100, 'pending', 1, 0, 'none',
             next_order, now, now, 'external'),
        )
        next_order += 1
        new_count += 1
    except Exception as e:
        log(f"插入失败: {dl} - {e}")

con.commit()
con.close()
log(f"同步完成: 新增 {new_count} 个, 更新 {update_count} 个, 跳过 {skip_count} 个, 本地特有 {not_found_count} 个")
PY
}

sync_via_sqlite3() {
    # 降级方案：python3 仅负责提取 URL 列表，sqlite3 负责写入
    TMPFILE=$(mktemp)
    python3 -c "
import json, sys
from urllib.request import urlopen, Request
req = Request(sys.argv[1], headers={'User-Agent': 'FnUnify-Sync/1.0'})
with urlopen(req, timeout=30) as resp:
    d = json.loads(resp.read().decode('utf-8'))
urls = set()
for k, v in d.get('apps', {}).items():
    if not isinstance(v, dict):
        continue
    if v.get('download_url'):
        urls.add(v['download_url'])
    rel = v.get('releases')
    if not isinstance(rel, dict):
        continue
    for rv in rel.values():
        pkg = rv.get('packages') if isinstance(rv, dict) else None
        if not isinstance(pkg, dict):
            continue
        for pv in pkg.values():
            if isinstance(pv, dict) and pv.get('download_url'):
                urls.add(pv['download_url'])
for u in urls:
    print(u)
" "${URL}" > "${TMPFILE}" 2>/dev/null

    if [ ! -s "${TMPFILE}" ]; then
        rm -f "${TMPFILE}"
        echo "WARN: 无法提取远程 URL 列表，跳过同步"
        exit 0
    fi

    local max_order
    max_order=$(sqlite3 "${DB}" "SELECT COALESCE(MAX(source_order),-1) FROM sources WHERE is_builtin=0;")
    local next_order=$((max_order + 1))
    local new_count=0
    local update_count=0
    local now
    now=$(date '+%Y-%m-%d %H:%M:%S')

    while IFS= read -r url; do
        [ -z "${url}" ] && continue
        local escaped
        escaped=$(echo "${url}" | sed "s/'/''/g")
        local existing_id
        existing_id=$(sqlite3 "${DB}" "SELECT id FROM sources WHERE url='${escaped}' AND is_builtin=0 AND source_type='external';")
        if [ -n "${existing_id}" ]; then
            sqlite3 "${DB}" "UPDATE sources SET name='${NAME}', updated_at='${now}' WHERE id='${existing_id}';"
            update_count=$((update_count + 1))
        else
            sqlite3 "${DB}" "INSERT INTO sources(url, name, is_default, is_builtin, priority, sync_status, sync_enabled, is_fork, overlap_risk, source_order, created_at, updated_at, source_type) VALUES('${escaped}','${NAME}',0,0,100,'pending',1,0,'none',${next_order},'${now}','${now}','external');"
            next_order=$((next_order + 1))
            new_count=$((new_count + 1))
        fi
    done < "${TMPFILE}"

    rm -f "${TMPFILE}"
    log "同步完成: 新增 ${new_count} 个, 更新 ${update_count} 个"
}

# 优先 python3；缺失则降级 sqlite3；都缺失则提示
if command -v python3 >/dev/null 2>&1; then
    sync_via_python
    exit $?
fi
if command -v sqlite3 >/dev/null 2>&1; then
    sync_via_sqlite3
    exit $?
fi
echo "WARN: 未找到 python3/sqlite3，请手动同步源"
exit 1
