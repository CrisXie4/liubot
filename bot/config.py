"""
Bot 配置管理模块
"""
import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # --- NapCat 连接 ---
    NAPCAT_WS_URL: str = os.getenv("NAPCAT_WS_URL", "ws://127.0.0.1:3001")
    NAPCAT_HTTP_URL: str = os.getenv("NAPCAT_HTTP_URL", "http://127.0.0.1:3000")
    NAPCAT_HTTP_TOKEN: str = os.getenv("NAPCAT_HTTP_TOKEN", "")
    NAPCAT_WS_TOKEN: str = os.getenv("NAPCAT_WS_TOKEN", "")

    # --- Bot 身份 ---
    BOT_QQ: str = os.getenv("BOT_QQ", "")
    OWNER_QQ: str = os.getenv("OWNER_QQ", "")

    # --- AI 聊天 ---
    LLM_API_KEY: str = os.getenv("LLM_API_KEY", "")
    LLM_API_URL: str = os.getenv("LLM_API_URL", "https://api.deepseek.com/v1")
    LLM_MODEL: str = os.getenv("LLM_MODEL", "deepseek-chat")
    LLM_TEMPERATURE: float = float(os.getenv("LLM_TEMPERATURE", "0.8"))
    LLM_MAX_TOKENS: int = int(os.getenv("LLM_MAX_TOKENS", "300"))

    # --- MiniMax 审核 ---
    MINIMAX_API_KEY: str = os.getenv("MINIMAX_API_KEY", "")
    MINIMAX_API_URL: str = os.getenv("MINIMAX_API_URL", "https://api.minimaxi.com/anthropic")
    LLM_MOD_ENABLED: bool = os.getenv("LLM_MOD_ENABLED", "true").lower() == "true"

    # --- Web UI ---
    WEB_HOST: str = os.getenv("WEB_HOST", "127.0.0.1")
    WEB_PORT: int = int(os.getenv("WEB_PORT", "8090"))
    WEB_PASSWORD: str = os.getenv("WEB_PASSWORD", "admin")

    # --- 安全 ---
    SECRET_KEY: str = os.getenv("SECRET_KEY", "")  # 加密主密钥（留空自动生成）
    JWT_EXPIRES_HOURS: int = int(os.getenv("JWT_EXPIRES_HOURS", "24"))
    WEB_ALLOWED_IPS: list[str] = [
        ip.strip() for ip in os.getenv("WEB_ALLOWED_IPS", "").split(",") if ip.strip()
    ]  # IP 白名单（逗号分隔）

    # --- Bot 行为 ---
    BOT_NAME: str = os.getenv("BOT_NAME", "小Q")
    AUTO_ACCEPT_INVITE: bool = os.getenv("AUTO_ACCEPT_INVITE", "false").lower() == "true"
    # 短时上下文消息条数（×2 = user+assistant 一轮）—— 1M 上下文模型下
    # 可以保留更多。设大点不是问题，反正会按 token 截断。
    CHAT_CONTEXT_SIZE: int = int(os.getenv("CHAT_CONTEXT_SIZE", "200"))
    # 上下文 token 上限（不写则按 model 限定的 1M 留出 200K 给回复）
    CHAT_CONTEXT_MAX_TOKENS: int = int(os.getenv("CHAT_CONTEXT_MAX_TOKENS", "800000"))
    AUTO_MOD_ENABLED: bool = os.getenv("AUTO_MOD_ENABLED", "true").lower() == "true"
    FRIEND_MODE: bool = False  # 全局默认
    _group_modes: dict = {}    # {group_id: "admin"|"friend"} — 只有手动设置的群

    # --- 数据目录 ---
    DATA_DIR: str = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

    @classmethod
    def _load_group_modes(cls):
        import json
        path = os.path.join(cls.DATA_DIR, "group_modes.json")
        if os.path.exists(path):
            try:
                with open(path, "r") as f:
                    cls._group_modes = {int(k): v for k, v in json.load(f).items()}
            except Exception:
                pass

    @classmethod
    def _save_group_modes(cls):
        import json
        os.makedirs(cls.DATA_DIR, exist_ok=True)
        path = os.path.join(cls.DATA_DIR, "group_modes.json")
        with open(path, "w") as f:
            json.dump({str(k): v for k, v in cls._group_modes.items()}, f)

    @classmethod
    def set_group_mode(cls, group_id: int, mode: str):
        """设置单个群的模式: admin / friend / auto"""
        if mode == "auto":
            cls._group_modes.pop(group_id, None)
        else:
            cls._group_modes[group_id] = mode
        cls._save_group_modes()

    @classmethod
    def get_group_mode(cls, group_id: int) -> str:
        """获取群模式：优先手动设置，否则返回 auto"""
        return cls._group_modes.get(group_id) or "auto"

    @classmethod
    def is_admin_mode(cls, group_id: int, bot_is_admin: bool = False) -> bool:
        """判断当前群是否应使用管理模式"""
        mode = cls.get_group_mode(group_id)
        if mode == "admin":
            return True
        if mode == "friend":
            return False
        return bot_is_admin

    @classmethod
    def validate(cls) -> list[str]:
        missing = []
        if not cls.OWNER_QQ: missing.append("OWNER_QQ（主人QQ号）")
        if not cls.BOT_QQ: missing.append("BOT_QQ（机器人QQ号）")
        return missing

    @classmethod
    def display(cls) -> str:
        return "\n".join([
            "=" * 50,
            "  当前配置",
            "=" * 50,
            f"  NapCat WS:     {cls.NAPCAT_WS_URL}",
            f"  NapCat HTTP:   {cls.NAPCAT_HTTP_URL}",
            f"  Bot QQ:        {cls.BOT_QQ or '(未设置)'}",
            f"  Owner QQ:      {cls.OWNER_QQ or '(未设置)'}",
            f"  Bot 名称:      {cls.BOT_NAME}",
            f"  LLM Model:     {cls.LLM_MODEL}",
            f"  LLM 审核:      {'开启' if cls.LLM_MOD_ENABLED else '关闭'} (MiniMax)",
            f"  Web UI:        http://{cls.WEB_HOST}:{cls.WEB_PORT}",
            f"  自动审核:       {'开启' if cls.AUTO_MOD_ENABLED else '关闭'}",
            f"  加密存储:       {'✓' if cls.SECRET_KEY else '自动生成'}",
            "=" * 50,
        ])


config = Config()
