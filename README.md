# FnUnify · 飞牛 fnOS 第三方应用聚合源

> 添加一个源，发现全部飞牛第三方应用。

FnUnify 聚合了 **FnDepot 生态（117+ 社区源）** 与 **fnos-apps（官方社区源）**，按应用名去重、保留最新版本，统一输出为 FnDepot V2 规范的 `fnpack.json`，并通过 GitHub Actions 每日自动刷新。

## 快速开始（fnOS 应用中心添加源）

添加源有两种方式，选一种即可：

### 方式一：JSON 直链（推荐）

在 fnOS 应用中心「第三方应用商店」→ 添加第三方源，输入以下 JSON 地址：

```
https://raw.githubusercontent.com/kitblue/FnUnify/main/fnpack.json
```

或使用 jsDelivr CDN（国内访问更快）：

```
https://cdn.jsdelivr.net/gh/kitblue/FnUnify@main/fnpack.json
```

### 方式二：仓库地址

直接填写仓库地址，应用中心会自动读取根目录的 `fnpack.json`：

```
https://github.com/kitblue/FnUnify
```

## 功能特性

- **单源发现**：合并 FnDepot 117 源 + fnos-apps，去重后约 300+ 应用
- **每日自动刷新**：GitHub Actions 每日 19:00（UTC+8）拉取全部上游源，自动更新 `fnpack.json`
- **去重策略**：按 appname 去重；fnos-apps（官方维护、持续更新）优先；其余源保留版本号最高者
- **标准 V2 格式**：兼容 FnDepot V2 规范（键式 fnpack.json，含 arch_diff 多架构下载地址）

## 项目结构

```
.
├── fnpack.json            # 聚合产物（FnDepot V2 规范）
├── repo_list.txt          # 上游源列表（FnDepot ∪ Moo 精选）
├── scripts/
│   └── aggregate.py       # 聚合脚本：拉取 → 去重 → 输出
└── .github/workflows/
    └── sync.yml           # 每日自动刷新
```

## 维护说明

- 上游源列表维护在 `repo_list.txt`，新增/移除源直接编辑该文件即可
- 手动触发聚合：仓库 Actions 页面 → Daily Aggregation → Run workflow
- 聚合脚本可在本地运行：`python scripts/aggregate.py`

## 数据来源

- [conversun/fnos-apps](https://github.com/conversun/fnos-apps) — 官方社区源，175+ 应用
- [710850609/FnDepot](https://github.com/710850609/FnDepot) — FnDepot 生态源列表
- [EWEDLCM/FnDepot](https://github.com/EWEDLCM/FnDepot) — FnDepot 社区源，涵盖更多实用工具与效率应用
- [Blue-Mink/moo](https://github.com/Blue-Mink/moo) — Moo 精选源列表
- 各社区维护者的 FnDepot 分支仓库

## License

MIT
