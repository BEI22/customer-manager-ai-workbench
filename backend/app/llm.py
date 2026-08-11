# -*- coding: utf-8 -*-
"""DeepSeek LLM 客户端（LangChain ChatOpenAI，OpenAI 兼容协议）。

- 配置了 DEEPSEEK_API_KEY 时返回真实模型；
- 未配置时 available() 返回 False，由 agent 使用规则模式兜底。
"""
from __future__ import annotations

import os

from langchain_openai import ChatOpenAI

DEFAULT_BASE = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"


def available() -> bool:
    return bool(os.getenv("DEEPSEEK_API_KEY", "").strip())


def get_chat_model(temperature: float = 0.2) -> ChatOpenAI:
    key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    base = os.getenv("DEEPSEEK_BASE_URL", DEFAULT_BASE).strip()
    model = os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL).strip()
    return ChatOpenAI(
        model=model,
        api_key=key or "sk-dummy",
        base_url=base,
        temperature=temperature,
    )


def get_json_model(temperature: float = 0.2) -> ChatOpenAI:
    """强制 JSON 输出的模型（DeepSeek 支持 response_format=json_object）。"""
    key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    base = os.getenv("DEEPSEEK_BASE_URL", DEFAULT_BASE).strip()
    model = os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL).strip()
    return ChatOpenAI(
        model=model,
        api_key=key or "sk-dummy",
        base_url=base,
        temperature=temperature,
        model_kwargs={"response_format": {"type": "json_object"}},
    )