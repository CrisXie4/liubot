"""
自动表情表态 — AI 看到群消息时低概率主动贴表情（不说话）
================================================================
模拟真人在群里的"潜水参与"：看到好笑的贴个😂，赞同的贴个👍。
不是每条都贴，低概率触发 + LLM 判断 + 限速三重控制。
"""
import logging
import random

logger = logging.getLogger("qqbot")

EMOJI_MAP = {
    "128514": "😂",
    "128513": "😁",
    "128512": "😀",
    "128525": "😍",
    "128077": "👍",
    "128079": "👏",
    "128293": "🔥",
    "128591": "🙏",
    "100": "💯",
    "128522": "😊",
}


async def maybe_react(bot, group_id: int, message_id: int, text: str, trigger_rate: float = 0.12):
    """低概率对一条群消息贴表情表态。

    Args:
        bot: OneBotClient
        group_id: 群号
        message_id: 要表态的消息 ID
        text: 消息纯文本（剥 CQ 码后）
        trigger_rate: 触发评估的概率（默认 12%）
    """
    if not message_id or not text or len(text.strip()) < 3:
        return
    if random.random() > trigger_rate:
        return

    from . import guardian
    if not guardian.check_auto_emoji_rate(group_id):
        return

    try:
        from . import llm_call, memory
        ms = memory.load_model_settings()
        if not ms.get("api_key"):
            return

        response = await llm_call.llm_create(
            priority="P3",
            model=ms.get("model", "gemini-3.5-flash-free"),
            max_tokens=10,
            temperature=0.3,
            system=(
                "你看一条群消息，决定要不要贴表情表态。"
                "只有好笑/赞同/惊讶/感动/给力时才贴。中性/无聊/看不懂就 NONE。"
                "只输出一个 emoji_id 或 NONE。可选: 128514(😂) 128513(😁) 128077(👍) "
                "128079(👏) 128293(🔥) 128525(😍) 100(💯) 128591(🙏) 128522(😊)"
            ),
            messages=[{"role": "user", "content": f"消息: {text[:100]}"}],
        )
        raw = response.content[0].text.strip()
        if raw.upper() == "NONE" or not raw:
            return

        emoji_id = "".join(c for c in raw if c.isdigit())
        if not emoji_id or emoji_id not in EMOJI_MAP:
            return

        ok = await bot.set_msg_emoji_like(message_id, emoji_id)
        if ok:
            logger.info(f"😎 自动表态 群{group_id} mid={message_id} → {EMOJI_MAP[emoji_id]}")
    except Exception as e:
        logger.debug(f"自动表态异常: {e}")
