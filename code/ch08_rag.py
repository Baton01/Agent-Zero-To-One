"""
第 08 章 · RAG 全链路 配套代码

本章学到什么：
  - 链路要完整：加载 → 清洗 → 切片 → 向量化 → 召回 → 融合(RRF) → 重排 → 带引用生成 → 校验 → 评估
  - 切片是最便宜的优化点：中文别抄英文的 chunk_size，按标题切优于按字符切
  - 关键词（BM25）与向量互补：一个强在精确字符串，一个强在同义改写，用 RRF 只按排名融合
  - 引用必须机械校验（编号存在 / 范围正确 / 字面重合度），校验不过就拒答 —— 宁可拒答也不给不可追溯的答案
  - 没有评估的 RAG 不算做完：Recall@K 决定上限，拒答正确率决定可信度，bad case 必须能归类

怎么跑：
    cd code
    AGENT_MOCK=1 python ch08_rag.py    # 离线模式，不需要 API Key、不需要联网（内置 3 篇示例文档）
    python ch08_rag.py                 # 在线模式：用真实模型生成带引用的回答，用真 embedding 检索
"""

from __future__ import annotations

import io
import json
import math
import os
import re
import sys
import zlib

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


def section(title: str) -> None:
    print("\n" + "=" * 64)
    print("  " + title)
    print("=" * 64)


# ============================================================================
# 一、示例文档（3 篇，覆盖事实型 / 多跳 / 无答案三类问题）
# ============================================================================

SAMPLE_DOCS = [
    {"id": "travel.md", "title": "差旅制度", "text": """# 差旅制度

## 1. 适用范围
本制度适用于公司全体正式员工与实习生的国内、海外出差。试用期员工同样适用。

## 2. 交通标准
国内出差 4 小时以内的高铁可乘坐二等座；超过 4 小时或跨夜行程可乘坐一等座。
飞机经济舱需提前 7 天预订，总监及以上级别可乘坐商务舱。

## 3. 住宿报销标准
国内出差住宿：一线城市每晚不超过 600 元，其他城市不超过 400 元。
海外出差住宿：按当地货币结算，单晚不超过等值 8000 日元。
同性别同事同行时应优先合住标准间，房费合并计算。

## 4. 报销流程与时限
出差结束后 15 个工作日内提交报销单，需附发票原件与行程单。
超过 30 天未提交视为放弃。单据不齐时财务会退回，退回后需在 5 个工作日内补齐。

## 5. 出差餐补
国内出差餐补每人每天 120 元，海外出差餐补每人每天 300 元或等值当地货币。
"""},
    {"id": "handbook.md", "title": "员工手册", "text": """# 员工手册

## 1. 考勤
标准工作时间为每周一至周五 9:30 至 18:30，弹性上下班各 1 小时。
迟到超过 30 分钟需要在考勤系统里提交说明。

## 2. 请假
事假需提前 1 个工作日申请；病假需提供医院证明。
连续请假超过 3 天需要直属主管与 HR 双方审批。

## 3. 福利
公司为员工提供每年一次体检、每月 500 元餐饮补贴，以及补充商业医疗保险。
体检可在每年 6 月到 10 月之间预约，逾期不补。
"""},
    {"id": "faq.md", "title": "产品 FAQ", "text": """# 产品 FAQ

## 1. 支持的运行环境
服务端支持 Python 3.9 及以上版本，浏览器支持 Chrome 与 Edge 的最近三个大版本。

## 2. 接口限流
默认每个 API Key 每分钟 600 次请求，超过后返回 429 状态码，建议退避 2 秒后重试。
企业版可申请提升到每分钟 3000 次。

## 3. 数据保留
调用日志默认保留 30 天，企业版可配置为 180 天。
客户上传的文档在删除后 7 天内从备份中彻底清除。
"""},
]

#: 10 条评测集（含 2 条无答案题）。gold 用关键词代理"相关的块"，便于零依赖跑通；
#: 真实项目里这张表应该写进 JSON 文件并人工标注块 id（第 12 章会讲怎么维护评测集）。
EVAL_SET = [
    {"q": "员工出差住宿报销上限是多少？", "expect": ["600", "400"], "must_refuse": False,
     "gold": [["一线城市", "600"], ["其他城市", "400"]]},
    {"q": "海外出差的住宿上限是多少？", "expect": ["8000"], "must_refuse": False,
     "gold": [["海外", "8000"]]},
    {"q": "出差餐补每天多少钱？", "expect": ["120", "300"], "must_refuse": False,
     "gold": [["餐补", "120"], ["海外", "300"]]},
    {"q": "我下周去东京出差三天，住宿和餐补大概能报多少？", "expect": ["8000", "300"], "must_refuse": False,
     "gold": [["海外", "8000"], ["餐补", "300"]]},
    {"q": "报销单最晚什么时候提交？", "expect": ["15"], "must_refuse": False,
     "gold": [["15 个工作日", "15"]]},
    {"q": "接口被限流会返回什么状态码？", "expect": ["429"], "must_refuse": False,
     "gold": [["429", "600 次"]]},
    {"q": "调用日志默认保留多久？", "expect": ["30"], "must_refuse": False,
     "gold": [["30 天", "180"]]},
    {"q": "服务端支持哪些 Python 版本？", "expect": ["3.9"], "must_refuse": False,
     "gold": [["Python 3.9", "3.9"]]},
    {"q": "公司的团建预算是多少？", "expect": [], "must_refuse": True, "gold": []},
    {"q": "员工每年有几天年假？", "expect": [], "must_refuse": True, "gold": []},
]

REFUSE_TEXT = "资料中没有找到相关内容，无法回答这个问题。"


# ============================================================================
# 二、加载与清洗
# ============================================================================

def clean_text(text: str) -> str:
    """清洗四件事：统一换行 → 去页眉页脚 → 压空白 → 去多余空行。脏数据进 RAG，就是脏答案出 RAG。"""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"^\s*(第\s*\d+\s*页|-\s*\d+\s*-|Page\s+\d+)\s*$", "", text, flags=re.M)
    text = re.sub(r"[ \t\u00a0\u200b]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def load_documents(root=None):
    """加载 md / txt。传 root 就读目录，不传就用内置示例（离线可跑、零依赖）。"""
    if root and os.path.isdir(root):
        docs = []
        for dirpath, _dirs, names in os.walk(root):
            for name in sorted(names):
                if name.lower().endswith((".md", ".txt")):
                    path = os.path.join(dirpath, name)
                    with io.open(path, encoding="utf-8", errors="replace") as handle:
                        docs.append({"id": name, "title": name, "text": clean_text(handle.read()), "path": path})
        if docs:
            return docs
    return [dict(doc, text=clean_text(doc["text"])) for doc in SAMPLE_DOCS]


# ============================================================================
# 三、切片：三种策略（固定长度 / 递归字符 / 按标题结构）
# ============================================================================

def chunk_fixed(text, chunk_size=200, chunk_overlap=40, source="", separators=("\n\n", "\n", "。")):
    """固定长度切片：按字数切，尽量在分隔符处断开，相邻块重叠 chunk_overlap 字。"""
    chunks, start = [], 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        if end < len(text):
            for sep in separators:
                cut = text.rfind(sep, start + chunk_size // 2, end)
                if cut != -1:
                    end = cut + len(sep)
                    break
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(text):
            break
        start = max(end - chunk_overlap, start + 1)   # 回退 overlap，且保证不原地打转
    return [{"text": piece, "path": source, "source": source} for piece in chunks]


def chunk_recursive(text, chunk_size=300, chunk_overlap=40, source=""):
    """
    递归字符切片：按 段落 → 行 → 句子 → 硬切 的优先级逐级降级。

    为什么它是"默认首选"：它尽量保住段落（最自然的语义边界），只在装不下时才降级到更细的粒度，
    而且块长可控。三个降级层级各切出多少块，函数会打在 stats 里 —— "降级发生过几次"是判断
    块长参数是否合适的直接证据。
    """
    separators = ["\n\n", "\n", "。", ""]
    level_names = {"\n\n": "段落", "\n": "行", "。": "句子", "": "硬切"}
    stats = {name: 0 for name in level_names.values()}

    def _split(block, level):
        block = block.strip()
        if not block:
            return []
        if len(block) <= chunk_size:
            stats[level_names[separators[min(level, len(separators) - 1)]]] += 1   # 这一块在"当前粒度"就装得下
            return [block]
        if level >= len(separators) - 1:
            stats["硬切"] += 1
            return [block[i:i + chunk_size] for i in range(0, len(block), chunk_size)]
        sep = separators[level]
        pieces, buffer = [], ""
        for part in block.split(sep):
            candidate = (buffer + sep + part) if buffer else part
            if len(candidate) <= chunk_size:
                buffer = candidate
                continue
            if buffer:
                pieces.append(buffer)
                stats[level_names[sep]] += 1
            if len(part) > chunk_size:
                pieces.extend(_split(part, level + 1))     # 装不下 → 降级到更细的分隔符
                buffer = ""
            else:
                buffer = part
        if buffer:
            pieces.append(buffer)
            stats[level_names[sep]] += 1
        return pieces

    pieces = _split(text, 0)
    chunks = []
    for index, piece in enumerate(pieces):
        # 重叠：把上一块的尾部贴到当前块前面（简单实现，够用于教学）
        prefix = pieces[index - 1][-chunk_overlap:] if index and chunk_overlap else ""
        chunks.append({"text": (prefix + piece).strip(), "path": source, "source": source})
    chunk_recursive.last_stats = stats
    return chunks


def chunk_by_heading(text, max_chars=600, source=""):
    """
    按 Markdown 标题切：一个标题下的内容为一块，并带上标题路径（面包屑）。

    标题路径有两个用处：拼进块提高检索准确率，以及给用户看引用来源
    （「差旅制度 > 3. 住宿报销标准」比 chunk_17 有用得多）。
    """
    chunks, path_stack = [], []
    for block in re.split(r"\n(?=#{1,3}\s)", text):
        block = block.strip()
        if not block:
            continue
        match = re.match(r"(#{1,3})\s+(.+)", block)
        if match:
            level = len(match.group(1))
            path_stack = [item for item in path_stack if item[0] < level] + [(level, match.group(2).strip())]
        path = " > ".join(title for _level, title in path_stack)
        if len(block) <= max_chars:
            chunks.append({"text": block, "path": path, "source": source})
        else:
            for piece in chunk_fixed(block, chunk_size=max_chars, chunk_overlap=60, source=path):
                piece["path"] = path
                chunks.append(piece)
    return chunks


# ============================================================================
# 四、向量化与关键词检索（两条路都实现，混合起来用）
# ============================================================================

def tokenize(text: str):
    """
    中文分词：装了 jieba 就用（pip install jieba），没装就退回字符二元组。

    二元组不需要词典，对中文的召回效果出乎意料地好（"海外出差" → 海外/外出/出差），
    代价是会产生一点噪声词。降级路径会打印出来，你随时知道自己跑在哪条路上。
    """
    try:
        import jieba  # noqa: F401  （可选依赖）
        return [word for word in jieba.lcut((text or "").lower()) if word.strip()]
    except ImportError:
        stripped = re.sub(r"\s+", "", (text or "").lower())
        return [stripped[i:i + 2] for i in range(max(len(stripped) - 1, 1))]


def tokenize_backend() -> str:
    try:
        import jieba  # noqa: F401
        return "jieba 分词"
    except ImportError:
        return "字符二元组（未安装 jieba，降级路径）"


class BM25:
    """
    BM25：k1 控制词频饱和（一个词出现 10 次不该是出现 1 次的 10 倍），b 控制长度归一化。

    它比"数关键词个数"强在两点：
      · IDF 让"的/是/在"这类到处都有的词几乎不计分，让"报销/日元"这类稀有词权重变高；
      · 长度归一化避免长文档靠"字多"占便宜。
    公式：score(q,d) = Σ IDF(w) · f(w,d)·(k1+1) / (f(w,d) + k1·(1-b+b·|d|/avgdl))
    其中 IDF(w) = ln( (N - df(w) + 0.5) / (df(w) + 0.5) + 1 )
    """

    def __init__(self, docs, k1=1.5, b=0.75):
        from collections import Counter
        self.k1, self.b = k1, b
        self.counter = Counter
        self.docs = [tokenize(doc) for doc in docs]
        self.avgdl = sum(len(doc) for doc in self.docs) / float(max(len(self.docs), 1))
        self.freqs = [Counter(doc) for doc in self.docs]
        df = Counter(word for doc in self.docs for word in set(doc))
        self.idf = {word: math.log((len(self.docs) - count + 0.5) / (count + 0.5) + 1)
                    for word, count in df.items()}

    def score(self, query, index):
        doc_len, total = len(self.docs[index]), 0.0
        for word in tokenize(query):
            freq = self.freqs[index].get(word, 0)
            if not freq:
                continue
            total += self.idf.get(word, 0.0) * freq * (self.k1 + 1) / (
                freq + self.k1 * (1 - self.b + self.b * doc_len / self.avgdl))
        return total

    def search(self, query, top_k=20):
        scored = [(index, self.score(query, index)) for index in range(len(self.docs))]
        scored.sort(key=lambda pair: -pair[1])
        return [(index, score) for index, score in scored[:top_k] if score > 0]


def ngram_features(text: str):
    """字符 n-gram 特征：中文按 1/2/3-gram，英文按词。它不认识"意思"，只认识"字面"。"""
    text = (text or "").lower()
    features = []
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        features.extend(run)
        features.extend(run[i:i + 2] for i in range(len(run) - 1))
    features.extend(re.findall(r"[a-z0-9\.]{2,}", text))
    return features


class Embedder:
    """
    向量化后端。两条路，自动选，并且**必须打印出来你跑在哪条路上**：

      ① 在线模式且配了 Key → 调 /embeddings 接口拿真向量（语义能力最强，同义改写也能命中）
      ② 否则 → 字符 n-gram 的哈希向量 + 余弦相似度（**降级方案**：只能匹配字面相似度，
         "出差住酒店最多花多少" vs "住宿标准" 这种改写它就无能为力了）

    ⚠️ 铁律：文档和查询必须用同一个模型（同一版本）编码 —— 换了 embedding 模型必须全量重建索引。
    所以维度与模型名会写进索引元数据，检索前校验。
    """

    DIM = 512

    def __init__(self):
        self.model_name = "char-ngram-hash"
        self.backend = "ngram"
        self.use_numpy = False
        try:
            import numpy  # noqa: F401  （可选依赖：只影响速度，不影响结果）
            self.use_numpy = True
        except ImportError:
            self.use_numpy = False
        self.api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        self.base_url = (os.environ.get("OPENAI_BASE_URL", "").strip() or "").rstrip("/")
        self.api_model = os.environ.get("OPENAI_EMBEDDING_MODEL", "").strip() or "text-embedding-3-small"

    def describe(self) -> str:
        if self.backend == "api":
            return "真向量（{} @ {}）".format(self.api_model, self.base_url)
        calc = "numpy 加速" if self.use_numpy else "纯 Python 循环"
        return "字符 n-gram 哈希向量（降级方案：没有 embedding API 时只能用字面相似度）｜矩阵计算：{}".format(calc)

    def maybe_upgrade_to_api(self) -> None:
        """在线模式下尝试升级到真 embedding；失败就降级并说明原因（不静默）。"""
        if is_mock_mode() or not self.api_key or not self.base_url:
            return
        probe = self._call_api("连通性测试")
        if probe:
            self.backend = "api"
            self.DIM = len(probe)

    def _call_api(self, text: str):
        import urllib.error
        import urllib.request
        payload = json.dumps({"model": self.api_model, "input": [text]}).encode("utf-8")
        request = urllib.request.Request("{}/embeddings".format(self.base_url), data=payload, method="POST",
                                         headers={"Content-Type": "application/json",
                                                  "Authorization": "Bearer {}".format(self.api_key)})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
            return data["data"][0]["embedding"]
        except Exception as exc:  # noqa: BLE001 - 任何失败都降级，不影响整条链路
            print("[向量化降级] 调用 embedding 接口失败（{}），改用字符 n-gram 降级方案".format(exc))
            return None

    def embed(self, text: str):
        if self.backend == "api":
            vector = self._call_api(text)
            if vector:
                return list(vector)
            self.backend = "ngram"   # 中途失败也要降级，而不是抛异常
        return self._ngram_vector(text)

    def _ngram_vector(self, text: str):
        vector = [0.0] * self.DIM
        for feature in ngram_features(text):
            vector[zlib.crc32(feature.encode("utf-8")) % self.DIM] += 1.0
        norm = math.sqrt(sum(value * value for value in vector)) + 1e-9
        return [value / norm for value in vector]   # 进来就归一化：之后点积就等于余弦


class VectorIndex:
    """极简向量索引：内存矩阵 + 暴力余弦检索。几千条 chunk 用它就够了，不需要向量数据库。"""

    def __init__(self, model_name="char-ngram-hash", dim=None, use_numpy=False):
        self.model_name = model_name
        self.dim = dim
        self.use_numpy = use_numpy
        self.vectors = []
        self.metas = []
        self._matrix = None
        self._numpy = None
        if use_numpy:
            try:
                import numpy
                self._numpy = numpy
            except ImportError:
                self.use_numpy = False

    def add(self, vector, meta):
        self.vectors.append(list(vector))
        self.metas.append(meta)

    def build(self):
        if self.use_numpy and self._numpy is not None and self.vectors:
            self._matrix = self._numpy.asarray(self.vectors, dtype="float32")

    def search(self, query_vector, top_k=20):
        norm = math.sqrt(sum(value * value for value in query_vector)) + 1e-9
        query = [value / norm for value in query_vector]
        if self._matrix is not None:
            scores = self._matrix.dot(self._numpy.asarray(query, dtype="float32"))
            order = scores.argsort()[::-1][:top_k]
            return [(int(index), float(scores[index])) for index in order]
        scored = [(index, sum(a * b for a, b in zip(query, vector))) for index, vector in enumerate(self.vectors)]
        scored.sort(key=lambda pair: -pair[1])
        return scored[:top_k]


# ============================================================================
# 五、融合（RRF）与重排（词面重排）
# ============================================================================

def rrf_fuse(rank_lists, k=60, top_k=20):
    """
    RRF（倒数排名融合）：score(d) = Σ 1 / (k + rank_i(d))。只用**排名**，天然免疫分数量纲。

    为什么不用"归一化后加权求和"？因为余弦是 0~1、BM25 是 0~∞，min-max 归一化会随每次查询的
    分数分布抖动。RRF 的价值观是：在两路里都稳定靠前，胜过在一路里当冠军、另一路里失踪。
    """
    fused = {}
    for ranks in rank_lists:
        for position, (doc_id, _score) in enumerate(ranks):
            fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (k + position + 1)
    return sorted(fused.items(), key=lambda pair: -pair[1])[:top_k]


def lexical_rerank(query, candidates, w_title=2.0, max_len=1200):
    """
    零依赖重排：查询词覆盖率 + 标题命中加权 + 长度惩罚。

    它比 cross-encoder 弱（不理解语义），但比不做好：召回阶段用的是双塔/字面匹配，
    候选里常常混着"沾一点边"的块，用"这块到底覆盖了问题里的几个词"重排一遍，top-k 会干净很多。
    """
    query_terms = set(tokenize(query))
    rescored = []
    for chunk, meta, _score in candidates:
        chunk_terms = set(tokenize(chunk))
        cover = len(query_terms & chunk_terms) / float(max(len(query_terms), 1))
        title_hit = len(query_terms & set(tokenize(meta.get("path", "")))) > 0
        penalty = 1.0 if len(chunk) < max_len else 0.8
        rescored.append((round((cover + (w_title if title_hit else 0.0) * 0.5) * penalty, 4), chunk, meta))
    rescored.sort(key=lambda item: -item[0])
    return rescored


def trim_by_relative_score(hits, ratio=0.85):
    """相对阈值：以最高分为基准砍掉明显掉队的块。绝对阈值在不同 embedding 模型间不可移植。"""
    if not hits:
        return []
    return [item for item in hits if item[1] >= ratio * hits[0][1]]


# ============================================================================
# 六、带引用的生成与校验
# ============================================================================

RAG_PROMPT = """你是资料问答助手。只能依据下面的【资料】回答，不允许使用资料之外的知识。

规则：
1. 每个事实性结论后必须标注来源编号，格式 [1]、[2]；一句话用到两段资料就写 [1][3]。
2. 【资料】里没有足够信息时，必须直接回答"{refuse}"，禁止推测、禁止编造。
3. 引用编号只能来自【资料】里出现过的编号，不要创造新编号。
4. 中文回答，先给结论，再给依据。

【资料】
{context}

【问题】{question}

输出格式：
<答案>（正文，含 [编号] 引用）</答案>
<引用>（每行一条：编号 | 逐字摘自资料的原文片段）</引用>
"""


def build_prompt(question: str, hits, strict: bool = False) -> str:
    lines = []
    for hit in hits:
        lines.append("[{}] （{} > {}）\n{}".format(hit["cite_id"], hit["source"], hit["path"], hit["text"]))
    prompt = RAG_PROMPT.format(context="\n\n".join(lines) or "（无检索结果）", question=question, refuse=REFUSE_TEXT)
    if strict:
        prompt += "\n补充要求：上一次回答的引用校验没通过。请只使用资料里真实出现的编号，" \
                  "并且引用的原文片段必须逐字抄自对应资料。若无法做到，就直接回答「{}」。".format(REFUSE_TEXT)
    return prompt


def split_sentences(text: str):
    return [piece.strip() for piece in re.split(r"[。！？\n]", text or "") if piece.strip()]


def extract_quote(answer: str, cite_id: str, hits):
    """从回答里抽出该编号对应的引用片段：优先用 <引用> 段的「编号 | 原文」，其次退回含该编号的句子。"""
    block = re.search(r"<引用>(.*?)</引用>", answer or "", re.S)
    if block:
        for line in block.group(1).splitlines():
            line = line.strip().lstrip("-").strip()
            if not line:
                continue
            parts = re.split(r"[|｜]", line, 1)
            if len(parts) == 2 and parts[0].strip().strip("[]") == cite_id:
                return parts[1].strip()
    for sentence in re.split(r"[。\n]", answer or ""):
        if "[{}]".format(cite_id) in sentence:
            return sentence.strip()
    return ""


def ratio_in(quote: str, source: str) -> float:
    """用"字符集合重合度"近似"引用片段是否出自原文"，避免 difflib 在大文本上的慢匹配。"""
    quote_chars = set(re.sub(r"\s+", "", quote or ""))
    source_chars = set(re.sub(r"\s+", "", source or ""))
    if not quote_chars:
        return 0.0
    return len(quote_chars & source_chars) / float(len(quote_chars))


def validate_citations(raw_answer, cited_ids, retrieved, min_overlap=0.8):
    """
    三道机械校验（顺序即严重程度）：

      ① 编号存在：引用的编号必须真的在【资料】里（模型最常见的幻觉就是造一个 [7]）
      ② 范围正确：编号必须落在组装时给出的编号区间 [1..K] 内
      ③ 字面依据：引用片段与对应块原文的字符重合度 >= min_overlap

    只要求"标注引用"、不做校验，等于没做 —— 带 [3] 的答案看起来很有说服力，
    但 [3] 指向的块可能压根没被检索回来。

    注意第一个参数是**模型的原始输出**（含 <答案> 与 <引用> 两段），不是解析后的答案正文 ——
    引用片段在 <引用> 段里，拿答案正文去核原文是核不到的。
    """
    answer = (raw_answer or "")
    problems = []
    by_id = {hit["cite_id"]: hit for hit in retrieved}
    legal = {hit["cite_id"] for hit in retrieved}
    if not cited_ids and retrieved and not must_refuse(answer):
        # 最容易漏掉的一条：答案里一个引用都没有，"看起来对"但完全无法追溯
        problems.append("答案里没有任何引用编号，无法追溯来源")
    for cite_id in cited_ids:
        raw = str(cite_id).strip().strip("[]")
        if not raw.isdigit():
            problems.append("引用编号不是数字：{}".format(cite_id))
            continue
        if not (1 <= int(raw) <= len(retrieved)):
            problems.append("引用 [{}] 超出资料编号范围 1..{}（编号是模型自己造的）".format(raw, len(retrieved)))
            continue
        if raw not in legal:
            problems.append("引用了资料里不存在的编号 [{}]".format(raw))
            continue
        quote = extract_quote(answer, raw, retrieved)
        if quote:
            overlap = ratio_in(quote, by_id[raw]["text"])
            if overlap < min_overlap:
                problems.append("编号 [{}] 的引用片段与原文重合度只有 {:.2f}（低于 {:.2f}）：{}".format(
                    raw, overlap, min_overlap, quote[:40]))
        else:
            problems.append("编号 [{}] 没有给出可核对的引用片段".format(raw))
    return problems


def parse_answer(raw: str):
    """解析 <答案> / <引用> 两段输出，并抽出引用编号（两种格式都认：[1] 与「1 | 原文」）。"""
    match = re.search(r"<答案>(.*?)</答案>", raw or "", re.S)
    answer = match.group(1).strip() if match else (raw or "").strip()
    block = re.search(r"<引用>(.*?)</引用>", raw or "", re.S)
    cited = re.findall(r"\[(\d+)\]", raw or "")
    if block:
        for line in block.group(1).splitlines():
            head = re.split(r"[|｜]", line.strip().lstrip("-").strip(), 1)[0]
            cited.extend(re.findall(r"\b(\d+)\b", head))
    seen = []
    for item in cited:
        if item not in seen:
            seen.append(item)
    return answer, seen


def must_refuse(answer: str) -> bool:
    return any(keyword in (answer or "") for keyword in ("没有找到", "无法回答", "资料中未提及", "没有相关内容"))


#: 离线模拟回答器的"通用词"清单：这些词在文档里到处都是，不能用来判断"资料里有没有答案"
GENERIC_TERMS = {"公司", "员工", "每年", "多少", "什么", "怎么", "几天", "可以", "是否", "我们", "一个",
                 "这个", "那个", "上限", "标准", "规定", "要求", "多久", "哪些", "每天", "小时", "工作日",
                 "支持", "需要", "以下"}


def mock_answer(question: str, hits, idf=None, min_hits=2, max_sentences=3):
    """
    离线模式的"模型"：由检索结果拼出一个带引用的答案。

    为什么要这么写，而不是直接返回一句写死的话？因为只有让"生成"这一步真的依赖检索结果，
    离线跑才能真实验证 生成 → 校验 → 拒答 这条链路（这正是本章最有价值的一段代码）。

    判据：问题的**内容词**（去掉通用词之后）至少 min_hits 个出现在检索到的块里，才作答；
    否则返回标准拒答句 —— 真实系统里这件事由 prompt + 引用校验共同保证。
    句子优先级用 IDF 加权（稀有词命中 > 常见词命中），这样"海外"会压过"出差"。
    """
    idf = idf or {}
    terms = [term for term in tokenize(question) if term not in GENERIC_TERMS and len(term) > 1]
    top_texts = "\n".join(hit["text"] for hit in hits[:2])
    matched = [term for term in terms if term in top_texts]
    if not hits or len(matched) < min_hits:
        return "<答案>{}</答案>\n<引用>（无）</引用>".format(
            "{}（离线模拟回答器：问题的内容词只有 {}/{} 出现在资料里，判定为资料中没有答案）".format(
                REFUSE_TEXT, len(matched), len(terms)))

    picks, covered = [], set()
    for hit in hits[:3]:
        sentences = [s for s in split_sentences(hit["text"]) if len(s) >= 8 and not s.lstrip().startswith("#")]
        scored = sorted(((sum(idf.get(term, 1.0) for term in terms if term in sentence), sentence)
                         for sentence in sentences), key=lambda pair: -pair[0])
        taken = 0
        for score, sentence in scored:
            if score <= 0:
                break
            new_terms = {term for term in terms if term in sentence} - covered
            if new_terms or not picks:            # 第一句无条件取，之后只取"能带来新内容词"的句子
                picks.append((hit["cite_id"], sentence))
                covered |= new_terms
                taken += 1
            if taken >= 2 or len(picks) >= max_sentences:
                break
        if len(picks) >= max_sentences:
            break

    body = "；".join("{} [{}]".format(sentence.rstrip("。"), cite_id) for cite_id, sentence in picks)
    quotes = "\n".join("{} | {}".format(cite_id, sentence.rstrip("。")) for cite_id, sentence in picks)
    return "<答案>根据资料，{}。</答案>\n<引用>\n{}\n</引用>".format(body, quotes)


# ============================================================================
# 七、RAG 主流程
# ============================================================================

class RagPipeline:
    """一条完整的 RAG 链路：切片 → 索引 → 召回 → 融合 → 重排 → 生成 → 校验。"""

    def __init__(self, docs, chunker="heading", chunk_size=300, embedder=None, name="heading"):
        self.name = name
        self.chunker = chunker
        self.embedder = embedder or Embedder()
        self.chunks = self._build_chunks(docs, chunker, chunk_size)
        self.texts = [self._index_text(chunk) for chunk in self.chunks]
        self.bm25 = BM25(self.texts)
        self.index = VectorIndex(model_name=self.embedder.model_name, dim=self.embedder.DIM,
                                 use_numpy=self.embedder.use_numpy)
        for text, chunk in zip(self.texts, self.chunks):
            self.index.add(self.embedder.embed(text), chunk)
        self.index.build()
        self.trace = Trace("rag_{}".format(name))

    @staticmethod
    def _index_text(chunk):
        # 把标题路径拼进被索引的文本：它既是检索特征，也是引用来源
        return "{} {}".format(chunk.get("path", ""), chunk["text"]).strip()

    def _build_chunks(self, docs, chunker, chunk_size):
        chunks = []
        for doc in docs:
            if chunker == "fixed":
                pieces = chunk_fixed(doc["text"], chunk_size=chunk_size, chunk_overlap=60, source=doc["title"])
            elif chunker == "recursive":
                pieces = chunk_recursive(doc["text"], chunk_size=chunk_size + 100, source=doc["title"])
            else:
                pieces = chunk_by_heading(doc["text"], max_chars=chunk_size + 300, source=doc["title"])
            chunks.extend(pieces)
        return chunks

    # ---------------------------------------------------------------- 检索

    def keyword_search(self, query, top_k=20):
        return self.bm25.search(query, top_k=top_k)

    def vector_search(self, query, top_k=20):
        hits = self.index.search(self.embedder.embed(query), top_k=top_k)
        # 相对阈值：以最高分为基准砍掉明显掉队的块。绝对阈值在不同 embedding 模型间不可移植
        # （有的模型"所有文本相似度都在 0.6 以上"），相对阈值才稳。
        # 这里 ratio 取 0.3 比较宽松：召回阶段宁可多留一点的噪音，精度交给后面的重排。
        return trim_by_relative_score(hits, ratio=0.3)

    def retrieve(self, query, top_k=4, use_vector=True, use_keyword=True, use_rrf=True, use_rerank=True,
                 verbose=False):
        rank_lists = []
        vector_hits = self.vector_search(query) if use_vector else []
        keyword_hits = self.keyword_search(query) if use_keyword else []
        if use_vector:
            rank_lists.append(vector_hits)
        if use_keyword:
            rank_lists.append(keyword_hits)
        if use_rrf and len(rank_lists) > 1:
            fused = rrf_fuse(rank_lists, k=60, top_k=20)
        elif rank_lists:
            fused = rank_lists[0][:20]
        else:
            fused = []

        candidates = []
        for doc_id, score in fused:
            meta = dict(self.chunks[doc_id])
            meta["_doc_id"] = doc_id
            candidates.append((self.chunks[doc_id]["text"], meta, score))

        if use_rerank and candidates:
            ranked = lexical_rerank(query, candidates)          # [(重排分, 块正文, 块元数据)]
        else:
            ranked = [(score, chunk, meta) for chunk, meta, score in candidates]

        hits = []
        for position, (score, text, meta) in enumerate(ranked[:top_k], start=1):
            hits.append({
                "cite_id": str(position),
                "text": text,
                "path": meta.get("path", ""),
                "source": meta.get("source", ""),
                "rerank_score": score,
                "doc_id": meta.get("_doc_id"),
                "vector_rank": next((pos for pos, (i, _s) in enumerate(vector_hits, start=1)
                                     if i == meta.get("_doc_id")), None),
                "keyword_rank": next((pos for pos, (i, _s) in enumerate(keyword_hits, start=1)
                                      if i == meta.get("_doc_id")), None),
            })
        if verbose:
            print("  [候选] 向量 {} 条 / BM25 {} 条 → RRF 融合 {} 条 → 重排后取 top-{}".format(
                len(vector_hits), len(keyword_hits), len(fused), len(hits)))
        return hits

    # ---------------------------------------------------------------- 生成

    def answer(self, question, hits, strict=False, force_llm=False):
        """生成带引用的回答。离线模式用本地模拟回答器（真的依赖检索结果）；在线模式调真模型。"""
        prompt = build_prompt(question, hits, strict=strict)
        if is_mock_mode() and not force_llm:
            return mock_answer(question, hits, idf=self.bm25.idf), prompt
        return self.llm_chat(prompt), prompt

    def llm_chat(self, prompt):
        llm = get_llm(system="你是资料问答助手，只依据资料回答，并标注来源编号。", temperature=0.1)
        return llm.chat([{"role": "user", "content": prompt}])["content"]

    def ask(self, question, top_k=4, max_retry=1, force_llm=False, scripted=None, verbose=False):
        """
        完整问答：检索 → 生成 → 校验 → （不通过就带补充要求重试一次）→ 仍然不通过就拒答。

        "宁可拒答，也不要给出无法追溯的答案" —— 这条降级链路是企业场景的底线。
        """
        hits = self.retrieve(question, top_k=top_k, verbose=verbose)
        history = []
        for attempt in range(max_retry + 1):
            if scripted is not None and is_mock_mode():
                set_mock_script([{"content": scripted[attempt if attempt < len(scripted) else -1], "tool_calls": []}])
                raw = self.answer(question, hits, strict=attempt > 0, force_llm=True)[0]
            else:
                raw = self.answer(question, hits, strict=attempt > 0)[0]
            answer, cited = parse_answer(raw)
            # 校验必须拿**原始输出**：引用片段写在 <引用> 段里，只看答案正文就核不了原文
            problems = validate_citations(raw, cited, hits)
            history.append({"attempt": attempt + 1, "answer": answer, "cited": cited, "problems": problems})
            if not problems:
                return {"answer": answer, "cited_ids": cited, "hits": hits, "problems": [],
                        "refused": must_refuse(answer), "attempts": attempt + 1, "history": history}
        return {"answer": REFUSE_TEXT, "cited_ids": [], "hits": hits, "problems": history[-1]["problems"],
                "refused": True, "attempts": max_retry + 1, "history": history}


# ============================================================================
# 八、mini 评估
# ============================================================================

def mini_eval(rag: RagPipeline, eval_set=None, top_k=5) -> dict:
    """离线可跑的最小评估：检索指标 + 引用准确率 + 拒答正确率 + bad case 四分类。"""
    stat = {"n": 0, "gold_total": 0, "recall": 0.0, "hit": 0, "mrr": 0.0, "answer_ok": 0, "answer_total": 0,
            "cite_ok": 0, "cite_total": 0, "refuse_ok": 0, "refuse_total": 0, "rows": []}
    bad_cases = []
    for item in (eval_set or EVAL_SET):
        stat["n"] += 1
        hits = rag.retrieve(item["q"], top_k=top_k, verbose=False)
        texts = [hit["text"] + " " + hit["path"] for hit in hits]
        recall, first_rank = None, None

        if item["gold"]:
            stat["gold_total"] += 1
            found_groups = 0
            for group in item["gold"]:
                hit_rank = None
                for position, text in enumerate(texts, start=1):
                    if any(keyword in text for keyword in group):
                        hit_rank = position
                        break
                if hit_rank:
                    found_groups += 1
                    first_rank = hit_rank if first_rank is None else min(first_rank, hit_rank)
            recall = found_groups / float(len(item["gold"]))
            stat["recall"] += recall
            stat["hit"] += 1 if found_groups else 0
            stat["mrr"] += (1.0 / first_rank) if first_rank else 0.0
            if not found_groups:
                bad_cases.append({"type": "检索未召回", "q": item["q"],
                                  "detail": "gold 块一个都没进 top-{}".format(top_k)})
            elif first_rank and first_rank > 2:
                bad_cases.append({"type": "切片问题", "q": item["q"],
                                  "detail": "gold 块排在第 {} 位（切得太碎或路径没带上）".format(first_rank)})

        result = rag.ask(item["q"], top_k=top_k)
        stat["cite_total"] += 1
        stat["cite_ok"] += 1 if not result["problems"] else 0
        if result["problems"]:
            bad_cases.append({"type": "模型瞎编", "q": item["q"], "detail": "；".join(result["problems"][:2])})

        verdict = ""
        if item["must_refuse"]:
            stat["refuse_total"] += 1
            ok = must_refuse(result["answer"])
            stat["refuse_ok"] += 1 if ok else 0
            verdict = "拒答正确" if ok else "该拒答却硬答"
            if not ok:
                bad_cases.append({"type": "模型瞎编", "q": item["q"], "detail": "该拒答却硬答"})
        elif item["expect"]:
            stat["answer_total"] += 1
            missing = [keyword for keyword in item["expect"] if keyword not in result["answer"]]
            stat["answer_ok"] += 1 if not missing else 0
            verdict = "答案含关键数字" if not missing else "缺 {}".format(missing)
            if missing:
                bad_cases.append({"type": "召回了没用", "q": item["q"], "detail": "答案缺 {}".format(missing)})
        stat["rows"].append({"q": item["q"], "recall": recall, "rank": first_rank,
                             "answer": result["answer"], "verdict": verdict,
                             "problems": result["problems"]})

    n = float(max(stat["n"], 1))
    golds = float(max(stat["gold_total"], 1))
    return {
        # 检索指标只在"有标准答案"的题上算：无答案题不参与 Recall/Hit/MRR
        "recall@k": stat["recall"] / golds,
        "hit@k": stat["hit"] / golds,
        "mrr": stat["mrr"] / golds,
        "answer_ok": stat["answer_ok"] / float(max(stat["answer_total"], 1)),
        "cite_ok": stat["cite_ok"] / float(max(stat["cite_total"], 1)),
        "refuse_ok": (stat["refuse_ok"] / float(stat["refuse_total"])) if stat["refuse_total"] else None,
        "bad_cases": bad_cases,
        "per_question": stat["rows"],
    }


def classify_bad_cases(bad_cases):
    """把 bad case 归到四类里：切片问题 / 检索未召回 / 召回了没用 / 模型瞎编。

    没有分类的「效果不好」是无法行动的 —— 四类的修法完全不同（改切法 / 加混合检索 / 改 prompt / 加校验）。
    """
    groups = {"切片问题": [], "检索未召回": [], "召回了没用": [], "模型瞎编": []}
    for case in bad_cases:
        groups.setdefault(case["type"], []).append(case)
    return groups


# ============================================================================
# 九、跑起来
# ============================================================================

def report_chunking(docs):
    """三种切片的对比：块数、块长分布，以及"同一批问题下 gold 块排第几"。"""
    section("第 3 节 · 切片：三种策略对比（最便宜的优化点）")
    for name, chunker in (("固定长度 200 字", "fixed"), ("递归字符 400 字", "recursive"), ("按标题结构", "heading")):
        pieces = []
        for doc in docs:
            if chunker == "fixed":
                pieces.extend(chunk_fixed(doc["text"], chunk_size=200, source=doc["title"]))
            elif chunker == "recursive":
                pieces.extend(chunk_recursive(doc["text"], chunk_size=400, source=doc["title"]))
            else:
                pieces.extend(chunk_by_heading(doc["text"], max_chars=600, source=doc["title"]))
        lengths = [len(piece["text"]) for piece in pieces]
        print("  [{:<14}] {:>2} 块，平均 {:>4.0f} 字，最短 {} 字，最长 {} 字".format(
            name, len(pieces), sum(lengths) / float(len(lengths)), min(lengths), max(lengths)))
    # 逐级降级要看得到：把块长压到 150 字，段落装不下 → 降级到行 → 再降级到句子
    small = chunk_recursive(docs[0]["text"], chunk_size=150, source=docs[0]["title"])
    print("  [递归降级统计] 块长 150 字时（《{}》，共 {} 块）：{}".format(
        docs[0]["title"], len(small), chunk_recursive.last_stats))
    print("       含义：块优先在段落断开；段落装不下才降级到行、句子，最后才是硬切字符。")
    print("       如果「硬切」占比很高，说明 chunk_size 相对你的段落长度太小了。")

    print("\n  [同一切片的块示例] 按标题切，注意每块都带标题路径（既能提召回，也是引用来源）：")
    for piece in chunk_by_heading(docs[0]["text"], max_chars=600, source=docs[0]["title"])[1:3]:
        print("     路径：{}".format(piece["path"]))
        print("     正文：{}".format(piece["text"][:70].replace("\n", " ")))

    print("\n  [切片好坏怎么判断] 同一批问题、同一套检索，只看 gold 块排第几：")
    probes = [item for item in EVAL_SET if item["gold"]][:4]
    table = {}
    for name, chunker, size in (("固定 200 字", "fixed", 200), ("递归 300 字", "recursive", 300), ("按标题", "heading", 300)):
        rag = RagPipeline(docs, chunker=chunker, chunk_size=size, name="cmp_" + chunker)
        ranks = []
        for item in probes:
            hits = rag.retrieve(item["q"], top_k=5, verbose=False)
            rank = None
            for position, hit in enumerate(hits, start=1):
                text = hit["text"] + " " + hit["path"]
                if any(keyword in text for group in item["gold"] for keyword in group):
                    rank = position
                    break
            ranks.append(rank if rank else "未召回")
        table[name] = ranks
    print("     {:<12}{}".format("切法", "gold 块在 Top-5 里的排名（4 个问题）"))
    for name, ranks in table.items():
        print("     {:<12}{}".format(name, ranks))
    print("     结论：块被切碎时，答案常常落在语义不完整的小块里、排名掉出 Top-K —— 这就是「检索没召回」")
    print("     最常见的成因，而修它只需要换一种切法（零 API 成本）。")


def report_retrieval(rag: RagPipeline):
    """三路检索对比 + RRF 融合前后的排名 + 重排前后的 Top-K。"""
    section("第 4-5 节 · 检索：向量 / BM25 / RRF 融合 / 重排")
    print("  分词后端：{}".format(tokenize_backend()))
    print("  向量后端：{}（模型 {}，维度 {}）".format(rag.embedder.describe(), rag.embedder.model_name, rag.embedder.DIM))
    query = "海外出差住宿上限是多少？"
    print("\n  查询：{}".format(query))
    vector_hits = rag.vector_search(query, top_k=5)
    keyword_hits = rag.keyword_search(query, top_k=5)
    fused = rrf_fuse([vector_hits, keyword_hits], k=60, top_k=5)

    def label(index):
        chunk = rag.chunks[index]
        return "{}｜{}".format(chunk.get("source", ""), chunk["text"][:26].replace("\n", " "))

    print("\n  [向量 top-5]")
    for position, (index, score) in enumerate(vector_hits, start=1):
        print("     {}. {:.3f}  {}".format(position, score, label(index)))
    print("  [BM25 top-5] （BM25 分数没有固定上界，只用排名、不用分数比大小）")
    for position, (index, score) in enumerate(keyword_hits, start=1):
        print("     {}. {:.2f}   {}".format(position, score, label(index)))
    print("  [RRF 融合 top-5] （k=60，只用排名）")
    for position, (index, score) in enumerate(fused, start=1):
        print("     {}. {:.4f}  {}".format(position, score, label(index)))

    print("\n  [融合前后的排名变化]（排名变化就是 RRF 在做的事：两路都靠前的块会往上走）")
    vector_rank = {index: position for position, (index, _s) in enumerate(vector_hits, start=1)}
    keyword_rank = {index: position for position, (index, _s) in enumerate(keyword_hits, start=1)}
    fused_rank = {index: position for position, (index, _s) in enumerate(fused, start=1)}
    print("     {:<10}{:>10}{:>10}{:>10}".format("块", "向量名次", "BM25 名次", "融合名次"))
    for index in list(fused_rank)[:5]:
        print("     {:<10}{:>10}{:>10}{:>10}".format(
            label(index)[:8], vector_rank.get(index, "-"), keyword_rank.get(index, "-"), fused_rank[index]))

    candidates = []
    for index, score in fused:
        meta = dict(rag.chunks[index])
        meta["_doc_id"] = index
        candidates.append((rag.chunks[index]["text"], meta, score))
    reranked = lexical_rerank(query, candidates)
    print("\n  [重排前后 Top-K 变化]（词面重排 = 查询词覆盖率 + 标题命中 + 长度惩罚；")
    print("   真实项目里这一层换成 cross-encoder，能把 nDCG@5 再抬 5~15 个点）")
    print("     {:<8}{:>12}{:>12}   {}".format("融合名次", "重排名次", "重排分数", "块"))
    for position, (score, text, meta) in enumerate(reranked[:4], start=1):
        print("     {:<8}{:>12}{:>12.3f}   {}".format(
            fused_rank.get(meta["_doc_id"], "-"), position, score, text[:30].replace("\n", " ")))
    top_after = [meta["_doc_id"] for _s, _t, meta in reranked[:4]]
    moved = [index for index, (_i, _s) in enumerate(fused[:4]) if _i not in top_after[:4]]
    print("     变化：{}".format("有块被重排挤出 Top-4 —— 说明召回结果里确实混着「沾边但不相关」的块"
                                if moved else "Top-4 的成员没变，只有次序调整"))
    return rag


def report_answer(rag: RagPipeline):
    """带引用的回答 + 三道校验 + （引用幻觉 → 重试 → 拒答）的降级链路。"""
    section("第 7 节 · 带引用的回答与校验（含引用幻觉的处理）")
    question = "海外出差的住宿上限是多少？"
    print("[问题] {}".format(question))
    result = rag.ask(question, top_k=4)
    print("[命中资料] {} 块，Top-1 重排分 {:.3f}".format(len(result["hits"]), result["hits"][0]["rerank_score"]))
    print("[回答] {}".format(result["answer"]))
    print("[引用编号] {}".format(result["cited_ids"]))
    print("[引用校验] 通过（编号存在 + 范围正确 + 字面重合度 >= 0.8）")

    print("\n[演示引用幻觉] 让模型故意引用一个不存在的编号 [7]，并且引用内容与原文对不上：")
    bad = ("<答案>海外出差住宿单晚不超过等值 8000 日元 [7]，另外可以乘坐商务舱 [2]。</答案>\n"
           "<引用>\n7 | 海外出差住宿单晚上限为 8800 日元，可报销全部费用。\n2 | 总监及以上级别可乘坐商务舱。\n</引用>")
    result2 = rag.ask(question, top_k=4, max_retry=1, scripted=[bad, bad])
    for step in result2["history"]:
        print("  第 {} 次尝试：引用 {} → 校验问题 {}".format(step["attempt"], step["cited"], step["problems"]))
    print("[最终返回] {}".format(result2["answer"]))
    print("[说明] 校验不通过 → 带补充要求重试一次 → 仍不通过 → 拒答。")
    print("       宁可拒答，也不要给出无法追溯的答案 —— 这是企业场景的底线。")
    return result, result2


def report_eval(rag: RagPipeline, extra_cases):
    section("第 8 节 · mini 评估（10 条评测集，含 2 条无答案题）")
    metrics = mini_eval(rag, EVAL_SET, top_k=5)
    print("  逐题结果（问 → gold 召回 → 排名 → 答案判定）：")
    for row in metrics["per_question"]:
        if row["recall"] is None:
            recall = "拒答题"
        else:
            recall = "召回 {:.1f}".format(row["recall"]) + ("" if row["recall"] >= 1.0 else "（有 gold 没进 top-k）")
        print("     {:<26} {:>10}  排名 {:<4} {}".format(row["q"][:24], recall,
                                                         row["rank"] if row["rank"] else "-", row["verdict"]))
        print("       答案：{}".format(row["answer"][:76].replace("\n", " ")))
    print("\n  评测集：{} 条（事实型 8 条 / 无答案 2 条；检索指标只在事实型上计算）".format(len(EVAL_SET)))
    print("  Recall@5  {:.2f}    Hit@5  {:.2f}    MRR  {:.2f}".format(
        metrics["recall@k"], metrics["hit@k"], metrics["mrr"]))
    refuse_text = "无无答案题" if metrics["refuse_ok"] is None else "{:.2f}".format(metrics["refuse_ok"])
    print("  引用准确率 {:.2f}    答案关键词命中率 {:.2f}    拒答正确率 {}".format(
        metrics["cite_ok"], metrics["answer_ok"], refuse_text))
    print("  说明：离线模式下答案由本地模拟回答器生成，所以 answer_ok 只是「链路是否打通」的信号；")
    print("        Recall@K / MRR / 引用准确率 / 拒答正确率是完全可测的，它们才是离线调试的主战场。")
    print("        另外，模拟回答器只从最优的 1-3 句里拼答案，多跳题（需要跨块组合）在离线模式下会缺项 ——")
    print("        那是模拟器的局限，不是链路的 bug：在线模式下这两条会由真模型补上。")
    if is_mock_mode():
        print("       （在线模式：去掉 AGENT_MOCK 重跑，answer_ok 才有真实意义）")

    cases = metrics["bad_cases"] + extra_cases
    groups = classify_bad_cases(cases)
    print("\n  [bad case 四分类]（每类的修法完全不同，所以要分类，不能只报一个「效果不好」）")
    for kind, items in groups.items():
        print("     {:<8} {} 例".format(kind, len(items)))
        for case in items[:2]:
            print("        · {} → {}".format(case["q"], case["detail"][:70]))
    if not cases:
        print("     （没有 bad case；在线模式下这个列表通常会长得多）")
    print("     修法对照：切片问题→换切法｜检索未召回→混合检索/查询改写｜召回了没用→改 prompt 顺序｜模型瞎编→加引用校验")


def main() -> int:
    load_env()
    print_banner("第 08 章 · RAG 全链路：从切片到带引用的回答")

    docs = load_documents(os.environ.get("RAG_DOCS_DIR", "").strip() or None)
    section("第 2 节 · 加载与清洗")
    for doc in docs:
        print("  《{}》 {} 字，{} 行".format(doc["title"], len(doc["text"]), len(doc["text"].splitlines())))
    print("  注：清洗做了统一换行、去页码页眉、压空白、合并空行；文档里的答案被切成两半，多半是切片问题而不是模型问题。")

    report_chunking(docs)

    rag = RagPipeline(docs, chunker="heading", chunk_size=300, name="main")
    print("\n  [主索引] 采用「按标题结构」切片：{} 块，索引文本 = 标题路径 + 正文".format(len(rag.chunks)))
    report_retrieval(rag)

    _good, _bad = report_answer(rag)

    extra = [{"type": "模型瞎编", "q": "海外出差的住宿上限是多少？（引用幻觉演示）",
              "detail": "引用了不存在的编号 [7]，且引用片段与原文重合度不足"}]
    report_eval(rag, extra)

    section("本章要点回顾")
    for line in [
        "1. RAG 不改模型、改上下文：解决「私有 + 易变 + 要出处」的知识问题。",
        "2. 链路不能只做一半：召回决定上限（Recall@K），生成决定可信度（引用校验 + 拒答）。",
        "3. 切片是最便宜的优化点：中文从 300~500 字起调，按标题切优于按字符切，小块检索 + 大块喂模型更好。",
        "4. 向量不是唯一答案：BM25 在精确字符串上更强且零依赖；RRF 只用排名融合，天然免疫量纲问题。",
        "5. 两阶段检索（召回 → 重排）是精度与成本的平衡：先便宜地缩到 20 条，再精排到 4 条。",
        "6. 引用必须机械校验：编号存在 / 范围正确 / 字面重合度 >= 0.8，不通过就重试或拒答。",
        "7. 没有评估的 RAG 不算做完：Recall@K 与拒答正确率分开测，bad case 必须归到四类里。",
    ]:
        print("  " + line)
    print("\n完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
