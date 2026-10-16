---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: 257c07a97525093c65a112a601161c42_4c075419c84211f1b5a6525400393706
    ReservedCode1: V+gWOOg/xoT6pfZ6zTzEdvG8axG0IU2QIwA1SsAhwvsyTm5IeMK103ex+HK5QOY0+aYnlGyMKLj7g+gKXbNjlH2UL9xN7zhXUMsadh7/Yl8gst5rs0vY/XcAMdz5PmHmJRYTeaPPruV49bUo5KgQn42Rjf0u2MHp7B1+j3vPO+2a54BGkHduBDrE6mI=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: 257c07a97525093c65a112a601161c42_4c075419c84211f1b5a6525400393706
    ReservedCode2: V+gWOOg/xoT6pfZ6zTzEdvG8axG0IU2QIwA1SsAhwvsyTm5IeMK103ex+HK5QOY0+aYnlGyMKLj7g+gKXbNjlH2UL9xN7zhXUMsadh7/Yl8gst5rs0vY/XcAMdz5PmHmJRYTeaPPruV49bUo5KgQn42Rjf0u2MHp7B1+j3vPO+2a54BGkHduBDrE6mI=
---



# FnUnify

> 面向飞牛 fnOS 的第三方应用聚合源与定制版应用商店仓库。一条命令、一次安装，即可在一个入口浏览并安装来自 152 个第三方源的 474 个应用。

## 这是什么

FnUnify 由两部分组成：

1. **聚合源（数据层）**：维护一份 `repo_list.txt` 第三方源清单，通过每日定时任务抓取各源的 `fnpack.json`，去重、校验、归一化后合并为一份符合 FnDepot 外部应用源 V2 规范的 `fnpack.json` 聚合源清单。
2. **定制版商店（应用层）**：基于开源 FnDepot 客户端定制构建的 `fndepot` 应用包，安装到 fnOS 后自动把聚合源预置进本地应用商店数据库，让"新增源、再等待同步"的流程变成开箱即用。

上游项目为 [Blue-Mink/FnDepot](https://github.com/Blue-Mink/FnDepot)，本项目在其基础上做源预置与发版管理；源格式规范参考 [EWEDLCM/FnDepot](https://github.com/EWEDLCM/FnDepot) 的"外部应用源 V2 编写说明"，发布仓库为本项目 [kitblue/FnUnify](https://github.com/kitblue/FnUnify)。

## 主要能力

- **大规模聚合**：聚合 152 个第三方源，去重后收录 474 个应用（含官方 [conversun/fnos-apps](https://github.com/conversun/fnos-apps) 源收录）。
- **格式兼容**：聚合脚本同时支持 FnDepot V1（无 `schema_version`，根节点直接是应用映射）、V2（`{schema_version, source_info, apps}` 三层结构）及个别上游的 `apps.json` 包装层，统一归一化为 V2 输出。
- **去重与溯源**：按应用显示名去重并保留数据最完整的一条；同名不同源的条目附加 `_fork_of` 溯源标记，便于追查来源。
- **质量保障**：自动做源可达性探测（`source_health_report.json`）与元数据字段覆盖率检查（`metadata_quality_report.json`），不可达或不完整的源会被标记出来。
- **CDN 直链**：`cdn_mirror.py` 可把 `download_url` 转换为 CDN 镜像直链，缓解 GitHub 下载不稳定问题。
- **安装即就绪**：定制版 `fpk` 安装/升级时，`seed_fnunify.sh` 会从本项目拉取最新聚合源并幂等写入本地商店数据库，用户手动添加的源不会被删除。
- **全自动发布**：GitHub Actions 每日自动重新聚合、比对、构建并发布定制版；上游发新版本时自动生成提醒 Issue。

## 聚合源格式（fnpack.json · FnDepot V2）

本项目发布物 `fnpack.json` 严格遵循 [EWEDLCM/FnDepot 外部应用源 V2 编写说明](https://github.com/EWEDLCM/FnDepot/blob/main/README.md)，客户端需为支持 V2 的版本（v0.0.7 以上）。

### 顶层结构

```json
{
  "schema_version": "2",
  "source_info": { "name": "...", "author": "...", "homepage": "...", "description": "..." },
  "apps": { "<appname>": { ... } }
}
```

| 字段 | 类型 | 要求 |
|---|---|---|
| `schema_version` | 字符串 | 必须精确为 `"2"`，不接受数字 2、`"2.0"`、`"2.1"` 等写法 |
| `source_info.name` / `source_info.author` | 字符串 | 必填，源显示名称与维护者 |
| `apps` | 对象 | 键名必须与 FPK manifest 的 `appname` 一致；仅允许字母、数字、点、下划线、短横线，且以字母或数字开头 |

### 应用必填字段

| 字段 | 类型 | 要求 |
|---|---|---|
| `display_name` | 字符串 | 应用显示名称 |
| `desc` | 字符串 | 应用简介，支持 HTML（客户端会清理危险内容） |
| `platform` | 数组 | 只允许 `all`、`x86`、`arm` |
| `categories` | 数组 | 固定分类，1~2 个，第一项为主分类 |
| `icon_url` | URL | 应用图标；缺失时本项目以 `assets/app-placeholder.png` 占位兜底 |
| `run_as` | 字符串 | 只允许 `package` 或 `service`（`root` 视为非法，聚合时统一映射为 `service`） |
| `install_type` | 字符串 | 空字符串=存储空间，`root`=系统空间 |
| `is_docker` | 布尔 | 是否为 Docker 应用 |
| `releases` | 对象 | 版本集合，至少一个可用版本 |

可选字段：`preview_urls`（最多 8 张）、`readme_url`、`bug_report_url`、`details_url`（拆分详情模式）、`maintainer`、`distributor`、`service_port`、`i18n`。

固定分类（不可自创）：`系统工具`、`文件系统`、`网络服务`、`开发工具`、`实用工具`、`媒体工具`、`安全工具`、`数据库`、`其他`。历史源中的旧分类（`编程开发`、`游戏地带`、`影音娱乐`、`生活服务`、`AI赋能`、`硬件驱动`、`智能智控`、`教育学习`）在聚合时统一映射到上述合法分类。

### releases / packages 与架构回退

```json
"releases": {
  "1.2.3": {
    "packages": {
      "all": { "download_url": "...", "size": 123456, "sha256": "..." },
      "x86": { "download_url": "...", "size": 123456, "sha256": "..." }
    }
  }
}
```

- 版本键为可比较的版本号（推荐 SemVer），不能用 `latest` 或日期文字。
- 安装包架构键只允许 `all`、`x86`、`arm`。
- 选择顺序：`packages.[当前架构]` → `packages.all`；`download_url` 必须存在于最终选中的分支。
- 字段合并顺序：应用级 → 版本级 → `packages.all` → `packages.[当前架构]`。
- `size` 为非负整数（Bytes），`sha256` 为 64 位十六进制（存在时客户端强制校验）。

### 客户端限制（发布前自查）

| 项 | 上限 |
|---|---|
| 主源 JSON | 约 2MB（本项目当前约 480KB） |
| 单源应用数 | 最多 5000（本项目当前 474） |
| 单应用版本数 | 最多 100 |
| 预览图 | 最多读取 8 张 |
| 详情 JSON | 约 512KB（拆分模式） |

### i18n 与拆分详情

- `i18n` 可出现在 `source_info`、应用、版本、安装包四层；可本地化字段为 `name/description`、`display_name/desc`、`changelog`。
- 回退顺序：精确语言 → 同语系 → `en-US` → `zh-CN` → 基础字段；基础字段仍然必须填写。
- 应用较多时可使用 `details_url` 拆分：主源只存索引与 `details_updated_at`，详情文件 `app_name` 必须与 `apps` 键名完全一致。

## 开始使用

### 方式一：安装定制版商店（推荐）

1. 从本仓库 [Releases](https://github.com/kitblue/FnUnify/releases) 下载最新的 `fndepot_unify_*.fpk` 安装包。
2. 在飞牛 fnOS 应用中心以"手动安装"方式上传该 `fpk` 完成安装。
3. 打开应用，等待首次源同步完成（安装钩子会自动预置聚合源），即可浏览全部应用。

### 方式二：手动添加聚合源

适用于已经在使用支持 V2 的 FnDepot 客户端（v0.0.7 以上）的用户：

1. 在 FnDepot 客户端中新增第三方源：
   - 源地址：`https://raw.githubusercontent.com/kitblue/FnUnify/main/fnpack.json`
2. 触发一次源同步，客户端即可加载聚合源中的应用。

> 注意：旧版客户端（v0.0.6 及以下）无法解析 V2 格式，若手动添加源后无法加载，请升级客户端或改用方式一安装定制版。

### 验证源是否可用

```bash
curl -s https://raw.githubusercontent.com/kitblue/FnUnify/main/fnpack.json | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['schema_version'], d['source_info']['name'], len(d['apps']))"
```

## 文件与目录速览

| 路径 | 作用 |
|---|---|
| `repo_list.txt` | 第三方源清单，一行一个 `owner/repo`，共 152 个有效源 |
| `official_apps.txt` | 官方应用排除清单，共 174 个，聚合时跳过 |
| `fnpack.json` | 聚合产物（FnDepot V2 格式），最终交付给客户端加载的源清单 |
| `assets/app-placeholder.png` | 图标缺失时的占位图（`icon_url` 兜底） |
| `scripts/aggregate.py` | 聚合主脚本：抓取、解析、去重、归一化、输出 V2 `fnpack.json` |
| `scripts/transform_to_v2.py` | 一次性/增量转换器：将 V1 平铺结构清洗为 V2 规范结构 |
| `scripts/sync_fndepot.py` | 定制版同步/构建/发布脚本：查上游版本、降一位发版、打包发布 |
| `scripts/source_quality_check.py` | 源质量与元数据完整性检查 |
| `scripts/cdn_mirror.py` | CDN 直链镜像工具 |
| `scripts/test_aggregate.py` | 聚合逻辑单元测试 |
| `scripts/sync_state.json` | 同步状态记录（上游版本、本地发版版本、构建时间） |
| `scripts/source_health_report.json` | 源可达性探测报告（CI 生成） |
| `scripts/metadata_quality_report.json` | 元数据质量报告（CI 生成） |
| `build_work/src/manifest` | 定制版应用包描述文件（版本、维护者、平台等） |
| `build_work/src/cmd/seed_fnunify.sh` | 安装/升级钩子：预置聚合源到本地商店数据库（兼容 V2 `releases` 与 V1 `download_url`） |
| `build_work/src/cmd/` | 定制版生命周期回调（安装、升级、卸载、配置等） |
| `build_work/src/i18n/` | 多语言文案（zh-CN / zh-TW / en-US / ja-JP / ru-RU） |
| `.github/workflows/fnunify-ci.yml` | 每日聚合 + 构建 + 发布流水线 |
| `.github/workflows/check-upstream.yml` | 每日上游版本检查与提醒流水线 |
| `dedup_strategy.md` | 应用去重策略说明 |
| `CONTRIBUTING.md` | 贡献指南 |

## 自动构建与版本管理

### 每日流水线（`fnunify-ci.yml`）

| 时间（北京时间） | 任务 |
|---|---|
| 11:00 | 运行 `aggregate.py` 重新聚合全部源，`fnpack.json` 有变化时自动提交推送 |
| 15:00 | 运行 `sync_fndepot.py --publish`，检测到上游新版本时构建定制版并发布 |

推送 `fnpack.json`、聚合/发布脚本等关键文件时也会触发流水线；`main` 分支外的改动不受影响，也可在 Actions 页面手动触发。

### 上游检查（`check-upstream.yml`）

每日 14:00（北京时间）检查上游 [Blue-Mink/FnDepot](https://github.com/Blue-Mink/FnDepot) 是否发布新版本，有则自动创建提醒 Issue，包含上游版本号、对应发版版本与结论。

### 发版策略：上游降一位

定制版采用"上游版本 - 1"的发版策略：

- 脚本将 `UPSTREAM_REPO` 指向上游原版仓库（自动查最新 release），将 `PUBLISH_REPO` 指向本项目仓库（客户端检查更新的目标）。
- 发布版本号由 `decrement_version()` 计算：仅对语义化版本最后一段减一，末段为 0 时向前一段借位并回退为 9（如 `0.6.6 → 0.6.5`、`0.6.0 → 0.5.9`）。
- 打包前将 `manifest` 的 `version` 改写为降一位后的版本号，`fpk` 文件名与 GitHub tag 同步（如 `fndepot_unify_v0.6.5.fpk`、`fndepot-v0.6.5`）。
- 若同名 release 已存在，先删除旧资产与 release 再重新创建，保证幂等发布。

这样做的目的：客户端检查更新时总能拿到比上游低一档的版本，便于验证升级路径；上游发布新版本后，本项目 CI 会在一到两个工作日内自动跟进发版。

## 注意事项

- **客户端版本兼容**：聚合源为 FnDepot V2 格式，需要客户端 v0.0.7 以上；旧版（v0.0.6 及以下）无法解析 V2，请升级客户端或安装定制版。
- **只增不删**：`seed_fnunify.sh` 同步时仅新增/更新聚合源中的应用，绝不删除用户在本地手动添加的源。
- **版本号含义**：仓库内 `fndepot_unify_v*.fpk` 为本地构建产物，当前正式发版以 `sync_state.json` 中 `last_version` 及 [Releases](https://github.com/kitblue/FnUnify/releases) 页面为准（当前发版 `fndepot_unify_v0.6.5.fpk`，tag `fndepot-v0.6.5`）。
- **去重范围**：聚合按应用显示名去重，同名不同源的条目以 `_fork_of` 标记保留来源，不会因去重丢失可用版本。
- **CI 失败通知**：聚合或构建任一环节失败，CI 会自动创建 `[CI失败]` 前缀的 Issue 便于追踪。

## 贡献

欢迎通过 Pull Request 补充新的第三方源（追加到 `repo_list.txt`）、修复聚合逻辑或改进文档。参与前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md) 与 [dedup_strategy.md](dedup_strategy.md)。

## 免责声明

本项目仅聚合第三方来源的应用元数据，所有应用版权归各自作者所有。聚合行为不修改任何上游代码，安装与使用第三方应用产生的风险由用户自行承担。
*（内容由AI生成，仅供参考）*
*（内容由AI生成，仅供参考）*
