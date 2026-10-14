# FnUnify — fnOS 第三方应用源订阅聚合

FnUnify 是一个 **FnDepot 源订阅聚合**项目：订阅社区公开的第三方 fnOS 应用源，将各源的全部应用**全量收录**到一份 `fnpack.json`（FnDepot V2 顶层键式规范），供 fnOS 应用商店一键添加使用。

## 项目性质（源订阅模式）

与"应用聚合源"不同，本项目**不做**以下处理：

- 不做应用去重合并（同一应用多源收录时各自保留，按源区分）
- 不改写作者/维护者字段（应用作者以各源原始数据为准，缺失即留空）
- 不生成间接跳转链接，`download_url` 直接指向各源原始下载地址

因此不存在"图标缺失靠兜底、作者显示错误、应用数量被去重压缩"等聚合模式固有问题。

## 数据概况（2026-10-14 生成）

| 指标 | 数值 |
| --- | --- |
| 订阅源数量 | 152 |
| 解析成功 | 150 |
| 全量收录条目 | 1780 |
| 可安装应用（含下载地址） | 1377 |
| 图标覆盖 | 97% |
| size 覆盖 | 82% |
| arch_diff 覆盖 | 87% |

## 使用方法

在 fnOS 应用商店 → 第三方源 → 添加源，填入：

```
https://raw.githubusercontent.com/kitblue/FnUnify/main/fnpack.json
```

或使用 jsDelivr CDN：

```
https://cdn.jsdelivr.net/gh/kitblue/FnUnify@main/fnpack.json
```

## 文件说明

| 文件 | 说明 |
| --- | --- |
| `fnpack.json` | FnDepot V2 源订阅聚合产物，FnDepot 客户端直读 |
| `repo_list.txt` | 订阅源清单（一行一个 `owner/repo`） |
| `scripts/aggregate.py` | 重新生成脚本：拉取全部源 → 全量收录 → 输出 fnpack.json |

## 重新生成

```bash
python scripts/aggregate.py
```

产物输出到仓库根目录 `fnpack.json`。

## 免责声明

所有应用条目均来自社区公开的第三方源，版权与维护归各源作者所有。本仓库仅做聚合与转发，不修改应用本体；如发现侵权内容请联系对应源作者，或在本仓库提 Issue 移除相关源。
