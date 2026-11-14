# 三个技能的比较、对抗性审查与优化

## 判断基础

技能的价值在于：将用户目标转成适配的操作，保存过程中不能丢失的信息，用可观察结果证明做到哪一步，并尽量减少无关依赖和副作用。命令退出成功、文献身份核验通过、数据读对和科学论点成立是不同层次的证据。

本次实际阅读项目技能与代码、已安装 Zotero/PDF/Documents/Spreadsheets 技能，以及 K-Dense 当前主仓库的 literature-review v1.11、citation-management v2.3 和 research-lookup v1.7。比较是工作流和接口审查，并做合成对抗性复现；没有以同题人工金标准完整运行所有对照技能，不能据此声称本项目召回率、准确率或耗时优于它们。

| 任务 | 本项目的优势 | 相对代价或不足 | 本次优化 |
| --- | --- | --- | --- |
| Word 动态引文：文献身份→动态域→实际插件执行→编号对应 | 比文库/BibTeX 技能更聚焦原生 Word 域、副本保护与异步状态，生成可检查报告 | Windows/Word/插件依赖较重；自动转换依赖明确 DOI 或编号映射，范围小于原生插件的条目和样式支持 | 异常域阻断，域内对应与全文覆盖分开报告，披露静态编号候选和附属部件；非 DOI 条目走插件搜索，单条插入不强加文末列表 |
| 检索整理：问题范围→来源覆盖→书目身份→证据→人工纳入 | 原始结果、预算、失败、更新核查、证据与人工决定分层，便于追查 | 比完整综述技能的跨学科发现、方法学、综合和文档输出更窄；原先补漏到主管线依赖手工映射 | 明确领域适配，加入轻量检索记录、种子查漏与覆盖缺口；新增离线确定性桥接并保留原观察和冲突 |
| 表格提取：看到目标→读对单元格→归属表头/单位/条件→回到原文 | 原矩阵、合并跨度、坐标、来源摘要与待审状态更结构化 | 比通用 PDF 技能缺少目标视觉检查；不负责 Word 版式或 Excel 公式验证；OCR/CSS 不能自动保证语义正确 | 对目标和异常区域核图，混合扫描页有依据地回退 OCR；明确 HTML 可见性、原始字符串和单元级定位 |

## 已复现的问题与处理

1. **有效子集掩盖未知引文域（代码缺陷）**：已有可闭合引文和文末列表时，额外平衡但未知/截断的 Zotero 域可能被忽略。已加入识别错误和回归测试，不能再宣称该集合完整对应。
2. **域内对应被扩大为整稿无漏引（说明缺口）**：静态 `[99]`、页眉页脚、脚尾注和批注并不自动在主文档映射验证内。现报告 verification_scope、candidate_static_numbers、auxiliary_citation_parts、coverage_warnings 与 whole_document_coverage_verified。普通数字候选不直接判成错误引文；数字映射字段保持原有域内含义。
3. **批量安装失败留下部分结果（代码缺陷）**：第二项复制失败时第一项曾已发布。现整批先暂存，发布失败回滚；受管更新保存备份，对本地修改和额外个人文件停止更新。
4. **技能安装耦合无关依赖（使用缺口）**：现在可选择单个技能，仅检查基础模块；检索不必先有 Word/OCR，HTML 提取也不要求扫描识别依赖。实际操作仍分别检查所需组件。
5. **补漏观察不能直接用作主 Record（工作流断点）**：新增 discovery_bridge。导出记录清空信任，保留源 SHA、完整观察、来源请求和覆盖/截断信息。显式或独立计算存在冲突时整项待核，不送入可能自动去重的主管线。
6. **混合页漏表（底层启发式边界）**：合成页含图片表及 144 字符原生标题/页脚，auto OCR 因整页字符阈值而不执行，JSON 可为提取完成但 0 表。目标图像已核对。技能现要求按目标查看并有依据地改用 always；没有更改解析器自动阈值，也不声称已解决所有混合页。
7. **HTML 隐藏文字混入数值（底层模型边界）**：含 CSS 隐藏 999 和可见 0.04* 的单元格会得到 DOM 文本 9990.04*。技能现核对可见表和 raw_html，保留歧义，不将 DOM 拼接当可见测量值。解析器仍不执行完整浏览器 CSS。
8. **学科范围与配置验证（路由缺口）**：主筛选模型含产物、宿主、生产关系和培养条件；新增词表不能证明适用于任意学科。现明确适配路线，并使用离线 load_topic 校验，避免默认联网主命令被误用为纯配置检查。

## 验证方式与限制

- 先复现失败，再用针对性测试验证引文错误、安装中断/发布失败、本地修改保护、无信任继承、冲突扣留和主 CLI 导入。
- 运行完整 unittest、官方技能结构校验、白名单与敏感模式发布检查、wheel/sdist 构建及工作区外安装验证；最终跨平台结果以对应 GitHub Actions 提交为准。
- 对混合 PDF 与隐藏 HTML 用实际合成输入做前向调用，记录是否查看目标、保留原始字符串及未完成步骤。此验证不等于真实文献语义金标准，也不把测试截图当研究数据。
- 未改变 Zotero/Word/Tesseract 的外部前提，没有替用户自动批准文献、推断因果或宣称检索穷尽。

## 对照来源

- [Zotero Word 插件使用说明](https://www.zotero.org/support/word_processor_plugin_usage)
- [K-Dense citation-management](https://github.com/K-Dense-AI/scientific-agent-skills/blob/main/skills/citation-management/SKILL.md)
- [K-Dense literature-review](https://github.com/K-Dense-AI/scientific-agent-skills/blob/main/skills/literature-review/SKILL.md)
- [K-Dense research-lookup](https://github.com/K-Dense-AI/scientific-agent-skills/blob/main/skills/research-lookup/SKILL.md)
- [OpenAI：技能行为评估](https://developers.openai.com/blog/eval-skills)

这些来源用于比较流程设计，未复制其技能或脚本；本机技能和运行日志不包含在公开报告中。
