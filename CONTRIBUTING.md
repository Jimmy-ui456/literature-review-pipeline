# 参与贡献

本仓库是唯一源码入口，不是从外层工作区手工同步的发行副本。请在本仓库根目录使用 Python 3.10+ 的虚拟环境安装项目，再运行：

```powershell
python -m pip install -e ".[parsing,test,release,citations]"
python -m unittest discover -v
python scripts/run_synthetic_demo.py --out-dir .validation/contributor-demo
python scripts/build_release.py --out-dir dist/contributor-release
```

POSIX 环境可使用相同的 `python` 命令。提交变更前，新增功能应有能实际运行的测试，且不得把网络、真实 Zotero 或真实研究数据作为默认测试前提。

只维护这里的代码、测试和文档；不要恢复工作区根目录的重复包或手工编辑 `dist/`、`.validation/` 中的生成物。发布脚本不提交、不推送、不上传文件；常规 Git 流程和生成物边界见 [docs/RELEASING.md](docs/RELEASING.md)。

安全回归要求：不得弱化身份核验、PDF 身份、证据或人工审核门禁；不要把未核验缓存、合成输入或自动提取的数字标记为可用于综述；附件工具必须保持默认 dry-run 和显式 `--apply` 才写入的行为。请避免提交 API key、本机绝对路径、PDF、数据库、缓存和生成输出。
