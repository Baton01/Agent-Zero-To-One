"""
本项目所有章节代码的共用模块。

    from common.llm import load_env, get_llm, chat, count_tokens_approx, estimate_cost
    from common.tools import Tool, ToolRegistry
    from common.trace import Trace

设计原则：只用标准库。这样"没装任何包"也不会卡住你 —— 装依赖本身不该是学习的门槛。
"""

from __future__ import annotations

__all__ = ["llm", "tools", "trace"]
