# Zotero 动态引文

本功能把 Word 中显式标注 DOI 的引用绑定到 Zotero 文献条目，生成 Word 的 `ADDIN ZOTERO_ITEM CSL_CITATION` 引文域及 `ADDIN ZOTERO_BIBL` 参考文献域，再调用已安装的 Zotero Word 插件完成数字编号与文末列表格式化。它也提供在 Word 当前光标位置打开 Zotero 插入引文窗口的命令。

这不是把编号写成普通文本。真正的格式化、编号重排、引用编辑和文献库更新由 Zotero 插件处理。本工具准备可识别的动态域，不替代 Zotero、Word 或 CSL 排版引擎。`convert` 成功仅代表动态域已经准备好，必须继续运行插件并核验结果。

## 安装与前提

```powershell
python -m pip install ".[citations]"
literature-cite --help
literature-cite-word --help
```

需要 Zotero Desktop，已启用的本机 API，以及 Word 中已经安装并加载的 `Zotero.dotm`。Windows 宏调用桥通过 pywin32 直接调用 Word COM 接口；API 读取、转换和离线结构校验可跨平台使用。没有 Word 插件时应从 Zotero 的设置中安装插件；本项目不打包第三方模板、不修改 Office 安全设置，也不把桌面软件安装到 Python 包里。

## 在现有动态引用文稿中继续插入

先在 Word 中保存文稿，并把光标放到需要引用的位置：

```powershell
literature-cite-word add-citation --document "review.docx"
```

在弹出的 Zotero 引用窗口中搜索并选择文献，按 Enter 插入。也可指定文稿中已有的书签，避免依赖光标：

```powershell
literature-cite-word add-citation --document "review.docx" --bookmark "cite_here"
literature-cite-word refresh --document "review.docx"
```

插件命令是异步的，命令返回 `dispatched` 只表示已经发送。处理完 Zotero 对话框后保存并核验：

```powershell
literature-cite-word finish --document "review.docx" --report "saved_check.json"
literature-cite verify "review.docx" --out "citation_check.json"
```

`finish` 会先检查 Word 内存中的当前域，未通过检查时不保存；保存前保留磁盘备份。返回 `saved_for_verification` 表示已保存并核验该快照，不代表收到 Zotero 的异步完成通知。

发送插件命令前会用三秒超时检查本机 Zotero API。Zotero 未启动或 API 不可用时，程序明确报错，不发送宏、不留下备份，也不会把请求记成已执行。

`preferences` 打开 Zotero 文档首选项以更换样式；`bibliography` 在当前位置添加或编辑参考文献列表。以上命令等价于调用已安装模板中的对应入口。插件动作前会在同一目录自动保存磁盘文件备份；有未保存编辑时会停止请求，由用户先保存。`finish` 也可显式用 `--pdf qa.pdf` 导出新的检查用 PDF，不会覆盖已有 PDF。

## 将带 DOI 的文本引用转换为动态域

支持这样的引用组：

```text
某研究结论[Author et al., 2025, Article title, DOI: 10.1000/example]。
多个来源[DOI: 10.1000/example; DOI: 10.1000/another]。
```

从独立克隆的仓库或任意已安装环境运行：

```powershell
literature-cite doctor
literature-cite snapshot --out library_snapshot.json
literature-cite scan "review.docx" --out citation_scan.json
literature-cite prepare "review.docx" --snapshot library_snapshot.json --out citation_plan.json
literature-cite convert --plan citation_plan.json --out "review_zotero.docx" --report conversion_check.json
literature-cite-word refresh --document "review_zotero.docx"
# 等待 Zotero 完成；如弹出对话框，先完成对话框。
literature-cite-word finish --document "review_zotero.docx" --report refreshed_check.json
literature-cite verify "review_zotero.docx" --out final_check.json
```

不安装命令行入口时，可分别使用 `python -m litsearch_zotero.dynamic_citations` 和 `python -m litsearch_zotero.word_plugin` 替代 `literature-cite` 与 `literature-cite-word`。

默认使用数字方括号 IEEE 样式。`prepare --style http://www.zotero.org/styles/ieee` 可显式指定；其他样式需要先在 Zotero 安装。最终投稿格式应从 Word 的 Zotero 文档首选项选择对应期刊样式。结构校验支持数字方括号，其他样式不提供数字对应完整性断言。

## 重复条目与已有数字引用

同一个 DOI 匹配多个 Zotero 条目时，`prepare` 返回 `ambiguous`，不会随机选择。审阅候选后写入 `selections.json`：

```json
{"10.1000/example": "ABCD1234"}
```

重新运行 `prepare --selections selections.json`。缺失条目需要先核实书目后导入 Zotero，再重新读取快照。本功能只读 Zotero API，不自动导入、合并或删除库内条目。

已有 `[1]`、`[2,4–6]` 等纯文本引用时，必须提供明确的编号到 DOI 映射：

```json
{"1": "10.1000/example", "2": "10.1000/another"}
```

使用 `prepare --references reference_dois.json`。已有文末列表默认保留并阻止自动替换；确认该标题之后全部是旧参考文献后，才可给 `convert` 加 `--replace-bibliography`。混合已有 EndNote/Zotero 域、修订痕迹或引用跨越超链接/书签等复杂结构时会停止，不会擅自解域或接受修订。

同一组中每个来源都必须有一个 DOI，并以分号分隔。混合未绑定的作者年份引用或数字引用时会停止；页眉、页脚和脚注等正文以外的 DOI 引用需要单独处理。已有数字引用不能在没有明确编号映射的情况下与 DOI 引用部分转换。

## 数据与验证边界

- `prepare` 保存原稿 SHA-256、引用所在段落及文本位置、DOI、Zotero 条目 key、规范 URI、版本和 CSL 元数据。转换前再次验证原稿与位置；输出必须是新文件。
- 只替换原有引用文字，保留其余正文文字、段落、格式及其他 ZIP 成员；仅新增必要的 Zotero 文档首选项及参考文献域。旧分页缓存随 Word 重新排版。
- `verify` 检查域平衡、唯一引文 ID、DOI/URI 双向绑定、同编号是否指向不同文献，以及数字引用集合与文末编号集合。它结合单条引文和文末 DOI 核对同组引用，不假定 CSL 排序后仍保持输入顺序。它不独立审阅原文，也不能仅凭结构证明 Zotero 插件已执行。
- `pending_refresh=true` 表示文末列表仍为空或为刷新占位符。只有引用组、文末数字与 DOI 全部闭合时，`numeric_correspondence_verified` 才为 true；不输出 DOI 或不使用数字方括号的样式可以保留有效动态域，但不能得到这一完整数字对应断言。
- 插件刷新后的数字映射、文末完整条目和实际重排实验应一并核查；本项目的科学证据及人工审阅要求仍然适用。
- 快照、计划、文稿和操作报告包含个人文献库标识与研究文字，应留在研究工作区；发行包只包含通用代码、文档和合成测试。
- `snapshot` 当前读取本机个人文库。不同账户、群组文库或库内条目迁移后，应重新建立绑定，不能只复制别人的 item key。

## 实现依据

协议由 Zotero 自身定义；本项目是独立实现，调用公开模板入口，不复制或分发第三方插件代码。

- [Zotero Word 插件使用说明](https://www.zotero.org/support/word_processor_plugin_usage)
- [Zotero 官方 Word 模板宏入口](https://github.com/zotero/zotero-word-for-windows-integration/blob/main/build/template/Zotero.dotm/word/vbaProject.bin/Zotero.bas)
- [Zotero 引文与文档首选项实现](https://github.com/zotero/zotero/blob/main/chrome/content/zotero/xpcom/integration.js)
