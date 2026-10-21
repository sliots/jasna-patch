# Windows Release 文件差异补丁

从两个 GitHub Release 的归档，或两个本地目录生成文件级差异包。默认源仓库是 [Kruk2/jasna](https://github.com/Kruk2/jasna/releases)，也支持其他公开源仓库。补丁由本仓库生成，不属于上游官方发布。

## 使用补丁

在本仓库的 [Releases](../../releases) 中选择与已安装版本对应的补丁。发布名称默认是 `新版标签-旧版标签.patch`，升级方向、来源和文件变化以该 Release 的说明及附件为准。

1. 关闭应用，备份原安装目录。
2. 下载补丁压缩包。单个 `.7z` 直接解压；若为 `.7z.001`、`.002` 等分卷，将全部分卷放到同一目录，从 `.001` 解压。
3. 将补丁解压到独立目录，按包内 `README.txt` 说明在 PowerShell 中执行：

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\Apply-Patch.ps1 -InstallDir "C:\Application"
   ```

`InstallDir` 应是旧版应用根目录，并包含发布配置指定的入口文件。脚本先校验全部旧版文件，再复制新增/修改文件、删除明确列出的旧文件，最后校验全部新版文件。用户额外文件保留；官方文件被修改时拒绝更新。失败时恢复备份，没有自动回滚。

补丁生成和文件更新验证不需要 GPU。CI 不启动 Windows 应用，也不测试 GPU 功能。

## GitHub Actions 自动发布

在 Actions 页面运行 **Build and publish Windows patch**，每次明确填写旧版和新版标签。工作流使用内置 `GITHUB_TOKEN`，不需要额外 PAT 或云环境 Secret。

| 输入 | 默认值 | 用途 |
| --- | --- | --- |
| `source_repository` | `Kruk2/jasna` | 上游 `owner/repository`。 |
| `old_tag` | 必填 | 已安装旧版的实际发布标签。 |
| `new_tag` | 必填 | 升级目标的实际发布标签，必须与旧版不同。 |
| `asset_pattern` | `jasna-windows-{version}.7z*` | 一个归档家族的文件名通配符；`{tag}` 为完整标签，`{version}` 去掉开头的 `v`。 |
| `release_tag` | 空 | 自动生成 `新版标签-旧版标签.patch`；可自定义以区分软件或平台。 |
| `entry_file` | `jasna.exe` | 两个版本共有的入口文件相对路径；空值适用于普通目录数据，不剥离外层目录。 |
| `volume_size_mb` | `1000` | 十进制 MB，范围 1..2000；压缩包超过该大小才分卷。 |
| `mode` | `publish` | 构建、验证和发布；已有完整 Release 只同步标签下的报告。`validate` 仅构建验证，不创建 Release 或标签。 |
| `compression_level` | `3` | 快速压缩、16 MiB 字典；`5` 用 64 MiB 字典获得更小的产物。 |
| `recovery_run_id` | 空 | 从该已完成运行的 `patch-build-output` 恢复已验证产物并补传缺失附件。 |

版本只来自本次输入。补丁名称、更新提示、来源链接、包内说明和报告均使用这组参数。源仓库、附件规则、入口文件和分卷大小可按项目调整。

支持单个 `.7z`、`.zip`，以及从 `.001` 开始的任意数量连续分卷。多个归档家族、缺失分卷或缺少 GitHub SHA-256 时停止。入口文件用于确定应用根目录：如果当前层只有一个子目录且尚未找到入口，会逐层剥离外层目录。

流程分别显示预检、下载解压、比较、打包、实际更新验证及发布的耗时。源附件先验证 SHA-256，再解压检查 CRC。最终压缩包必须实际更新一份干净旧版，其文件集合、大小和 SHA-256 与原始新版完全相同，才能发布。所有远端发布附件也会核对 SHA-256。

## 报告与发布标签

`main` 保存通用源码和文档，不保存某一次构建的结果。每个补丁的实际报告有两个一致的入口：

- 对应 Release 的 `diff.json`、`source-assets.json`、`verification.json`、`SHA256SUMS.txt` 附件。
- 对应发布标签下的 `reports/` 目录，内容与上述附件逐字节一致。

发布时，工作流从本次校验通过的附件创建独立报告提交，并让发布标签指向该提交；不会把报告写回 `main`。标签快照包含生成时的源码和通用 README，整个 `reports/` 目录由本次报告替换，避免混入历史结果。`SHA256SUMS.txt` 校验的是 Release 附件，包括未提交到 Git 的压缩包。

已有完整 Release 重跑时，先验证全部远端附件及报告绑定，再补齐或修正标签快照；报告已经一致则不创建新提交。此路径不重新下载上游归档、不重建补丁、不修改发布附件。更新标签只允许快进；检测到并发修改时停止。

`verification.json` 记录生成补丁时的验证运行，重跑同步报告不会改写原始验证记录。历史报告应从对应标签或 Release 查看。

若草稿已有部分附件，使用原配置并填写 `recovery_run_id`。脚本校验版本、源仓库、入口、分卷参数及产物 SHA-256，只补传缺失文件；同名但内容不同的附件会停止。恢复产物保留 3 天。不同标签可以并行，同一标签的运行排队。

## 本地目录模式

需要 Python 3.11+；打包需要支持目标格式的 7-Zip，实际更新验证还需要 PowerShell。用实际标签设置 `OLD_TAG` 和 `NEW_TAG` 后运行：

```bash
python3 scripts/build_patch.py /path/to/old /path/to/new /path/to/patch \
  --old-tag "$OLD_TAG" --new-tag "$NEW_TAG"
```

该模式不需要 GitHub，也不要求目录内存在可执行文件。需要检查入口或记录上游来源时，增加 `--entry-file bin/App.exe --source-repository owner/app`。

输出包含 `payload/`、增删改清单、旧新版全量文件清单、更新脚本和说明。变化文件保存新版完整内容，不使用二进制 delta。按需用 7-Zip 打包，超过分卷阈值时切成标准连续分卷；发布前解压最终包并实际应用，核对全量新版文件，然后生成附件校验清单。

原组件许可证和声明继续适用，见补丁内 `upstream-notices/` 及相应上游发行包。

## 开发检查

```bash
python3 -m unittest discover -s tests -v
```

完整集成测试需要 `7zz`（或 `7z`）和 `pwsh`；缺少工具时明确标为跳过。测试覆盖参数和来源绑定、下载校验、分卷、真实 PowerShell 更新、错误基准拒绝，以及报告快照和标签同步。CI 中执行完整测试集。
