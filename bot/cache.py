"""
通用 TTL 缓存模块
- 带过期时间的字典缓存
- 支持可选的 maxsize 淘汰策略
- 线程安全（适合 asyncio 单线程模型）
"""
import time
import threading
from collections import OrderedDict
from functools import wraps
from typing import Any, Callable, Optional


class TTLCache:
    """带 TTL 的本地缓存"""

    def __init__(self, ttl: float = 60.0, maxsize: int = 256, name: str = "cache"):
        """
        Args:
            ttl: 缓存条目的生存时间（秒）
            maxsize: 最大条目数，超出后淘汰最旧的
            name: 缓存名称，用于日志
        """
        self._ttl = ttl
        self._maxsize = maxsize
        self._name = name
        self._store: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        """获取缓存值，过期返回 None"""
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expiry, value = entry
            if time.time() > expiry:
                del self._store[key]
                return None
            # 移到末尾（LRU）
            self._store.move_to_end(key)
            return value

    def set(self, key: str, value: Any, ttl: Optional[float] = None):
        """存入缓存"""
        expiry = time.time() + (ttl if ttl is not None else self._ttl)
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
            self._store[key] = (expiry, value)
            # 淘汰最旧的
            while len(self._store) > self._maxsize:
                self._store.popitem(last=False)

    def invalidate(self, key: Optional[str] = None):
        """使缓存失效：指定 key 或全部"""
        with self._lock:
            if key is None:
                self._store.clear()
            else:
                self._store.pop(key, None)

    def prune(self):
        """清理所有过期条目"""
        now = time.time()
        with self._lock:
            expired = [k for k, (expiry, _) in self._store.items() if now > expiry]
            for k in expired:
                del self._store[k]

    def stats(self) -> dict:
        """缓存命中统计"""
        with self._lock:
            return {
                "name": self._name,
                "size": len(self._store),
                "maxsize": self._maxsize,
                "ttl": self._ttl,
            }


def ttl_cache(ttl: float = 60.0, maxsize: int = 128, key_fn: Optional[Callable] = None):
    """装饰器：为函数添加 TTL 缓存

    缓存 key 基于函数名 + 参数组合。
    可通过 key_fn 自定义 key 生成逻辑。
    """
    cache = TTLCache(ttl=ttl, maxsize=maxsize)

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            if key_fn:
                cache_key = key_fn(*args, **kwargs)
            else:
                # 默认 key: func_name + str(args) + str(sorted kwargs)
                raw = f"{func.__module__}.{func.__qualname__}:{args}:{sorted(kwargs.items())}"
                cache_key = raw
            result = cache.get(cache_key)
            if result is not None:
                return result
            result = func(*args, **kwargs)
            cache.set(cache_key, result)
            return result

        wrapper.cache = cache
        wrapper.cache_invalidate = cache.invalidate
        return wrapper

    return decorator


# ═══════════════════════════════════════════════════════════
#  全局缓存实例
# ═══════════════════════════════════════════════════════════

# 关键词权重缓存（每 30 秒刷新一次）
keyword_weights_cache = TTLCache(ttl=30, maxsize=2, name="keyword_weights")

# 学习知识库缓存（每 10 秒刷新）
learned_knowledge_cache = TTLCache(ttl=10, maxsize=2, name="learned_knowledge")

# 人设配置缓存（每 30 秒刷新）
personality_cache = TTLCache(ttl=30, maxsize=2, name="personality")

# 模型设置缓存（每 30 秒刷新）
model_settings_cache = TTLCache(ttl=30, maxsize=2, name="model_settings")

# Bot 管理员状态缓存（每 5 分钟过期，支持状态变更）
bot_admin_cache = TTLCache(ttl=300, maxsize=100, name="bot_admin")

# jieba 分词结果缓存（永久，但有上限）
jieba_cache = TTLCache(ttl=3600, maxsize=5000, name="jieba")

# 违规统计计数缓存（每 15 秒刷新）
violation_count_cache = TTLCache(ttl=15, maxsize=2, name="violation_count")

# 群组模式缓存（每 60 秒刷新）
group_mode_cache = TTLCache(ttl=60, maxsize=50, name="group_mode")

# 群组成员列表缓存（每 60 秒刷新）
group_member_list_cache = TTLCache(ttl=60, maxsize=30, name="group_member_list")

# 用户画像缓存（每 120 秒刷新）
user_profile_cache = TTLCache(ttl=120, maxsize=200, name="user_profile")

# 群级人设缓存（每 30 秒刷新）
group_personality_cache = TTLCache(ttl=30, maxsize=100, name="group_personality")

# 群友影响力缓存（每 120 秒刷新）
influence_cache = TTLCache(ttl=120, maxsize=200, name="influence")


# ═══════════════════════════════════════════════════════════
#  辅助函数
# ═══════════════════════════════════════════════════════════

def cached_jieba_lcut(text: str) -> list[str]:
    """带缓存的 jieba 分词（减少重复分词开销）"""
    if len(text) > 100:
        # 长文本直接分词，不缓存（缓存命中率低）
        import jieba
        return jieba.lcut(text)
    result = jieba_cache.get(text)
    if result is not None:
        return result
    import jieba
    result = jieba.lcut(text)
    jieba_cache.set(text, result)
    return result
