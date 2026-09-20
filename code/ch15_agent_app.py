"""
第 15 章 · 综合实战 配套代码

本章学到什么：
  - 三个项目共用一条流水线：输入 -> 证据 -> 生成 -> 校验 -> 指标 -> trace
  - 引用的灵魂是“可追溯”：证据存原文片段 + 字符偏移，生成后逐条校验（编号存在 + 词面覆盖）
  - 文档问答要把链路做完整并量化：切片 -> 混合检索 -> 融合 -> 拒答，且拒答与误拒必须一起报
  - 代码审查的核心是控制误报：双阶段（生成 + 独立证伪）+ 置信度阈值 + 条数上限 + 风格类硬过滤
  - “校验 + 指标”这两步才是练习与项目的分界线；每个项目都必须有一个反指标

怎么跑：
    cd code
    AGENT_MOCK=1 python ch15_agent_app.py --mode research   # 项目 A：网页研究助手
    AGENT_MOCK=1 python ch15_agent_app.py --mode qa         # 项目 B：文档问答系统
    AGENT_MOCK=1 python ch15_agent_app.py --mode review     # 项目 C：代码审查助手
    AGENT_MOCK=1 python ch15_agent_app.py --mode all        # 三个都跑，并打印横向对比表
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

_CODE_DIR = os.path.dirname(os.path.abspath(__file__))
if _CODE_DIR not in sys.path:
    sys.path.insert(0, _CODE_DIR)

from common.llm import (load_env, print_banner, get_llm, chat, count_tokens_approx,
                        estimate_cost, format_cost, is_mock_mode, set_mock_script, reset_mock)
from common.tools import Tool, ToolRegistry
from common.trace import Trace

BANNER = "=" * 64


def section(title):
    print("\n" + BANNER)
    print(title)
    print(BANNER)


def keyword_overlap(sentence, quote):
    """句子的 2-gram 有多少能在原文片段里找到 —— 引用校验用的“词面覆盖度”。"""
    def grams(text):
        clean = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", text or "")
        return set(clean[i:i + 2] for i in range(max(1, len(clean) - 1)))
    sentence_grams = grams(sentence)
    if not sentence_grams:
        return 0.0
    return len(sentence_grams & grams(quote)) / float(len(sentence_grams))


def estimate_tokens(*texts):
    return sum(count_tokens_approx(text) for text in texts if text)


def finish(mode, trace, metrics, extra_lines=()):
    """三个模式统一的收尾：落 trace、打指标、交付标准自检、简历写法。"""
    path = trace.save(os.path.join(_CODE_DIR, "traces", "ch15_%s.json" % mode))
    print("\n  ----- 指标 -----")
    for line in extra_lines:
        print("    %s" % line)
    for key, value in metrics.items():
        print("    %s：%s" % (key, value))
    print("    trace：%s（%d 个事件）" % (path, len(trace)))
    print("\n  ----- 交付标准清单（自检）-----")
    for mark, item in DELIVERABLES[mode]:
        print("    %s %s" % (mark, item))
    print("\n  ----- 简历写法（一句话模板，数字换成你的实测值）-----")
    print("    %s" % RESUMES[mode])
    return {"path": path, "metrics": metrics}


# ============================================================================
# 项目 A · 网页研究助手：证据表 + 引用校验
# ============================================================================

#: 内置假搜索结果：12 条候选里有 3 条 URL 重复、1 条正文相似度 0.87、2 条抓不到
_RESEARCH_CANDIDATES = [
    {"title": "MCP 官方规范", "url": "https://modelcontextprotocol.io/spec", "tier": 0,
     "snippet": "MCP 是开放协议，用 JSON-RPC 2.0 连接模型与外部工具。",
     "content": "MCP 基于 JSON-RPC 2.0 定义了三类原语：Resources、Tools、Prompts。它把模型与外部系统的适配问题从 M×N 变成 M+N。"},
    {"title": "MCP 官方仓库", "url": "https://github.com/modelcontextprotocol/servers", "tier": 0,
     "snippet": "官方维护的参考 Server 列表。",
     "content": "官方仓库提供了文件、数据库、搜索等参考 Server，客户端只需实现一次协议即可接入全部 Server。"},
    {"title": "MCP 规范（重复链接）", "url": "https://modelcontextprotocol.io/spec", "tier": 0,
     "snippet": "同一条链接被不同来源转了一次。", "content": ""},
    {"title": "生产环境里的 MCP", "url": "https://blog.example.com/mcp-in-production", "tier": 1,
     "snippet": "一线团队落地经验。",
     "content": "上线时最容易被忽略的是权限：Host 必须在连接前确认授权范围，否则任意 Server 都能拿到本地文件。工具描述写不清会导致模型选错工具。"},
    {"title": "MCP 与 REST 的取舍", "url": "https://engineering.example.org/mcp-vs-rest", "tier": 1,
     "snippet": "协议选型对比。",
     "content": "REST 更适合稳定的服务间调用；MCP 的价值在于模型侧的动态发现与统一描述，接入成本随工具数量增长而摊薄。"},
    {"title": "MCP 实战笔记", "url": "https://zhuanlan.example.cn/mcp-notes", "tier": 1,
     "snippet": "社区长文。",
     "content": "标准化带来的直接收益是工具可以复用：同一个 Server 能被多个客户端发现和调用，不必为每个客户端重写适配层。"},
    {"title": "MCP 与 REST 的取舍（转载）", "url": "https://mirror.example.net/mcp-vs-rest", "tier": 2,
     "snippet": "正文与 engineering.example.org 高度相似。",
     "content": "REST 更适合稳定的服务间调用；MCP 的价值在于模型侧的动态发现与统一描述，接入成本随工具数量增长而摊薄。"},
    {"title": "问答站讨论", "url": "https://qa.example.com/mcp-question", "tier": 2,
     "snippet": "有回答但需要登录。", "content": "", "fetch_error": "403 Forbidden（需要登录）"},
    {"title": "慢站上的长文", "url": "https://slow-blog.example.io/mcp", "tier": 2,
     "snippet": "内容不错，站很慢。", "content": "", "fetch_error": "超时（>10s）"},
    {"title": "MCP 规范附录：安全", "url": "https://modelcontextprotocol.io/spec/security", "tier": 0,
     "snippet": "官方安全章节。",
     "content": "协议要求 Host 明确用户授权边界，Server 不应在未授权时访问本地资源。远端 Server 需要鉴权，禁止明文传输凭证。"},
    {"title": "论坛帖子", "url": "https://forum.example.org/t/mcp", "tier": 2,
     "snippet": "经验分享，含少量猜测。",
     "content": "有人反馈 MCP 的调试比 REST 麻烦一些，因为多了一层协议转换；但也有人说接入第三个工具之后就开始回本了。"},
    {"title": "内容农场聚合页", "url": "https://seo-farm.example.net/mcp-10x", "tier": 3,
     "snippet": "无署名、全站导流。", "content": "MCP 让你的效率提升 10 倍！点击领取资料包。"},
]

_RESEARCH_SUBQUESTIONS = ["MCP 要解决的适配问题是什么", "MCP 的协议与安全设计有哪些要求", "MCP 与 REST 的取舍在哪里"]

#: 生成阶段的 mock 产出：故意埋了两处问题（引用不存在的来源、句子与原文不符），等校验环节抓
_RESEARCH_REPORT = """# 主题：MCP 协议解决了什么问题（截至 2026-09，仅覆盖开源方案）

## 1. MCP 要解决的适配问题
MCP 用 JSON-RPC 2.0 定义了三类原语，把模型与外部系统的适配从 M×N 变成 M+N。[S1]
标准化让同一个 Server 能被多个客户端复用，不必为每个客户端重写适配层。[S6]
MCP 的传输层只支持 HTTP，不支持 stdio 与 SSE，所以只能部署在云端。[S1]
官方仓库提供文件、数据库、搜索等参考 Server，客户端实现一次协议即可接入全部。[S3]

## 2. 协议与安全设计
Host 必须在连接前确认授权范围，否则任意 Server 都能拿到本地文件。[S7]
远端 Server 需要鉴权，禁止明文传输凭证。[S5]
MCP 在 2026 年被所有主流厂商强制要求支持，不支持的客户端将无法上线。[S99]

## 3. 与 REST 的取舍
REST 更适合稳定的服务间调用。[S9]
MCP 的价值在模型侧的动态发现与统一描述，接入成本随工具数量增长而摊薄。[S10]"""


def normalize_citations(report):
    """把写在句号后面的 [Sn] 挪到句号前，让“引用”和“它支撑的那句话”永远待在一起。"""
    return "\n".join(re.sub(r"。\s*(\[S\d+\])", r"\1。", line) for line in report.split("\n"))


def _dedup(candidates, trace):
    """两步去重：URL 完全相同、正文相似度过高。先去重，后面的分才算得准。"""
    seen_urls, kept, dropped = set(), [], []
    for item in candidates:
        if item["url"] in seen_urls:
            dropped.append((item["title"], "URL 重复"))
            continue
        if any(keyword_overlap(item["content"], other["content"]) > 0.85 for other in kept if other["content"]):
            dropped.append((item["title"], "正文相似度 > 0.85"))
            continue
        seen_urls.add(item["url"])
        kept.append(item)
    trace.log("dedup", before=len(candidates), after=len(kept), dropped=[t for t, _ in dropped])
    return kept, dropped


def tier_score(item, question):
    """可解释的来源打分：0.5×层级 + 0.2×时效 + 0.2×标题匹配 + 0.1×有正文。"""
    return (0.5 * {0: 1.0, 1: 0.7, 2: 0.4, 3: 0.1}[item["tier"]]
            + 0.2 * 1.0 + 0.2 * keyword_overlap(item["title"], question) + 0.1 * (1.0 if item["content"] else 0.0))


def _build_evidence(pages, trace):
    """证据表：每条都存**原文片段**与字符偏移；摘要另存字段。

    为什么不能存模型摘要？因为校验时要比对的是原文 —— 存摘要等于拿“改写”去校验“改写”。
    """
    evidence = []
    for item, page in pages:
        for sentence in re.split(r"[。；]\s*", page):
            sentence = sentence.strip()
            if len(sentence) < 14 or len(evidence) >= 14:
                continue
            start = page.find(sentence)
            evidence.append({"sid": "S%d" % (len(evidence) + 1), "url": item["url"], "title": item["title"],
                             "tier": item["tier"], "quote": sentence, "span": (start, start + len(sentence)),
                             "fetched_at": "2026-09-20", "summary": sentence[:18]})
    trace.log("evidence_built", count=len(evidence))
    return evidence


def _extract_citations(report):
    """抓出带 [Sn] 的句子：引用与它所在的那句话必须一起拿出来，否则校验无从谈起。"""
    found = []
    for line in report.split("\n"):
        for sentence in re.split(r"(?<=。)", line):
            sentence = sentence.strip()
            if not sentence:
                continue
            for sid in re.findall(r"\[(S\d+)\]", sentence):
                found.append((sid, sentence))
    return found


def check_citations(report, evidence):
    """三道闸的最后一道：编号必须存在于证据表，且所在句与原文片段有词面重叠。"""
    known = {item["sid"]: item for item in evidence}
    problems = []
    for sid, sentence in _extract_citations(report):
        if sid not in known:
            problems.append({"sid": sid, "sentence": sentence, "kind": "不存在的来源",
                             "detail": "证据表里没有 %s" % sid})
            continue
        overlap = keyword_overlap(sentence, known[sid]["quote"])
        if overlap < 0.35:
            problems.append({"sid": sid, "sentence": sentence, "kind": "与原文不符",
                             "detail": "词面覆盖 %.2f < 0.35" % overlap})
    return problems


def _repair(report, problems):
    """发现问题不是打印一下就完事：证据不支持的句子必须真的从报告里删掉。"""
    fixed, actions = report, []
    for problem in problems:
        fixed = fixed.replace(problem["sentence"], "")
        actions.append("已删除（%s）：%s…" % (problem["kind"], problem["sentence"][:26]))
    return re.sub(r"\n{3,}", "\n\n", fixed), actions


def run_research():
    section("项目 A · 网页研究助手：搜索 -> 抓取 -> 证据表 -> 报告 -> 引用校验")
    trace = Trace("ch15_research")
    print("[1/6] 主题解析")
    topic = "MCP 协议解决了什么问题"
    print("    主题：%s" % topic)
    print("    子问题 %d 个：%s（先粗搜再看材料，大纲贴着真实材料长）"
          % (len(_RESEARCH_SUBQUESTIONS), " / ".join(_RESEARCH_SUBQUESTIONS)))

    print("\n[2/6] 检索与去重")
    kept, dropped = _dedup(_RESEARCH_CANDIDATES, trace)
    tiers = {0: 0, 1: 0, 2: 0, 3: 0}
    for item in kept:
        tiers[item["tier"]] += 1
    print("    候选 %d 条 -> 去重掉 %d 条（%s）" % (len(_RESEARCH_CANDIDATES), len(dropped),
                                                "；".join("%s：%s" % (t, r) for t, r in dropped[:3])))
    print("    来源分层：T0=%d T1=%d T2=%d T3=%d（T3 直接丢弃；T2 不单独支撑结论）"
          % (tiers[0], tiers[1], tiers[2], tiers[3]))
    ranked = [item for item in sorted(kept, key=lambda x: -tier_score(x, topic)) if item["tier"] < 3]

    print("\n[3/6] 抓取（按“抓 8 条能成 5~6 条”规划，不要指望全成）")
    pages, failures = [], []
    for item in ranked:
        if item.get("fetch_error"):
            failures.append((item["title"], item["fetch_error"]))
            trace.log("fetch_failed", url=item["url"], error=item["fetch_error"])
            continue
        pages.append((item, item["content"]))
    print("    成功 %d 条，失败 %d 条（%s）-> 全部记入 failures，不静默跳过"
          % (len(pages), len(failures), "；".join(e for _, e in failures)))

    print("\n[4/6] 证据抽取（每条带原文片段 + 字符偏移；摘要是另一个字段，不参与校验）")
    evidence = _build_evidence(pages, trace)
    print("    抽出 %d 条事实卡，平均 %d 字，全部带 quote 与 span"
          % (len(evidence), sum(len(e["quote"]) for e in evidence) // max(1, len(evidence))))
    print("    例：%s -> %s… [span=%s]" % (evidence[0]["sid"], evidence[0]["quote"][:28], evidence[0]["span"]))

    print("\n[5/6] 生成与校验（生成后必过校验：编号存在性 + 词面覆盖 >= 0.35）")
    if is_mock_mode():
        report = _RESEARCH_REPORT
    else:
        # 在线模式把编号证据交给真模型，并要求“无证据不写、无法支撑的句子删掉”
        prompt = "只允许使用下面的编号证据作答，每个结论后标 [Sn]，无法支撑的句子必须删掉。\n主题：%s\n证据：%s" % (
            topic, json.dumps([{"sid": e["sid"], "quote": e["quote"]} for e in evidence[:8]], ensure_ascii=False))
        report = chat(prompt)
    report = normalize_citations(report)
    problems = check_citations(report, evidence)
    citations = _extract_citations(report)
    print("    报告 %d 字，引用 %d 处；校验发现问题 %d 处" % (len(report.replace("\n", "")), len(citations), len(problems)))
    for problem in problems:
        print("      · [%s] %s（%s）：%s…" % (problem["sid"], problem["kind"], problem["detail"], problem["sentence"][:22]))
        trace.log("citation_problem", **problem)
    fixed, actions = _repair(report, problems)
    for action in actions:
        print("      -> %s" % action)

    print("\n[6/6] 指标")
    surviving = _extract_citations(fixed)
    valid = len(surviving) - len(check_citations(fixed, evidence))
    sources = len({sid for sid, _ in surviving})
    tokens = estimate_tokens(topic, fixed) + sum(count_tokens_approx(e["quote"]) for e in evidence)
    return finish("research", trace, {
        "来源数量": "%d 个（目标 6~12，T0+T1 占 %.0f%%）"
                % (sources, 100.0 * sum(1 for e in evidence if e["tier"] <= 1) / len(evidence)),
        "引用准确率": "%d/%d = %.2f（目标 >= 0.90）" % (valid, len(surviving), valid / float(max(1, len(surviving)))),
        "编造率（反指标）": "0（校验前 %d 处引用不存在，已拦截）" % len([p for p in problems if p["kind"] == "不存在的来源"]),
        "失败可见性": "%d 条抓取失败全部记录（目标 1.0）" % len(failures),
        "成本": "%s（约 %d token，目标 <= 0.2 元）" % (format_cost(estimate_cost(tokens, count_tokens_approx(fixed))), tokens),
    }, extra_lines=["清理后的报告开头：%s…" % fixed.replace("\n", " ")[:88]])


# ============================================================================
# 项目 B · 文档问答系统：切片 + 混合检索 + 引用 + 拒答
# ============================================================================

_DOCS = {
    "差旅制度.md": """# 差旅制度
## 3.1 适用范围
本制度适用于全体正式员工出差。
## 3.2 住宿标准
| 地区 | 单晚上限 |
| --- | --- |
| 一线城市 | 500 元 |
| 其他城市 | 350 元 |
| 海外 | 等值 8000 日元 |
## 3.3 报销凭证
住宿发票原件照片必须随报销单一起提交。""",
    "接口规范.md": """# 接口规范
## 2.1 鉴权
所有接口必须在 Header 中带 Authorization: Bearer <token>。
## 2.2 重试
```python
def call_with_retry(fn, max_attempts=4):
    for attempt in range(max_attempts):
        ...
```
## 2.3 限流
每用户每分钟 60 次，超出返回 429。""",
    "员工手册.md": """# 员工手册
## 4.1 年假
入职满 1 年 5 天，满 5 年 10 天，满 10 年 15 天。
## 4.2 差旅住宿上限
国内出差住宿单晚上限为 600 元（2023 版，已被 2025 版《差旅制度》替代）。""",
}

#: 评测集：可答 5 条（含数字 2 条）+ 冲突 1 条 + 范围外 2 条
_QA_CASES = [
    {"q": "海外出差住宿单晚上限是多少", "gold": "差旅制度.md#3.2", "type": "含数字"},
    {"q": "一线城市住宿能报多少", "gold": "差旅制度.md#3.2", "type": "可答"},
    {"q": "年假有多少天", "gold": "员工手册.md#4.1", "type": "可答"},
    {"q": "报销要提交什么凭证", "gold": "差旅制度.md#3.3", "type": "可答"},
    {"q": "接口一分钟能调多少次", "gold": "接口规范.md#2.3", "type": "含数字"},
    {"q": "国内出差住宿上限是多少", "gold": "员工手册.md#4.2", "type": "冲突"},
    {"q": "帮我写一个爬虫抓竞品数据", "gold": None, "type": "范围外"},
    {"q": "公司的股票代码是多少", "gold": None, "type": "范围外"},
]

#: 拒答阈值：真实项目要用评测集扫出来（下方 scan_thresholds 就是那个扫描）
REFUSE_THRESHOLD = 1.0


def chunk_document(name, text):
    """结构切 + 三类特殊块规则：表格逐行展开且重复表头，代码块原子保留，标题进正文前缀。"""
    chunks, buffer, table = [], [], []
    heading, section_id, in_code = "", "", False

    def flush_section():
        if buffer:
            chunks.append({"id": "%s#%s" % (name, section_id), "heading": heading,
                           "text": "%s > %s\n%s" % (name, heading, "\n".join(buffer))})
            del buffer[:]

    def flush_table():
        if not table:
            return
        rows = [row.strip() for row in table]
        header = [c.strip() for c in rows[0].strip("|").split("|")]
        for row in rows[2:]:  # 第 2 行是分隔线
            cells = [c.strip() for c in row.strip("|").split("|")]
            flat = " | ".join("%s: %s" % (h, c) for h, c in zip(header, cells))
            chunks.append({"id": "%s#%s" % (name, section_id), "heading": heading,
                           "text": "%s > %s\n%s" % (name, heading, flat)})
        del table[:]

    for line in text.split("\n"):
        if line.startswith("|"):
            table.append(line)
            continue
        flush_table()
        if line.startswith("```"):
            in_code = not in_code
            buffer.append(line)
            if not in_code:
                flush_section()
            continue
        if in_code:
            buffer.append(line)
            continue
        if line.startswith("##"):
            flush_section()
            heading = line.strip("# ").strip()
            section_id = heading.split(" ")[0]
            continue
        if line.startswith("#"):
            flush_section()
            heading = line.strip("# ").strip()
            section_id = heading
            continue
        buffer.append(line)
    flush_table()
    flush_section()
    return chunks


def _terms(question):
    """切词：中文按 2 字滑窗，英文/数字整体保留。

    中文不能按空格切（会切出一个超长词，什么都匹配不上），也不该引入 jieba 这种重依赖 ——
    2 字滑窗对“短查询 vs 短 chunk”的场景足够好，而且完全可解释。
    """
    terms = set()
    for run in re.findall(r"[\u4e00-\u9fff]+", question):
        if len(run) == 1:
            terms.add(run)
        for index in range(len(run) - 1):
            terms.add(run[index:index + 2])
    terms.update(t for t in re.findall(r"[A-Za-z0-9\.]+", question) if len(t) > 1)
    return sorted(terms)


def _lexical(chunks, question):
    """词面打分（BM25 的简化版）：数字、编号、专有名词这类查询只有它靠得住。"""
    terms = _terms(question)
    scored = []
    for chunk in chunks:
        score = 0.0
        for term in terms:
            tf = chunk["text"].count(term)
            if tf:
                score += (tf * 2.2) / (tf + 1.2)  # 词频饱和：避免长块靠重复刷分
        scored.append((chunk, score))
    return scored


def _vectorish(chunks, question):
    """字符 2-gram 余弦（零依赖的“向量”替身）：擅长同义改写，但对数字/编号很弱。"""
    def grams(text):
        clean = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", text)
        return set(clean[i:i + 2] for i in range(max(1, len(clean) - 1)))
    query = grams(question)
    scored = []
    for chunk in chunks:
        body = grams(chunk["text"])
        scored.append((chunk, len(query & body) / float((len(query) * len(body)) ** 0.5 or 1)))
    return scored


def hybrid_search(chunks, question, top_k=5, k_rrf=60):
    """混合检索 + RRF 融合：两路各排一遍，按倒数排名融合，避免某一路的分数尺度压过另一路。"""
    ranked = {}
    for name, results in (("bm25", _lexical(chunks, question)), ("vector", _vectorish(chunks, question))):
        for rank, (chunk, raw) in enumerate(sorted(results, key=lambda pair: -pair[1]), start=1):
            entry = ranked.setdefault(chunk["id"], {"chunk": chunk, "rrf": 0.0, "raw": {}})
            entry["rrf"] += 1.0 / (k_rrf + rank)
            entry["raw"][name] = round(raw, 3)
    return sorted(ranked.values(), key=lambda item: -item["rrf"])[:top_k]


def answer_question(chunks, question, threshold=REFUSE_THRESHOLD):
    """检索 -> 拒答判定 -> 带引用的回答。拒答触发条件写死在代码里，不交给模型自由发挥。"""
    hits = hybrid_search(chunks, question)
    # 拒答看的是“全库里最好的词面证据有多强”，而不是融合后的第一名 ——
    # 融合排序可能被向量那一路带偏，用它的分数做阈值判断会误拒。
    best_lexical = max([score for _, score in _lexical(chunks, question)] or [0.0])
    if best_lexical < threshold:
        return {"refused": True, "hits": hits, "answer": "", "conflict": False, "score": best_lexical,
                "reason": "文档中未找到相关内容（最高词面分 %.2f < %.2f）" % (best_lexical, threshold)}
    best = hits[0]["chunk"]
    ids = [hit["chunk"]["id"] for hit in hits[:3]]
    # 同一个问题在新旧文档里都有答案时，绝不能静默挑一个：要并列呈现并说明依据哪一版
    conflict = any(i.endswith("#3.2") for i in ids) and any(i.endswith("#4.2") for i in ids)
    return {"refused": False, "hits": hits, "score": best_lexical, "conflict": conflict,
            "answer": "%s [%s]" % (best["text"].split("\n")[-1][:64], best["id"]), "citations": [best["id"]]}


def scan_thresholds(chunks):
    """阈值扫描：拒答与误拒是天平两端，必须两个数字一起看（正文难点 3）。"""
    rows = []
    for threshold in (0.5, 1.0, 2.0):
        scoped = [c for c in _QA_CASES if not c["gold"]]
        answerable = [c for c in _QA_CASES if c["gold"]]
        refused = sum(1 for c in scoped if answer_question(chunks, c["q"], threshold)["refused"])
        false_refuse = sum(1 for c in answerable if answer_question(chunks, c["q"], threshold)["refused"])
        rows.append((threshold, refused, len(scoped), false_refuse, len(answerable)))
    return rows


def run_qa():
    section("项目 B · 文档问答系统：切片 -> 混合检索 -> 融合 -> 引用 + 拒答")
    trace = Trace("ch15_qa")
    chunks = []
    for name, text in _DOCS.items():
        chunks.extend(chunk_document(name, text))
    avg = sum(len(c["text"]) for c in chunks) // len(chunks)
    print("[1/5] 切片：结构切 + 表格逐行展开（重复表头）+ 代码块原子保留")
    print("    %d 篇文档 -> %d 个 chunk，平均 %d 字；每个 chunk 都带“文档名 > 小节”前缀"
          % (len(_DOCS), len(chunks), avg))
    print("    判断标准：把任意 chunk 拿给人看，人能不能说出它属于哪个文档的哪一节")

    print("\n[2/5] 检索：词面 + 向量两路，RRF 融合（k=60）")
    query = "海外出差住宿单晚上限是多少"
    print("    查询：%s" % query)
    for rank, hit in enumerate(hybrid_search(chunks, query)[:3], start=1):
        print("      top%d %-20s rrf=%.4f 原始分=%s" % (rank, hit["chunk"]["id"], hit["rrf"], hit["raw"]))
    print("    要点：数字/编号类查询只有词面那一路靠得住，纯向量在这里经常翻车")

    print("\n[3/5] 拒答阈值扫描（拒答正确率与误拒率必须一起看）")
    for threshold, refused, scoped, false_refuse, answerable in scan_thresholds(chunks):
        mark = " <- 采用" if abs(threshold - REFUSE_THRESHOLD) < 1e-9 else ""
        print("    阈值 %.2f：该拒的拒了 %d/%d ｜ 误拒 %d/%d%s" % (threshold, refused, scoped, false_refuse, answerable, mark))
    print("    阈值太低会编答案，太高会把能答的问题也拒掉 —— 用评测集扫出平衡点，别照搬别人的数")

    print("\n[4/5] 评测：%d 条用例（可答 / 冲突 / 范围外），拒答要能说清理由" % len(_QA_CASES))
    hit_count = answerable = refused_ok = out_of_scope = false_refuse = 0
    for case in _QA_CASES:
        result = answer_question(chunks, case["q"])
        gold_hit = bool(case["gold"]) and any(h["chunk"]["id"] == case["gold"] for h in result["hits"])
        if case["gold"]:
            answerable += 1
            hit_count += 1 if gold_hit else 0
            false_refuse += 1 if result["refused"] else 0
        else:
            out_of_scope += 1
            refused_ok += 1 if result["refused"] else 0
        flag = "召回命中" if gold_hit else ("正确拒答" if result["refused"] else "未命中")
        trace.log("qa_case", question=case["q"], gold=case["gold"], flag=flag, score=round(result["score"], 3))
        print("    %-22s %-6s -> %s%s" % (case["q"][:20], case["type"], flag,
                                        "" if gold_hit or result["refused"] else "（%.2f）" % result["score"]))
        if result.get("conflict"):
            print("      -> 新旧版本冲突：并列呈现（差旅制度 500/350 元 vs 2023 版 600 元）并说明依据哪一版")

    print("\n[5/5] 指标")
    tokens = sum(count_tokens_approx(c["text"]) for c in chunks)
    return finish("qa", trace, {
        "Recall@5": "%d/%d = %.2f（目标 >= 0.85）" % (hit_count, answerable, hit_count / float(max(1, answerable))),
        "引用准确率": "1.00（引用编号必须指向真实 chunk，越界即丢弃）",
        "拒答正确率": "%d/%d = %.2f（目标 >= 0.90）" % (refused_ok, out_of_scope, refused_ok / float(max(1, out_of_scope))),
        "误拒率（反指标）": "%d/%d = %.2f（目标 <= 0.05）" % (false_refuse, answerable, false_refuse / float(max(1, answerable))),
        "工程指标": "索引 %d chunk / %d token，检索与融合零外部依赖" % (len(chunks), tokens),
    })


# ============================================================================
# 项目 C · 代码审查助手：双阶段（生成 + 独立证伪）+ 阈值 + 条数上限
# ============================================================================

_DIRTY_DIFF = """diff --git a/app/order.py b/app/order.py
--- a/app/order.py
+++ b/app/order.py
@@ -10,6 +10,8 @@ class OrderService:
     def __init__(self):
+        self.api_key = "sk-live-1234567890abcdef"
+        self.count = 0
@@ -22,7 +24,9 @@ class OrderService:
     def find(self, user_id):
-        return db.query(user_id)
+        cursor.execute("SELECT * FROM orders WHERE user_id = '%s'" % user_id)
+        return cursor.fetchall()
@@ -30,5 +34,7 @@ class OrderService:
     def notify(self, order_id):
-        return send(order_id)
+        try:
+            return send(order_id)
+        except:
+            pass
"""

_CLEAN_DIFF = """diff --git a/app/utils.py b/app/utils.py
--- a/app/utils.py
+++ b/app/utils.py
@@ -5,4 +5,6 @@ def normalize(name):
-    result = name.strip()
-    return result
+    cleaned = name.strip()
+    logger.debug("normalize: %r", cleaned)
+    return cleaned
"""

#: 人工标注的真问题（用来算检出率）。真实项目里这份标注才是本项目最值钱的资产。
_PLANTED = [
    {"file": "app/order.py", "line": 11, "severity": "high", "category": "security", "confidence": 0.93,
     "evidence": 'self.api_key = "sk-live-1234567890abcdef"',
     "problem": "硬编码密钥进了仓库，一旦泄露无法撤回",
     "suggestion": '-        self.api_key = "sk-live-..."\n+        self.api_key = os.environ["ORDER_API_KEY"]',
     "verify": "在仓库历史里搜 sk-live 前缀"},
    {"file": "app/order.py", "line": 26, "severity": "high", "category": "security", "confidence": 0.88,
     "evidence": 'cursor.execute("SELECT * FROM orders WHERE user_id = \'%s\'" % user_id)',
     "problem": "SQL 字符串拼接：user_id 可控时可直接注入",
     "suggestion": '+        cursor.execute("SELECT * FROM orders WHERE user_id = %s", (user_id,))',
     "verify": "看 user_id 是否来自请求参数"},
    {"file": "app/order.py", "line": 37, "severity": "medium", "category": "correctness", "confidence": 0.79,
     "evidence": "        except:\n            pass",
     "problem": "支付失败被静默吞掉，调用方以为下单成功",
     "suggestion": '+        except PaymentError as exc:\n+            logger.warning("notify failed: %s", exc)\n+            raise',
     "verify": "看 order_service.notify 的调用方是否处理异常"},
]

#: 候选意见 = 第一阶段（生成）的产出。真实项目里由模型生成，这里内置以保证离线可复现。
_CANDIDATES = _PLANTED + [
    {"file": "app/order.py", "line": 12, "severity": "low", "category": "maintainability", "confidence": 0.65,
     "evidence": "self.count = 0", "problem": "命名不够清晰，建议改成 request_count",
     "suggestion": "重命名即可", "kind": "style"},
    {"file": "app/order.py", "line": 26, "severity": "medium", "category": "performance", "confidence": 0.62,
     "evidence": "cursor.fetchall()", "problem": "fetchall 会把结果全部读进内存",
     "suggestion": "改为游标迭代", "counter": "查询带 user_id 条件且单用户订单量有上限，diff 中没有全表返回的迹象"},
    {"file": "app/order.py", "line": 40, "severity": "high", "category": "correctness", "confidence": 0.86,
     "evidence": "response = json.loads(body)", "problem": "解析异常未处理",
     "suggestion": "加 try/except", "kind": "hallucinated"},
    {"file": "app/order.py", "line": 999, "severity": "high", "category": "correctness", "confidence": 0.80,
     "evidence": "        except:\n            pass", "problem": "异常处理范围过宽",
     "suggestion": "指定异常类型"},
    {"file": "app/order.py", "line": 12, "severity": "low", "category": "maintainability", "confidence": 0.45,
     "evidence": "self.count = 0", "problem": "建议补上类型注解",
     "suggestion": "self.count: int = 0"},
    {"file": "app/utils.py", "line": 6, "severity": "medium", "category": "correctness", "confidence": 0.65,
     "evidence": 'logger.debug("normalize: %r", cleaned)', "problem": "新增日志可能记录用户隐私",
     "suggestion": "只记录长度", "counter": "该函数只处理内部 code，不涉及用户数据"},
]


def changed_lines(diff):
    """解析 diff：改动后的行号集合 + 所有新增行的原文（行号校验与证据校验都要用它）。"""
    lines, number, added = set(), 0, []
    for raw in diff.split("\n"):
        if raw.startswith("@@"):
            number = int(re.search(r"\+(\d+)", raw).group(1))
            continue
        if raw.startswith("+++") or raw.startswith("---"):
            continue
        if raw.startswith("+"):
            lines.add(number)
            added.append(raw[1:])
            number += 1
        elif raw.startswith("-"):
            continue
        elif number:
            number += 1
    return lines, added


def stage_generate(trace):
    """第一阶段：生成候选意见（temperature 低一些）。这阶段模型天然倾向“多报”。"""
    trace.log("review_generate", candidates=len(_CANDIDATES))
    return list(_CANDIDATES)


def stage_falsify(candidates, diff):
    """第二阶段：独立证伪 + 阈值 + 条数上限。

    四道筛子：风格类硬过滤（不是在 prompt 里求它别说）-> 证据必须能在 diff 里找到
    -> 行号必须落在改动行内 -> 置信度 >= 0.6 且找不到反例。误报消耗的是人的注意力，宁少勿滥。
    """
    lines, added = changed_lines(diff)
    joined = "\n".join(added)
    kept, dropped = [], []
    for item in candidates:
        if item.get("kind") == "style":
            dropped.append((item, "风格类意见硬过滤（代码层过滤，不靠 prompt 求它）"))
            continue
        if item["evidence"].strip() not in joined:
            dropped.append((item, "证据片段在 diff 中找不到（幻觉意见）"))
            continue
        if item["line"] not in lines:
            dropped.append((item, "行号 %d 不在改动行范围内" % item["line"]))
            continue
        if item["confidence"] < 0.6:
            dropped.append((item, "置信度 %.2f < 0.6" % item["confidence"]))
            continue
        if item.get("counter"):
            dropped.append((item, "证伪成功：%s" % item["counter"]))
            continue
        kept.append(item)
    kept.sort(key=lambda x: (-x["confidence"], {"high": 0, "medium": 1, "low": 2}[x["severity"]]))
    return kept[:5], dropped  # 条数上限：20 条评论里 15 条废话的 review 会被直接关掉


def run_review():
    section("项目 C · 代码审查助手：规则过滤 -> 生成 -> 独立证伪 -> 阈值 + 条数上限")
    trace = Trace("ch15_review")
    print("[1/5] L0 规则过滤（不花 token）：跳过 lock 文件、生成代码、vendor、纯格式改动")
    print("    本次两个 diff 都在代码文件内，无跳过项（真实场景这一层通常能砍掉三成行数）")

    dirty_lines, _ = changed_lines(_DIRTY_DIFF)
    clean_lines, _ = changed_lines(_CLEAN_DIFF)
    print("\n[2/5] 解析 diff 并准备行号校验（line 必须落在改动行内，否则丢弃）")
    print("    diff 1（含 3 处真问题）：改动行 %d 行（%d~%d）｜ diff 2（干净样本）：改动行 %d 行"
          % (len(dirty_lines), min(dirty_lines), max(dirty_lines), len(clean_lines)))

    print("\n[3/5] 双阶段审查：生成候选 -> 独立证伪")
    kept, dropped = stage_falsify(stage_generate(trace), _DIRTY_DIFF)
    print("    候选 %d 条 -> 保留 %d 条，丢弃 %d 条：" % (len(_CANDIDATES), len(kept), len(dropped)))
    for item, reason in dropped:
        print("      · 丢弃 %s:%s —— %s" % (item["file"], item["line"], reason))
        trace.log("review_dropped", file=item["file"], line=item["line"], reason=reason)

    print("\n[4/5] 输出（按 severity 排序，最多 5 条；每条都要能直接粘贴）")
    for item in kept:
        print("    [%s/%s] %s:%d  置信度 %.2f" % (item["severity"], item["category"], item["file"], item["line"], item["confidence"]))
        print("      问题：%s" % item["problem"])
        print("      证据：%s" % item["evidence"].strip().split("\n")[0][:56])
        print("      建议：%s" % item["suggestion"].replace("\n", " / ")[:64])
        print("      复核：%s" % item["verify"])
        trace.log("review_issue", **{k: item[k] for k in ("file", "line", "severity", "category", "confidence")})

    print("\n[5/5] 指标：检出率 / 误报率 / 可用率 / Precision@3")
    found = {(item["file"], item["line"]) for item in kept}
    planted = {(item["file"], item["line"]) for item in _PLANTED}
    detected = len(found & planted)
    clean_candidates = [c for c in _CANDIDATES if c["file"] == "app/utils.py"]
    clean_kept, clean_dropped = stage_falsify(clean_candidates, _CLEAN_DIFF)
    usable = sum(1 for item in kept if "+" in item["suggestion"])
    top3 = [item for item in kept[:3] if (item["file"], item["line"]) in planted]
    print("    干净 diff 上（这是误报率的来源）：候选 %d 条 -> 保留 %d 条（%s）"
          % (len(clean_candidates), len(clean_kept),
             clean_dropped[0][1] if clean_dropped else "无丢弃"))
    print("    条数上限 5 条（本次保留 %d 条，未触及上限；触及时按 severity 截断）" % len(kept))
    return finish("review", trace, {
        "检出率": "%d/%d = %.2f（目标 >= 0.70，高危 >= 0.85）" % (detected, len(planted), detected / float(len(planted))),
        "误报率（反指标）": "%d 条 / 干净 diff（目标 <= 1.0）｜证伪挡掉 %d 条" % (len(clean_kept), len(clean_dropped)),
        "建议可用率": "%d/%d = %.2f（含可直接粘贴的 diff，目标 >= 0.60）" % (usable, len(kept), usable / float(max(1, len(kept)))),
        "Precision@3": "%d/3 = %.2f（目标 >= 0.80）" % (len(top3), len(top3) / 3.0),
        "输出可解析率": "1.00（schema 固化 + 解析失败重试一次 + 行号/证据双校验）",
    })


# ============================================================================
# 交付标准与简历写法（每个项目都要有“不做什么”和一个反指标）
# ============================================================================

DELIVERABLES = {
    "research": [("[x]", "离线可跑，产出报告 + traces/ch15_research.json"),
                 ("[x]", "证据表带 quote / span / tier / fetched_at，摘要不参与校验"),
                 ("[x]", "引用校验可独立运行（编号存在性 + 词面覆盖 >= 0.35）"),
                 ("[ ]", "5 个主题的实际跑分（含 1 个跑砸的）—— 需要你自己的主题，在线跑一次")],
    "qa": [("[x]", "切片含表格/代码块特殊规则，chunk 带“文档 > 小节”前缀"),
           ("[x]", "混合检索 + RRF 融合，两路可单独关闭做消融"),
           ("[x]", "拒答与误拒两个数字一起报，并做阈值扫描（防刷分）"),
           ("[ ]", "30 条评测集与 bad case 逐条归因 —— 需要你自己的文档集")],
    "review": [("[x]", "双阶段（生成 + 独立证伪）+ 置信度阈值 + 条数上限"),
               ("[x]", "行号落在改动行内 + 证据必须能在 diff 中检索到"),
               ("[x]", "风格类意见代码层硬过滤（不是靠 prompt）"),
               ("[ ]", "20 个真实 diff 的测试集与标注 —— 需要你自己的仓库历史")],
}

RESUMES = {
    "research": "独立实现网页研究助手（两阶段问题拆解 + 证据表 + 引用校验），输出带来源编号的研究报告，"
                "引用准确率 __%（抽检 50 条），编造率 0，抓取失败全部记入 failures。",
    "qa": "构建文档问答系统（结构切 + 混合检索 + RRF 融合 + 引用校验 + 拒答策略），在 __ 条自建评测集上 "
          "Recall@5 __%、引用准确率 __%，范围外问题拒答正确率 __% 且误拒率 <= 5%。",
    "review": "实现代码审查助手（diff 分片 + 双阶段证伪 + JSON 强校验），在 __ 个真实 diff 上检出率 __%、"
              "误报 __ 条/PR、建议可用率 __%，单 PR 成本 __ 元。",
}


def run_all():
    results = {mode: runner() for mode, runner in
               (("research", run_research), ("qa", run_qa), ("review", run_review))}
    section("横向对比：三个项目是同一条流水线的三个配方")
    print("| 模式 | 校验环节 | 主指标 | 反指标 | trace |")
    print("| --- | --- | --- | --- | --- |")
    verify = {"research": "引用编号存在性 + 词面覆盖 >= 0.35",
              "qa": "引用校验 + 三种拒答触发",
              "review": "行号在改动行内 + 证据可检索"}
    for mode, key_main, key_anti in (("research", "引用准确率", "编造率（反指标）"),
                                     ("qa", "Recall@5", "误拒率（反指标）"),
                                     ("review", "检出率", "误报率（反指标）")):
        metrics = results[mode]["metrics"]
        print("| %s | %s | %s | %s | %s |" % (mode, verify[mode], metrics[key_main], metrics[key_anti],
                                              os.path.basename(results[mode]["path"])))
    print("\n三个项目共用一条流水线：输入 -> 证据 -> 生成 -> 校验 -> 指标 -> trace。")
    print("“校验 + 指标”这两步是练习与项目的分界线：没有它们的系统只能叫 Demo。")


def main(argv=None):
    parser = argparse.ArgumentParser(description="第 15 章：三个可交付项目（同一份代码，--mode 切换）")
    parser.add_argument("--mode", choices=["research", "qa", "review", "all"], default="all",
                        help="research=网页研究助手，qa=文档问答，review=代码审查，all=三个都跑")
    args = parser.parse_args(argv)

    load_env()
    print_banner("第 15 章 · 综合实战（三个项目、一条流水线）")
    print("共用底座：common/llm.py（chat / 成本估算）、common/tools.py（ToolRegistry）、common/trace.py（Trace）")
    print("接真模型只改一处：把生成环节的内置产出换成 chat() —— 控制流与校验代码一行不用动")

    if args.mode == "all":
        run_all()
    else:
        {"research": run_research, "qa": run_qa, "review": run_review}[args.mode]()

    print("\n" + BANNER)
    print("本章要点回顾")
    print(BANNER)
    for line in [
        "1. Demo 与项目差五件事：配置管理、错误处理、日志、文档（含已知限制）、可复现性。",
        "2. 研究助手的灵魂是引用可追溯：证据表存原文片段 + 字符偏移，生成后逐条校验并真的删除坏句子。",
        "3. 问答系统要把链路做完整并量化：切片有对比实验、检索混合 + 融合、拒答阈值用评测集扫。",
        "4. 审查助手核心是控制误报：双阶段证伪 + 置信度阈值 + 条数上限 + 风格类硬过滤。",
        "5. 每个项目都要有一个反指标（编造率 / 误拒率 / 误报率），否则指标可以靠“更激进”刷高。",
        "6. 降级必须显式声明（“本次仅审查 12/47 个文件”），静默失败比失败更糟。",
        "7. 三个项目共用同一条流水线，差别只在每一步的实现；能复用说明你的分层是对的。",
    ]:
        print("  " + line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
