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
