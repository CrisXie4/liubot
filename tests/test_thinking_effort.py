"""DeepSeek thinking_effort（effort 映射）测试。

DeepSeek Anthropic 兼容端点：effort 经 output_config 传入，
服务端实际映射 low→low, medium→high, high→high, xhigh→high, max→max；
thinking/reasoning_effort 与 output_config 同时传会冲突，必须只传后者。
"""
import asyncio
import json
import os
import sys
import uuid
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import pytest

from bot import memory, llm_call, config

FAKE_API_KEY = "test-" + uuid.uuid4().hex


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(memory, "MODEL_SETTINGS_PATH",
                        str(tmp_path / "model_settings.json"))
    memory.cache.model_settings_cache.invalidate("model_settings")
    monkeypatch.setattr(config, "LLM_EXTRA_PROVIDERS", "")
    monkeypatch.setattr(config, "LLM_PROVIDERS_EXCLUSIVE", False)
    yield
    memory.cache.model_settings_cache.invalidate("model_settings")


# ─── _parse_env_providers：thinking_effort 透传 ───

def test_parse_passes_valid_effort():
    raw = json.dumps([
        {"api_url": "https://api.deepseek.example/anthropic",
         "api_key": FAKE_API_KEY,
         "model": "deepseek-v4-flash-vision-exp",
         "supports_vision": True, "max_context_tokens": 1000000,
         "thinking_effort": "high"},
    ])
    providers = memory._parse_env_providers(raw)
    assert len(providers) == 1
    p = providers[0]
    assert p["thinking_effort"] == "high"
    assert p["supports_vision"] is True
    assert p["max_context_tokens"] == 1000000


def test_parse_drops_invalid_effort():
    raw = json.dumps([
        {"api_url": "https://a.example", "api_key": FAKE_API_KEY,
         "model": "m1", "thinking_effort": "ultra"},
        {"api_url": "https://b.example", "api_key": FAKE_API_KEY,
         "model": "m2", "thinking_effort": "LOW"},
    ])
    providers = memory._parse_env_providers(raw)
    assert "thinking_effort" not in providers[0]
    assert providers[1]["thinking_effort"] == "low"  # 大小写归一


# ─── _apply_thinking：映射 + max_tokens 保底 ───

def test_effort_map_matches_server_mapping():
    assert llm_call.EFFORT_MAP["low"] == "low"
    assert llm_call.EFFORT_MAP["medium"] == "high"
    assert llm_call.EFFORT_MAP["high"] == "high"
    assert llm_call.EFFORT_MAP["xhigh"] == "high"
    assert llm_call.EFFORT_MAP["max"] == "max"


def test_apply_thinking_injects_and_bumps_max_tokens():
    kwargs = {"max_tokens": 800, "messages": []}
    llm_call._apply_thinking(kwargs, {"thinking_effort": "medium"})
    # medium 在客户端就映射为 high（与服务端实际映射一致）
    assert kwargs["extra_body"]["output_config"] == {"effort": "high"}
    # 思考 token 计入 max_tokens，800 保底抬到 2000
    assert kwargs["max_tokens"] == llm_call.THINKING_MIN_MAX_TOKENS


def test_apply_thinking_keeps_larger_max_tokens():
    kwargs = {"max_tokens": 5000}
    llm_call._apply_thinking(kwargs, {"thinking_effort": "max"})
    assert kwargs["max_tokens"] == 5000
    assert kwargs["extra_body"]["output_config"] == {"effort": "max"}


def test_apply_thinking_noop_without_effort():
    kwargs = {"max_tokens": 800}
    llm_call._apply_thinking(kwargs, {"model": "m"})
    llm_call._apply_thinking(kwargs, {"thinking_effort": "bogus"})
    assert not (kwargs.get("extra_body") or {}).get("output_config")
    assert kwargs["max_tokens"] == 800


# ─── llm_create 全链路：只有声明 thinking_effort 的渠道收到 output_config ───

def test_llm_create_passes_output_config_per_provider(monkeypatch):
    async def _no_token(priority):
        return
    monkeypatch.setattr(llm_call.rate_limiter, "acquire_token", _no_token)

    providers = [
        {"api_url": "https://plain.example", "api_key": FAKE_API_KEY,
         "model": "plain-model"},
        {"api_url": "https://api.deepseek.example/anthropic",
         "api_key": FAKE_API_KEY,
         "model": "deepseek-v4-flash-vision-exp", "supports_vision": True,
         "thinking_effort": "high"},
    ]
    monkeypatch.setattr(llm_call, "_get_providers", lambda: providers)
    monkeypatch.setattr(llm_call, "_get_or_create_client",
                        lambda k, u: object())

    captured = []

    async def _fake_call(client, *, max_retries=None, **kwargs):
        captured.append(kwargs)
        if kwargs["model"] == "plain-model":
            # 永久性错误（非重试/非鉴权/非超限）→ llm_create 切下一个渠道
            raise RuntimeError("502 gateway no longer exists")
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")],
            stop_reason="end_turn",
        )
    monkeypatch.setattr(llm_call, "_call_with_retries", _fake_call)

    async def _run():
        return await llm_call.llm_create(
            model="ignored", max_tokens=800, system="s", priority="P0",
            messages=[{"role": "user", "content": "hi"}],
        )

    resp = asyncio.run(_run())
    assert resp.content[0].text == "ok"
    assert len(captured) == 2
    # 第一个渠道（未声明 effort）不带 output_config，max_tokens 保持 800
    assert not (captured[0].get("extra_body") or {}).get("output_config")
    assert captured[0]["max_tokens"] == 800
    # 第二个渠道（deepseek）带 effort=high，max_tokens 抬到保底
    assert captured[1]["extra_body"]["output_config"] == {"effort": "high"}
    assert captured[1]["max_tokens"] == llm_call.THINKING_MIN_MAX_TOKENS
