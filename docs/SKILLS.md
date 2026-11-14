# 三个可调用技能

按常用任务提供三个 skill，核心处理代码仍由本仓库的 Python 包维护：

| 技能 | 使用示例 | 实际模块 |
| --- | --- | --- |
| word-zotero-citations | 使用 $word-zotero-citations 将这份 Word 综述转换为动态引用并核对编号 | litsearch_zotero.dynamic_citations / word_plugin |
| literature-research | 使用 $literature-research 检索目标文献并用种子论文补漏 | litsearch / litsearch.discovery |
| paper-data-extraction | 使用 $paper-data-extraction 提取论文表格并保留单位和条件 | litsearch.document_parse |

## 安装

在本仓库克隆根目录，先选择用于执行的 Python 3.10+ 独立环境：

```text
python -m pip install ".[parsing,citations]"
python scripts/install_skills.py
```

安装器默认写入 CODEX_HOME 下的 skills 目录，未设置该变量时使用个人 .codex/skills。它会检查所选技能的基础模块、复制指令与参考文件，并在各安装目录生成仅用于本机的 runtime.json。目标已有同名技能时默认停止，不覆盖个人修改。

只安装检索技能可用 `python scripts/install_skills.py --skill literature-research`，无需为它安装 Word 或 PDF/OCR 的依赖。PDF 表格、OCR 和 Windows 插件依赖只在相应操作需要时检查和安装；安装成功不表示桌面软件和服务已经就绪。

`--update` 只更新带有 managed_files 摘要清单且文件未被本地修改的受管技能。更新保留 `.skill-backup-*` 备份；新增、缺失或改过的个人文件会阻止自动更新。旧版无摘要安装需先人工核对和备份，不盲目覆盖。整批先暂存再发布，复制或发布失败会回滚本轮已发布目录；恢复失败时备份仍保留，需按具体错误处理。

可用 `--python` 指定实际执行环境的解释器，`--destination` 指定技能安装目录。两者都接受带空格的路径，调用时按 shell 规则引用。runtime.json 将这个解释器绑定到技能；跨项目调用时不依赖当前目录刚好是源码目录。移动或删除执行环境后，应重新核对绑定。安装器只复制技能，不自动安装 Zotero、Word、Tesseract 或语言包。

源码 ZIP 和 sdist 包含 skills 及安装器；wheel 提供底层模块，技能安装仍从仓库或源码发行包执行。也可以直接复制各 skills 子目录到使用环境支持的技能目录，再自行选好已安装项目的解释器。新技能是否显示以客户端重新加载后的技能列表为准。

## 使用与边界

明确使用 `$技能名`，也可用与其描述匹配的自然语言请求。技能会读取相关指令，按实际需求调用已有模块；没有新增远端服务，也不要求第三方账号登录才能做离线任务。

- 引文：支持普通 DOI 引用转换、已有动态引文的插入/刷新及离线校验。插件对话框需要实际完成；域内数字闭合与全文引用覆盖分别报告。不同样式的可验证范围见 [动态引文说明](ZOTERO_DYNAMIC_CITATIONS.md)。
- 检索：主入口默认联网，discovery 默认只规划；微生物筛选模型不泛用于其他学科。`python -m litsearch.discovery_bridge` 可将补漏观察转换为未授信 Record，保留来源与冲突，随后仍需核验和人审。见 [管线说明](PIPELINE_GUIDE.md)。
- 提取：数字、空格、合并表头、单位、条件和原文位置均需保留；对目标表和异常区域做视觉核对，混合扫描页或隐藏 HTML 内容不能只靠退出码判断。见 [解析工具说明](ENGINEERING_TOOLS.md)。

## 验证

技能经过结构校验和离线实际调用验证，安装器覆盖独立解释器绑定、既有技能保护和私有文件拒绝复制。CI 在 Windows/Linux、Python 3.10/3.14 从安装 wheel 后的环境重跑测试。合成验证不代表当次已访问真实网络、真实文库或完成了 Word 异步刷新。

个人 runtime.json、研究文稿、PDF、文库快照与验证日志不属于公开技能资源。
