"""
QQ Bot 核心包
"""
from typing import Optional

from .onebot_client import OneBotClient
from .config import config
from . import db

# 先加载所有子模块（handler 的 from . import 需要它们在 bot 命名空间里）
from . import automod
from . import trainer
from . import blacklist
from . import personality
from . import scheduler
from . import learner
from . import memory
from . import sticker
from . import media_reader
from . import content_reader
from . import quote_handler
from . import group_personality
from . import decision_engine
from . import influence
from . import welcome
from . import guardian
from . import llm_mod
from . import ai_brain
from . import humanizer
from . import chat
from . import violation
from . import image_mod
from . import cache
from . import ai_tools
from . import consciousness
from . import web_search
from . import groups
from . import summary
from . import security
from . import settings
from . import runtime_switches

# handler 最后加载——它的 from . import 需要上面所有模块都在 bot 命名空间中
# identity 和 reply_policy 已从 handler 模块级导入移除，改为函数内懒加载
from .handler import handle_group_message, handle_notice, check_group_name, monitor_all_group_names, check_member_names

# 全局 Bot 实例引用（main.py 启动时设置，web API 读取）
bot_instance: Optional[OneBotClient] = None
