# FnUnify

面向飞牛 fnOS 的第三方应用聚合源与定制版应用商店。聚合 13 个第三方源（含自有子源 kitblue/fnthird），去重后收录 **195** 个应用（默认排除 fnOS 官方商店已收录应用）。

## 这是什么

- **聚合源（数据层）**：维护 `repo_list.txt` 源清单，每日抓取各源 `fnpack.json`，去重、校验、归一化后合并为一份符合 FnDepot 外部应用源 V2 规范的 `fnpack.json`。
- **定制版商店（应用层）**：基于开源 [Blue-Mink/FnDepot](https://github.com/Blue-Mink/FnDepot) 定制构建的 `fndepot` 应用包，安装后自动预置聚合源，开箱即用。

## 快速开始

**方式一：安装定制版（推荐）**

1. 从 [Releases](https://github.com/kitblue/FnUnify/releases) 下载最新 `fndepot_unify_*.fpk`。
2. 在飞牛 fnOS 应用中心以"手动安装"方式上传安装。
3. 首次启动自动同步聚合源，即可浏览全部应用。

**方式二：手动添加聚合源**

适用于支持 V2 的 FnDepot 客户端（v0.0.7 以上）：

- 源地址：`https://raw.githubusercontent.com/kitblue/FnUnify/main/fnpack.json`
- 触发一次源同步即可加载。

## 聚合源清单（repo_list.txt · 13 个）

`shuangji66/FnDepot`、`bekafka/FnDepot`、`rockchild2332/fnos-apps-fndepot`、`RROrg/fn-apps`、`Brian099/FnDepot`、`yuexps/FnDepot`、`cmbya/FnDepot`、`magiccode1412/FnDepot`、`Kasbuky-sudo/FnDepot`、`Hxido-RXM/FnDepot`、`conversun/fnos-apps`（官方源）、`EWEDLCM/FnDepot`、`kitblue/fnthird`（自有子源）。

## 主要能力

- **大规模聚合**：13 个源统一归一化为 FnDepot V2 格式（兼容 V1 与 `apps.json` 包装层）。
- **去重与溯源**：按应用显示名去重并保留数据最完整条目，同名不同源以 `_fork_of` 标记、`_src` 保留多来源。
- **官方不重复**：默认排除 fnOS 官方商店已收录应用。
- **独立应用承接**：FanTune 与 Blue-Mink/FnDepot 独立应用等经自有子源 fnthird 收录。
- **全自动发布**：GitHub Actions 每日聚合、构建并发布定制版；上游发新版本自动提醒。

## 自动化与发版

- **每日流水线**（北京时间）：11:00 聚合源（`aggregate.py`，有变化自动提交）；15:00 构建并发布定制版（`sync_fndepot.py --publish`）。
- **上游检查**：每日检查上游 Blue-Mink/FnDepot 新版本并创建提醒 Issue。
- **发版策略**：定制版采用"上游版本 - 1"策略（如 `0.6.6 → 0.6.5`），保证客户端总能验证升级路径；同名 release 已存在时先删后建，幂等发布。

## 文件速览

| 路径 | 作用 |
|---|---|
| `repo_list.txt` | 第三方源清单 |
| `official_apps.txt` | 官方应用排除清单 |
| `fnpack.json` | 聚合产物（FnDepot V2 源清单） |
| `assets/app-placeholder.png` | 图标缺失占位图 |
| `scripts/aggregate.py` | 聚合主脚本 |
| `scripts/sync_fndepot.py` | 定制版同步/构建/发布脚本 |
| `scripts/transform_to_v2.py` | V1 → V2 转换器 |
| `scripts/update_official_apps.py` | 官方排除清单同步 |
| `build_work/src/` | 定制版应用包源码（manifest、回调、i18n） |
| `.github/workflows/` | CI 流水线 |

## 注意事项

- 聚合源为 FnDepot V2 格式，需客户端 v0.0.7 以上。
- `seed_fnunify.sh` 仅新增/更新应用，绝不删除用户手动添加的源。
- 聚合按应用显示名去重，不会因去重丢失可用版本。
- 聚合或构建失败时，CI 自动创建 `[CI失败]` Issue。

## 贡献

欢迎通过 Pull Request 补充第三方源（追加到 `repo_list.txt`）或改进聚合逻辑。

## 免责声明

本项目仅聚合第三方来源的应用元数据，所有应用版权归各自作者所有；聚合不修改任何上游代码，安装与使用第三方应用的风险由用户自行承担。
