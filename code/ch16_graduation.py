"""
第 16 章 · 毕业设计与面试 配套代码

本章学到什么：
  - 毕设不是把第 15 章再做一遍：题目自己定、指标自己定并给依据、至少 2 组替代方案对比
  - 选题四问：数据拿得到吗、效果验证得了吗、有没有真实取舍、四个里程碑跑得完吗
  - 需求文档的作用是把范围钉住：具体角色 + 可量化标准（含反指标）+ ≥3 条“不做什么”
  - 技术方案最值钱的部分是替代方案对比表：选择 / 替代 / 为什么没选 / 代价
  - 交付看四件事：README 四问（怎么跑/怎么配/怎么扩展/已知限制）、一键复现、离线演示、失败分类

怎么跑：
    cd code
    AGENT_MOCK=1 python ch16_graduation.py                      # 离线演示：选题表 + 各模板 + 检查清单
    AGENT_MOCK=1 python ch16_graduation.py --init my-project    # 生成毕设项目骨架
    AGENT_MOCK=1 python ch16_graduation.py --init my-project --dry-run   # 只看会生成哪些文件
    AGENT_MOCK=1 python ch16_graduation.py --init my-project --out ../04-Outputs/my-project
"""

from __future__ import annotations

import argparse
import json
import os
import sys

_CODE_DIR = os.path.dirname(os.path.abspath(__file__))
if _CODE_DIR not in sys.path:
    sys.path.insert(0, _CODE_DIR)

from common.llm import (load_env, print_banner, get_llm, chat, count_tokens_approx,
                        estimate_cost, format_cost, is_mock_mode, set_mock_script, reset_mock)
from common.tools import Tool, ToolRegistry
from common.trace import Trace

BANNER = "=" * 64

#: 8 个选题方向与评分预估（口径：选题与需求 15 / 技术方案与取舍 20 / 实现 20 / 评估 25 / 交付 10 / 文档 10）
TOPICS = [
    ("1 个人知识助理", "RAG 全链路 + 增量索引 + 引用校验 + 多轮追问", "★★☆ · 20-25h", "85-92",
     "退化成“第 08 章作业”：没有增量更新、没有评测集"),
    ("2 自动化周报生成", "多源聚合（commit/issue/文档）+ 时间窗 + 去重 + 模板化输出", "★★☆ · 18-22h", "82-90",
     "数据源权限拿不到；先做离线样例数据版本"),
    ("3 竞品监控分析", "定时抓取 + 变更检测（diff）+ 变更摘要 + 分流通知", "★★★ · 25-30h", "80-88",
     "合规与反爬边界不写清；“抓不到”没有降级方案"),
    ("4 招聘简历筛选", "简历解析 -> 结构化抽取 -> 打分 -> 排序 + 理由", "★★★ · 25-30h", "78-88",
     "偏见与隐私；必须写脱敏方案与“不做自动拒绝”的边界"),
    ("5 智能客服工单分流", "分类 + 优先级 + 路由 + 转人工（拒答）+ 混淆矩阵", "★★★ · 25-32h", "85-93",
     "类别不均衡时只报准确率；要报每类召回"),
    ("6 内容审核流水线", "规则 + 模型两级过滤 + 阈值 + 人工复核队列 + 审计日志", "★★★★ · 30-36h", "82-90",
     "只做模型不做规则（成本爆炸）；漏杀/误杀指标要分开"),
    ("7 数据分析助手", "自然语言 -> SQL -> 只读执行 -> 图表 + 结论 + 失败重试", "★★★★ · 32-40h", "84-93",
     "SQL 不校验就执行（重大扣分）；必须只读 + 行数上限"),
    ("8 你自己的选题", "自己定：四问全过才动手", "? · ≤40h", "?",
     "数据拿不到 / 效果验证不了 / 没有取舍 / 里程碑跑不完 —— 任一条过不了就别选"),
]

#: 选题四问：四问全过才动手（写不出“方案 A vs 方案 B”说明这题没有技术含量）
SELECTION_FOUR = [
    ("数据能不能拿到？", "具体到“我能拿到 30 条可比对的样本”。拿不到真实数据就先用样例数据把链路跑通，并写清接真数据的接口在哪。"),
    ("效果能不能验证？", "有没有客观指标（准确率/召回率/延迟/成本）。纯主观题要降级为“一致性 + 3 人打分取均值”，并提前说明评分标准。"),
    ("有没有真实取舍？", "能不能写出至少两组“方案 A vs 方案 B，为什么选 A，代价是什么”。写不出来说明这题没有技术含量。"),
    ("四个里程碑能不能跑完？", "用第 5 节的标准估时，总工时超过 40 小时的题目，砍到 30 小时以内。"),
]

#: 四个里程碑：每个都要有可运行的产出，做完就能演示给同学看
MILESTONES = [
    ("M1 骨架跑通（6-8h）", "一条命令跑通端到端（可离线 mock），打印中间结果与最终产出",
     "换一台机器 10 分钟内能跑出结果吗？", "卡在“想把架构设计完美”：丑陋但完整胜过优雅但跑不起来"),
    ("M2 接真实数据与模型（8-10h）", "真实数据 + 真模型跑通，落 trace（token/延迟/成本）",
     "单次成本与 P95 延迟是多少？", "卡在数据清洗（会吃掉 60% 时间）：给自己设硬上限，脏数据处理写成“已知限制”"),
    ("M3 评估闭环（8-10h）", "≥20 条评测集 + 一键跑分 + 指标报告 + 5 条 bad case",
     "你能说出最难过的 3 条是哪条、为什么吗？", "卡在标注（20 条看起来轻松，实际要 3 小时）：边开发边攒"),
    ("M4 交付打磨（4-6h）", "README 四问齐全 + 已知限制 ≥3 条 + 3 分钟演示脚本",
     "陌生人照 README 能复现你的指标吗？", "卡在“再改一个功能”：冻结功能，只修 bug 与补文档"),
]

#: 面试前一晚的三组检查（材料 / 口述 / 状态）
NIGHT_BEFORE = [
    ("材料", ["三个项目各一页纸卡片：六段式关键词 + 指标数字 + 一个失败故事（写关键词，不写完整句子）",
              "两张架构图（截图或手画扫描），能 30 秒讲清数据流",
              "指标数字表：主指标 / 反指标 / 成本 / 延迟 / 规模（口径也写上）",
              "demo 的离线模式确认可跑（网络挂了也能演），并录一段 3 分钟备份视频"]),
    ("口述", ["90 秒自我介绍录音听一遍：我是谁 -> 做过的 2 个项目 + 指标 -> 我想做的方向",
              "每个项目准备 5 个追问：技术选型 / 为什么不用 X / 流量 10 倍 / 效果不好怎么查 / 你的贡献是什么",
              "准备“不会”的答法：“这块我没做过，我的理解是……如果要验证，我会先……”——别硬编"]),
    ("状态", ["提前 10 分钟进会议；设备（耳机/摄像头/共享屏幕）提前测一次",
              "环境变量与 key 检查一遍（能在线演示，但默认准备离线方案）",
              "准备 2 个反问，写在纸上放在手边；睡够 7 小时（状态比多做一道题重要）"]),
]

FILLER_WORDS = "负责、参与、赋能、闭环、一站式、大幅提升、显著优化、深入理解、熟练掌握、各类、相关、等主流技术"


# ============================================================================
# 一、骨架生成：目录结构 + 各模板文件
# ============================================================================

def readme_template(name):
    """README 必须回答四件事，顺序照抄：怎么跑 / 怎么配 / 怎么扩展 / 已知限制。"""
    return """# {name}

【待填：一句话说清给谁解决什么问题。例：给客服组用的工单分流助手，把人工看标题的时间从 40 秒降到 5 秒。】

## 怎么跑

```bash
pip install -r requirements.txt
python src/main.py --demo            # 离线模式：不需要 Key、不联网，输出完整过程
python src/main.py --input "帮我看看这条工单该派给谁"   # 在线模式：读 .env
python evals/run_eval.py             # 一键跑评测集，输出指标报告
```

## 怎么配

```bash
cp .env.example .env
#   1) OPENAI_API_KEY   ：你的 Key（不要提交进仓库）
#   2) OPENAI_BASE_URL  ：服务商地址，注意以 /v1 结尾
#   3) OPENAI_MODEL     ：模型名，必须与服务商匹配
```

没有 Key 会怎样：自动回落到 `--demo` 离线模式，流程能跑通、指标可复现，但不代表真实模型的效果。

## 怎么扩展

| 想做的事 | 改哪个文件 | 说明 |
| --- | --- | --- |
| 加一个工具 | `src/main.py` 的 `build_tools()` | 写一个带类型注解的函数 + 一行注册，schema 自动生成 |
| 换一个模型 | `.env` 的 `OPENAI_MODEL` | 不改代码；换完**必须重跑评测集**再宣布变好 |
| 换一批数据 | `data/` 目录 + `config.json` 的 `data_dir` | 索引要重建，报告里记 `index_version` |
| 调阈值/预算 | `config.json` | 阈值改动必须附上评测集跑分，不能只凭感觉 |
| 加一个评测用例 | `evals/cases.jsonl` | 每条都要写 notes：为什么加它 |

## 已知限制

1. 【待填：我知道但这次没做的（例：没做增量索引，文档更新要重建全量）】
2. 【待填：数据/环境导致的边界（例：表格跨页时切片会丢表头，扫描件 PDF 不入索引）】
3. 【待填：能力边界（例：跨文件的架构级问题审不了；不做自动拒绝，只给建议）】

## 评估结果（填你自己的实测值）

| 指标 | 数值 | 口径 |
| --- | --- | --- |
| 主指标 | 【待填】 | 【待填：多少条用例、怎么判的】 |
| 反指标 | 【待填】 | 【待填：例：误报率、编造率、误拒率】 |
| P95 延迟 | 【待填】 | 【待填：样本数】 |
| 单次成本 | 【待填】 | 【待填：模型名 + 价格档位】 |
""".format(name=name)


def main_py_template():
    """生成的入口脚本只依赖标准库，保证在任何机器上 `python src/main.py --demo` 都能跑。"""
    return '''"""项目入口。骨架件：先保证 --demo 离线可跑，再往里填你自己的逻辑。"""
from __future__ import annotations

import argparse
import json
import os

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def build_tools():
    """在这里注册你的工具。每个工具：函数 + 一句给模型看的说明 + 参数 schema。"""
    def echo(text: str) -> str:
        """原样返回输入，用来验证工具调用链路是通的。"""
        return "echo: %s" % text

    return {"echo": {"func": echo, "description": "原样返回输入，用于连通性验证",
                     "parameters": {"type": "object", "properties": {"text": {"type": "string"}},
                                    "required": ["text"]}}}


def run_demo(config):
    """离线演示：不需要 Key、不联网，把整条链路走一遍并打印中间结果。"""
    tools = build_tools()
    print("=" * 60)
    print("离线演示（%s）" % config.get("project", "unnamed"))
    print("=" * 60)
    print("可用工具：%s" % ", ".join(sorted(tools)))
    for step, (name, spec) in enumerate(sorted(tools.items()), start=1):
        print("  步骤 %d：调用 %s(...) -> %s" % (step, name, spec["func"](text="hello")))
    print("  最终产出：这里应该是你的业务结果（报告 / 分类 / SQL / 审查意见）")
    thresholds = config.get("thresholds", {})
    print("  当前阈值：%s" % json.dumps(thresholds, ensure_ascii=False))
    print("下一步：把 run_demo 换成真实链路，并在 evals/cases.jsonl 里补到 20 条以上")


def main(argv=None):
    parser = argparse.ArgumentParser(description="项目入口")
    parser.add_argument("--demo", action="store_true", help="离线演示（不需要 Key）")
    parser.add_argument("--input", help="在线模式的输入")
    args = parser.parse_args(argv)
    config = load_config()
    if args.demo or not args.input:
        run_demo(config)
        return 0
    print("在线模式：把这里换成真实调用（读 .env 里的 OPENAI_* ）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def run_eval_template():
    """一键跑分脚本：先能跑、能出数，再谈调优。指标口径必须写在报告里。"""
    return '''"""一键跑分：读 evals/cases.jsonl，逐条判定，输出通过率与失败清单。"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def load_cases():
    cases = []
    with open(os.path.join(HERE, "cases.jsonl"), "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line and not line.startswith("#"):
                cases.append(json.loads(line))
    return cases


def judge(case, answer):
    """判定器：先精确/关键词，再上规则。别一上来就用模型打分（会漂移）。"""
    spec = case.get("judge", {})
    if spec.get("type") == "keywords":
        missing = [k for k in spec.get("must", []) if k not in answer]
        return (not missing), ("缺少关键词 %s" % missing) if missing else "ok"
    return answer.strip() == str(spec.get("expect", "")).strip(), "精确匹配"


def run_case(case):
    """把这里换成你的真实调用；骨架先返回一个占位答案，让指标链路先通。"""
    return case.get("demo_answer", "")


def main():
    cases = load_cases()
    passed, failed = 0, []
    for case in cases:
        ok, reason = judge(case, run_case(case))
        passed += 1 if ok else 0
        if not ok:
            failed.append((case.get("id"), case.get("category"), reason))
    rate = passed / float(len(cases) or 1)
    print("用例 %d 条，通过 %d 条，通过率 %.1f%%" % (len(cases), passed, rate * 100))
    by_category = {}
    for case in cases:
        bucket = by_category.setdefault(case.get("category", "unknown"), [0, 0])
        bucket[1] += 1
        bucket[0] += 1 if judge(case, run_case(case))[0] else 0
    for name, (ok, total) in sorted(by_category.items()):
        print("  %-10s %d/%d" % (name, ok, total))
    for cid, category, reason in failed:
        print("  失败 %s(%s)：%s" % (cid, category, reason))
    return 0 if rate >= 0.75 else 1


if __name__ == "__main__":
    sys.exit(main())
'''


def requirements_template():
    return """# 依赖能少则少：每多一个依赖，陌生人多一道安装失败的坎
# 本骨架的 src/main.py 只用标准库，所以这两行也可以先注释掉
# openai>=1.0.0
# requests>=2.31.0
"""


def env_example_template():
    return """# 复制成 .env 后填写。.env 不要提交进 Git（.gitignore 已忽略）
OPENAI_API_KEY=
OPENAI_BASE_URL=https://api.deepseek.com/v1
OPENAI_MODEL=deepseek-chat
# 单任务成本上限（元）
AGENT_BUDGET_CNY=0.5
"""


def gitignore_template():
    return """.env
__pycache__/
*.pyc
traces/*.json
!traces/.gitkeep
data/index/
"""


def config_template(name):
    return json.dumps({
        "project": name,
        "model": "deepseek-chat",
        "temperature": 0.2,
        "max_steps": 8,
        "data_dir": "data",
        "thresholds": {"refuse": 0.35, "confidence_min": 0.6, "max_issues": 5},
        "budgets": {"per_task_cny": 0.5, "per_user_cny": 2.0, "daily_cny": 10.0},
        "index_version": "v1",
    }, ensure_ascii=False, indent=2) + "\n"


def cases_template():
    """评测集模板：3 条样例 + 字段说明写在 notes 里；目标是 20 条以上、四类配额齐全。"""
    samples = [
        {"id": "case-001", "category": "common", "input": "【待填：标准问法】",
         "judge": {"type": "keywords", "must": ["关键词1", "关键词2"]},
         "demo_answer": "关键词1 关键词2", "gold": "【待填：期望答案要点】",
         "notes": "常见场景：基本能力不能退化；judge 是代码可执行的判定，不是一句描述"},
        {"id": "case-002", "category": "boundary", "input": "【待填：参数缺失/多意图/超长输入】",
         "judge": {"type": "keywords", "must": ["已追问"]},
         "demo_answer": "已追问", "gold": "【待填】",
         "notes": "边界情况：测鲁棒性。真实项目里这类用例应当用规则判定（查调了哪些工具、是否追问），"
                  "形如 code/ch12_eval.py 的 judge_rule"},
        {"id": "case-003", "category": "refuse", "input": "【待填：知识库里没有的问题】",
         "judge": {"type": "keywords", "must": ["未找到"], "must_not": ["我认为"]},
         "demo_answer": "未找到相关规定", "gold": "应当明确说不知道",
         "notes": "拒绝样本：考“该说不”的能力；这类最容易在改 prompt 后退化，必须进 CI"},
    ]
    return "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in samples)


def requirements_doc_template(name):
    return """# 需求文档：{name}

## 1. 用户是谁

【待填：一个具体角色 + 一个具体场景。不要写“所有需要 XX 的人”。
例：客服组 6 人，每天处理约 300 张工单，早班高峰集中在 9:00-11:00。】

## 2. 解决什么问题

【待填：他们现在怎么做？成本是多少（分钟/次、错误率）？
例：目前全靠人工看标题分流，平均每张 40 秒，高峰积压；错分要转两次，用户等待翻倍。】

## 3. 成功标准（可量化 3~5 条，含至少 1 个反指标）

| 指标 | 目标 | 怎么测 |
| --- | --- | --- |
| 【待填】主指标 | 【待填】 | 【待填：多少条样本、谁判、判定标准】 |
| 【待填】反指标 | 【待填】 | 【待填：例：误报率/编造率/误拒率】 |
| P95 延迟 | 【待填】 | 【待填：采样多少次】 |
| 单次成本 | 【待填】 | 【待填：模型与价格档位】 |

## 4. 明确不做什么（≥3 条，每条对应一个你本来想做的功能）

1. 【待填】
2. 【待填】
3. 【待填】

## 5. 边界与约束

【待填：数据范围（哪些可用/不可用）、成本上限、延迟要求、合规要求（脱敏、保留期）。】
""".format(name=name)


def design_doc_template(name):
    return """# 技术方案：{name}

## 1. 架构（文字描述就够，评审看的是数据流不是配色）

```text
入口层（CLI / 定时任务 / 一个 HTTP 接口，三选一即可）
   ↓
编排层（分派 + 轮次上限 + 交付清单检查）
   ↓
角色 A 解析/检索   角色 B 判断/生成   角色 C 校验/复核
   ↓
基础设施：证据表 / 索引 / 工具注册表 / Trace / 评测集
```

## 2. 模块划分（每个模块都要能独立测）

| 模块 | 职责 | 输入 -> 输出 | 复用前 15 章的哪一块 |
| --- | --- | --- | --- |
| 编排器 | 分派任务、控制轮次、检查交付清单 | 用户请求 -> 最终产出 | 第 11 章 Supervisor |
| 角色 A | 【待填】 | 【待填】 | 第 04/05/08 章 |
| 角色 B | 【待填】 | 【待填】 | 第 03/06 章 |
| 校验器 | 引用校验 / 拒答 / 格式校验 | 结论 -> 通过或打回 | 第 08/12 章 |
| 评测 | 评测集跑分与报告 | 评测集 -> 指标 | 第 12 章 |

## 3. 替代方案对比（至少 2 组，这是评审与面试官最看重的部分）

| 决策点 | 选择 | 替代方案 | 为什么没选 | 代价（必须写清楚） |
| --- | --- | --- | --- | --- |
| 编排方式 | 【待填】 | 【待填】 | 【待填】 | 【待填】 |
| 检索方案 | 【待填】 | 【待填】 | 【待填】 | 【待填】 |
| 校验策略 | 【待填】 | 【待填】 | 【待填】 | 【待填：至少一行的代价要明确变大】 |
""".format(name=name)


def failures_doc_template():
    return """# 失败分类与处理

按**环节**归类，而不是写成“模型不行”。归因分布决定下一步先修哪一类。

| 环节 | 表现 | 归因 | 处理策略 | 本次条数 |
| --- | --- | --- | --- | --- |
| 输入解析 | 字段缺失、格式怪 | 数据质量 | 清洗 + 缺字段走默认分支 | 【待填】 |
| 检索/召回 | 正确材料没进候选 | 切片/检索 | 调切片、加混合检索 | 【待填】 |
| 工具调用 | 参数错、超时、返回异常 | 工具契约 | 严格 schema + 重试 + 降级 | 【待填】 |
| 推理/判断 | 材料在，结论错 | 提示词/模型 | 拆步、加对比示例、换模型 | 【待填】 |
| 输出格式 | JSON 解析失败、字段缺失 | 约束不足 | schema 校验 + 一次重试 | 【待填】 |
| 拒答 | 该拒没拒 / 不该拒拒了 | 阈值 | 用评测集扫阈值，双指标一起看 | 【待填】 |

## 结论（归因分布 + 下一步）

【待填：例：20 条失败里，检索类 9 条、推理类 6 条、格式类 3 条、拒答类 2 条；
因此下一步优先改切片（影响面最大）。】
"""


def report_template():
    return """# 评估报告 v1

系统：model=【待填】, temperature=【待填】, index=【待填】, 日期=【待填】

主指标：【待填】｜ 反指标：【待填】
工程指标：P95 延迟 【待填】｜ 单次成本 【待填】｜ 规模（文档数/chunk 数/用例数）【待填】

## 5 条 bad case

1. 【待填：错在哪一环 + 你改了什么 + 指标变化多少】

## 改动记录

- v1：【待填：改了什么，指标从多少到多少】
"""


def build_skeleton(name):
    """返回 {相对路径: 文件内容}。骨架只依赖标准库，保证克隆下来就能跑。"""
    return {
        "README.md": readme_template(name),
        "requirements.txt": requirements_template(),
        ".env.example": env_example_template(),
        ".gitignore": gitignore_template(),
        "config.json": config_template(name),
        "src/main.py": main_py_template(),
        "evals/cases.jsonl": cases_template(),
        "evals/run_eval.py": run_eval_template(),
        "evals/report.md": report_template(),
        "docs/requirements.md": requirements_doc_template(name),
        "docs/design.md": design_doc_template(name),
        "docs/failures.md": failures_doc_template(),
        "traces/.gitkeep": "",
    }


def init_project(name, out_dir, dry_run=False):
    """生成骨架。--dry-run 只打印将要创建的文件列表，不落盘。"""
    files = build_skeleton(name)
    root = out_dir or os.path.join(os.getcwd(), name)
    print("项目名：%s" % name)
    print("目标目录：%s" % os.path.abspath(root))
    print("将创建 %d 个文件：" % len(files))
    for path in sorted(files):
        content = files[path]
        print("  %-26s %5d 字节  %3d 行" % (path, len(content.encode("utf-8")), content.count("\n")))
    if dry_run:
        print("\n（--dry-run：没有写入任何文件。去掉 --dry-run 就会真的创建。）")
        return 0
    for path, content in files.items():
        target = os.path.join(root, path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(content)
    print("\n骨架已生成。接下来三件事：")
    print("  1. cd %s && python src/main.py --demo     # 先确认离线能跑通" % os.path.abspath(root))
    print("  2. 把 docs/requirements.md 的四节换成你自己的题目（用户是谁 / 可量化目标 / 不做什么 / 边界）")
    print("  3. 把 evals/cases.jsonl 填到 20 条以上（四类配额：常规 / 边界 / 拒绝 / 对抗）")
    print("  全文搜索【待填】就能找到所有占位符。")
    return 0


# ============================================================================
# 二、默认演示：选题表 + 各模板 + 检查清单
# ============================================================================

def show_topics():
    print("\n" + BANNER)
    print("[1] 八个毕设方向与评分预估")
    print(BANNER)
    print("评分口径（满分 100）：选题与需求 15 ｜ 技术方案与取舍 20 ｜ 实现与代码质量 20 ｜ "
          "评估与失败分析 25 ｜ 交付与演示 10 ｜ 文档与可复现 10")
    print("\n%-18s %-42s %-14s %-8s %s" % ("方向", "核心能力点", "难度·工时", "评分预估", "最容易被扣分的地方"))
    for title, skill, effort, score, risk in TOPICS:
        print("%-18s %-42s %-14s %-8s %s" % (title, skill, effort, score, risk))
    print("\n四步选题法（四问全过才动手）：")
    for index, (question, detail) in enumerate(SELECTION_FOUR, start=1):
        print("  %d. %s" % (index, question))
        print("     %s" % detail)
    print("\n反直觉建议：选题偏向“脏数据 + 有验证”的方向。数据干净、验证不了的题目"
          "（比如“做一个聊天机器人”）看上去好做，实际最难拿分 —— 因为没有失败、没有指标、没有故事。")


def show_requirements_template(name="<项目名>"):
    print("\n" + BANNER)
    print("[2] 需求文档模板（作用是把范围钉住，防止你在第 10 天突然想加个前端）")
    print(BANNER)
    print(requirements_doc_template(name))


def show_design_template(name="<项目名>"):
    print("\n" + BANNER)
    print("[3] 技术方案模板（评审最看重第 3 节的替代方案对比表）")
    print(BANNER)
    print(design_doc_template(name))


def show_milestones():
    print("\n" + BANNER)
    print("[4] 四个里程碑与检查清单（每个都要有可运行产出）")
    print(BANNER)
    for title, output, question, pitfall in MILESTONES:
        print("\n  %s" % title)
        print("    可运行产出：%s" % output)
        print("    验收问句：%s" % question)
        print("    常见卡点：%s" % pitfall)
    print("\n  评估要求（M3 的产出）：")
    print("    · 评测集配方：常规 50% / 边界 20% / 拒绝 15% / 对抗 15%，四类都要有")
    print("    · 指标报告一页：系统信息（model/temperature/index/日期）+ 主指标 + 反指标 + 成本延迟")
    print("    · 失败要按环节分类（输入解析/检索/工具/推理/格式/拒答）并给出归因分布")
    print("    · 结论要落到“下一步优先修哪一类、为什么”")


def show_night_before():
    print("\n" + BANNER)
    print("[5] 面试前一晚检查清单")
    print(BANNER)
    for group, items in NIGHT_BEFORE:
        print("\n  %s：" % group)
        for item in items:
            print("    [ ] %s" % item)
    print("\n  六段式项目表达（每段 30~60 秒）：业务问题 -> 技术选型 -> 系统设计 -> "
          "评估指标 -> 故障与优化 -> 最终结果")
    print("  三个必考变体的一句话答案：")
    print("    · 最大贡献 -> 指向一个具体决策：“证伪阶段是我加的，它把误报从 2.1 降到 0.6，代价是检出率掉 5 个点。”")
    print("    · 如果重做 -> 答顺序不答技术：“我会把评测集提到第一周就建，当时拖到第三周，前两周没有基线。”")
    print("    · 怎么分工 -> 说实话 + 说清接口：“我把接口定成 cases.jsonl 的字段格式，各自能独立测。”")
    print("\n  简历公式：动词 + 系统名 + 三个关键技术点 + 可量化结果（含口径）")
    print("  空话词清单（见面就删）：%s" % FILLER_WORDS)
    print("  删完之后如果这条描述没有任何数字或文件名，它就不该出现在简历上。")


def show_pipeline_hint():
    """把本章和前一章的代码串起来：骨架生成之后，往里搬第 15 章的流水线。"""
    print("\n" + BANNER)
    print("[6] 骨架到手之后：往里搬哪一块")
    print(BANNER)
    rows = [
        ("证据表 + 引用校验", "ch15_agent_app.py 的 _build_evidence / check_citations"),
        ("切片 + 混合检索 + 拒答", "ch15_agent_app.py 的 chunk_document / hybrid_search / answer_question"),
        ("双阶段审查 + 阈值", "ch15_agent_app.py 的 stage_generate / stage_falsify"),
        ("mini harness（会话/权限/子 Agent）", "ch14_mini_harness.py 的 Session / PermissionGate / SubAgent"),
        ("评测集 + 失败分类 + 指标", "ch12_eval.py 的 EVAL_SET / classify_failure / build_report"),
        ("缓存 + 重试 + 预算熔断", "ch13_deploy.py 的 ExactCache / call_with_retry / CostGuard"),
    ]
    for name, where in rows:
        print("  %-32s <- %s" % (name, where))
    print("\n  方法：复制一份到你的 src/ 下面改，别直接 import chXX 脚本（教学标本要保持原样）。")


def main(argv=None):
    parser = argparse.ArgumentParser(description="第 16 章：毕业设计脚手架与面试准备")
    parser.add_argument("--init", metavar="NAME", help="生成毕设项目骨架")
    parser.add_argument("--name", dest="init", metavar="NAME", help="同 --init（第 16 章正文用的是 --name）")
    parser.add_argument("--out", metavar="DIR", help="输出目录（默认：当前目录下的 <NAME>/）")
    parser.add_argument("--dry-run", action="store_true", help="只打印将要创建的文件列表，不落盘")
    args = parser.parse_args(argv)

    load_env()
    print_banner("第 16 章 · 毕业设计与面试")

    if args.init:
        print("\n[init] 生成毕设项目骨架")
        return init_project(args.init, args.out, args.dry_run)

    show_topics()
    show_requirements_template()
    show_design_template()
    show_milestones()
    show_night_before()
    show_pipeline_hint()

    print("\n" + BANNER)
    print("本章要点回顾")
    print(BANNER)
    for line in [
        "1. 毕设与练习的区别：题目自己定、指标自己定并给依据、至少 2 组替代方案对比、≥20 条评测集。",
        "2. 选题四问全过才动手；偏向“脏数据 + 可验证”，别选看上去好做但验证不了的题。",
        "3. 需求文档把范围钉住：具体角色 + 可量化标准（含反指标）+ ≥3 条“不做什么”。",
        "4. 技术方案的价值在替代方案对比表：选择 / 替代 / 为什么没选 / 代价——凡选择必有代价。",
        "5. 四个里程碑每个都要有可运行产出，评测集从 M1 就开始攒。",
        "6. 交付看四件事：README 四问、一键复现、离线演示、失败分类归因。",
        "7. 面试按岗位换重点；项目题用六段式；追问三框架：承认 X 的适用场景、把 10 倍换算成绝对数字、分段评估。",
        "8. 简历只留数字与文件名；每个数字都要能答“怎么测的”。",
    ]:
        print("  " + line)
    print("\n生成你自己的骨架：python ch16_graduation.py --init my-project --dry-run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
