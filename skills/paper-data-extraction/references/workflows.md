# 解析与结果

以下路径是示例，使用实际输入绝对路径和新的输出目录。python 使用 SKILL.md 选定的解释器；表格依赖 pdfplumber，扫描 PDF/图片还需 parsing extra、外部 Tesseract 及语言包。

## 常用命令

```text
python -m litsearch.document_parse paper.pdf --pages 3-5 --table-strategy text --ocr off --out-dir table-01
python -m litsearch.document_parse scanned.pdf --pages 3 --ocr auto --language eng+chi_sim --out-dir ocr-01
python -m litsearch.document_parse table.html --out-dir html-01
python -m litsearch.document_parse scan.png --ocr always --language eng --out-dir image-01
```

页序可用 `--pages 1,3-5`；先核对纸面页码对应关系。PDF 默认 table-strategy=lines、ocr=off，图片在 off 时不能做 OCR。auto 不保证每页调用 OCR，应查看实际页级结果；中文语言包缺失时报告缺项，不把错误空文本当作论文无数据。

预算参数见 `--help`，包括 `--max-pages`、`--max-input-bytes`、`--max-output-bytes`、`--max-seconds` 等；只在任务需要时调节。`--tesseract` 可指定已存在的引擎，不将用户路径拼成 shell 代码。CLI 在独立进程中执行以落实总时限；不要为省事改为无界循环解析。

## 结果检查

- 退出 0：所请求提取完成，结果仍为 extracted_pending_review；退出 3：partial；退出 2：无效输入、失败或预算不能满足。
- document.json 中保留源 SHA、页码、文本、OCR 坐标/置信度以及表格原矩阵、跨度、图注/单位/脚注线索。字段以实际 JSON 为准，不凭记忆捏造字段。
- 对 untrusted 表格，报告不能可靠提取的页/表。不要将错列数字重新排列成看似可信的结果。
- 原始矩阵与整理表分开留存，清理表格不覆盖原始输出。数字和原文条件之间的归属需要阅读确认。

## 整理表建议

按用户实际问题选择列；发酵数据通常包含来源、文件页序、表号、宿主/菌株、改造、培养条件、指标、原始数值、单位、时间/规模、对照、脚注和复核状态。不要为了填满列而推断缺失值。过程参数表可以使用材料/配方/温度/时长/量测方式等适用字段，不强套生物学列名。
