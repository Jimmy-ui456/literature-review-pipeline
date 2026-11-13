# 命令与结果

下列路径是示例输入，使用用户实际文件的绝对路径和新的输出文件名。`python` 替换为 SKILL.md 所述解释器；输出集中到本次研究运行目录，绝不能覆盖原稿。

## 转换与检查

```text
python -m litsearch_zotero.dynamic_citations scan review.docx --out scan.json
python -m litsearch_zotero.dynamic_citations doctor
python -m litsearch_zotero.dynamic_citations snapshot --out library_snapshot.json
python -m litsearch_zotero.dynamic_citations prepare review.docx --snapshot library_snapshot.json --out plan.json
python -m litsearch_zotero.dynamic_citations convert --plan plan.json --out review_zotero.docx --report converted.json
python -m litsearch_zotero.word_plugin refresh --document review_zotero.docx
python -m litsearch_zotero.word_plugin finish --document review_zotero.docx --report saved.json
python -m litsearch_zotero.dynamic_citations verify review_zotero.docx --out checked.json
```

按步骤检查输出后继续；不是无条件批处理。可先完成离线扫描，再在 Zotero 可达时读取快照。已有可信且适用于当前文库的快照可供离线 prepare，但插件刷新仍需桌面软件。

支持引用示例：`[DOI: 10.1000/example]`、`[Author, 2025, DOI: 10.1000/example; DOI: 10.1000/another]`。每个来源须有一个 DOI 并用分号分隔；混入无 DOI 的作者年份项会停止。编号映射文件形如 `{"1":"10.1000/example"}`，传给 scan 和 prepare 的 `--references`。条目重复时，核对候选后用 `--selections` 指定 `{"10.1000/example":"ABCD1234"}`；这些只是格式样例，不能充当真实文献。

prepare 可用 `--style` 指定 Zotero 样式 URI，默认 IEEE。只有 ready=true 才进入 convert。原稿变化会使计划失效，重新扫描和准备；不能手改哈希蒙混通过。`--replace-bibliography` 的使用条件见 SKILL.md。

## 插入与刷新

```text
python -m litsearch_zotero.word_plugin add-citation --document review.docx
python -m litsearch_zotero.word_plugin add-citation --document review.docx --bookmark cite_here
python -m litsearch_zotero.word_plugin refresh --document review.docx
python -m litsearch_zotero.word_plugin finish --document review.docx --report saved.json
```

前两条择一执行。用户未指定书签时使用当前光标，不编造书签位置。`preferences` 更换文档样式，`bibliography` 添加或编辑文末列表；仅在相应任务要求时调用。已有未保存编辑时，先明确其归属并保存，不丢弃。宏动作有独立备份；finish 返回 saved_for_verification，不构成 Zotero 异步完成回执。

## 结果判读

- `scan/prepare/convert/verify` 正常为 0；ready=false、结构失败或无效输入为 2。仍需读取 JSON：结构有效也可能 pending_refresh=true。
- 完整核验看 `numeric_correspondence_verified`；缺 DOI 或使用非数字样式时不能虚报一一对应已证明。
- API 不可达时可以继续离线 scan/verify；停止依赖 API 的步骤。插件、受保护文稿、条目歧义等阻塞应说明具体位置和所缺输入。
