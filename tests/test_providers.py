"""Verify multi-provider setup."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from bot import memory, llm_call

ms = memory.load_model_settings()
providers = ms.get("providers", [])
print(f"Configured providers ({len(providers)}):")
for i, p in enumerate(providers):
    print(f"  [{i}] {p.get('model')} @ {p.get('api_url')}")
    print(f"      vision: {p.get('supports_vision')}")
    print(f"      key: {p.get('api_key', '')[:8]}...{p.get('api_key', '')[-4:]}")

print()
# Verify round-robin picks different providers
print("Round-robin test (5 picks):")
for i in range(5):
    picked = llm_call._pick_provider()
    if picked:
        p, _ = picked
        print(f"  pick {i}: {p.get('model')} @ {p.get('api_url')[:30]}")

# Verify vision preference
print()
print("Vision preference test (needs_vision=True):")
for i in range(3):
    picked = llm_call._pick_provider(needs_vision=True)
    if picked:
        p, _ = picked
        print(f"  pick {i}: {p.get('model')} (vision={p.get('supports_vision')})")

# Verify _kwargs_has_image detection
print()
print("Image detection test:")
test1 = {"messages": [{"role": "user", "content": "hello"}]}
test2 = {"messages": [{"role": "user", "content": [{"type": "image", "source": {}}, {"type": "text", "text": "hi"}]}]}
test3 = {"messages": [{"role": "user", "content": [{"type": "text", "text": "plain"}]}]}
print(f"  text only: {llm_call._kwargs_has_image(test1)} (expect False)")
print(f"  with image: {llm_call._kwargs_has_image(test2)} (expect True)")
print(f"  text block only: {llm_call._kwargs_has_image(test3)} (expect False)")

print()
print("OK")
