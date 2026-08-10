"""
测试两个模型是否支持 vision（图片输入）
使用项目里的 baidu.png 作为测试图
"""
import asyncio
import base64
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import httpx

API_URL = "https://api.ccode.vip/v1/messages"
API_KEY = "sk-CFUDbSygYY8yvdL2tuNzllwkJs696XGTYLVTmr0KX333oRLO"
IMG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "baidu.png")


async def test_vision(model: str) -> tuple[bool, str]:
    """测试某个模型是否支持图片输入。返回 (success, info)"""
    if not os.path.exists(IMG_PATH):
        return False, f"测试图不存在: {IMG_PATH}"

    with open(IMG_PATH, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode()

    payload = {
        "model": model,
        "max_tokens": 200,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
                {"type": "text", "text": "这张图片里有什么？用一句话描述（30字以内）。"},
            ],
        }],
    }
    headers = {
        "x-api-key": API_KEY,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(API_URL, headers=headers, json=payload)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}: {r.text[:200]}"
        data = r.json()
        text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")
        usage = data.get("usage", {})
        return True, f"返回: {text.strip()!r} | in={usage.get('input_tokens')} out={usage.get('output_tokens')} | 实际模型={data.get('model')}"
    except Exception as e:
        return False, f"异常: {type(e).__name__}: {e}"


async def main():
    print("=" * 60)
    print("  测试模型 vision 支持")
    print("=" * 60)

    for model in ["gemini-3.5-flash-free", "minimax-m3-free"]:
        print(f"\n--- {model} ---")
        ok, info = await test_vision(model)
        print(f"{'✅ 支持 vision' if ok else '❌ 不支持 vision'}")
        print(f"   {info}")

    print("\n" + "=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
