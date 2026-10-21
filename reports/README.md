# 发布报告

`main` 不保存具体构建报告。请在对应 Release 下载报告附件，或切换到相应发布标签查看此目录。

工作流会将校验通过的 `diff.json`、`source-assets.json`、`verification.json` 和 `SHA256SUMS.txt` 原样写入标签快照，替换此目录；报告提交不会写回 `main`。

校验清单中的压缩包是 Release 附件，不存入 Git。验证报告保留原始构建运行的信息，同步标签不会重新生成这些记录。
