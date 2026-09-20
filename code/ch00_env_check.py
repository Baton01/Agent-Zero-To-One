"""
第 00 章 · 环境准备与第一次模型调用 配套代码

本章学到什么：
  - 一次模型调用就是一次 HTTPS POST：发一段 JSON 出去，收一段 JSON 回来
  - 不用装任何第三方包也能跑完本书：AGENT_MOCK=1 时模型响应由内置规则模拟
  - 出错时第一件事是读服务端返回的 body，而不是猜（401/404/429 都是这么查出来的）

怎么跑：
    cd code
    AGENT_MOCK=1 python ch00_env_check.py            # 离线模式，不需要 API Key
    python ch00_env_check.py                         # 在线模式，需要 .env 里配好 Key
    python ch00_env_check.py --check-network         # 额外测一次 base_url 连通性（会联网）
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import sys
import urllib.error
import urllib.request

_CODE_DIR = os.path.dirname(os.path.abspath(__file__))
if _CODE_DIR not in sys.path:
    sys.path.insert(0, _CODE_DIR)

from common.llm import (  # noqa: E402
    chat,
    describe_mode,
    is_mock_mode,
    load_env,
    print_banner,
)

# Windows 控制台的编码不一定是 UTF-8：这里只把「编码失败」降级成替换字符，不让脚本崩在 print 上
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(errors="replace")
        except (ValueError, OSError):
            pass

#: 读者最关心的三个可选项：它们分别服务第 08 章（向量检索/分词）和第 13 章（Web 服务）
OPTIONAL_DEPS = [
    ("numpy", "第 08 章 RAG 的向量检索", "降级为纯 Python 实现，速度慢但结果一致"),
    ("jieba", "第 08 章中文分词", "降级为字符 n-gram 切分"),
    ("fastapi", "第 13 章 Web 服务骨架", "跳过 Web 部分，不影响其他章节"),
]


def section(title: str) -> None:
    print("\n" + "=" * 64)
    print("  " + title)
    print("=" * 64)


def mask(secret: str) -> str:
    """密钥打码：只留前 4 位和后 4 位。截图、录屏、发群求助时都要这样处理。"""
    if not secret:
        return "（空）"
    if len(secret) <= 8:
        return secret[0] + "*" * (len(secret) - 1)
    return "{}****{}".format(secret[:4], secret[-4:])


def check_python() -> bool:
    version = platform.python_version()
    ok = sys.version_info >= (3, 9)
    print("[1/6] Python 版本      : {} {}".format(version, "（满足要求 >= 3.9）" if ok else "（过低！）"))
    if not ok:
        print("      警告：本书要求 Python 3.9 及以上，请先升级再继续。")
    return ok


def check_runtime() -> None:
    print("[2/6] 解释器绝对路径  : {}".format(sys.executable))
    print("      当前工作目录    : {}".format(os.getcwd()))
    print("      本脚本所在目录  : {}".format(_CODE_DIR))
    print("      操作系统        : {} {} / {}".format(platform.system(), platform.release(), platform.machine()))


def check_venv() -> None:
    # sys.prefix 与 sys.base_prefix 不同，说明当前解释器来自一个虚拟环境
    if sys.prefix != sys.base_prefix:
        print("[3/6] 虚拟环境        : 已激活（{}）".format(sys.prefix))
    else:
        print("[3/6] 虚拟环境        : 未检测到 —— 建议创建，见正文第 3 节")


def check_env_file() -> None:
    env_path = os.path.join(_CODE_DIR, ".env")
    if not os.path.isfile(env_path):
        print("[4/6] .env 文件        : 未找到（{}）".format(env_path))
        print("      没有它也能学：会自动进入离线 Mock 模式。")
        return
    print("[4/6] .env 文件        : 已找到（{}）".format(env_path))
    values = load_env()
    for key in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"):
        value = os.environ.get(key, values.get(key, ""))
        if key == "OPENAI_API_KEY":
            value = mask(value)
        print("      {:<17}= {}".format(key, value or "（未设置）"))


def check_optional_deps() -> None:
    section("补充检查：可选依赖（都没装也不影响本书主线）")
    for name, purpose, fallback in OPTIONAL_DEPS:
        installed = importlib.util.find_spec(name) is not None
        status = "已安装" if installed else "未安装"
        print("  · {:<9} {:<16} {}".format(name, status, "用于 " + purpose))
        if not installed:
            print("      → 不影响学习：{}".format(fallback))


def build_payload(prompt: str, model: str) -> dict:
    """手工拼出请求体。看懂它，就看懂了所有 SDK 的内核。"""
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是一个简洁的助手，回答不超过 50 个字。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.7,
        "stream": False,
    }


def describe_request(url: str, payload: dict) -> None:
    """把「将要发出去的报文」打印出来 —— 这是第 00 章的核心里程碑。"""
    print("  POST {}".format(url))
    print("  Content-Type : application/json")
    print("  Authorization: Bearer {}".format(mask(os.environ.get("OPENAI_API_KEY", ""))))
    print("  Body:")
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    for line in body.splitlines():
        print("    " + line)


def call_model_once(prompt: str, api_key: str, base_url: str, model: str, timeout: float = 30.0) -> dict:
    """只用标准库发一次 /chat/completions 请求，返回原始响应 dict。"""
    url = base_url.rstrip("/") + "/chat/completions"
    # 顺序永远是「先 dumps 再 encode」：中文必须编码成 UTF-8 字节
    body = json.dumps(build_payload(prompt, model), ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        # 关键技巧：HTTPError 本身可读，服务端到底说了什么全在 body 里
        detail = error.read().decode("utf-8", "ignore")
        raise RuntimeError("HTTP {} ← 服务端返回：{}".format(error.code, detail[:300]))


def check_network(base_url: str, api_key: str) -> bool:
    """
    测一次连通性。判据是「有没有拿到 HTTP 响应」，而不是「状态码是不是 200」：
    401/404 也说明网络是通的（问题在 Key 或路径上），这能把问题定位范围直接减半。
    """
    url = base_url.rstrip("/") + "/models"
    request = urllib.request.Request(
        url,
        headers={"Authorization": "Bearer " + (api_key or "none")},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=8.0) as response:
            print("  HTTP {} —— 网络连通，且路径与鉴权都没问题。".format(response.status))
            return True
    except urllib.error.HTTPError as error:
        print("  HTTP {} —— 网络连通（收到了服务端响应）".format(error.code))
        print("  状态码不是 200，说明问题出在 Key / 路径 / 模型权限上，而不是网络。")
        return True
    except urllib.error.URLError as error:
        print("  连不上：{}".format(error.reason))
        print("  这很正常：没联网 / 需要代理 / base_url 写错。离线学习不受影响。")
        return False
    except Exception as error:  # noqa: BLE001 - 自检工具不该因为任何异常而崩掉
        print("  {}: {}".format(type(error).__name__, error))
        return False


def try_one_call() -> None:
    section("第 6 项（本章核心）：真正发一次请求，把请求和响应都看清楚")
    prompt = "用一句话解释什么是 Python。"
    base_url = os.environ.get("OPENAI_BASE_URL", "").strip() or "https://api.deepseek.com/v1"
    model = os.environ.get("OPENAI_MODEL", "").strip() or "deepseek-chat"
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    url = base_url.rstrip("/") + "/chat/completions"

    print("── 将要发出的请求报文 ──────────────────────────────────────")
    describe_request(url, build_payload(prompt, model))
    print("── 响应 ────────────────────────────────────────────────────")

    if is_mock_mode():
        # 离线：不真的发出去，但走的是同一套「取 content、看 usage」的代码路径
        print("  当前是离线模式，上面的报文没有真的发出去（不联网、不花钱）。")
        llm_reply = chat(prompt)
        print("  回答预览：{}".format(llm_reply.replace("\n", " ")[:80]))
        print("  下面是在线时你会看到的响应结构（字段名与在线完全一致）：")
        preview = {
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "……"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 32, "completion_tokens": 21, "total_tokens": 53},
        }
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        print("  小结：choices[0].message.content 是回答，usage 是账单，finish_reason 是停止原因。")
        return

    print("  正在请求 {} ……".format(url))
    try:
        raw = call_model_once(prompt, api_key, base_url, model)
    except Exception as error:  # noqa: BLE001 - 排错信息已经由 call_model_once 整理好了
        print("  请求失败：{}".format(error))
        print("  按正文第 8 节的顺序排查：先读上面的服务端 body，再对照 401/404/429 的分类。")
        return

    choice = (raw.get("choices") or [{}])[0]
    usage = raw.get("usage") or {}
    print("  模型回答：{}".format((choice.get("message") or {}).get("content", "")))
    print("  finish_reason : {}".format(choice.get("finish_reason")))
    print(
        "  usage         : prompt_tokens={prompt_tokens} completion_tokens={completion_tokens} "
        "total_tokens={total_tokens}".format(
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
        )
    )
    print("  看到真实的 usage 数字，就是本章通关的标志。")


def main() -> int:
    parser = argparse.ArgumentParser(description="第 00 章环境自检")
    parser.add_argument("--check-network", action="store_true",
                        help="额外做一次 base_url 连通性测试（会联网；判据是能否拿到 HTTP 响应）")
    args = parser.parse_args()

    load_env()
    print_banner("第 00 章 · 环境自检报告")

    python_ok = check_python()
    check_runtime()
    check_venv()
    check_env_file()
    print("[5/6] 当前模式        : {}".format(describe_mode()))

    check_optional_deps()

    if args.check_network:
        section("附加检查：base_url 连通性")
        base_url = os.environ.get("OPENAI_BASE_URL", "").strip() or "https://api.deepseek.com/v1"
        print("  目标：{}".format(base_url))
        check_network(base_url, os.environ.get("OPENAI_API_KEY", "").strip())

    try_one_call()

    section("结论与下一步")
    print("  环境可用：{}".format("是" if python_ok else "否（请先升级 Python）"))
    if is_mock_mode():
        print("  你可以在没有 API Key 的情况下学完前面所有章节。")
        print("  下一步：把 code/.env.example 复制为 code/.env，填上 Key，再重跑本脚本把模式切到「在线」。")
    else:
        print("  已配置 Key，在线模式可用；离线模式随时可用 AGENT_MOCK=1 复现。")

    section("本章要点回顾")
    print("  1. 一次模型调用 = 一次 HTTPS POST，SDK 只是这层 HTTP 的包装。")
    print("  2. 请求三件套：URL（base_url + /chat/completions）、Authorization: Bearer <key>、JSON body。")
    print("  3. messages 是请求的全部语义：system 定规则、user 是输入、assistant 是模型上轮的原话。")
    print("  4. 取值路径固定：choices[0].message.content / finish_reason / usage。")
    print("  5. 没有 Key 也能学：AGENT_MOCK=1 强制离线；Mock 验证流程，不验证效果。")
    print("  6. 排错顺序：先读服务端 body → 再按 401/404/400/429 分类 → 用 Mock 二分法定位是配置问题还是代码问题。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
