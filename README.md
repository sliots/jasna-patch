# Jasna Windows 增量更新包

从 [Kruk2/jasna](https://github.com/Kruk2/jasna/releases) 的官方 Windows 发布附件生成文件级差异包。本仓库不是上游官方发布渠道。

## v0.11.1 → v0.11.2

补丁 Release：`v0.11.2-v0.11.1.patch`。

1. 关闭 Jasna，备份原安装目录。
2. 下载补丁全部附件。若为 `.7z.001`、`.002` 等分卷，将所有分卷放到同一目录，从 `.001` 解压；单个 `.7z` 直接解压。
3. 将补丁解压到独立目录，打开 PowerShell 执行：

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\Apply-Patch.ps1 -InstallDir "C:\Jasna"
   ```

   `InstallDir` 应直接包含 `jasna.exe`。

更新脚本先校验全部官方 v0.11.1 文件，再覆盖新增/修改文件、删除明确列出的旧文件，最后校验全部官方 v0.11.2 文件。未列入官方清单的用户文件保留；如果官方文件已被修改，脚本会拒绝更新。失败时请恢复备份，没有自动回滚。此补丁不需要 GPU 来生成或应用；应用本身仍有原有系统和 GPU 要求。

## 生成方法

需要 Python 3.11+ 和支持该归档格式的新版 7-Zip。

1. 下载两个版本的全部 `jasna-windows-*.7z.*` 附件，对照 GitHub Release 提供的 SHA-256 校验。
2. 分别从 `.7z.001` 解压到 `old/` 和 `new/`，目录内应直接包含 `jasna.exe`。
3. 生成补丁目录：

   ```bash
   python3 scripts/build_patch.py old new patch
   ```

4. 在 `patch/` 内使用 7-Zip 创建 7z 包。压缩结果超过 **1,000,000,000 字节**时按该大小切分为 `.7z.001`、`.002` 等标准连续分卷；不超过时保留单个 `.7z`。
5. 解压最终压缩包，用包内 PowerShell 脚本更新一份干净的旧版本，核对新版本全部文件的大小及 SHA-256，并确认不存在旧版多余文件。
6. 为发布附件生成 `SHA256SUMS.txt`。

`diff.json` 记录新增、修改、删除文件及差异文件总大小；`old-manifest.json`、`new-manifest.json` 记录两个版本的全量文件校验信息。变化文件以新版本完整内容保存，不使用二进制 delta；压缩比取决于实际变化。

上游文件及其组件保留各自许可证和声明，参见上游对应版本的 `LICENSING.md`、`RELEASE_SOURCES.md` 及发行包内许可证文件。

## GitHub Actions 自动发布

在 Actions 页面手动运行 **Build and publish Windows patch**。工作流使用 GitHub 内置 `GITHUB_TOKEN`，不需要额外 PAT 或云环境 Secret。

可选输入：

| 输入 | 默认值 | 用途 |
| --- | --- | --- |
| `mode` | `publish` | 构建并发布；已发布版本会校验远端清单后成功跳过。选择 `validate` 可重新构建和验证，保留产物但不发布。 |
| `compression_level` | `3` | 快速压缩，字典 16 MiB；`5` 使用 64 MiB 字典以获得更小的产物。 |
| `recovery_run_id` | 空 | 发布中断时，填写保留了 `patch-build-output` 的已完成运行 ID，复用其已验证产物并补传缺失附件。 |

流程分为预检、工具安装、旧版下载解压、新版下载解压、内容比较、压缩分卷、实际更新验证、发布。每个阶段单独显示耗时，摘要也记录阶段时间。上游八个分卷和发布附件始终经过 SHA-256 校验，最终压缩包仍会解压并实际执行 PowerShell 更新，随后独立核对全量文件集合和内容。

目标固定为 v0.11.1 → v0.11.2。首次发布须先创建 `v0.11.2-v0.11.1.patch` 草稿；完整的已发布版本重跑不会重建或覆盖。若草稿已有部分附件，使用 `recovery_run_id` 恢复：校验原产物和已存在的远端附件，仅补传缺失文件。遇到同名但内容不同的附件会停止，避免覆盖。产物保留 3 天；下载恢复产物需工作流的 `actions: read` 权限。

新构建使用 Ubuntu runner 并检查至少 38 GB 可用空间，必要时仅清理该临时 runner 上预装的 Android、.NET、Swift 和 GHC SDK。已发布版本的预检跳过路径不需要清理 SDK 或下载原始分卷。验证覆盖文件更新，不包含 Windows 应用启动或 GPU 功能测试。Action 均固定到使用 Node 24 的官方提交。

同一份约 626 MB 输入的本地压缩基准：级别 5 用时 169 秒、产物 150.3 MB；级别 3 用时 36 秒、产物 164.2 MB。压缩耗时减少约 79%，体积增加约 9%。这是本地单次基准，GitHub runner 的实际耗时以阶段日志为准，详见 `reports/action-review.json`。已有 Release 附件不会因默认参数变化而更新。

本地检查：

```bash
python3 -m unittest discover -s tests -v
```

完整小样本集成测试需 `7zz`（或 `7z`）和 `pwsh`，实际创建、解压补丁并执行更新脚本；缺少工具时该项会明确标为跳过。GitHub Ubuntu runner 上运行同一组测试。
