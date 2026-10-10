"""chat.py 上下文窗口冻结的前缀稳定性测试（纯逻辑，不联网不调 LLM）"""
from bot import chat


def _msg(i, content="普通消息"):
    return {"role": "user", "content": f"{content}{i}", "importance": 0.5,
            "ts": 1000.0 + i, "msg_id": i}


def test_window_start_frozen_across_appends():
    """新消息追加后窗口起点必须原地不动——这是前缀缓存的命根子"""
    gid = 999001
    chat.clear_context(gid)
    chat._chat_contexts[gid] = [_msg(i) for i in range(100)]
    start1 = chat._select_window(gid, chat._chat_contexts[gid], 100000)

    chat._chat_contexts[gid].extend(_msg(100 + i) for i in range(10))
    start2 = chat._select_window(gid, chat._chat_contexts[gid], 100000)

    assert start2 == start1
    assert (chat._msg_fingerprint(chat._chat_contexts[gid][start1])
            == chat._msg_fingerprint(chat._chat_contexts[gid][start2]))
    chat.clear_context(gid)


def test_window_stays_put_until_budget_exhausted():
    """预算没吃满之前，连续多轮追加都不应触发重裁"""
    gid = 999002
    chat.clear_context(gid)
    chat._chat_contexts[gid] = [_msg(i, "一句话内容") for i in range(100)]
    budget = 5000
    start1 = chat._select_window(gid, chat._chat_contexts[gid], budget)
    for r in range(5):
        chat._chat_contexts[gid].extend(
            _msg(1000 + r * 10 + i, "一句话内容") for i in range(10))
        start = chat._select_window(gid, chat._chat_contexts[gid], budget)
        assert start == start1, f"第{r}轮追加就发生了重裁，前缀缓存会被打碎"
    chat.clear_context(gid)


def test_window_retrims_when_budget_actually_exhausted():
    """预算真吃满才重裁，且起点只往后挪（丢更旧的），绝不回滑"""
    gid = 999003
    chat.clear_context(gid)
    chat._chat_contexts[gid] = [_msg(i, "很长很长的消息内容" * 200) for i in range(60)]
    budget = 3000
    start1 = chat._select_window(gid, chat._chat_contexts[gid], budget)
    assert start1 > 0  # 装不下全部 → 起点在中间

    last = start1
    for r in range(8):
        chat._chat_contexts[gid].extend(
            _msg(100 + r * 10 + i, "很长很长的消息内容" * 200) for i in range(10))
        start = chat._select_window(gid, chat._chat_contexts[gid], budget)
        assert start >= last, "窗口起点回滑了——这会打碎前缀缓存"
        last = start
    chat.clear_context(gid)


def test_build_context_prefix_stable(monkeypatch):
    """端到端：连续两次构建（中间隔若干新消息），公共前缀必须保持字节一致"""
    # 本地开发配置里 context_size 可能很小（会触发条数裁剪干扰本测试），
    # 显式注入一个大窗口，隔离测"追加不滑动"这件事本身
    monkeypatch.setattr(
        chat.memory, "load_model_settings",
        lambda: {"context_size": 400, "max_context_tokens": 800000},
    )
    gid = 999004
    chat.clear_context(gid)
    chat._chat_contexts[gid] = [_msg(i, "群聊流水") for i in range(80)]

    msgs1 = chat._build_context_messages(gid)
    chat._chat_contexts[gid].extend(_msg(200 + i, "群聊流水") for i in range(5))
    msgs2 = chat._build_context_messages(gid, exclude_msg_id=204)

    n = min(len(msgs1), len(msgs2))
    assert n >= len(msgs1) - 1, "旧消息凭空消失，前缀必然断裂"
    common = 0
    for a, b in zip(msgs1, msgs2):
        if a["content"] == b["content"] and a["role"] == b["role"]:
            common += 1
        else:
            break
    assert common == n, "消息列表中途发生变化，前缀缓存会在变化处全部失效"
    chat.clear_context(gid)


def test_window_empty_context():
    gid = 999005
    chat.clear_context(gid)
    assert chat._select_window(gid, [], 4000) == 0
    chat.clear_context(gid)
