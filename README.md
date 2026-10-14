# FnUnify — fnOS 第三方应用源订阅聚合

FnUnify 是一个 **FnDepot 源订阅聚合**项目：订阅社区公开的第三方 fnOS 应用源，将各源的全部应用**全量收录**到一份 `fnpack.json`（FnDepot V2 顶层键式规范），供 fnOS 应用商店一键添加使用。

## 项目性质（源订阅聚合）

FnUnify 是**源订阅聚合**：订阅社区公开的第三方 fnOS 应用源，将各源应用**全量收录**后按规则去重，产出一份 `fnpack.json`（FnDepot V2 顶层键式规范），供第三方应用商店（如 FnDepot）作为**源订阅**添加使用。

本项目遵循以下原则：

- **仅收录社区第三方源**：官方 conversun/fnos-apps 仓库的应用不收录（聚合时排除官方应用 174 个）
- **同应用多源去重**：对同一应用的多源条目按 display_name/规范化包名比对去重，合并 152 个源约 1205 条源条目后收录 320 个应用
- **不改写作者/维护者字段**：应用作者以各源原始数据为准，缺失即留空
- **不生成间接跳转链接**：`download_url` 直接指向各源原始下载地址

## 原版 FnDepot 的限制与适配

**使用场景**：本聚合源面向第三方应用商店（如 FnDepot），作为**源订阅**添加使用。

原版 FnDepot（EWEDLCM/FnDepot）存在以下解析限制，无法直接加载本聚合源：

| 限制项 | 原版表现 | 本项目适配 |
| --- | --- | --- |
| **源大小上限** | fnpack.json 超过 1024KB 时客户端拒绝解析 | 按规则去重后收录 320 个应用，fnpack.json 控制在合理大小 |
| **源行数上限** | 源文件行数过多时解析失败 | 采用每应用一行的紧凑格式（322 行），远低于客户端解析上限 |
| **schema_version 校验** | 原版只认 schema_version=1 格式 | 本项目使用 schema_version=2 包装层（兼容 V1/V2），定制版已解除此限制 |
| **来源校验** | 原版要求源名匹配特定模式 | 使用 GitHub raw/jsDelivr 直链方式规避 |

> **适配结论**：原版商店的解析限制使其**无法直接**使用本聚合源；安装定制版 `fndepot_unify`（内置 FnUnify 源并解除解析限制）后，添加源即可正常使用。

## 使用方法

在 fnOS 应用商店 → 第三方源 → 添加源，填入：

```
https://raw.githubusercontent.com/kitblue/FnUnify/main/fnpack.json
```

或使用 jsDelivr CDN：

```
https://cdn.jsdelivr.net/gh/kitblue/FnUnify@main/fnpack.json
```

## 定制版 FnDepot 客户端（fndepot_unify）

`fndepot_unify` 是 FnDepot 客户端（v0.4.6）的定制版，**不修改前端 JS 与后端二进制**，而是通过 fpk 安装/升级钩子在 **cmd 脚本层**注入源订阅：把 FnUnify 订阅源**幂等写入客户端本地数据库**（`${TRIM_PKGVAR}/fndepot.db` 的 sources 表），由客户端原生逻辑接管同步/解析/去重，安装后打开即用、无需手动添加源。

定制点：
- 新增 `cmd/seed_fnunify.sh`：以 python3（优先）/ sqlite3 幂等插入 FnUnify 源记录（URL 唯一约束防重）
- `cmd/common`：install_callback / upgrade_callback 在 rebuild-persistent-data 之后调用 seed 脚本

版本策略：版本号跟随原项目（当前 0.4.6），不随编译递增；原版发布新版本后，
由 `scripts/sync_fndepot.py` 自动完成「下载原版 fpk → 注入定制 → 重打包 → 发布」。

发布：https://github.com/kitblue/FnUnify/releases/tag/fndepot-v0.4.6
安装包：`fndepot_unify_v0.4.6.fpk`（appname 保持 `fndepot`，可覆盖安装原版）

## 文件说明

| 文件 | 说明 |
| --- | --- |
| `fnpack.json` | FnDepot V2 源订阅聚合产物，FnDepot 客户端直读 |
| `repo_list.txt` | 订阅源清单（一行一个 `owner/repo`） |
| `scripts/aggregate.py` | 重新生成脚本：拉取全部源 → 全量收录 → 源健康检查 → 元数据质量统计 → 去重 → 输出 fnpack.json |
| `scripts/sync_fndepot.py` | 定制版自动发版流水线：检查原版版本 → 下载 → 定制（DB 预置） → 重打包 → 发布 |
| `scripts/source_quality_check.py` | 源质量评估：基于健康报告与聚合结果输出 Markdown 报告（保留/降级/移除建议） |
| `scripts/test_aggregate.py` | 聚合脚本单元测试（unittest，无第三方依赖） |
| `fndepot_unify/` | FnDepot 客户端解包副本（含定制脚本） |
| `fndepot_unify_v0.4.6.fpk` | 定制版安装包：安装/升级时自动内置 FnUnify 源 |

## 健康监控与质量报告

聚合流程内置三类可观测报告，全部输出到 `scripts/` 目录，供人工巡检或 CI 消费：

| 报告文件 | 生成时机 | 内容 |
| --- | --- | --- |
| `scripts/source_health_report.json` | `aggregate.py` 每次运行 | 每个源的可达性探测结果：状态（ok/unreachable）、HTTP 状态码、失败原因、检查时间 |
| `scripts/metadata_quality_report.json` | `aggregate.py` 每次运行 | 关键字段（display_name/desc/version/download_url/icon_url/author）覆盖率、缺失字段明细 |
| `scripts/source_quality_report.md` | 手动运行 `source_quality_check.py` | 源质量分级与处理建议（保留/降级/移除），只建议不自动修改 `repo_list.txt` |

### 使用方式

```bash
# 生成聚合产物 + 源健康报告 + 元数据质量报告（三合一）
python scripts/aggregate.py

# 基于最新健康报告生成源质量评估 Markdown
python scripts/source_quality_check.py
```

### CI 集成

- `.github/workflows/fnunify-ci.yml`：`generate` / `build` 两个 job 失败时自动创建 GitHub Issue（标题含日期与 job 名，正文附失败日志摘要）
- `.github/workflows/check-upstream.yml`：每日 14:00（北京时间）检查 EWEDLCM/FnDepot 最新 release 版本，与 `scripts/sync_state.json` 记录的原版版本比对，发现新版本时创建 Issue 提醒触发定制版构建

### 定制版版本追踪

```bash
# 检查上游是否发布新版本（CI 友好输出，返回码 0=无需构建 / 2=有待构建）
python scripts/sync_fndepot.py --check-only
```

输出形如 `CHECK_ONLY upstream=0.4.6 local=0.4.6 need_build=no published_at=...`，可由 CI 直接解析。

### 自动化测试

```bash
python scripts/test_aggregate.py -v
```

覆盖：V1/V2 源解析、同名多源去重评分、元数据完整性检查、fnpack.json 输出结构校验（schema_version=2、apps 键结构）。

## 重新生成

```bash
python scripts/aggregate.py
```

产物输出到仓库根目录 `fnpack.json`。

## 免责声明

所有应用条目均来自社区公开的第三方源，版权与维护归各源作者所有。本仓库仅做聚合与转发，不修改应用本体；如发现侵权内容请联系对应源作者，或在本仓库提 Issue 移除相关源。
