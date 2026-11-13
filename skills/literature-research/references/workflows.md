# 运行模式

`python` 使用 SKILL.md 选定的解释器，文件路径替换为本次实际输入。主包只需基础依赖，运行目录与源码可不同；所有输入和输出建议用绝对路径。程序不自动下载安装外部桌面软件。

## 首次检索

```text
python -m litsearch --topic pantothenate --source europepmc --search-max 100 --out-dir search-01
```

这是联网命令。PubMed 需要 `--email`；OpenAlex 需要 `--email` 和 `--openalex-api-key`，先核对本版本 `--help`。不要在公开日志回显凭据。默认不下载 PDF；本次要求下载时使用 `--download-pdfs --email ... --max-pdfs ...`。本地 PDF 使用 `--pdf-dir`，文件名不能证明身份匹配。

新课题的完整配置以安装包 `litsearch/profiles/*.json` 及 `litsearch.topics.load_topic` 的实际 schema 为准，复制后修改，再用 `--topic topic.json` 验证；不要仅从此示例猜字段。工程主题另有 `litsearch.engineering`，不把 wet_etch 当主入口内置 topic。

## 离线复审

```text
python -m litsearch --topic pantothenate --from-json records.json --cache-dir cache --pdf-dir pdfs --skip-download --skip-pdf-extraction --out-dir review-01
```

输入为 Record 数组或 `{"records":[...]}`。网络复核需要显式 `--verify-online`；正式请求重新核验可加 `--refresh-verification`。已有派生字段不能自动作为权威信任输入。

人工审阅后，在与原记录、配置和证据匹配的重跑中使用 `--review-decisions review_decisions.json`。定量 claim 需保存指标、值、单位、培养条件、对照与定位；不知道的项明确 not_reported，不从摘要猜出。

## 同义词与引文补漏

```text
python -m litsearch.discovery --topic pantothenate --alias "vitamin B5" --out-dir discovery-plan
python -m litsearch.discovery --topic pantothenate --aliases-json aliases.json --seeds-json seeds.json --online --max-requests 20 --max-pages 12 --max-records 300 --max-depth 1 --out-dir discovery-live
```

两条分别表示仅规划和实际联网；不需要先重复执行计划。aliases.json 是普通短语数组。seeds.json 是种子数组，单项用 `{"doi":"10.1000/example"}` 或 `{"source":"MED","id":"123456"}`；这里只说明格式，不将示例标识当真实来源。Crossref 在此工具中不提供向前被引接口；用 Europe PMC 的可用路线，按实际结果说明覆盖范围。

discovery.json 保留 candidates、identifiers、observations 的 payload/route/request_id、conflicts 和 approval；这些字段不等同于已通过身份核验的 Record。保留候选原始溯源，冲突来源不能静默选赢家。

## 结果判断

主入口：0 仍可能有 warnings；2 为错误。看 run_manifest.json 与 events.jsonl，候选输出不等于通过人审。

discovery：0 表示所请求的计划或预算内工作完成，2 表示 partial，1 表示失败。必须从报告判断是否真实联网，不能把 offline_plan 说成“已检索”。

每次输出提供 literature_candidates.csv/.xlsx、identity_audit.csv 及本次相关的证据卡/人工模板；review_ready.json 可能合理为空。不要为填满最终参考文献清单而降低门禁。
