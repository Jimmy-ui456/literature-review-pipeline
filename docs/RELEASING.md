# 唯一源码与发行流程

## 维护边界

`github_release/literature-search/` 在原研究工作区里已经是独立 Git 仓库，现在明确作为唯一源码入口。独立克隆后，克隆根目录就是这个入口，不依赖外层研究工作区。

- 只在本仓库维护 `litsearch/`、`litsearch_zotero/`、`tests/`、`scripts/`、`docs/`、配置和 `pyproject.toml`。
- 外层的 `litsearch/__init__.py`、旧 Python 入口和 Zotero 脚本只做兼容转发，实际导入来自本仓库；不再维护业务代码副本。`litsearch.legacy` 保存旧脚本需要的兼容 API。
- 外层依赖本地研究资料的历史脚本与测试不属于可发布核心。论文、研究输出、审查/批准材料及其他项目留在工作区，发布脚本不会从父目录采集文件。
- 以前的发行 ZIP 和验证快照是历史产物，不再作为同步来源，也不在本次迁移中删除或重写。

## 本地验证与生成发行文件

从本仓库根目录、独立虚拟环境执行：

```powershell
python -m pip install -e ".[parsing,test,release,citations]"
python -m unittest discover -s tests -v
python scripts/build_release.py --out-dir dist/local-release
```

Windows 与 POSIX 使用相同的 Python 命令。`--out-dir` 的相对路径以本仓库为基准；只允许新的 `dist/` 子目录，拒绝已存在的目标、符号链接和 Windows junction/reparse 路径，不删除或覆盖历史产物。省略该参数时使用带 UTC 时间的目录名。

发布脚本依次执行现有白名单和敏感信息门禁、隔离复制、wheel/sdist 构建及其内容门禁，再生成固定成员顺序和时间戳的源码 ZIP、逐文件 SHA-256 清单。构建使用当前 Python 环境及显式安装的 `release` 依赖，不启动隐式联网安装的隔离构建。它从当前工作树取内容，包括尚未提交但已在白名单内的文件；发布前仍须人工检查 Git diff 和敏感信息，哈希及模式扫描不是科学或安全保证。

```text
dist/local-release/
  source/                         # 生成的隔离暂存区；不维护、不反向同步
  artifacts/
    literature-search-source.zip # 白名单源码，供下载/核对
    *.whl
    *.tar.gz
  release_manifest.json           # 成功标记、源码/产物字节数与 SHA-256
```

只有全部请求的门禁通过后才写成功清单。失败时保留部分输出供检查，不能当作可交付包；修复后用新目录重试。只需要离线源码 ZIP 时可加 `--source-only`，此模式的清单明确把 wheel/sdist 门禁标为 `not_run`，不等于完整发布验证。源码 ZIP 的成员和时间戳固定；不宣称 wheel/sdist 或整个清单逐字节可复现。

CI 调用同一个 `scripts/build_release.py`，随后安装生成的 wheel，在源码目录之外运行测试和合成演示，避免测试误用工作树代替安装包。CI 不代表跨课题科学评测。

`tmp/` 是本机临时研究资料目录，与 `.validation/`、`dist/` 一样明确排除；它不会进入源码包、安装包或 Git 提交。其他未知目录仍会触发发行门禁，不能静默忽略。

## 上传 GitHub

普通开发只需要本仓库内的提交、推送流程，不先复制到其他源码目录：

```powershell
git status --short
git diff
git add -p
# 新增文件需逐个检查后显式 git add；上面的 -p 不会自动加入未跟踪文件。
git commit -m "Describe the reviewed change"
git push
```

`dist/`、`.validation/`、虚拟环境和运行输出已经被 `.gitignore` 排除。源码直接推送到 GitHub；若需要附件，可人工从 `artifacts/` 选择生成包上传到相应 Release，而不是把它们提交回源码。发布脚本不会自动 commit、push、创建 Release 或上传文件。
