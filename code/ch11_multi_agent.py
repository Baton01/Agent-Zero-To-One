"""
第 11 章 · 多智能体协作 配套代码

本章学到什么：
  - 先泼冷水：多数任务单 Agent 就够；四条判断标准都不满足就老老实实用单 Agent + Skill
  - 角色是契约不是名字：职责 / 输入 / 输出 / 停止条件 / 失败回退 / 不许做什么，缺一项都会出事
  - 三种协调方式：Supervisor（可控）、Pipeline（可预测）、Graph（可恢复）；自由对话式不推荐
  - 四类典型故障都有机械解法：轮次上限治循环争论、目标复述治漂移、隔离+压缩治膨胀、交付清单治责任真空
  - 上下文隔离才是多 Agent 的核心价值：独立上下文 + 只回传压缩结论

怎么跑：
    cd code
    AGENT_MOCK=1 python ch11_multi_agent.py    # 离线模式，不需要 API Key（框架与控制流全部真实执行）
    python ch11_multi_agent.py                 # 在线模式：三个 worker 真的调模型
"""

from __future__ import annotations

import json
import os
import sys
import time

_CODE_DIR = os.path.dirname(os.path.abspath(__file__))
if _CODE_DIR not in sys.path:
    sys.path.insert(0, _CODE_DIR)

from common.llm import load_env, print_banner, get_llm, chat, count_tokens_approx, count_message_tokens, estimate_cost, format_cost, is_mock_mode, set_mock_script, reset_mock  # noqa: E402 - 上面的 sys.path 引导必须在 import 之前
from common.tools import Tool, ToolRegistry  # noqa: E402 - 上面的 sys.path 引导必须在 import 之前
from common.trace import Trace  # noqa: E402 - 上面的 sys.path 引导必须在 import 之前

# Windows 控制台的编码不一定是 UTF-8；这里只把"编码失败"降级为替换字符，别让脚本崩在 print 上
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(errors="replace")
        except (ValueError, OSError):
            pass

OBJECTIVE = "对比向量数据库的检索方案，给出可落地的选型建议"
DELIVERABLES = ["facts", "sources", "draft", "verdict"]


def section(title: str) -> None:
    print("\n" + "=" * 64)
    print("  " + title)
    print("=" * 64)


def overlap(text_a: str, text_b: str) -> float:
    """字符二元组重合度：漂移检测与「无进展」判断都用它，实现简单、判据机械。"""
    def bigrams(text):
        chars = [char for char in (text or "") if not char.isspace()]
        return set(chars[i] + chars[i + 1] for i in range(len(chars) - 1))
    first = bigrams(text_a)
    if not first:
        return 0.0
    return len(first & bigrams(text_b)) / float(len(first))


def compress(items, budget=120) -> str:
    """
    把子 Agent 的一堆结论压成一句话再回传主管 —— 上下文隔离的关键动作。

    两个注意点：① 摘要里要留「指针」（来源 id、路径），主管想问细节时能回查；
    ② 压缩一定会丢信息，这是有意的取舍：判断标准是「主管下一步决策需不需要它」。
    """
    joined = "；".join(items)
    if len(joined) <= budget:
        return joined
    return joined[:budget] + "……（已压缩，细节留在子 Agent 自己的上下文里）"


def ask_json(prompt: str, mock_result: dict, system="你只输出 JSON，不要任何解释。") -> dict:
    """
    子 Agent 的「干活」入口：在线模式真调模型并解析 JSON，离线模式返回内置的规则结果。

    这样写的好处：**控制流（契约校验、漂移检测、轮次上限、交付清单）在两种模式下是同一份代码**，
    离线跑验证的是「脚手架和保险装对没有」，在线跑才是「效果好不好」。
    """
    if is_mock_mode():
        return mock_result
    raw = chat(prompt, system=system, temperature=0.2)
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except ValueError:
            pass
    print("  [!] 模型没有返回合法 JSON，回退到规则结果（真实项目里应该重试一次再回退）")
    return mock_result


# ============================================================================
# 一、契约：角色不是名字，是要落到代码里的字段
# ============================================================================

class Contract(object):
    """
    一个角色的契约。六项缺一项都会出事：
      没有「停止条件」就会无限干；没有「失败回退」就会卡死；没有「不许做」就会越界。
    这里的契约是**数据**，所以能在运行前校验输入、运行后校验输出。
    """

    def __init__(self, role, duty, inputs, outputs, stop_when, on_failure, must_not_do):
        self.role = role
        self.duty = duty
        self.inputs = inputs
        self.outputs = outputs
        self.stop_when = stop_when
        self.on_failure = on_failure
        self.must_not_do = must_not_do
        self.violations = 0

    def check_input(self, payload) -> list:
        return [field for field in self.inputs if not payload.get(field)]

    def check_output(self, result) -> list:
        return [field for field in self.outputs if not (result or {}).get(field)]

    def describe(self) -> str:
        rows = [("职责", self.duty), ("输入", ", ".join(self.inputs)), ("输出", ", ".join(self.outputs)),
                ("停止条件", self.stop_when), ("失败回退", self.on_failure), ("不许做", self.must_not_do)]
        lines = ["{}：{}".format(self.role, rows[0][1])]
        pad = " " * (len(self.role) + 1)
        lines.extend("{}{}：{}".format(pad, key, value) for key, value in rows[1:])
        return "\n".join(lines)


CONTRACTS = {
    "researcher": Contract(
        role="researcher",
        duty="只收集事实与来源，不写结论、不写正文",
        inputs=["question"],
        outputs=["facts", "sources", "restated_goal"],
        stop_when="facts 至少 3 条且每条都能对应 sources 里的来源",
        on_failure="退化为最多 2 条事实 + 标注证据不足，并把不确定性写进输出",
        must_not_do="不许做价值判断，不许给出建议",
    ),
    "writer": Contract(
        role="writer",
        duty="只把给定的事实写成草稿，不新增事实",
        inputs=["objective", "facts"],
        outputs=["draft", "restated_goal"],
        stop_when="草稿覆盖全部 facts 且不含 facts 之外的数据",
        on_failure="退化为只给大纲（三段小标题 + 每段一句话）",
        must_not_do="不许编造 facts 之外的数据，不许改目标",
    ),
    "reviewer": Contract(
        role="reviewer",
        duty="对照验收标准判断草稿合不合格，给出可定位的问题清单",
        inputs=["draft", "criteria"],
        outputs=["verdict", "issues"],
        stop_when="明确给出 pass 或 revise",
        on_failure="退化为只回 verdict=revise + 一条问题",
        must_not_do="不许重写草稿（那是 writer 的活）",
    ),
}


class Worker(object):
    """带契约的 worker：输入校验 → 干活 → 输出校验 → 失败回退。"""

    def __init__(self, contract: Contract, work, fallback=None):
        self.contract = contract
        self.work = work
        self.fallback = fallback
        self.calls = 0
        self.prompt_tokens = 0

    def run(self, payload, trace: Trace):
        self.calls += 1
        trace.log("worker_start", worker=self.contract.role, call=self.calls)
        new_tokens = count_tokens_approx(json.dumps(payload, ensure_ascii=False))
        self.prompt_tokens += new_tokens
        missing = self.contract.check_input(payload)
        if missing:
            self.contract.violations += 1
            trace.log("input_contract_violation", worker=self.contract.role, missing=missing)
            print("  [!] {} 输入契约不满足，缺字段：{}（宁可报错也别让它瞎跑，省 token）".format(
                self.contract.role, missing))
        result = self.work(payload)
        bad = self.contract.check_output(result)
        if bad:
            self.contract.violations += 1
            trace.log("output_contract_violation", worker=self.contract.role, missing=bad)
            print("  [!] {} 输出契约不满足，缺字段：{}".format(self.contract.role, bad))
            if self.fallback:
                print("      → 触发失败回退（降级不是重试）：{}".format(self.contract.on_failure))
                result = self.fallback(payload)
                trace.log("fallback_used", worker=self.contract.role)
        trace.log("worker_done", worker=self.contract.role)
        return result


# ============================================================================
# 二、三个 worker 的"干活"实现（离线确定性结果，在线可换成真模型）
# ============================================================================

FACTS = [
    "向量检索（ANN）在千万级向量上召回率高、延迟低，但内存占用大",
    "BM25 在精确字符串（型号、错误码）上更强，且零依赖、可解释",
    "混合检索用 RRF 融合两路排名，公开评测里常比单路 Recall@10 高 5~15 个点",
]
SOURCES = ["docs/benchmark.md#ann", "docs/benchmark.md#bm25", "paper:rrf-2009"]


def researcher_work(payload):
    return ask_json(
        "你是 researcher。目标：{}\n只收集事实与来源，不写结论。".format(payload.get("question")),
        {"facts": list(FACTS), "sources": list(SOURCES),
         "restated_goal": "收集向量数据库检索方案的事实与来源：{}".format(payload.get("question"))})


def researcher_fallback(payload):
    return {"facts": FACTS[:1], "sources": SOURCES[:1],
            "restated_goal": "证据不足，只给出 1 条事实：{}".format(payload.get("question"))}


def writer_work(payload):
    facts = payload.get("facts") or []
    body = "；".join(fact[:24] for fact in facts)
    if payload.get("revision"):
        # 修订走的是同一个 worker（同一份契约、同一条 trace），只是提示词里带上审稿意见
        draft = payload.get("draft") or ""
        return ask_json(
            "你是 writer。只修改被指出的部分，不要重写全文。目标：{}\n意见：{}".format(
                payload.get("objective"), payload.get("revision")),
            {"draft": draft + "（已按意见补充对比表：ANN / BM25 / 混合 三行；来源：docs/benchmark.md）",
             "restated_goal": "修订草稿使其满足验收标准：{}".format(payload.get("objective"))})
    return ask_json(
        "你是 writer。只把给定事实写成草稿，不许新增事实。目标：{}\n事实：{}".format(
            payload.get("objective"), facts),
        {"draft": "选型建议：中小规模优先【BM25 + 向量混合】，用 RRF 融合；"
                  "理由：{}。".format(body),
         "restated_goal": "把事实写成选型建议草稿：{}".format(payload.get("objective"))})


def writer_fallback(payload):
    return {"draft": "（降级）大纲：一、方案；二、理由；三、风险。",
            "restated_goal": "降级只给大纲：{}".format(payload.get("objective"))}


def writer_work_drifted(payload):
    """故意漂移的 writer：把目标改成「顺便把整个项目重构一遍」（第 4.2 节的现场）。"""
    result = writer_work(payload)
    result["restated_goal"] = "对比一下向量数据库，顺便把整个项目重构一遍并向老板汇报进度"
    return result


def reviewer_work(payload):
    draft = payload.get("draft") or ""
    issues = []
    if "对比表" not in draft:
        issues.append("缺少对比表")
    if "来源" not in draft and "docs/" not in draft:
        issues.append("结论没有标注来源")
    verdict = "pass" if not issues else "revise"
    return ask_json(
        "你是 reviewer。对照验收标准审查草稿：{}\n草稿：{}".format(payload.get("criteria"), draft),
        {"verdict": verdict, "issues": issues or ["无"]},)


def reviewer_forever_revise(payload):
    """故意永远不满意的 reviewer：「语气不够正式」这种**无法判定**的意见，正是循环争论的燃料。"""
    return {"verdict": "revise", "issues": ["初稿语气不够正式；再改一版"]}


def writer_revise(payload):
    draft = payload.get("draft") or ""
    return {"draft": draft + "（已按意见补充对比表：ANN / BM25 / 混合 三行；来源：docs/benchmark.md）",
            "restated_goal": "修订草稿：{}".format(payload.get("objective"))}


# ============================================================================
# 三、Supervisor：一个主管调度多个 worker，四道保险都装在这里
# ============================================================================

class Supervisor(object):
    """主管模式：能全局控轮次、预算、交付清单，也能做强制裁决。"""

    MAX_ROUNDS = 3
    DRIFT_THRESHOLD = 0.45
    RETURN_BUDGET = 120     # 子 Agent 回传结论的字符上限（超预算就压缩）

    def __init__(self, objective, deliverables=None, max_rounds=MAX_ROUNDS,
                 drift_threshold=DRIFT_THRESHOLD, writer=None, reviewer=None):
        self.objective = objective
        self.deliverables = list(deliverables or DELIVERABLES)
        self.max_rounds = max_rounds
        self.drift_threshold = drift_threshold
        self.trace = Trace("ch11_multi_agent")
        self.workers = {
            "researcher": Worker(CONTRACTS["researcher"], researcher_work, researcher_fallback),
            "writer": Worker(CONTRACTS["writer"], writer or writer_work, writer_fallback),
            "reviewer": Worker(CONTRACTS["reviewer"], reviewer or reviewer_work, None),
        }
        self.collected = {}

    # ---------------------------------------------------------------- 三道保险

    def check_drift(self, restated: str, worker: str) -> bool:
        """目标复述 + 重合度比对：漂移不是靠「人看一眼」发现的，而是靠机械检查。"""
        score = overlap(restated or "", self.objective)
        self.trace.log("drift_check", worker=worker, restated=(restated or "")[:40], score=round(score, 2))
        if score < self.drift_threshold:
            print("  [!] 检测到任务漂移（{}）：复述目标「{}」".format(worker, (restated or "")[:28]))
            print("      与原始目标重合度只有 {:.2f}（阈值 {:.2f}）→ 打回，要求重新复述并缩小范围".format(
                score, self.drift_threshold))
            self.trace.log("drift_detected", worker=worker, score=round(score, 2))
            return True
        return False

    def check_deliverables(self, missing_extra=None) -> list:
        """收尾逐项打勾：在终点做一次存在性检查，比在过程中祈祷有效得多。"""
        missing = [item for item in self.deliverables if not self.collected.get(item)]
        if missing_extra:
            missing.extend(missing_extra)
        self.trace.log("deliverable_check", missing=missing)
        if missing:
            print("\n[!] 责任真空检查：交付清单里还没有人产出的项 → {}".format("、".join(missing)))
            print("    → 显式指派给 reviewer 兜底补齐（每项都必须有人认领，否则不算完成）")
            self.trace.log("reassign", to="reviewer", items=missing)
        else:
            print("\n交付清单检查：全部有主，可以收尾")
        return missing

    def run_rounds(self, draft: str, verbose=True):
        """审查 → 修订循环。轮次上限是止损，不是提质 —— 要提质应该改验收标准或拆任务。"""
        review = None
        for round_no in range(1, self.max_rounds + 1):
            if verbose:
                print("\n[轮次 {}/{}] reviewer 开始审查".format(round_no, self.max_rounds))
            review = self.workers["reviewer"].run({"draft": draft, "criteria": "草稿必须含对比表与来源标注"},
                                                   self.trace)
            self.collected["verdict"] = review.get("verdict")
            if verbose:
                print("  reviewer 判定：{}，问题：{}".format(
                    review.get("verdict"), "；".join(review.get("issues") or [])))
            if review.get("verdict") == "pass":
                return draft, review, "pass"
            if round_no == self.max_rounds:
                print("  [!] 已达轮次上限 {}：强制停止修订，把分歧点交给人工".format(self.max_rounds))
                self.trace.log("round_limit_hit", round=self.max_rounds)
                return draft, review, "limit"
            revised = self.workers["writer"].run({"objective": self.objective,
                                                  "facts": self.collected.get("facts", []),
                                                  "draft": draft,
                                                  "revision": review.get("issues")}, self.trace)
            draft = revised.get("draft", draft)
            if verbose:
                print("  → 修订后的草稿：{}".format(draft[:60]))
        return draft, review, "limit"

    # ---------------------------------------------------------------- 主流程

    def run(self, verbose=True) -> dict:
        print("主管：目标 = {}".format(self.objective))
        print("主管：轮次上限 = {}，漂移阈值 = {:.2f}，交付清单 = {}".format(
            self.max_rounds, self.drift_threshold, "、".join(self.deliverables)))
        started = time.time()

        print("\n[第 1 步] 分派给 researcher（输入契约只给它必需字段，别的按需检索）")
        research = self.workers["researcher"].run({"question": self.objective}, self.trace)
        self.collected["facts"] = research.get("facts")
        self.collected["sources"] = research.get("sources")
        summary = compress(research.get("facts") or [])
        print("  researcher 的 {} 条事实已压缩为 {} 字后回传（子上下文已隔离）".format(
            len(research.get("facts") or []), len(summary)))
        self.check_drift(research.get("restated_goal"), "researcher")

        print("\n[第 2 步] 分派给 writer（只拿结论，不拿 researcher 的过程）")
        written = self.workers["writer"].run(
            {"objective": self.objective, "facts": self.collected["facts"]}, self.trace)
        self.collected["draft"] = written.get("draft")
        print("  writer 草稿（{} 字）：{}".format(len(self.collected["draft"] or ""),
                                                  (self.collected["draft"] or "")[:70]))
        self.check_drift(written.get("restated_goal"), "writer")

        if verbose:
            print("\n[第 3 步] 进入审查循环")
        draft, review, status = self.run_rounds(self.collected["draft"], verbose=verbose)
        self.collected["draft"] = draft
        cost = self._cost_estimate()
        missing = self.check_deliverables()
        return {"status": status, "draft": draft, "review": review, "missing": missing,
                "elapsed": time.time() - started, "cost": cost, "collected": dict(self.collected)}

    def _cost_estimate(self) -> dict:
        """不调模型也能算账：把每个 worker 实际收到的 payload 规模加起来。"""
        prompt = sum(worker.prompt_tokens for worker in self.workers.values())
        return {"prompt_tokens": prompt, "workers": {role: worker.calls for role, worker in self.workers.items()}}


# ============================================================================
# 四、Pipeline：research → write → review → revise，每步都有产物
# ============================================================================

def run_pipeline(objective: str):
    section("第 2 节 · Pipeline 模式：固定顺序，每步有产物（对比 Supervisor）")
    stages = [
        ("research", lambda: researcher_work({"question": objective})),
        ("write", lambda: writer_work({"objective": objective, "facts": list(FACTS)})),
    ]
    outputs = {}
    for name, action in stages:
        result = action()
        outputs[name] = result
        print("  [阶段 {}] 产物：{}".format(name, json.dumps(result, ensure_ascii=False)[:90]))
    draft = outputs["write"]["draft"]
    review = reviewer_work({"draft": draft, "criteria": "草稿必须含对比表与来源标注"})
    print("  [阶段 review] verdict={}，问题={}".format(review["verdict"], review["issues"]))
    if review["verdict"] == "revise":
        revised = writer_revise({"objective": objective, "draft": draft})
        print("  [阶段 revise] 修订后：{}".format(revised["draft"][:70]))
    else:
        revised = {"draft": draft}
        print("  [阶段 revise] 无需修订")
    print("  [Pipeline 特征] 可预测、易调试、每步有产物；但路径写死、不能回头。")
    print("     要断点续跑就上 Graph（节点=步骤，边=条件，状态可存）—— 那是它唯一的决定性优势。")
    return revised["draft"]


# ============================================================================
# 五、四类故障各跑一遍
# ============================================================================

def fault_round_limit():
    section("第 4.1 节 · 故障一：循环争论 → 轮次上限 + 强制裁决")
    supervisor = Supervisor(OBJECTIVE, max_rounds=3, reviewer=reviewer_forever_revise)
    result = supervisor.run(verbose=False)
    print("\n裁决：{}（分歧点：{}）".format(
        "强制停止，交人工" if result["status"] == "limit" else "通过",
        "；".join(result["review"].get("issues") or [])))
    events = [entry for entry in supervisor.trace.entries if entry["event"] == "round_limit_hit"]
    print("证据：trace 里的 round_limit_hit 事件 = {}".format(
        [{"round": entry.get("round")} for entry in events]))
    print("关键点：reviewer 的每条意见都必须是**可判定**的（「缺少对比表」可判定，「语气不够正式」不可判定）；")
    print("        可判定性这条规则能从源头消灭大部分争论。")


def fault_drift():
    section("第 4.2 节 · 故障二：任务漂移 → 强制复述目标 + 重合度比对")
    supervisor = Supervisor(OBJECTIVE, writer=writer_work_drifted)
    result = supervisor.run(verbose=False)
    print("\n处理：重新下发目标 + 缩小范围（只做选型对比，不许碰代码）")
    print("副作用好处：目标与约束被反复强调，模型更不容易忘。")


def fault_context_bloat(rounds=5):
    section("第 4.3 节 · 故障三：上下文膨胀 → 隔离 + 压缩回传")
    print("模拟两种组织方式，每一轮都真实统计 token（count_message_tokens）——")
    print("模型假设：共享上下文里每个 Agent 都把'上一轮完整记录'读进来并写回同一份历史（自由对话式的现实），")
    print("          隔离模式里主管只收到压缩后的结论。")
    shared_system = ("你是协作主管。本轮任务由 researcher、writer、reviewer 三个角色共同完成；"
                     "所有角色共享同一份对话记录，每个人都能看到其他人说过的一切，并在发言时引用相关历史，"
                     "以便保持一致。请在每个角色发言后检查是否需要补充说明，并把结论写回共享记录，"
                     "让后续角色能够基于完整历史继续工作。注意保持格式统一、结论可追溯、分工不重不漏。")
    shared = [{"role": "system", "content": shared_system}]
    isolated = [{"role": "system", "content": "你是主管，只接收子 Agent 的压缩结论，不接收它们的过程。"}]
    rows = []
    for round_no in range(1, rounds + 1):
        transcript = "\n".join(message["content"] for message in shared)
        shared.append({"role": "user", "content": "第 {} 轮记录（引用完整历史）：{}\n本轮结论：{}".format(
            round_no, transcript,
            "混合检索在召回率与成本之间取得平衡，建议中小规模优先采用；"
            "ANN 适合千万级向量但内存占用大，需要评估机器预算。" * 3)})
        isolated.append({"role": "user", "content": "第 {} 轮回传结论：{}".format(
            round_no, "混合检索 Recall@10 提升 8 个点，来源 docs/benchmark.md；" * 4)})
        rows.append((round_no, count_message_tokens(shared), count_message_tokens(isolated)))
    print("\n  轮次   共享上下文的 token   隔离 + 压缩后的主管 token   倍数")
    print("  " + "-" * 62)
    for round_no, shared_tokens, isolated_tokens in rows:
        print("  {:<6}{:<21}{:<26}{:.1f}".format(round_no, shared_tokens, isolated_tokens,
                                                 shared_tokens / float(max(isolated_tokens, 1))))
    print("  " + "-" * 60)
    print("  第 {} 轮时差 {:.1f} 倍。形状才是重点：共享上下文随轮次平方/指数增长，隔离 + 压缩是线性。".format(
        rows[-1][0], rows[-1][1] / float(max(rows[-1][2], 1))))
    print("  （本章正文的实测：420 → 13020 对 244 → 1220，第 5 轮差 10.7 倍；具体倍数取决于"
          "「每个 Agent 引用多少历史」，但趋势一定成立。）")
    print("  处理：子 Agent 独立上下文 + 只回传压缩结论 + 超预算就摘要（第 07 章的压缩在这里直接复用）。")


def fault_deliverables():
    section("第 4.4 节 · 故障四：责任真空 → 交付清单逐项打勾")
    supervisor = Supervisor(OBJECTIVE)
    supervisor.collected = {"draft": "草稿有了", "verdict": "pass"}   # 故意让 sources 缺失
    missing = supervisor.check_deliverables()
    print("缺失项：{}".format("、".join(missing) or "（无）"))
    print("本质：在终点做一次存在性检查，比在过程中祈祷有效得多。")
    print("     而且清单项必须和某个角色的 outputs 字段对应 —— 没人产出的项，应该在定清单时就砍掉。")


def fault_contract_violation():
    section("第 3 节 · 契约要落到代码里：跑之前校验输入，跑之后校验输出")
    trace = Trace("contract_demo")
    researcher = Worker(CONTRACTS["researcher"], researcher_work, researcher_fallback)
    print("  [输入违规] 传入 {}（缺 question）：".format({"foo": "bar"}))
    researcher.run({"foo": "bar"}, trace)

    broken = Worker(CONTRACTS["researcher"], lambda payload: {"facts": ["只有事实，没有来源"]},
                    researcher_fallback)
    print("\n  [输出违规] worker 忘了返回 sources 与 restated_goal：")
    result = broken.run({"question": OBJECTIVE}, trace)
    print("  → 回退产物：{}".format(json.dumps(result, ensure_ascii=False)[:90]))
    print("\n  两个设计要点：① 校验放在「跑之前」和「跑之后」；② 回退是**降级**不是重试")
    print("  （减少输出要求 / 缩小范围 / 明确上报做不了），重试通常只是再错一次。")


# ============================================================================
# 六、成本对比与 trace 落盘
# ============================================================================

def report_cost(supervisor: Supervisor, pipeline_draft_len: int):
    section("第 6 节 · 成本对比：先算账，再决定要不要上多 Agent")
    single = {"prompt": 9200, "completion": 1800, "latency": 26}
    supervisor_row = {"prompt": 11400, "completion": 2100, "latency": 34}
    parallel_row = {"prompt": 11400, "completion": 2100, "latency": 19}
    measured = supervisor._cost_estimate()
    print("  本章正文的量级参考：")
    print("  {:<32}{:>12}{:>12}{:>10}".format("方案", "输入 token", "输出 token", "延迟"))
    for name, row in (("单 Agent 一路到底", single), ("Supervisor + 2 worker", supervisor_row),
                      ("Supervisor + 2 worker（并行）", parallel_row)):
        print("  {:<32}{:>12}{:>12}{:>8}s".format(name, row["prompt"], row["completion"], row["latency"]))
    print("\n  本次实际跑的 Supervisor（按各 worker 收到的 payload 规模统计）：")
    print("     输入约 {} token（不含模型侧的系统提示与历史），worker 调用次数：{}".format(
        measured["prompt_tokens"], measured["workers"]))
    print("     三个规律：① 多 Agent 输入 token 通常比单 Agent 高 20%~200%（每个子 Agent 都要带自己的背景）；")
    print("              ② 唯一能降延迟的手段是并行（串行分工只会更高：34s > 26s）；")
    print("              ③ 买的是「上下文更干净 + 可对抗检查 + 可并行」，买不到这三样就是纯亏。")
    print("     判断顺序：单 Agent → 加 Skill 补流程 → 还不够，再拆 Agent。")


def main() -> int:
    load_env()
    print_banner("第 11 章 · 多智能体协作：契约 / 协调方式 / 四类故障")

    section("第 1 节 · 常见角色模式与它们的输入输出契约")
    for role, contract in CONTRACTS.items():
        print(contract.describe())
        print("")

    fault_contract_violation()

    section("第 2 节 · 两个角色 + 一个循环：Supervisor 模式的一次完整协作")
    supervisor = Supervisor(OBJECTIVE)
    result = supervisor.run(verbose=True)
    print("\n最终状态：{}　产出：草稿 {} 字，事实 {} 条，来源 {} 个，未解决项 {} 个".format(
        result["status"], len(result["draft"] or ""), len(result["collected"].get("facts") or []),
        len(result["collected"].get("sources") or []), len(result["missing"])))

    pipeline_draft = run_pipeline(OBJECTIVE)

    fault_drift()
    fault_round_limit()
    fault_context_bloat()
    fault_deliverables()

    report_cost(supervisor, len(pipeline_draft))

    section("Trace 落盘与摘要（复盘与评估的唯一依据，别省）")
    path = supervisor.trace.save(os.path.join(_CODE_DIR, "traces", "ch11_multi_agent.json"))
    print(supervisor.trace.summary())
    counts = supervisor.trace.counts()
    print("事件统计（JSON 里能查到的证据）：{}".format(
        "  ".join("{}: {}".format(key, value) for key, value in sorted(counts.items()))))
    print("已写入 {}（真实项目里这是复盘与评估的唯一依据）".format(path))
    print("有了它你才能回答：这一轮到底谁干了什么、为什么停下。")

    section("本章要点回顾")
    for line in [
        "1. 先泼冷水：四条判断标准（上下文隔离 / 对抗检查 / 并行 / 角色差异）不满足就不要拆 Agent。",
        "2. 角色是契约不是名字：职责、输入、输出、停止条件、失败回退、禁令，六项都要落到代码里校验。",
        "3. 协调方式三选一：Supervisor（可控）、Pipeline（可预测）、Graph（可恢复）；自由对话式不推荐。",
        "4. 四类故障都有机械解法：轮次上限、目标复述、隔离+压缩、交付清单。",
        "5. 上下文隔离是核心价值：子 Agent 只回传压缩结论，但要保留指针（来源 id / 路径）。",
        "6. 成本要算总账：多 Agent 输入 token 高 20%~200%，只有并行能降延迟。",
        "7. 保险是必需品不是可选项：删掉它们，多 Agent 就退化成「一群 Agent 瞎聊」。",
    ]:
        print("  " + line)
    print("\n完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
