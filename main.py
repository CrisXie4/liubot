#!/usr/bin/env python3
"""
QQ 智能群管机器人 v2.0 — 主入口

功能:
  - 主人手动标记违规 + 自动撤回
  - 自动审核引擎（关键词权重 + ML 分类器）
  - 黑名单管理
  - AI 聊天（可配置人设）
  - Web 控制面板（仪表盘 + 管理）
  - 定时消息

架构:
  NapCatQQ (协议端) ←→ OneBot v11 ←→ Python Bot + Web UI

使用:
  1. 确保 NapCatQQ 已启动并登录
  2. 配置 .env 文件
  3. pip install -r requirements.txt
  4. python main.py
  5. 打开 http://127.0.0.1:8090 访问控制面板
"""
import asyncio
import logging
import random
import signal
import sys
import threading
import time

import uvicorn

import bot as bot_mod
from bot import OneBotClient, config, db, handle_group_message, handle_notice, automod, trainer, scheduler, security, timeutil
config._load_group_modes()
from web.server import app as web_app

# ─── 初始化安全模块 ───
security.init_security(secret_key=config.SECRET_KEY)

# ==================== 日志 ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("qqbot")
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("aiohttp").setLevel(logging.WARNING)
logging.getLogger("anthropic").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("uvicorn").setLevel(logging.WARNING)

# 全局 Bot 实例引用已移至 bot.__init__.py 中，此处不再重复定义


# ==================== Web 服务器 ====================

class UvicornThread(threading.Thread):
    """在独立线程中运行 Uvicorn"""
    def __init__(self):
        super().__init__(daemon=True)
        self._server: uvicorn.Server | None = None

    def run(self):
        config_obj = uvicorn.Config(
            web_app,
            host=config.WEB_HOST,
            port=config.WEB_PORT,
            log_level="warning",
        )
        self._server = uvicorn.Server(config_obj)
        self._server.run()

    def stop(self):
        if self._server:
            self._server.should_exit = True


# ==================== 主程序 ====================

async def main():
    print(r"""
   ____  ____     ____        __
  / __ \/ __ \   / __ )____  / /_
 / / / / / / /  / __  / __ \/ __/
/ /_/ / /_/ /  / /_/ / /_/ / /
\___\_\____/  /_____/\____/\__/

   QQ 智能群管机器人  v2.0
   Web UI: """ + f"http://{config.WEB_HOST}:{config.WEB_PORT}")

    # --- 验证配置 ---
    missing = config.validate()
    if missing:
        logger.error("缺少必要配置:")
        for item in missing:
            logger.error(f"  - {item}")
        logger.error("请编辑 .env 文件后重新启动")
        sys.exit(1)

    logger.info(config.display())

    # --- 初始化 ---
    db.init_db()
    logger.info("数据库已初始化")

    # 启动 Web UI
    web_thread = UvicornThread()
    web_thread.start()
    logger.info(f"🌐 Web 控制面板: http://{config.WEB_HOST}:{config.WEB_PORT}")

    # 创建 Bot
    bot = OneBotClient()
    bot_mod.bot_instance = bot

    @bot.on_event
    async def on_event(client: OneBotClient, event: dict):
        post_type = event.get("post_type", "")
        if post_type == "message":
            message_type = event.get("message_type", "")
            if message_type == "group":
                await handle_group_message(client, event)
            elif message_type == "private":
                user_id = event.get("user_id", 0)
                raw = event.get("raw_message", "").strip()

                # 注：加群验证已改为群内进行（@ 出题 + 群内判题 + 答错踢出），
                # 不再走私聊验证流程。详见 bot/verify.py。

                # 主人私聊命令
                # ── 主人命令优先 ──
                is_owner = str(user_id) == config.OWNER_QQ
                owner_cmd_handled = False
                if is_owner and raw:
                    from bot import guardian, blacklist
                    parts = raw.split(maxsplit=1)
                    cmd = parts[0] if parts else ""
                    arg = parts[1] if len(parts) > 1 else ""
                    if cmd in ("同意", "批准") and arg:
                        owner_cmd_handled = True
                        detail = guardian.approve_action(arg)
                        if detail:
                            if detail["action"] == "kick":
                                await client._call("set_group_kick",
                                    {"group_id": detail["group_id"], "user_id": detail["user_qq"]})
                                await client.send_group_msg(detail["group_id"],
                                    f"👢 {detail['user_name']}({detail['user_qq']}) 已被移出群聊")
                            elif detail["action"] == "blacklist":
                                blacklist.add_to_blacklist(detail["user_qq"], detail["user_name"],
                                    detail["group_id"], detail["reason"], "ban", user_id)
                                await client.send_group_msg(detail["group_id"],
                                    f"🚫 {detail['user_name']}({detail['user_qq']}) 已被拉黑")
                            await client.send_private_msg(user_id,
                                f"✅ 已执行: {detail['user_name']} {detail['action']}")
                        else:
                            await client.send_private_msg(user_id, f"审批 ID {arg} 不存在或已过期")
                    elif cmd in ("拒绝", "驳回") and arg:
                        owner_cmd_handled = True
                        detail = guardian.reject_action(arg)
                        if detail:
                            await client.send_private_msg(user_id,
                                f"❌ 已拒绝: {detail['user_name']} 的 {detail['action']}")
                        else:
                            await client.send_private_msg(user_id, f"审批 ID {arg} 不存在或已过期")
                    elif cmd == "待审批":
                        owner_cmd_handled = True
                        pending = guardian.get_pending_actions()
                        if not pending:
                            await client.send_private_msg(user_id, "📭 没有待审批的操作")
                        else:
                            lines = ["📋 待审批操作:"]
                            for p in pending:
                                lines.append(f"[{p['id']}] {p['action']} — {p['user_name']}({p['user_qq']})")
                                lines.append(f"  消息: {p['message'][:60]}")
                                lines.append(f"  理由: {p['reason']}")
                            lines.append("\n回复 同意 <ID> 或 拒绝 <ID>")
                            await client.send_private_msg(user_id, "\n".join(lines))

                # ── 非命令消息 → 纯 AI 聊天（主人和普通用户都走这里）──
                if not owner_cmd_handled and raw:
                    try:
                        from bot import chat as chat_mod
                        from bot.humanizer import send_human_like

                        # 获取用户昵称
                        user_name = str(user_id)
                        try:
                            info = await client.get_stranger_info(user_id)
                            if info:
                                user_name = info.get("nickname", "") or user_name
                        except Exception:
                            pass

                        # AI 聊天（注入群聊记忆）
                        reply = await chat_mod.chat_private_reply(user_id, user_name, raw)
                        if reply:
                            await send_human_like(client, 0, reply, private_user_id=user_id)
                        else:
                            await client.send_private_msg(user_id,
                                f"你好~ 我是{config.BOT_NAME}！想聊什么都可以跟我说～")
                    except Exception as e:
                        logger.warning(f"私聊AI异常: {e}")
                        await client.send_private_msg(user_id, "唔…脑子有点卡，待会再说？")
        elif post_type == "notice":
            await handle_notice(client, event)
        elif post_type == "request":
            # 🔑 加群验证
            from bot import verify as _verify
            asyncio.create_task(_verify.handle_group_add_request(client, event))

    # 设置调度器的消息发送函数
    from bot.scheduler import scheduler as sched
    sched.set_sender(lambda gid, msg: bot.send_group_msg(gid, msg))

    # 启动调度器
    asyncio.create_task(sched.start())

    # 启动权威时间同步（后台：启动时立即同步 + 每 30min 一次）
    from bot import timeutil
    asyncio.create_task(timeutil.start_background_sync())
    logger.info("启动时间同步后台任务（10 源随机抽样，每 30min 同步）")

    # 尝试加载 ML 模型
    if config.AUTO_MOD_ENABLED:
        trainer.train_if_ready()

    # ── 意识系统 + 定时任务 ──
    from bot.summary import generate_and_publish_summary
    from bot import consciousness as cs, guardian
    from bot.ai_brain import (
        share_news_if_ready, sync_qq_status,
        maybe_proactive_action, daily_self_summary,
        maybe_auto_essence, is_big_news,
    )
    _last_summary_date = None
    _last_reflection_hour = -1
    # 设为 time.time() - 590，让 consciousness_loop 启动后 10 秒内触发首次 QQ 状态同步
    # 必须在 bot.connect() 之后（WS 已连接）才能调 API
    _last_status_sync = time.time() - 590

    async def consciousness_loop():
        """意识主循环 — 每分钟 tick 一次"""
        nonlocal _last_summary_date, _last_reflection_hour, _last_status_sync
        # 播报醒来（只记内心状态，不要写成问候语——日记会被喂给模型当上下文）
        state = cs.get_state()
        if not state.get("sleeping"):
            cs.add_diary(state, "上线了", "awake")
            cs.save()

        while True:
            await asyncio.sleep(60)
            now = timeutil.now_shanghai()
            today = now.strftime("%Y%m%d")
            current_hour = now.hour

            # ── 1. 意识 tick（情绪衰减、昼夜节律、日记等）──
            state = cs.get_state()
            cs.tick(state)

            # ── 1.1 补发暂存回复（429 重试耗尽时入队的请求）──
            try:
                from bot import llm_call as _llm
                await _llm.flush_pending_replies(bot)
            except Exception as e:
                logger.debug(f"补发队列扫描跳过: {e}")

            # ── 1.5 空闲思考（群安静时 LLM 自主产生内心戏）──
            if cs.should_idle_think(state):
                asyncio.create_task(cs.generate_idle_thought(state))

            # ── 2. QQ 状态同步（每 10 分钟检查一次）──
            if time.time() - _last_status_sync > 600:
                _last_status_sync = time.time()
                try:
                    await sync_qq_status(bot)
                except Exception as e:
                    logger.debug(f"状态同步跳过: {e}")

            # ── 3. 记忆压缩（每 6 小时）──
            if current_hour % 6 == 0 and now.minute == 0:
                asyncio.create_task(bot_mod.memory.compress_memories())

            # ── 4. 主动行为（意识驱动：无聊找人聊天、好奇心学习等）──
            if not state.get("sleeping") and cs.should_be_active(state):
                try:
                    proactive_msg = await maybe_proactive_action(bot)
                    if proactive_msg:
                        r = await bot._call("get_group_list", {})
                        groups = r.get("data", []) if r.get("status") == "ok" else []
                        for g in groups:
                            gid = g.get("group_id", 0)
                            if gid:
                                await bot.send_group_msg(gid, proactive_msg)
                                await asyncio.sleep(random.uniform(4, 7))
                except Exception as e:
                    logger.debug(f"主动行为跳过: {e}")

            # ── 5. 新闻分享（每 2 小时）──
            try:
                news = await share_news_if_ready(bot)
                if news:
                    big = is_big_news(
                        news if isinstance(news, str)
                        else " ".join(s.get("data", {}).get("text", "") for s in news if isinstance(s, dict))
                    )
                    r = await bot._call("get_group_list", {})
                    groups = r.get("data", []) if r.get("status") == "ok" else []
                    for g in groups:
                        gid = g.get("group_id", 0)
                        if not gid:
                            continue
                        msg_to_send = news
                        # 重大新闻：满足限速 + @全体额度 → 给该群加 [CQ:at,qq=all]
                        if big and guardian.check_at_all_rate(gid):
                            try:
                                remain = await bot.get_group_at_all_remain(gid)
                                can = bool(remain and remain.get("can_at_all"))
                            except Exception:
                                can = False
                            if can:
                                if isinstance(msg_to_send, str):
                                    msg_to_send = "[CQ:at,qq=all] " + msg_to_send
                                elif isinstance(msg_to_send, list):
                                    msg_to_send = [{"type": "at", "data": {"qq": "all"}}] + msg_to_send
                        await bot.send_group_msg(gid, msg_to_send)
                        await asyncio.sleep(random.uniform(5, 8))
            except Exception as e:
                logger.debug(f"新闻分享跳过: {e}")

            # ── 6. 每日 17:00 群聊总结 ──
            if current_hour == 17 and now.minute == 0 and _last_summary_date != today:
                _last_summary_date = today
                logger.info("⏰ 触发每日 17:00 群聊总结")
                try:
                    r = await bot._call("get_group_list", {})
                    groups = r.get("data", []) if r.get("status") == "ok" else []
                    for g in groups:
                        gid = g.get("group_id", 0)
                        if gid:
                            try:
                                # 使用群级 Bot 名称（优先群人设 > 全局人设 > .env）
                                from bot.group_personality import get_group_bot_name
                                bot_name = get_group_bot_name(gid)
                                summary_text = await generate_and_publish_summary(bot, gid, bot_name)
                                # 总结同步上传群文件（.txt），方便群友回看
                                if summary_text:
                                    try:
                                        import os as _os
                                        import tempfile as _tf
                                        date_tag = now.strftime("%Y%m%d")
                                        fn = f"{date_tag}_总结.txt"
                                        fd, tmp_path = _tf.mkstemp(suffix=".txt", prefix="qqbot_summary_")
                                        try:
                                            with _os.fdopen(fd, "w", encoding="utf-8") as f:
                                                f.write(summary_text)
                                            await bot.upload_group_file(gid, tmp_path, fn)
                                            logger.info(f"📤 群{gid} 总结已传群文件: {fn}")
                                        finally:
                                            try:
                                                _os.remove(tmp_path)
                                            except OSError:
                                                pass
                                    except Exception as e:
                                        logger.debug(f"总结传群文件失败 群{gid}: {e}")
                                await asyncio.sleep(random.uniform(3, 5))
                            except Exception as e:
                                logger.warning(f"群{gid}自动总结失败: {e}")
                except Exception as e:
                    logger.error(f"每日总结异常: {e}")

            # ── 7. 自我反思（每天 23:00）──
            if current_hour == 23 and _last_reflection_hour != 23:
                _last_reflection_hour = 23
                logger.info("🧘 触发每日自我反思")
                try:
                    reflection = await daily_self_summary()
                    if reflection:
                        logger.info(f"今日反思: {reflection[:80]}")
                except Exception as e:
                    logger.debug(f"自我反思异常: {e}")
            elif current_hour != 23:
                _last_reflection_hour = current_hour

            # ── 8. 定时群名/昵称扫描（每 2 天，凌晨 3:00）──
            _last_name_scan_date = getattr(consciousness_loop, '_last_name_scan_date', '')
            today_str = now.strftime('%Y-%m-%d')
            # 每2天（偶数日）凌晨3:00执行一次
            if now.hour == 3 and now.minute == 0 and _last_name_scan_date != today_str:
                if timeutil.today_shanghai().day % 2 == 0:
                    asyncio.create_task(
                        bot_mod.monitor_all_group_names(bot)
                    )
                    consciousness_loop._last_name_scan_date = today_str

            # ── 9. 自主设精华（每 30 分钟：整点 / 半点）──
            if now.minute in (0, 30):
                try:
                    n = await maybe_auto_essence(bot)
                    if n:
                        logger.info(f"⭐ 自主设了 {n} 条精华")
                except Exception as e:
                    logger.debug(f"自主设精华跳过: {e}")

            # ── 保存意识状态 ──
            if now.minute % 5 == 0:
                cs.save()

    asyncio.create_task(consciousness_loop())

    # --- 启动 ---
    try:
        await bot.connect()
    except asyncio.CancelledError:
        pass
    finally:
        await bot.close()
        await sched.stop()
        web_thread.stop()


async def shutdown(bot: OneBotClient):
    logger.info("正在关闭 Bot...")
    await bot.close()
    sys.exit(0)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("用户中断")
    except Exception as e:
        logger.error(f"致命错误: {e}")
        raise
