---
name: literature-research
description: 根据研究问题检索文献、去重核验身份、生成阅读清单和证据卡，并用同义词与种子论文追踪补漏。用于综述前的检索整理或离线复审；不代替人工纳入决定和原文论证核查。
---

# 文献检索与补漏

使用 literature-review-pipeline 的 `litsearch` 和 `litsearch.discovery`。将研究问题转为检索配置、可追溯候选及下一步阅读材料；区别实际检索、离线计划、身份核验和人工审阅。

## 环境与输入

有同目录 `runtime.json` 时读取 `python` 并用作解释器；否则选用能导入 `litsearch` 的 Python 3.10+。先用模块 `--help` 检查入口。缺包时定位项目克隆后安装该项目，不能在无关当前目录执行 `pip install .`。命令中的 python 指选定解释器，以参数列表或正确引号调用。

Windows 中文日志使用 UTF-8：给子进程设置 PYTHONUTF8=1、PYTHONIOENCODING=utf-8；捕获输出时按 UTF-8 解码。

从用户问题和已有材料确定目标对象、研究范围、时间要求、检索源及预算。仅当缺失信息会实质改变检索时澄清；可以直接推断的选择应说明。内置 pantothenate、lysine、glutathione 是课题词表；新课题用项目 profile schema 建立独立配置，不将无关问题强套入泛酸。检索词是普通词或短语，不能在别名字段塞 AND/OR 表达式。相关命令和配置定位见 [运行模式](references/workflows.md)。

## 选择运行模式

- **首次检索**：主 `python -m litsearch` 默认实际联网。明确检索源和数量预算，保存原始记录、去重结果、身份审计、候选表和审阅模板。PubMed/OpenAlex 缺少必要联系方式或凭据时，仅询问缺失配置，不编造；凭据不写入技能或公开仓库。
- **补漏**：`litsearch.discovery` 默认仅离线计划。用户要求实际检索时加 `--online`，选择别名、种子、来源、深度及请求/页/记录预算。用返回状态区分 offline_plan、complete、partial 和 failed；预算触顶、接口不支持和失败路径均保留。
- **离线复审**：主入口用 `--from-json` 读取 Record 格式；不加 `--verify-online`。联网重新核验只有在本次任务要求时启用。离线结果可能因缓存缺失或过期而保持未核验。

每次使用新的输出目录。不要覆盖旧检索、隐藏截断或把退出 0 自动写成全部完成：主程序可能 completed_with_warnings，须查看 run_manifest.json 和 events.jsonl。

## 输出与人工审核

候选表用于阅读，identity_audit 用于书目冲突，evidence_cards 用于定位原文，review_decisions.template 是待填模板。只有与当前课题/记录/证据指纹相符且通过既有门禁的真实审阅决定，才能产生 review_ready 和最终 RIS。不得代造 reviewer、审批理由或证据 ID。

discovery 的 candidates/observations 不是主入口 Record schema，不能将 discovery.json 直接传给 `--from-json`。先交付独立补漏清单和溯源；若本次还需纳入主管线，再按已安装代码的数据模型构造未授信 Record，保留发现来源和冲突，运行身份核验。不能改个字段名便沿用“已核验/已批准”。局部补漏和主检索结果应分别报告，直到完成显式合并和重新核验。

交付候选表与审计文件链接，说明查询范围、实际来源、取得数量、去重情况、未核验/冲突/截断数量及待阅读项。有明确时间范围时核对实际记录年份和来源过滤能力，不能只因查询包含年份便声称全量满足。
