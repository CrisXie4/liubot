"""
测试 llm_create 路径：
1. 加载 model_settings，确认主模型 + fallback 配置
2. 用主模型调用一次（应成功）
3. 模拟主模型失败（用一个不存在的模型名）→ 自动降级到 fallback
"""
import asyncio
import os
import sys
import logging

# Windows 控制台默认 GBK，强制 UTF-8 输出（避免 emoji 报错）
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# 让脚本能 import bot.*（项目根目录在 tests/ 的上一级）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

from bot import memory, llm_call


async def test_primary():
    """测试主模型 gemini-3.5-flash-free"""
    ms = memory.load_model_settings()
    print(f"\n[配置] 主模型: {ms.get('model')}")
    print(f"[配置] Fallback: {ms.get('fallback_model')}")
    print(f"[配置] API URL: {ms.get('api_url')}")
    print(f"[配置] API Key: {ms.get('api_key', '')[:6]}...{ms.get('api_key', '')[-4:]}")

    print("\n--- 测试 1: 主模型 gemini-3.5-flash-free ---")
    try:
        resp = await llm_call.llm_create(
            model=ms.get("model", "gemini-3.5-flash-free"),
            max_tokens=50,
            messages=[{"role": "user", "content": "用一句话介绍你自己，10字以内"}],
        )
        text = resp.content[0].text.strip()
        print(f"✅ 主模型成功: {text}")
        print(f"   用量: in={resp.usage.input_tokens} out={resp.usage.output_tokens}")
        return True
    except Exception as e:
        print(f"❌ 主模型失败: {type(e).__name__}: {e}")
        return False


async def test_fallback_direct():
    """直接测试 fallback 模型 minimax-m3-free"""
    ms = memory.load_model_settings()
    fb = ms.get("fallback_model", "minimax-m3-free")
    print(f"\n--- 测试 2: 直接调用 fallback 模型 {fb} ---")
    try:
        resp = await llm_call.llm_create(
            model=fb,
            max_tokens=50,
            messages=[{"role": "user", "content": "用一句话介绍你自己，10字以内"}],
        )
        text = resp.content[0].text.strip()
        print(f"✅ Fallback 成功: {text}")
        print(f"   用量: in={resp.usage.input_tokens} out={resp.usage.output_tokens}")
        return True
    except Exception as e:
        print(f"❌ Fallback 失败: {type(e).__name__}: {e}")
        return False


async def test_fallback_trigger():
    """测试主模型不存在时，自动降级到 fallback"""
    ms = memory.load_model_settings()
    fb = ms.get("fallback_model", "minimax-m3-free")
    print(f"\n--- 测试 3: 主模型用一个不存在的名字 → 应自动降级到 {fb} ---")
    try:
        resp = await llm_call.llm_create(
            model="this-model-does-not-exist-xyz-12345",
            max_tokens=50,
            messages=[{"role": "user", "content": "say hello"}],
        )
        text = resp.content[0].text.strip()
        print(f"✅ 自动降级成功: {text}")
        print(f"   实际返回模型: {getattr(resp, 'model', '?')}")
        return True
    except Exception as e:
        print(f"❌ 自动降级失败: {type(e).__name__}: {e}")
        return False


async def main():
    print("=" * 60)
    print("  测试 llm_call.llm_create — 主模型 + fallback")
    print("=" * 60)

    ok1 = await test_primary()
    ok2 = await test_fallback_direct()
    ok3 = await test_fallback_trigger()

    print("\n" + "=" * 60)
    print(f"  结果汇总: 主模型 {'✅' if ok1 else '❌'} | "
          f"Fallback {'✅' if ok2 else '❌'} | "
          f"自动降级 {'✅' if ok3 else '❌'}")
    print("=" * 60)

    if ok1 and ok2 and ok3:
        print("\n🎉 全部通过！主备模型都能用，自动降级也工作正常。")
        sys.exit(0)
    else:
        print("\n⚠️ 有失败项，请查看上方日志。")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
