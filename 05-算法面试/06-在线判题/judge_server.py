"""
本地判题服务 + LLM 代理（零第三方依赖）。

它解决两个问题：

1. **判题**：网页是静态的，跑不了 Python。这个服务在本地把用户代码跑起来，逐用例判定。
2. **LLM 代理**：网页直接调你的大模型 API 会暴露密钥、还会被浏览器跨域拦住。
   这个服务在本地转发一次 —— 密钥只存在你机器的配置文件里，网页永远碰不到。

启动：
    cd 05-算法面试/06-在线判题
    python judge_server.py                    # 默认 http://127.0.0.1:8900
    python judge_server.py --port 9000

然后在网页上点「算法面试轨道 → 做题」即可。网页会自动探测这个服务。

密钥配置（三选一，优先级从低到高）：
    1. 系统环境变量 OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL
    2. 项目里的 code/.env            ← 和前面 17 章共用同一份，配一次就行
    3. 本目录的 judge.env            ← 想单独用别的模型时写这里

只监听 127.0.0.1，局域网内其他机器访问不到。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))          # 项目根目录
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from harness import VERDICT_LABEL, load_testcases, run_all  # noqa: E402
from languages import LANGUAGE_LABELS, get_backend, language_status  # noqa: E402

#: 请求体上限：1 MB。超过直接拒绝，避免被一个畸形请求拖死。
MAX_BODY = 1024 * 1024
#: 提交代码的长度上限
MAX_CODE = 200_000
#: 判题默认超时（秒），网页可以覆盖
DEFAULT_TIMEOUT = 6.0

#: 允许跨域访问的来源。空列表 = 不做限制（不推荐）。
#: 只放行本机 http 与 file://（file:// 的 Origin 是字符串 "null"）。
_LOCAL_ORIGIN = re.compile(r"^http://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$")

#: 这些接口会执行代码、花 API 额度或改配置，跨域一律拒绝
SENSITIVE_PATHS = ("/judge", "/chat", "/config", "/config/test", "/submissions/clear")

PROBLEMS = {}


# ---------------------------------------------------------------------------
# 大模型配置
# ---------------------------------------------------------------------------

def load_env_file(path, target):
    """把 .env 里的键值读进 target 字典（不覆盖已有的）"""
    if not os.path.isfile(path):
        return
    try:
        with io.open(path, encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                if line.lower().startswith("export "):
                    line = line[7:].strip()
                key, _, value = line.partition("=")
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                target.setdefault(key.strip(), value)
    except OSError:
        pass


#: 网页端写配置时落的文件。优先级最高，所以"网页上配的"说了算。
JUDGE_ENV = os.path.join(HERE, "judge.env")

#: 页面上给用户的常见服务商预设（URL / 模型名）。只是降低填错率的提示，不做校验。
PROVIDER_PRESETS = [
    {"name": "DeepSeek", "baseUrl": "https://api.deepseek.com/v1", "model": "deepseek-chat"},
    {"name": "通义千问", "baseUrl": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus"},
    {"name": "智谱 GLM", "baseUrl": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash"},
    {"name": "月之暗面 Kimi", "baseUrl": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k"},
    {"name": "硅基流动", "baseUrl": "https://api.siliconflow.cn/v1", "model": "deepseek-ai/DeepSeek-V3"},
    {"name": "OpenAI", "baseUrl": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
]


def llm_config():
    """
    收集大模型配置。优先级：系统环境变量 < code/.env < judge.env。

    注意：我们**只读取，不打印**密钥 —— 日志里出现密钥是很常见的低级事故。
    额外返回 source / sourceFile，网页上要告诉用户"这个配置是从哪读到的"，
    否则他改了 code/.env 却没生效时会一脸茫然（其实是被 judge.env 覆盖了）。
    """
    config = {
        "api_key": os.environ.get("OPENAI_API_KEY", "").strip(),
        "base_url": os.environ.get("OPENAI_BASE_URL", "").strip(),
        "model": os.environ.get("OPENAI_MODEL", "").strip(),
        "timeout": os.environ.get("AGENT_TIMEOUT", "").strip(),
        "source": "系统环境变量" if os.environ.get("OPENAI_API_KEY", "").strip() else "",
        "sourceFile": "",
    }
    # 后者覆盖前者 —— 网页写的是 judge.env，所以它优先级最高
    for path, label in ((os.path.join(ROOT, "code", ".env"), "code/.env"),
                        (JUDGE_ENV, "06-在线判题/judge.env")):
        if not os.path.isfile(path):
            continue
        collected = {}
        load_env_file(path, collected)
        if not collected.get("OPENAI_API_KEY"):
            continue
        for key, value in collected.items():
            if key == "OPENAI_API_KEY" and value:
                config["api_key"] = value
            elif key == "OPENAI_BASE_URL" and value:
                config["base_url"] = value
            elif key == "OPENAI_MODEL" and value:
                config["model"] = value
            elif key == "AGENT_TIMEOUT" and value:
                config["timeout"] = value
        config["source"] = label
        config["sourceFile"] = path

    if not config["base_url"]:
        config["base_url"] = "https://api.openai.com/v1"
    if not config["model"]:
        config["model"] = "deepseek-chat"
    try:
        config["timeout"] = float(config["timeout"]) if config["timeout"] else 60.0
    except ValueError:
        config["timeout"] = 60.0
    return config


def save_llm_config(base_url, api_key, model, timeout=None):
    """
    把配置写进 judge.env。

    写这个文件而不是 code/.env，是因为 code/.env 是前面 17 章共用的 ——
    在网页上改配置不该顺手动到学员已有的那份。judge.env 优先级又高于 code/.env，
    所以"网页上配的"说了算，且随时可以删掉这个文件退回去。
    """
    lines = [
        "# 由网页端「AI 配置」写入。删掉这个文件就会退回使用 code/.env。",
        "OPENAI_BASE_URL=%s" % base_url.strip(),
        "OPENAI_API_KEY=%s" % api_key.strip(),
        "OPENAI_MODEL=%s" % model.strip(),
    ]
    if timeout:
        lines.append("AGENT_TIMEOUT=%s" % timeout)
    tmp = JUDGE_ENV + ".tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")
    os.replace(tmp, JUDGE_ENV)              # 原子替换，避免写一半留下坏配置
    try:
        os.chmod(JUDGE_ENV, 0o600)          # 只有本人可读写（Windows 上会被忽略，无害）
    except OSError:
        pass
    return JUDGE_ENV


def test_llm_config(config):
    """用一次极小的调用验证配置是否真的通。返回 (ok, 说明)。"""
    body = {
        "model": config["model"],
        "messages": [{"role": "user", "content": "回复两个字：可用"}],
        "max_tokens": 16,
        "temperature": 0,
    }
    url = config["base_url"].rstrip("/") + "/chat/completions"
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + config["api_key"]},
    )
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=min(config["timeout"], 30)) as response:
            raw = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
        except Exception:                   # noqa: BLE001
            detail = ""
        hint = {
            401: "API Key 不对，或者这个 Key 没开通该模型。",
            403: "权限不足 —— 检查账号是否实名、是否开通了这个模型。",
            404: "接口地址或模型名不对。最常见的是 BASE_URL 少了 /v1，或者模型名写成了别家的。",
            429: "被限流，或者余额不足。",
        }.get(exc.code, "看下面的服务端返回。")
        return False, "HTTP %d：%s\n%s" % (exc.code, hint, detail)
    except urllib.error.URLError as exc:
        return False, "连不上：%s\n检查 URL 是否拼错、网络是否通。" % exc.reason
    except json.JSONDecodeError:
        return False, "服务端返回的不是合法 JSON —— 多半是 URL 指到了一个网页而不是 API 接口。"

    choices = raw.get("choices") or []
    message = (choices[0].get("message") if choices else None) or {}
    reply = (message.get("content") or "").strip()
    elapsed = int((time.time() - started) * 1000)
    return True, "通了（%d ms）。模型回了：%s" % (elapsed, reply[:60] or "（空回复）")


# ---------------------------------------------------------------------------
# 提交记录
# ---------------------------------------------------------------------------
#
# 存在本地 JSON 文件里，而不是只放浏览器 localStorage —— 这样换浏览器、清缓存
# 之后记录还在。参考项目用数据库存 Judge/JudgeCase 两张表，这里用一个文件等价替代：
# 每条记录既是一次提交（整体结论），也内嵌了逐用例的结果（time / memory），
# 对应它的 Judge 与 JudgeCase。

SUBMISSIONS_PATH = os.path.join(HERE, "submissions.json")
MAX_SUBMISSIONS = 500

#: 各状态在"题目掌握度"里的优先级：accepted 最高
STATUS_RANK = {"accepted": 3, "partial": 2, "wrong_answer": 1, "runtime_error": 1,
               "timeout": 1, "memory_limit": 1, "compile_error": 1}


class SubmissionStore:
    def __init__(self, path=SUBMISSIONS_PATH):
        self.path = path
        self.records = []
        self.load()

    def load(self):
        if not os.path.isfile(self.path):
            return
        try:
            with io.open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
            self.records = data.get("submissions", []) if isinstance(data, dict) else []
        except (ValueError, OSError):
            self.records = []

    def save(self):
        tmp = self.path + ".tmp"
        payload = {"$schema": "azto-submissions-v1", "submissions": self.records}
        with io.open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=1))
        os.replace(tmp, self.path)          # 原子替换，避免写一半崩了留个坏文件

    def add(self, record):
        import datetime
        now = datetime.datetime.now()
        record["id"] = now.strftime("%Y%m%d-%H%M%S-") + str(len(self.records) % 1000).zfill(3)
        record["submitTime"] = now.strftime("%Y-%m-%d %H:%M:%S")
        self.records.insert(0, record)      # 新的在前
        if len(self.records) > MAX_SUBMISSIONS:
            self.records = self.records[:MAX_SUBMISSIONS]
        self.save()
        return record

    def summary(self, record):
        """列表用：去掉源码和逐用例细节，只留结论"""
        return {
            "id": record.get("id"),
            "problemId": record.get("problemId"),
            "problemTitle": record.get("problemTitle"),
            "verdict": record.get("verdict"),
            "verdictLabel": record.get("verdictLabel"),
            "passed": record.get("passed"),
            "total": record.get("total"),
            "ms": record.get("ms"),
            "memoryKB": record.get("memoryKB"),
            "submitTime": record.get("submitTime"),
            "codeLength": len(record.get("code") or ""),
            "language": record.get("language", "python"),
            "languageLabel": record.get("languageLabel", "Python 3"),
            "source": record.get("source", "web"),
        }

    def list(self, problem_id=None, limit=50):
        rows = self.records
        if problem_id:
            rows = [r for r in rows if r.get("problemId") == problem_id]
        return [self.summary(r) for r in rows[:limit]]

    def get(self, submission_id):
        for record in self.records:
            if record.get("id") == submission_id:
                return record
        return None

    def clear(self, problem_id=None):
        before = len(self.records)
        if problem_id:
            self.records = [r for r in self.records if r.get("problemId") != problem_id]
        else:
            self.records = []
        self.save()
        return before - len(self.records)

    def stats(self):
        """
        按题聚合：每道题的掌握状态、尝试次数、最好的用时与内存。
        这是"题目列表上那个绿勾"的数据来源。
        """
        out = {}
        for record in self.records:
            pid = record.get("problemId")
            if not pid:
                continue
            entry = out.setdefault(pid, {
                "status": "untried", "attempts": 0, "accepted": 0,
                "bestMs": None, "bestMemoryKB": None,
                "lastVerdict": "", "lastSubmitTime": "",
            })
            entry["attempts"] += 1
            verdict = record.get("verdict", "")
            if verdict == "accepted":
                entry["accepted"] += 1
                entry["status"] = "solved"
                ms = record.get("ms")
                if ms and (entry["bestMs"] is None or ms < entry["bestMs"]):
                    entry["bestMs"] = round(ms, 2)
                mem = record.get("memoryKB")
                if mem and (entry["bestMemoryKB"] is None or mem < entry["bestMemoryKB"]):
                    entry["bestMemoryKB"] = mem
            elif entry["status"] != "solved":
                entry["status"] = "attempted"
            if not entry["lastSubmitTime"]:
                entry["lastVerdict"] = verdict
                entry["lastSubmitTime"] = record.get("submitTime", "")
        return out


STORE = SubmissionStore()


def mask(secret):
    """只露头尾各 4 位，用于状态展示"""
    if not secret:
        return ""
    if len(secret) <= 12:
        return "****"
    return secret[:4] + "…" + secret[-4:]


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

def problem_index():
    """给网页用的题目索引（不含用例内容，别把 6MB 塞进浏览器）"""
    items = []
    for pid, spec in PROBLEMS.items():
        items.append({
            "id": pid,
            "title": spec.get("title", pid),
            "level": spec.get("level", ""),
            "entry": spec.get("entry", ""),
            "kind": spec.get("kind", "function"),
            "params": spec.get("params", []),
            "types": spec.get("types", {}),
            "returnType": spec.get("returnType", ""),
            "inplace": bool(spec.get("inplace")),
            "leetcode": spec.get("leetcode", ""),
            "caseCount": len(spec.get("cases", [])),
            "signature": signature_of(spec),
        })
    return items


def signature_of(spec):
    """生成一句"要写什么"的签名提示，显示在编辑器上方"""
    entry = spec.get("entry", "solve")
    if spec.get("kind") == "operations":
        ops = spec["cases"][0]["ops"] if spec.get("cases") else []
        methods = []
        for op in ops[1:]:
            if op[0] not in methods:
                methods.append(op[0])
        return "class %s —— 需要实现：%s" % (entry, "、".join(methods))
    return "def %s(%s)" % (entry, ", ".join(spec.get("params", [])))


class Handler(BaseHTTPRequestHandler):
    server_version = "AZTOJudge/1.0"

    # ---- 基础工具 ----

    def log_message(self, fmt, *args):
        # 只记一行，且不打印请求体（里面可能有用户代码）
        sys.stderr.write("[judge] %s %s\n" % (self.address_string(), fmt % args))

    def _origin(self):
        """
        返回 (是否放行, 该回给浏览器的 Allow-Origin 值)。

        没带 Origin 的请求（curl、本机脚本）放行 —— 那种情况不存在"被网页借用身份"的问题。
        浏览器发来的必须是本机来源：127.0.0.1 / localhost 的 http，或者 file:// 的 "null"。
        """
        origin = self.headers.get("Origin")
        if origin is None:
            return True, "*"
        if origin == "null" or _LOCAL_ORIGIN.match(origin):
            return True, origin
        return False, None

    def _cors(self):
        allowed, value = self._origin()
        # 不放行就不发 CORS 头 —— 浏览器会自己把响应拦掉
        self.send_header("Access-Control-Allow-Origin", value if allowed else "null")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")

    def _reject_foreign_origin(self, path):
        """
        敏感接口的来源闸门。返回 True 表示已拒绝并回复完毕。

        为什么服务端也要拦：CORS 只管"网页能不能读响应"，管不住"请求有没有发出去"。
        一个表单 POST 不需要 CORS 也会到达服务端，所以必须在服务端先拒。
        """
        if not any(path.startswith(item) for item in SENSITIVE_PATHS):
            return False
        allowed, _ = self._origin()
        if allowed:
            return False
        origin = self.headers.get("Origin", "")
        self._send({
            "ok": False,
            "error": "拒绝来自 %s 的跨域请求" % origin,
            "hint": "这个接口能在你机器上执行代码、或使用你的 API 密钥，只接受本机页面调用（"
                    "http://127.0.0.1 或 file:// 打开的网页）。"
                    "如果你确实要从别的地址访问，请把该地址加进 judge_server.py 的 _LOCAL_ORIGIN。",
        }, 403)
        return True

    def _send(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, text, status=200):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return None
        if length > MAX_BODY:
            raise ValueError("请求体过大（上限 %d 字节）" % MAX_BODY)
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    # ---- HTTP 方法 ----

    def do_OPTIONS(self):
        allowed, _ = self._origin()
        self.send_response(204 if allowed else 403)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        if self._reject_foreign_origin(path):
            return

        if path in ("/", "/index.html"):
            config = llm_config()
            html = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>AZTO 判题服务</title><style>
body{font:15px/1.7 system-ui,"Microsoft YaHei",sans-serif;max-width:720px;margin:60px auto;padding:0 24px;color:#1c2030}
h1{font-size:20px}code{background:#f1f3f9;padding:2px 6px;border-radius:4px}
.ok{color:#2f9e6e}.warn{color:#c0801f}table{border-collapse:collapse;margin:16px 0}
td,th{border:1px solid #e3e6ef;padding:6px 12px;text-align:left;font-size:14px}
</style></head><body>
<h1>AZTO 本地判题服务</h1>
<p class="ok">服务正在运行。</p>
<table>
<tr><th>题库</th><td>%d 道题 / %d 个用例</td></tr>
<tr><th>大模型</th><td>%s</td></tr>
</table>
<p>回到项目网页里的 <code>算法面试轨道 → 做题</code> 即可开始。</p>
<p>命令行判题：<code>python judge.py list</code>、<code>python judge.py stub 1-两数之和</code></p>
</body></html>""" % (
                len(PROBLEMS),
                sum(len(s.get("cases", [])) for s in PROBLEMS.values()),
                ("已配置：<b>%s</b> @ %s（密钥 %s）" % (
                    config["model"], config["base_url"], mask(config["api_key"])))
                if config["api_key"] else
                '<span class="warn">未配置</span> —— 把 API Key 写进 <code>code/.env</code> 或本目录的 <code>judge.env</code>')
            return self._send_html(html)

        if path == "/status":
            config = llm_config()
            return self._send({
                "ok": True,
                "problems": len(PROBLEMS),
                "cases": sum(len(s.get("cases", [])) for s in PROBLEMS.values()),
                "languages": language_status(),
                "llm": {
                    "configured": bool(config["api_key"]),
                    "model": config["model"] if config["api_key"] else "",
                    "baseUrl": config["base_url"] if config["api_key"] else "",
                    "keyMasked": mask(config["api_key"]),
                },
            })

        if path == "/problems":
            return self._send({"ok": True, "problems": problem_index()})

        if path == "/config":
            config = llm_config()
            return self._send({
                "ok": True,
                "configured": bool(config["api_key"]),
                "baseUrl": config["base_url"],
                "model": config["model"],
                "keyMasked": mask(config["api_key"]),
                "source": config["source"],
                "sourceFile": config["sourceFile"],
                "writableFile": JUDGE_ENV,
                "presets": PROVIDER_PRESETS,
            })

        if path == "/stats":
            stats = STORE.stats()
            solved = sum(1 for v in stats.values() if v["status"] == "solved")
            attempted = sum(1 for v in stats.values() if v["status"] == "attempted")
            return self._send({
                "ok": True,
                "total": len(PROBLEMS),
                "solved": solved,
                "attempted": attempted + solved,
                "submissions": len(STORE.records),
                "byProblem": stats,
            })

        # /submissions?problemId=xx&limit=50
        if path.startswith("/submissions"):
            query = {}
            if "?" in self.path:
                for pair in self.path.split("?", 1)[1].split("&"):
                    if "=" in pair:
                        key, _, value = pair.partition("=")
                        query[key] = urllib.parse.unquote(value)
            # /submissions/<id> 取单条（含源码）
            parts = [p for p in path.split("/") if p]
            if len(parts) >= 2:
                record = STORE.get(parts[1])
                if not record:
                    return self._send({"ok": False, "error": "找不到这条提交记录"}, 404)
                return self._send({"ok": True, "submission": record})
            limit = int(query.get("limit", "50") or 50)
            return self._send({
                "ok": True,
                "submissions": STORE.list(query.get("problemId"), limit=limit),
                "total": len(STORE.records),
            })

        return self._send({"ok": False, "error": "未知路径：%s" % path}, 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        if self._reject_foreign_origin(path):
            return
        try:
            payload = self._read_json()
        except ValueError as exc:
            return self._send({"ok": False, "error": str(exc)}, 413)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return self._send({"ok": False, "error": "请求体不是合法 JSON：%s" % exc}, 400)

        if payload is None:
            return self._send({"ok": False, "error": "请求体为空"}, 400)

        if path == "/judge":
            return self.handle_judge(payload)
        if path == "/config":
            return self.handle_save_config(payload)
        if path == "/config/test":
            return self.handle_test_config(payload)
        if path == "/chat":
            return self.handle_chat(payload)
        if path == "/submissions/clear":
            removed = STORE.clear(payload.get("problemId"))
            return self._send({"ok": True, "removed": removed})
        return self._send({"ok": False, "error": "未知路径：%s" % path}, 404)

    # ---- 判题 ----

    def handle_judge(self, payload):
        problem_id = str(payload.get("problemId", ""))
        code = payload.get("code", "")
        timeout = payload.get("timeout") or DEFAULT_TIMEOUT
        language = str(payload.get("language") or "python").lower()

        if problem_id not in PROBLEMS:
            return self._send({"ok": False, "error": "题库里没有这道题：%s" % problem_id}, 404)
        if language not in LANGUAGE_LABELS:
            return self._send({
                "ok": False,
                "error": "不支持的语言：%s" % language,
                "hint": "目前支持：%s" % "、".join(LANGUAGE_LABELS.values()),
            }, 400)
        if not isinstance(code, str) or not code.strip():
            return self._send({"ok": False, "error": "代码是空的"}, 400)
        if len(code) > MAX_CODE:
            return self._send({"ok": False, "error": "代码太长了（上限 %d 字符）" % MAX_CODE}, 413)

        # 编译器没装时给出可执行的提示，而不是让判题直接炸
        status = language_status().get(language, {})
        if not status.get("available"):
            return self._send({
                "ok": False,
                "error": "%s 不可用：%s" % (status.get("label", language), status.get("reason", "")),
                "hint": "装好编译器后重启这个服务。Python 和 C++ 都是开箱可用的（C++ 需要 g++ 或 clang++）。",
            }, 400)

        try:
            timeout = max(1.0, min(float(timeout), 20.0))
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT

        spec = PROBLEMS[problem_id]
        try:
            report = run_all(code, spec, timeout=timeout, language=language)
        except Exception as exc:                      # noqa: BLE001 - 判题器自身出问题也要给出可读信息
            return self._send({"ok": False, "error": "判题器内部错误：%s: %s" % (type(exc).__name__, exc)}, 500)

        report["ok"] = True
        report["problemId"] = problem_id
        report["language"] = language
        report["languageLabel"] = LANGUAGE_LABELS.get(language, language)
        report["verdictLabel"] = VERDICT_LABEL.get(report["verdict"], report["verdict"])
        report["leetcode"] = spec.get("leetcode", "")
        report["submissionId"] = None

        # 运行模式（只跑第一个用例）不记入提交历史 —— 那是调试，不是提交
        if payload.get("mode") != "run":
            record = {
                "problemId": problem_id,
                "problemTitle": spec.get("title", problem_id),
                "verdict": report["verdict"],
                "verdictLabel": report["verdictLabel"],
                "passed": report["passed"],
                "total": report["total"],
                "ms": report.get("ms", 0),
                "memoryKB": report.get("memoryKB", 0),
                "code": code,
                "language": language,
                "languageLabel": LANGUAGE_LABELS.get(language, language),
                "source": payload.get("source", "web"),
                "cases": [{"seq": item.get("index"), "status": item.get("status"),
                           "ms": item.get("ms"), "memoryKB": item.get("memoryKB"),
                           "note": item.get("note", "")}
                          for item in report.get("results", [])],
            }
            try:
                saved = STORE.add(record)
                report["submissionId"] = saved["id"]
            except OSError as exc:
                report["storeError"] = "提交记录没能保存：%s" % exc

        # 用例的输入输出算"答案"，但学员提交后看到自己错在哪是最有价值的反馈，
        # 所以全部返回 —— 本地工具，没有防作弊的需求。
        return self._send(report)

    # ---- AI 接口配置 ----

    def handle_save_config(self, payload):
        """
        保存配置到 judge.env。

        密钥不落日志、不回显 —— 只回打码后的样子。前端拿不到完整密钥，
        也就不会不小心把它渲染到页面上或者写进截图里。
        """
        base_url = str(payload.get("baseUrl", "")).strip()
        api_key = str(payload.get("apiKey", "")).strip()
        model = str(payload.get("model", "")).strip()

        # 允许只改一部分：没传的字段沿用现在的值（比如只换模型，不想重填密钥）
        current = llm_config()
        if not api_key:
            api_key = current["api_key"]
        if not base_url:
            base_url = current["base_url"]
        if not model:
            model = current["model"]

        if not base_url or not model:
            return self._send({"ok": False, "error": "URL 和模型名都要填"}, 400)
        if not api_key:
            return self._send({"ok": False, "error": "API Key 不能为空"}, 400)
        if not re.match(r"^https?://", base_url):
            return self._send({
                "ok": False,
                "error": "URL 要以 http:// 或 https:// 开头",
                "hint": "常见写法：https://api.deepseek.com/v1 —— 注意大多数服务商都要求以 /v1 结尾。",
            }, 400)

        try:
            path = save_llm_config(base_url, api_key, model)
        except OSError as exc:
            return self._send({"ok": False, "error": "写配置失败：%s" % exc}, 500)

        saved = llm_config()
        return self._send({
            "ok": True,
            "configured": True,
            "baseUrl": saved["base_url"],
            "model": saved["model"],
            "keyMasked": mask(saved["api_key"]),
            "source": saved["source"],
            "sourceFile": path,
            "message": "已保存到 %s。不需要重启服务，下一次提问就用新配置。" % path,
        })

    def handle_test_config(self, payload):
        """用一次极小的调用验证配置。填完先点这个，比写完代码再发现 Key 错了省事。"""
        base_url = str(payload.get("baseUrl", "")).strip()
        api_key = str(payload.get("apiKey", "")).strip()
        model = str(payload.get("model", "")).strip()

        current = llm_config()
        config = {
            "base_url": base_url or current["base_url"],
            "api_key": api_key or current["api_key"],
            "model": model or current["model"],
            "timeout": current["timeout"],
        }
        if not config["api_key"]:
            return self._send({"ok": False, "error": "还没填 API Key"}, 400)
        if not re.match(r"^https?://", config["base_url"]):
            return self._send({"ok": False, "error": "URL 要以 http:// 或 https:// 开头"}, 400)

        ok, message = test_llm_config(config)
        return self._send({"ok": ok, "message": message,
                           "baseUrl": config["base_url"], "model": config["model"]},
                          200 if ok else 400)

    # ---- LLM 代理 ----

    def handle_chat(self, payload):
        config = llm_config()
        if not config["api_key"]:
            return self._send({
                "ok": False,
                "error": "还没有配置大模型 API Key。",
                "hint": "把 OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL 写进 code/.env"
                        "（和前面 17 章共用同一份），或者在本目录建一个 judge.env。改完重启这个服务。",
            }, 400)

        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            return self._send({"ok": False, "error": "messages 不能为空"}, 400)

        body = {
            "model": payload.get("model") or config["model"],
            "messages": messages,
            "temperature": payload.get("temperature", 0.3),
        }
        if payload.get("max_tokens"):
            body["max_tokens"] = payload["max_tokens"]

        url = config["base_url"].rstrip("/") + "/chat/completions"
        request = urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + config["api_key"],
                "Accept": "application/json",
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=config["timeout"]) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:600]
            except Exception:                          # noqa: BLE001
                detail = ""
            hint = {
                401: "API Key 无效或没读到，检查 code/.env 里的 OPENAI_API_KEY。",
                404: "接口地址或模型名不对，最常见的是 OPENAI_BASE_URL 少了 /v1。",
                429: "被限流或余额不足。",
            }.get(exc.code, "看看下面的服务端返回。")
            return self._send({"ok": False, "error": "调用大模型失败：HTTP %d" % exc.code,
                               "hint": hint, "detail": detail}, 502)
        except urllib.error.URLError as exc:
            return self._send({"ok": False, "error": "连不上大模型服务：%s" % exc.reason,
                               "hint": "检查网络与 OPENAI_BASE_URL 是否拼错。"}, 502)
        except json.JSONDecodeError:
            return self._send({"ok": False, "error": "大模型返回的不是合法 JSON"}, 502)

        choices = raw.get("choices") or []
        message = (choices[0].get("message") if choices else None) or {}
        return self._send({
            "ok": True,
            "content": message.get("content") or "",
            "model": raw.get("model", body["model"]),
            "usage": raw.get("usage") or {},
        })


def main() -> int:
    parser = argparse.ArgumentParser(description="本地判题服务 + LLM 代理")
    parser.add_argument("--port", type=int, default=8900, help="端口，默认 8900")
    parser.add_argument("--host", default="127.0.0.1",
                        help="监听地址，默认只监听本机（改 0.0.0.0 会让同网段其他人也能提交代码）")
    args = parser.parse_args()

    global PROBLEMS
    PROBLEMS = load_testcases()
    if not PROBLEMS:
        print("读不到题库：%s" % os.path.join(HERE, "testcases.json"))
        print("请确认文件存在，且先跑一次：python harness.py --selfcheck")
        return 2

    config = llm_config()
    total_cases = sum(len(s.get("cases", [])) for s in PROBLEMS.values())

    print("=" * 70)
    print("  AZTO 本地判题服务")
    print("=" * 70)
    print("  题库      %d 道题 / %d 个用例" % (len(PROBLEMS), total_cases))
    if STORE.records:
        solved = sum(1 for v in STORE.stats().values() if v["status"] == "solved")
        print("  历史记录  %d 次提交，其中 %d 道题已通过" % (len(STORE.records), solved))
    langs = language_status()
    print("  判题语言  " + "　".join(
        "%s %s" % (info["label"], "可用" if info["available"] else "不可用")
        for info in langs.values()))
    for info in langs.values():
        if not info["available"]:
            print("            %s 不可用：%s" % (info["label"], info["reason"]))
    if config["api_key"]:
        print("  大模型    %s @ %s（密钥 %s）" % (
            config["model"], config["base_url"], mask(config["api_key"])))
    else:
        print("  大模型    未配置 —— AI 讲解功能不可用")
        print("            把 API Key 写进 code/.env，或在 %s 建一个 judge.env" % HERE)
    print("  地址      http://%s:%d" % (args.host, args.port))
    print()
    print("  保持这个窗口开着，然后回网页点「算法面试轨道 → 做题」。")
    print("  按 Ctrl+C 停止。")
    print("=" * 70)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
