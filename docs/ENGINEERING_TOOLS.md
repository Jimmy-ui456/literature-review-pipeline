# 工程工具：联网验证、补漏检、表格与 OCR

这些是独立工具入口，适合安装、发布和单独核验。原有综述流程、论文库和审核决定不会被自动重跑或迁移。默认测试只使用手写合成材料；本文的在线命令是将来由使用者明确启用的操作。

## 安装

在发行目录运行：

```sh
python -m pip install .
# PDF 表格：
python -m pip install ".[tables]"
# PDF 表格、扫描 PDF/图片渲染和 OCR 的 Python 依赖：
python -m pip install ".[parsing]"
# 合成 PDF 集成测试使用 reportlab：
python -m pip install ".[parsing,test]"
```

HTML 表格使用标准库，无额外解析依赖。PDF 表格使用 pdfplumber；扫描 PDF 渲染使用 pypdfium2；图片使用 Pillow。OCR 还需要单独安装 Tesseract 可执行程序及所需语言包：Ubuntu 可安装 tesseract-ocr；其他平台按 Tesseract 官方安装说明配置。Python 安装包不会偷偷安装原生引擎。若程序不在 PATH，使用 --tesseract 指定自己的可执行程序。中文需安装 chi_sim 语言数据，再指定 --language eng+chi_sim。

安装后有三个命令：literature-service-probe、literature-discover、literature-parse；对应的 python -m 调用在下面给出。所有输出应使用新的独立目录。

## 3：真实服务验证工具

```sh
# 只写计划，不联网
python -m litsearch.service_probe --out-dir validation/service-plan
# 将占位文字替换成已知公开数据后，才执行真实请求
python -m litsearch.service_probe --online --query "公开检索词" --doi "替换成真实DOI" --title "该DOI对应完整题名" --email "your-real-email@example.org" --out-dir validation/service-live
```

在线验证包含 Europe PMC、Crossref、PubMed 搜索分页、响应结构、精确身份，以及故意换标题后的拒绝检查。身份测试的 DOI 须同时被这些服务收录；not_indexed 和请求失败会在报告中区分。OpenAlex 通过 --openalex 显式启用，从 OPENALEX_API_KEY 环境变量读取凭据；凭据不写进命令示例或响应诊断。--download-pdf 另外启用元数据明确标为 OA 的 PDF 获取及首页身份核对，失败时继续后续候选。

默认 --page-size 2、--max-pages 3、--max-bytes 5242880、--timeout 30、--retries 2、--max-pdf-candidates 3。宽查询很容易达到页数预算；truncated 表示此次覆盖未完成，不等于服务断线。退出码 0 表示离线计划生成或所有已请求阶段通过，1 表示失败/截断，2 表示无效输入输出；被跳过的可选项不会标成全部服务已验证。

probe_report.json 保存阶段状态、UTC 时间、返回数量和响应摘要，response_*.bin 保存脱敏快照。每个快照区分原始响应哈希与保存后哈希；可核对逐阶段检查点，不使用旧项目核验缓存。该工具测接口及适配器，不产生准确率或科学审批。

## 4：补漏检工具

```sh
# 默认只生成离线计划
python -m litsearch.discovery --topic lysine --alias "额外的目标物别名" --out-dir validation/discovery-plan
# 在使用者准备好输入后显式启用检索和引用追踪
python -m litsearch.discovery --topic lysine --aliases-json aliases.json --seeds-json seeds.json --online --out-dir validation/discovery-live
# 非生命科学主题可使用自定义课题配置和 Crossref 候选检索
python -m litsearch.discovery --topic custom-topic.json --source crossref --online --out-dir validation/crossref-live
# 用先前快照离线复放；原计划、预算和哈希都要相符
python -m litsearch.discovery --replay validation/discovery-live --out-dir validation/discovery-replay
```

aliases.json 是普通短语数组，例如 ["目标物的另一种写法", "明确的同义词"]；不接受检索语法。seeds.json 是标识符数组，每项必须是 {"source":"MED","id":"已知PMID"} 或 {"doi":"已知DOI"} 的形状；请将占位文字替换成有效标识符。没有种子时只检索，不虚构引用图。

Europe PMC 支持检索、向后找参考文献、向前找引用文章。Crossref 可检索一般学科论文并提供已登记的向后引用；它没有本工具所用的向前引用接口。覆盖范围会保存到报告，不宣称查全所有文献。--source 可指定检索源；未指定时使用 Europe PMC。相同 DOI/来源 ID 的候选保留全部发现途径及冲突信息；没有可靠标识符的参考条目保留为 unresolved。

默认请求/页/观察条目/引用深度预算是 50/30/1000/1；--page-size 默认为 100。计划中的用户别名是独立检索路线；种子先登记，DOI 解析与向后/向前引用路线分别审计。调度先轮流请求别名、种子路线、来源基线和主题词的首页，再按路线轮流请求后续页，避免宽泛词分页耗尽预算而跳过具体别名或种子。请求失败不重试。预算计数包含重复观察和未解析条目；请求数包含失败请求，page_attempts 记录发起的页请求，pages 只计通过响应结构验证的页面，因此网络失败不会伪装成成功页或消耗页预算。请求/页预算及未完成的路线都会保留在报告中。

`discovery.json` 使用 schema_version 2；coverage 按 source/provider、query/seed、route_class、方向和深度列路线及页数、API 报告总数、返回数和原因。`priority_order` 给出固定顺序：1 用户别名，2 显式种子解析/引文，3 来源基线，4 主题词，5 新发现邻居；即使预算截断，未尝试路线仍保留并带预算原因，部分路线标为 budget_truncated。状态包括 unattempted、failed、zero_hit、partial、api_reported_end、budget_truncated；Crossref 未提供的向前接口或无法解析的 DOI 会标为 unavailable，不与失败或零命中混为一谈。counts.requests、counts.page_attempts 和 counts.pages 分别是请求总数、页请求尝试数和已验证页面数。达到预算或待扩展深度边界会明确报告 partial。plan.json 保存课题快照、指纹、查询和预算；discovery.json 保存候选、引用边、冲突、覆盖和失败；responses.json 及 response_*.json 保存请求索引和原响应哈希。

退出码 0 表示离线计划或所声明范围内的读取结束，2 表示部分结果，1 表示输入、复放或输出错误。候选一直保持未审阅状态，不自动进入稿件或人工批准清单。

## 5、6：复杂表格及 OCR

```sh
# 本地 UTF-8 HTML，保留 rowspan/colspan、表头和脚注
python -m litsearch.document_parse local-table.html --out-dir validation/html-table
# 有文字的 PDF；只读取指定页
python -m litsearch.document_parse local-paper.pdf --pages 1,3-5 --out-dir validation/pdf-table
# 扫描页自动 OCR；原生文字与 OCR 分层保存
python -m litsearch.document_parse scanned.pdf --ocr auto --language eng+chi_sim --out-dir validation/ocr
# 无框线表可显式尝试文字位置策略
python -m litsearch.document_parse local-paper.pdf --table-strategy text --out-dir validation/text-table
```

document.json 保存来源 SHA-256、页码、原生字符、OCR 词位置与置信度，以及表格的单元格、合并跨度、原始矩阵和位置。空格不被猜填。HTML 表头及 tfoot 按显式结构记录；PDF 的表头、单位、邻近图注和脚注是待核对线索。PDF 无框表优先在表题附近、同一栏的局部区域找表；检测到双栏时避免把整页两栏正文并入同一张表。结构或几何可信度不足的候选标为 `untrusted` / `not_reliably_extracted`，不暴露伪造单元格，并将该页标为 partial。跨页续表仅标候选关系，不自动拼接或继承实验条件。

OCR 默认关闭。auto 在原生文字不足时启用，always 强制保留另一层识别结果；原始 PDF/图片保持原样。扫描表格的行列是位置启发式候选，仍可能把双栏正文当表格，必须核对；低置信度的原生 PDF 表格会明确拒绝可靠提取，而不是包装成 pending review。数字、单位、实验条件不会自动改写。所有结果保持 AI pending、science approval none、human review null。

输入大小、页数、格数、跨度、像素、OCR 超时、总时间和输出大小均有显式限额。CLI 使用隔离解析进程；缺少依赖、损坏文件或失败页面会报错或返回部分结果。退出码 0 表示提取完成但待核对，3 表示部分结果，2 表示失败。详细参数用 --help 查看。

## 合成验证与发布

```sh
python -m unittest discover -v
python scripts/run_engineering_tools_demo.py --out-dir validation/synthetic-tools
python scripts/release_check.py --root . --prepare-stage validation/source-stage
```

演示只生成手写 HTML，核对合并表头、空格和脚注，并生成两个离线计划；过程中阻断 socket 连接。它不读取真实论文，不验证真实服务，也不声称已实测原生 OCR 引擎。PDF 和 OCR 适配器另有合成/故障注入测试；原生引擎可用时才执行相应集成测试。

发行门禁检查新模块、命令入口和 optional extras 是否进入 wheel/sdist。CI 安装解析依赖并在 Linux 安装 Tesseract，使用合成输入验证；默认 CI 不联网检索学术服务。没有对当前项目论文库执行这些新工具。

## 参考的一手资料与开源案例

实现独立编写，参考接口和设计，没有复制上游结果或采用其准确率作为本项目指标：

- [Crossref REST API 文档](https://github.com/CrossRef/rest-api-doc)：搜索游标、DOI 元数据和参考文献字段。
- [rOpenSci EuropePMC](https://github.com/ropensci/europepmc)：检索、参考文献及被引文章访问；同时核对其 API 文档。
- [pdfplumber](https://github.com/jsvine/pdfplumber)：基于几何位置提取文字、表格和单元格，适用于文字 PDF。
- [Tesseract](https://github.com/tesseract-ocr/tesseract)：原生 OCR、语言数据及 TSV 词坐标/置信度。
- [pypdfium2 Python API](https://pypdfium2.readthedocs.io/en/stable/python_api.html)：页面渲染和像素/PDF 坐标转换。

这些工具补的是工程能力；跨课题准确率/召回率评测仍需独立人工标注数据，未包含在本次四项代码补全中。
