# Windows Release 文件差异补丁

从两个 GitHub Release 的 Windows 归档，或两个本地目录生成文件级差异包。默认配置使用 [Kruk2/jasna](https://github.com/Kruk2/jasna/releases)，也可选择其他公开源仓库、版本和附件规则。本仓库不是上游官方发布渠道。

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
   python3 scripts/build_patch.py old new patch --old-tag v0.11.1 --new-tag v0.11.2 --entry-file jasna.exe --source-repository Kruk2/jasna
   ```

4. 在 `patch/` 内使用 7-Zip 创建 7z 包。压缩结果超过 **1,000,000,000 字节**时按该大小切分为 `.7z.001`、`.002` 等标准连续分卷；不超过时保留单个 `.7z`。
5. 解压最终压缩包，用包内 PowerShell 脚本更新一份干净的旧版本，核对新版本全部文件的大小及 SHA-256，并确认不存在旧版多余文件。
6. 为发布附件生成 `SHA256SUMS.txt`。

`diff.json` 记录新增、修改、删除文件及差异文件总大小；`old-manifest.json`、`new-manifest.json` 记录两个版本的全量文件校验信息。变化文件以新版本完整内容保存，不使用二进制 delta；压缩比取决于实际变化。

上游文件及其组件保留各自许可证和声明，参见上游对应版本的 `LICENSING.md`、`RELEASE_SOURCES.md` 及发行包内许可证文件。

## GitHub Actions 自动发布

在 Actions 页面手动运行 **Build and publish Windows patch**。工作流使用 GitHub 内置 `GITHUB_TOKEN`，不需要额外 PAT 或云环境 Secret。

| 输入 | 默认值 | 用途 |
| --- | --- | --- |
| `source_repository` | `Kruk2/jasna` | 上游 `owner/repository`。 |
| `old_tag` | `v0.11.1` | 当前安装的旧版本实际标签。 |
| `new_tag` | `v0.11.2` | 升级目标版本的实际标签。 |
| `asset_pattern` | `jasna-windows-{version}.7z*` | 匹配一个归档家族的文件名通配符；`{tag}` 为完整标签，`{version}` 仅去掉开头的 `v`。 |
| `release_tag` | 空 | 自动生成 `新版标签-旧版标签.patch`；可自定义以区分软件或平台。 |
| `entry_file` | `jasna.exe` | 旧新版都存在的入口文件相对路径，例如 `bin/App.exe`；空值适用于普通目录数据，不做入口检查或外层目录剥离。 |
| `volume_size_mb` | `1000` | 十进制 MB，范围 1..2000。压缩包超过该大小才分卷。 |
| `mode` | `publish` | 构建并发布；已发布且校验完整则成功跳过。`validate` 只构建验证和保留产物，不创建或发布 Release。 |
| `compression_level` | `3` | 快速压缩、16 MiB 字典；`5` 用 64 MiB 字典获得更小的产物。 |
| `recovery_run_id` | 空 | 使用该已完成运行中的 `patch-build-output`，复用已验证产物并补传缺失附件。 |

例如 Jasna 的下次更新，只需将 `old_tag` 改为 `v0.11.2`、`new_tag` 改为实际发布的新版本，其余参数沿用默认值。补丁名称、更新脚本提示、来源链接、README 和校验报告都会使用所选版本。

其他软件的配置示例：

```text
source_repository: owner/app
old_tag: v1.0.0
new_tag: v1.1.0
asset_pattern: App-{version}-win64.zip
entry_file: bin/App.exe
release_tag: app-v1.1.0-v1.0.0.patch
volume_size_mb: 500
```

支持单个 `.7z`、`.zip`，以及从 `.001` 开始的任意数量连续分卷，例如 `.7z.001` 到 `.7z.005`。匹配到多个归档家族或缺失分卷会停止，需要收窄通配符。所有所选附件必须有 GitHub API 提供的 SHA-256；脚本不会绕过缺失的完整性校验。内置 Token 可读取公开源仓库；其他私有源仍需相应读取权限。

入口文件用于确定应用根目录。如果入口尚未出现且当前层只有一个子目录，会逐层剥离外层目录，支持 `App-1.0/` 和 `App-1.1/` 这样的不同包装目录。入口路径必须对旧新版都有效。若布局更复杂，可直接解压后用本地目录模式指定根目录。

发布模式会自动创建缺失的目标草稿，并在校验通过后发布。完整的已发布版本重跑不会重建或覆盖。若草稿已有部分附件，填入 `recovery_run_id` 并使用原来的配置：脚本校验版本、源仓库、入口、分卷配置及产物 SHA-256，仅补传缺失文件；同名但内容不同的远端附件会停止，避免覆盖。产物保留 3 天，下载恢复产物需 `actions: read` 权限。不同标签可并行运行，同一标签的运行排队执行。

流程分为预检、工具安装、旧版下载解压、新版下载解压、内容比较、压缩分卷、实际更新验证和发布，各阶段独立显示耗时。入口、版本、源仓库和校验数量不依赖 Jasna。磁盘需求先按所选附件大小估算，下载后读取归档实际解压大小再次检查；空间不足时提前停止。只在需要时清理临时 Ubuntu runner 上预装的 SDK。Action 均固定到使用 Node 24 的官方提交。

## 本地目录模式

不需要 GitHub，也不需要目录中存在 `.exe`：

```bash
python3 scripts/build_patch.py /path/to/old /path/to/new /path/to/patch \
  --old-tag 1.0 --new-tag 2.0
```

需要检查入口及记录上游来源时，增加 `--entry-file bin/App.exe --source-repository owner/app`。输出包含 `payload/`、增删改清单、旧新版全量校验信息和通用 PowerShell 更新脚本，随后可自行用 7-Zip 打包。

## 检查与性能

```bash
python3 -m unittest discover -s tests -v
```

完整小样本集成测试需 `7zz`（或 `7z`）和 `pwsh`；缺少工具时该项明确标为跳过。测试覆盖 Jasna 兼容路径、其他项目版本、ZIP 外层目录、不同入口、无入口普通数据、真实分卷和更新、错误基准拒绝及恢复参数绑定。CI 不启动应用或执行 GPU 功能测试。

同一份约 626 MB 输入的本地压缩基准：级别 5 用时 169 秒、产物 150.3 MB；级别 3 用时 36 秒、产物 164.2 MB。压缩耗时减少约 79%，体积增加约 9%。这是本地单次基准，GitHub runner 实际耗时以阶段日志为准，详见 `reports/action-review.json`。已有 Release 附件不会因脚本或默认参数变化而更新。
