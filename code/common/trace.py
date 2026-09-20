"""
简易 Trace：记录一次 Agent 运行的每一步，用于调试和评估（第 12 章的重点）。

为什么需要它：
    Agent 出问题时，你看到的现象只是"答案不对"。真正的原因可能藏在
    第 3 步的工具参数、第 5 步被裁剪掉的上下文、或者模型在第 7 步提前收工。
    没有 trace，你只能靠猜。

记什么：
    每次模型调用（用了多少 token、返回了什么）、每次工具执行（参数、结果、耗时）、
    每一步的自定义事件（比如"触发压缩"、"达到步数上限"）。

用法：
    from common.trace import Trace

    trace = Trace("weather_agent")
    trace.log("llm_call", step=1, prompt_tokens=320, content="我需要查询天气")
    trace.log("tool_call", step=1, tool="get_weather", arguments={"city": "北京"}, elapsed=0.4)
    print(trace.summary())
    trace.save()          # 默认存到 traces/<name>_<时间戳>.json
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe(obj: Any, limit: int = 2000) -> Any:
    """把任意对象变成可 JSON 序列化、且不会撑爆文件的形式。"""
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        if isinstance(obj, str) and len(obj) > limit:
            return obj[:limit] + "...[已截断，原长 {} 字符]".format(len(obj))
        return obj
    if isinstance(obj, dict):
        return {str(k): _safe(v, limit) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_safe(item, limit) for item in obj]
    try:
        text = json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = repr(obj)
    if len(text) > limit:
        text = text[:limit] + "...[已截断]"
    return text


class Trace:
    """一次运行的事件流水。"""

    def __init__(self, name: str, verbose: bool = False) -> None:
        self.name = name
        self.verbose = verbose
        self.started_at = time.time()
        self.entries: List[Dict[str, Any]] = []
        self.start_time_str = _now_iso()

    # ------------------------------------------------------------ 记录

    def log(self, event: str, **fields: Any) -> Dict[str, Any]:
        """
        记一个事件。event 是事件名（如 llm_call / tool_call / stop），
        fields 是任意附加字段，会原样存进 JSON。
        """
        entry: Dict[str, Any] = {
            "seq": len(self.entries) + 1,
            "time": _now_iso(),
            "elapsed": round(time.time() - self.started_at, 3),
            "event": event,
        }
        entry.update(_safe(fields))
        self.entries.append(entry)
        if self.verbose:
            print("  [trace] #{seq:<3} {event} {extra}".format(
                seq=entry["seq"],
                event=event,
                extra=json.dumps({k: v for k, v in entry.items() if k not in ("seq", "time", "elapsed", "event")},
                                 ensure_ascii=False)[:160],
            ))
        return entry

    def llm_call(self, step: int, response: Dict[str, Any], note: str = "") -> Dict[str, Any]:
        """记录一次模型调用的标准字段。"""
        usage = response.get("usage") or {}
        return self.log(
            "llm_call",
            step=step,
            content=(response.get("content") or "")[:500],
            tool_calls=[c.get("name") for c in response.get("tool_calls") or []],
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            finish_reason=response.get("finish_reason"),
            mock=response.get("mock", False),
            note=note,
        )

    def tool_call(self, step: int, name: str, arguments: Dict[str, Any], result: str, elapsed: float) -> Dict[str, Any]:
        """记录一次工具执行。ok 根据结果字符串里的错误标记推断。"""
        failed = result.startswith("[工具错误")
        return self.log(
            "tool_call",
            step=step,
            tool=name,
            arguments=arguments,
            ok=not failed,
            result=result[:500],
            elapsed=round(elapsed, 3),
        )

    # ------------------------------------------------------------ 汇总

    @property
    def duration(self) -> float:
        return round(time.time() - self.started_at, 3)

    def counts(self) -> Dict[str, int]:
        """各类事件出现了多少次。"""
        result: Dict[str, int] = {}
        for entry in self.entries:
            result[entry["event"]] = result.get(entry["event"], 0) + 1
        return result

    def token_usage(self) -> Dict[str, int]:
        """累计 token 用量。"""
        prompt = sum(int(e.get("prompt_tokens", 0) or 0) for e in self.entries if e["event"] == "llm_call")
        completion = sum(int(e.get("completion_tokens", 0) or 0) for e in self.entries if e["event"] == "llm_call")
        return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}

    def tool_failures(self) -> List[Dict[str, Any]]:
        """所有失败的工具调用（第 12 章做失败分类的原料）。"""
        return [e for e in self.entries if e["event"] == "tool_call" and not e.get("ok", True)]

    def find_stop_reason(self) -> Optional[str]:
        """找出本次运行是怎么停下来的。"""
        for entry in reversed(self.entries):
            if entry["event"] in ("stop", "finish"):
                return str(entry.get("reason", entry["event"]))
        return None

    def summary(self) -> str:
        """人类可读的运行摘要。"""
        counts = self.counts()
        usage = self.token_usage()
        failures = self.tool_failures()
        lines = [
            "=" * 64,
            "Trace: {}（{}）".format(self.name, self.start_time_str),
            "-" * 64,
            "事件统计：{}".format(
                "  ".join("{}×{}".format(k, v) for k, v in sorted(counts.items())) or "（无）"
            ),
            "Token  ：输入 {} ｜ 输出 {} ｜ 合计 {}".format(
                usage["prompt_tokens"], usage["completion_tokens"], usage["total_tokens"]
            ),
            "工具失败：{} 次".format(len(failures)),
            "停止原因：{}".format(self.find_stop_reason() or "（未显式记录）"),
            "总耗时  ：{} 秒".format(self.duration),
            "=" * 64,
        ]
        if failures:
            lines.insert(-1, "失败明细：")
            for item in failures[:5]:
                lines.insert(-1, "  · 第 {} 步 {}({}) → {}".format(
                    item.get("step", "?"),
                    item.get("tool", "?"),
                    json.dumps(item.get("arguments", {}), ensure_ascii=False)[:60],
                    str(item.get("result", ""))[:100],
                ))
        return "\n".join(lines)

    # ------------------------------------------------------------ 导出

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "started_at": self.start_time_str,
            "duration_seconds": self.duration,
            "counts": self.counts(),
            "token_usage": self.token_usage(),
            "stop_reason": self.find_stop_reason(),
            "entries": self.entries,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent)

    def save(self, path: Optional[str] = None) -> str:
        """
        存成 JSON 文件。默认路径：当前目录下的 traces/<名字>_<时间戳>.json
        返回实际写入的路径。
        """
        if path is None:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            directory = os.path.join(os.getcwd(), "traces")
            os.makedirs(directory, exist_ok=True)
            path = os.path.join(directory, "{}_{}.json".format(self.name, stamp))
        else:
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(self.to_json())
        return path

    # ------------------------------------------------------------ 容器协议

    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self) -> Iterator[Dict[str, Any]]:
        return iter(self.entries)

    def __repr__(self) -> str:
        return "<Trace {} {} 个事件 {} 秒>".format(self.name, len(self.entries), self.duration)


__all__ = ["Trace"]
