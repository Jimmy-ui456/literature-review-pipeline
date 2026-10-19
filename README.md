# 综述文献检索与证据整理

> 成熟度：早期实验工具，适合试用、审查和贡献；尚无跨课题准确率基准，也不承诺自动完成可靠综述。本项目采用 MIT License，见 [LICENSE](LICENSE)。

这是一个面向综述准备的文献检索与证据整理工具，当前提供可配置的微生物代谢工程课题与独立工程主题预检。它把检索、去重、身份核验、PDF 身份比对、证据定位和人工审阅分开保存；候选记录、自动分类或同句数字都不是科学结论。

## 唯一源码与发布入口

本 Git 仓库就是唯一源码入口：在原研究工作区中路径为 `github_release/literature-search/`；独立克隆后直接使用克隆根目录。功能代码、配置、测试、文档和打包配置只在这里维护。外层工作区的历史脚本只作转发，不再同步另一套实现。

发行文件由脚本生成，不能当作另一份源码编辑：

```powershell
python -m pip install -e ".[parsing,test,release]"
python -m unittest discover -s tests -v
python scripts/build_release.py --out-dir dist/local-release
```

脚本生成白名单源码 ZIP、wheel、sdist 和 SHA-256 清单；输出目录必须是新的 `dist/` 子目录。CI 使用同一发布脚本。日常上传 GitHub 只需在本仓库检查改动、提交和推送；无需复制源码或同步工作区。详细流程见 [RELEASING.md](docs/RELEASING.md)，详细管线用法见 [PIPELINE_GUIDE.md](docs/PIPELINE_GUIDE.md)。

## 安装

需要 Python 3.10 或更高版本。在本仓库根目录创建独立环境后安装：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install .
```

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
```

查看命令和运行测试：

```powershell
python -m litsearch --help
python -m unittest discover -v
```

```sh
python -m litsearch --help
python -m unittest discover -v
```

内置课题为 `pantothenate`、`lysine` 和 `glutathione`。它们是词表和筛选协议，不代表覆盖所有文献。

## 默认离线演示

以下命令只处理仓库内的手写合成输入，并主动阻断程序运行范围内的 socket 联网；不检索网络、不读取或写入 Zotero，也不使用真实论文、PDF 或标识符。

```powershell
python scripts/run_synthetic_demo.py --out-dir .validation/demo-output
```

```sh
python scripts/run_synthetic_demo.py --out-dir .validation/demo-output
```

输入在 `examples/synthetic/records.json`。其中的数量文字仅用于测试候选提取，不能作为生物学、发酵或产量事实。演示输出会保留未核验身份、待人工审核的记录；`review_ready.json` 必须为空。

## 联网检索

联网检索不是默认测试。请先查看参数，并准备有效联系邮箱、网络条件和对数据源限流的理解：

```powershell
python -m litsearch --help
python -m litsearch --topic pantothenate --source europepmc --out-dir outputs/pantothenate
```

```sh
python -m litsearch --help
python -m litsearch --topic pantothenate --source europepmc --out-dir outputs/pantothenate
```

若使用 PubMed 或 OpenAlex，必须提供有效 `--email`；OpenAlex 还需要 API key。对已有 JSON 的离线复审使用 `--from-json`，只有显式加入 `--verify-online` 才重新联网核实身份。

## 输出与人审

每次运行要求新的空输出目录。`run_manifest.json` 和 `events.jsonl` 记录运行参数、计数与异常；`all_screened_records.json`、CSV/XLSX 和 `review_decisions.template.json` 用于检查候选；`review_ready.json`、RIS 和 review materials 只包含通过身份、证据与人工审核门禁的条目。输出中的“产量核对状态”不会自动归属产量或建立结论。

人工审核者应阅读证据卡和原文，填写与该课题指纹和记录指纹匹配的审阅 JSON，再以 `--review-decisions` 重新运行。没有人工决定，或身份/PDF/证据条件不满足时，条目不会进入可用于综述的集合。

## 可选 Zotero 工具

支持 Word 动态数字引文：`pip install ".[citations]"` 后，使用 `literature-cite` 扫描、绑定 DOI、生成和校验动态引文域；使用 `literature-cite-word add-citation --document review.docx` 在 Word 光标处打开 Zotero 插入窗口，或用 `refresh` 更新编号与文末列表。需要本机 Zotero 和已安装的 Word 插件；插件命令发送不等于刷新完成。完整调用流程见 [Zotero 动态引文](docs/ZOTERO_DYNAMIC_CITATIONS.md)。

`litsearch_zotero` 是本机辅助扩展，核心包不依赖它。先只查看帮助：

```powershell
python -m litsearch_zotero.attach_pdfs --help
python -m litsearch_zotero.audit_missing_pdfs --help
```

附件工具默认是 dry-run，但仍可能读取本机 API；只有显式 `--apply` 才会写入。对真实 Zotero 库操作前，应先审阅 dry-run 输出并备份数据。

限制、架构和参与方式分别见 [docs/limitations.md](docs/limitations.md)、[docs/architecture.md](docs/architecture.md) 与 [CONTRIBUTING.md](CONTRIBUTING.md)。本项目采用 [MIT License](LICENSE)，版权署名为 Jimmy-ui456；历史候选文本与第三方资料边界见 [许可证状态](LICENSE-STATUS.md)。

## 工程主题与Word报告预检

新增独立 `engineering-review` / `python -m litsearch.engineering` 入口，支持湿法刻蚀分面计划、Crossref候选检索及引用/条件预检。它使用独立工程配置，不改变旧微生物筛选和审批规则。运行流程、输入格式和边界见 [工程检索与预检说明](docs/ENGINEERING_REVIEW.md)。

## 联网验证、补漏检、表格与 OCR

新增三个独立命令：`literature-service-probe`、`literature-discover`、`literature-parse`。前两个默认只生成离线计划；第三个仅解析显式指定的本地 PDF/HTML/图片。解析依赖按需安装，OCR 需外部 Tesseract；结果保留原文位置与待核对状态。完整用法及合成演示见 [工程工具说明](docs/ENGINEERING_TOOLS.md)。

## 发布状态与反馈

公开准备的核查范围和未完成能力见 [发布准备说明](docs/RELEASE_READINESS.md)。遇到问题请通过仓库 Issues 提交运行环境、命令、错误和脱敏后的最小示例；不要提交论文全文、私有研究数据或 API 密钥。
