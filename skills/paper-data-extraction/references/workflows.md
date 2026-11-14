# 解析与结果

以下路径是示例，使用实际输入绝对路径和新的输出目录。python 使用 SKILL.md 选定的解释器；表格依赖 pdfplumber，扫描 PDF/图片还需 parsing extra、外部 Tesseract 及语言包。

## 常用命令

```text
python -m litsearch.document_parse paper.pdf --pages 3-5 --table-strategy text --ocr off --out-dir table-01
python -m litsearch.document_parse scanned.pdf --pages 3 --ocr auto --language eng+chi_sim --out-dir ocr-01
python -m litsearch.document_parse table.html --out-dir html-01
python -m litsearch.document_parse scan.png --ocr always --language eng --out-dir image-01
```

页序可用 `--pages 1,3-5`；先核对纸面页码对应关系。PDF 默认 table-strategy=lines、ocr=off，图片在 off 时不能做 OCR。auto 以整页原生文字字符数为阈值（`--min-native-chars` 默认 20），不能检测每个表区域是否为图片。原生页脚超过阈值、目标表却是扫描图时，auto 可能完全不做 OCR；核对目标页后用 `--ocr always --pages ...` 仅处理有关页面。中文语言包缺失时报告缺项，不把错误空文本当作论文无数据。

预算参数见 `--help`，包括 `--max-pages`、`--max-input-bytes`、`--max-output-bytes`、`--max-seconds` 等；只在任务需要时调节。`--tesseract` 可指定已存在的引擎，不将用户路径拼成 shell 代码。CLI 在独立进程中执行以落实总时限；不要为省事改为无界循环解析。

## 结果检查

- 退出 0：所请求提取完成，结果仍为 extracted_pending_review；退出 3：partial；退出 2：无效输入、失败或预算不能满足。
- document.json 中保留源 SHA、页码、文本、OCR 坐标/置信度以及表格原矩阵、跨度、图注/单位/脚注线索。字段以实际 JSON 为准，不凭记忆捏造字段。
- 对 untrusted 表格，报告不能可靠提取的页/表。不要将错列数字重新排列成看似可信的结果。
- 原始矩阵与整理表分开留存，清理表格不覆盖原始输出。数字和原文条件之间的归属需要阅读确认。

## 目标核对与回退

| 观察到的情况 | 后续动作 |
| --- | --- |
| 用户指定 Table 2，但 JSON 为 0 表或只提取相邻文字 | 查看指定页/表所在区域；确认页序和图片内容后，尝试适用的文字策略或 always OCR，不报告“原文无表” |
| OCR 数字置信度很高，但关键值、表头或列归属可疑 | 对照原图检查小数点、负号、指数、单位与脚注；confidence_only_not_validated 不构成正确性证明 |
| HTML 可见值与 raw_matrix 不一致 | 检查 raw_html 与可见表；CSS 隐藏文字、折叠行、重复打印版本可能被一起提取，保留歧义而非直接拼成数值 |

使用已有 PDF 查看器、pypdfium2 或 pdftoppm 渲染所需页/区域；已有图片直接查看。只核对与任务有关的表和异常位置。没有查看工具时保留原始提取结果并标明视觉待核对，不能仅凭 JSON 声称“已与原文完全一致”。不同策略或 OCR 结果冲突时，两次输出都留存，记录采纳依据，不覆盖到只剩一份看似正确的表。

## 整理表建议

按用户实际问题选择列；发酵数据通常包含来源、文件页序、表号、宿主/菌株、改造、培养条件、指标、原始数值、单位、时间/规模、对照、脚注和复核状态。不要为了填满列而推断缺失值。过程参数表可以使用材料/配方/温度/时长/量测方式等适用字段，不强套生物学列名。

整理副本至少应可回到一个原始单元格或一组明确列出的单元格：`source_file`、`source_sha256`、`file_page`、`table_id`、`cell_ids`（或原始行列/坐标）、`value_raw`、`unit_raw`、`header_raw`、`note_raw`、`method`、`visual_check`、`unresolved`。这是整理表的字段约定，不是声称解析器直接提供了全部语义字段。单元格 ID、原始文字和位置从实际 document.json 取；表号、单位与脚注的归属由阅读确认。

`value_raw` 保持字符串，保留 `0.040`、`<0.04`、`-0.04`、`0.04*` 等区别；不为转成浮点数而丢精度、比较符或注记。`visual_check` 可为 inspected / pending / not_required 并附定位依据；不自动等于科学批准。转换后的值另列数值、单位、公式及依据，不覆盖 value_raw。源单元格以 `=`、`+` 等开头时作为原文字符串输出，写 XLSX 时不能误存为公式；需要 CSV 防公式处理时使用另列显示值，保留 JSON 中的原始字符串。
