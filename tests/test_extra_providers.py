"""LLM_EXTRA_PROVIDERS 环境变量注入 provider + 独占模式 + 上下文超限 failover 测试。

背景：免密钥 llama.cpp 公共端点（如 bigmodel.d1kt.cn）n_ctx 只有 32K，
超限返回 400 exceed_context_size_error —— llm_create 必须切下一个
provider 而不是中断；独占模式（LLM_PROVIDERS_EXCLUSIVE）下其他渠道
全部停用，全渠道超限时必须抛出而非死循环。
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

# 每次运行随机生成的占位值：构造上即不可能是任何真实服务的凭据，
# 测试里所有渠道均挂在 RFC 2606 保留的 .example 域名下。
FAKE_API_KEY = "test-" + uuid.uuid4().hex


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch, tmp_path):
    """每个用例独立：缓存清空 + JSON 路径指向临时文件 + env 注入/独占开关复位。"""
    monkeypatch.setattr(memory, "MODEL_SETTINGS_PATH",
                        str(tmp_path / "model_settings.json"))
    memory.cache.model_settings_cache.invalidate("model_settings")
    monkeypatch.setattr(config, "LLM_EXTRA_PROVIDERS", "")
    monkeypatch.setattr(config, "LLM_PROVIDERS_EXCLUSIVE", False)
    yield
    memory.cache.model_settings_cache.invalidate("model_settings")


def _write_settings_json(tmp_path):
    """写入带默认渠道 + 一个 JSON 额外渠道的设置文件。"""
    (tmp_path / "model_settings.json").write_text(json.dumps({
        "api_key": FAKE_API_KEY, "api_url": "https://default.example",
        "model": "default-model",
        "providers": [
            {"api_url": "https://json-two.example", "api_key": FAKE_API_KEY,
             "model": "m2"},
        ],
    }, ensure_ascii=False), encoding="utf-8")


# ─── _parse_env_providers ───

def test_parse_valid_entries():
    raw = json.dumps([
        {"api_url": "https://a.example", "api_key": FAKE_API_KEY,
         "model": "m1", "supports_vision": True, "max_context_tokens": 24000},
        {"api_url": "http://b.example", "api_key": FAKE_API_KEY, "model": "m2"},
    ])
    providers = memory._parse_env_providers(raw)
    assert len(providers) == 2
    assert providers[0]["supports_vision"] is True
    assert providers[0]["max_context_tokens"] == 24000
    assert providers[1]["supports_vision"] is False   # 缺省 False
    assert "max_context_tokens" not in providers[1]  # 未声明则不带


def test_parse_rejects_bad_entries():
    raw = json.dumps([
        {"api_url": "ftp://bad.example", "api_key": FAKE_API_KEY, "model": "m"},  # 非 http/https
        {"api_url": "https://nokey.example", "model": "m"},                      # 缺 api_key
        {"api_url": "https://nomodel.example", "api_key": FAKE_API_KEY},         # 缺 model
        "not-a-dict",
    ])
    assert memory._parse_env_providers(raw) == []


def test_parse_rejects_non_json_and_non_list():
    assert memory._parse_env_providers("") == []
    assert memory._parse_env_providers("{broken json") == []
    assert memory._parse_env_providers('{"api_url": "https://x.example"}') == []


# ─── load_model_settings 合并顺序（普通模式） ───

def test_env_provider_order_and_dedup(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "LLM_EXTRA_PROVIDERS", json.dumps([
        {"api_url": "https://env-one.example", "api_key": FAKE_API_KEY,
         "model": "m1", "supports_vision": True},
    ]))
    # JSON 额外渠道：一个与 env 同 url+model（应被去重），一个独立（应保留）
    (tmp_path / "model_settings.json").write_text(json.dumps({
        "api_key": FAKE_API_KEY, "api_url": "https://default.example",
        "model": "default-model",
        "providers": [
            {"api_url": "https://env-one.example", "api_key": FAKE_API_KEY,
             "model": "m1"},
            {"api_url": "https://json-two.example", "api_key": FAKE_API_KEY,
             "model": "m2", "supports_vision": True},
        ],
    }, ensure_ascii=False), encoding="utf-8")

    providers = memory.load_model_settings()["providers"]
    assert [p["api_url"] for p in providers] == [
        "https://env-one.example",   # env 注入最高优先（运维显式指定的主渠道）
        "https://default.example",   # 默认渠道第二
        "https://json-two.example",  # 独立 JSON 渠道保留
    ]


def test_no_env_providers_keeps_json_only():
    providers = memory.load_model_settings()["providers"]
    # 无 env 注入时 providers[0] 恒为默认渠道（向后兼容）
    assert providers[0].get("api_url") is not None


def test_vision_pick_prefers_env_provider(monkeypatch):
    monkeypatch.setattr(config, "LLM_EXTRA_PROVIDERS", json.dumps([
        {"api_url": "https://free-vision.example", "api_key": FAKE_API_KEY,
         "model": "mm-9b", "supports_vision": True},
    ]))
    picked = llm_call._pick_provider(needs_vision=True)
    assert picked is not None
    provider, _ = picked
    assert provider["api_url"] == "https://free-vision.example"


# ─── 独占模式（LLM_PROVIDERS_EXCLUSIVE） ───

def test_exclusive_mode_drops_other_providers(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "LLM_EXTRA_PROVIDERS", json.dumps([
        {"api_url": "https://solo.example", "api_key": FAKE_API_KEY,
         "model": "mm-9b", "supports_vision": True,
         "max_context_tokens": 24000},
    ]))
    monkeypatch.setattr(config, "LLM_PROVIDERS_EXCLUSIVE", True)
    _write_settings_json(tmp_path)  # 默认渠道 + JSON 渠道都应被忽略

    ms = memory.load_model_settings()
    assert [p["api_url"] for p in ms["providers"]] == ["https://solo.example"]
    # 渠道声明 24K 窗口 → 聊天上下文从默认 800K 收紧，避免每条消息超限
    assert ms["max_context_tokens"] == 24000


def test_exclusive_mode_without_env_providers_ignored(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "LLM_PROVIDERS_EXCLUSIVE", True)
    _write_settings_json(tmp_path)

    providers = memory.load_model_settings()["providers"]
    # env 没配渠道时独占开关不生效（避免把 provider 列表清空）
    assert [p["api_url"] for p in providers] == [
        "https://default.example", "https://json-two.example",
    ]


# ─── 上下文超限检测 + failover ───

class _FakeExc(Exception):
    pass


_OVERFLOW_MSG = (
    'Error code: 400 - {"message":"request (66105 tokens) exceeds the '
    'available context size (32768 tokens)","type":"exceed_context_size_error"}'
)


def test_context_overflow_detection():
    llama_cpp = _FakeExc(_OVERFLOW_MSG)
    openai_style = _FakeExc(
        "Error code: 400 - context_length_exceeded: This model's maximum "
        "context length is 4096 tokens."
    )
    assert llm_call._is_context_overflow(llama_cpp)
    assert llm_call._is_context_overflow(openai_style)
    assert not llm_call._is_context_overflow(_FakeExc("400 - invalid message format"))


def test_llm_create_fails_over_on_context_overflow(monkeypatch):
    """小窗口 provider 超限 → 不中断，切到下一个大窗口 provider 成功返回。"""
    providers = [
        {"api_url": "https://small.example", "api_key": FAKE_API_KEY,
         "model": "small", "supports_vision": False},
        {"api_url": "https://big.example", "api_key": FAKE_API_KEY,
         "model": "big", "supports_vision": False},
    ]
    monkeypatch.setattr(memory, "load_model_settings", lambda: {"providers": providers})

    calls = []

    class FakeMessages:
        async def create(self, **kwargs):
            calls.append(kwargs.get("model"))
            if len(calls) == 1:
                raise _FakeExc(_OVERFLOW_MSG)
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="ok")],
                stop_reason="end_turn",
            )

    class FakeClient:
        messages = FakeMessages()

    monkeypatch.setattr(llm_call, "_get_or_create_client",
                        lambda api_key, api_url: FakeClient())

    resp = asyncio.run(llm_call.llm_create(
        model="placeholder", max_tokens=10, priority="P0",
        messages=[{"role": "user", "content": "hi"}],
    ))
    assert calls == ["small", "big"]      # 第一个超限后确实切了 provider
    assert resp.content[0].text == "ok"   # 用 provider 自己的 model 并成功返回


def test_llm_create_raises_when_all_providers_overflow(monkeypatch):
    """独占单渠道且超限：必须抛出（调用方兜底处理），不能 60s 死循环。"""
    providers = [
        {"api_url": "https://only.example", "api_key": FAKE_API_KEY,
         "model": "small", "supports_vision": False},
    ]
    monkeypatch.setattr(memory, "load_model_settings", lambda: {"providers": providers})

    class FakeMessages:
        async def create(self, **kwargs):
            raise _FakeExc(_OVERFLOW_MSG)

    class FakeClient:
        messages = FakeMessages()

    monkeypatch.setattr(llm_call, "_get_or_create_client",
                        lambda api_key, api_url: FakeClient())

    with pytest.raises(RuntimeError, match="上下文超限"):
        asyncio.run(llm_call.llm_create(
            model="placeholder", max_tokens=10, priority="P0",
            messages=[{"role": "user", "content": "hi"}],
        ))
