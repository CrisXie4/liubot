"""消息编号烤入上下文测试 — python tests/test_msg_id_in_context.py

AI 要引用旧消息就得看得见消息 ID：add_context 入库时把 (msg:ID) 烤进
正文尾部（历史写入后不改写、保前缀缓存，所以不能渲染时再拼）。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import chat

GID = 777001


class TestMsgIdInContext(unittest.TestCase):

    def setUp(self):
        chat._chat_contexts.pop(GID, None)

    def tearDown(self):
        chat._chat_contexts.pop(GID, None)

    def test_msg_id_baked_into_content(self):
        chat.add_context(GID, "user", "[嘟嘟] 说: 周末去爬山吗", 0.4, msg_id=123456)
        entry = chat._chat_contexts[GID][-1]
        self.assertIn("(msg:123456)", entry["content"])
        self.assertEqual(entry["msg_id"], 123456)  # 去重用的字段不受影响

    def test_no_msg_id_content_untouched(self):
        chat.add_context(GID, "assistant", "好耶 一起去")
        entry = chat._chat_contexts[GID][-1]
        self.assertNotIn("(msg:", entry["content"])
        self.assertEqual(entry["msg_id"], 0)

    def test_build_context_messages_render_id(self):
        """AI 可见的上下文里带着编号"""
        chat.add_context(GID, "user", "[张三] 说: 那个接口文档发我下", 0.4, msg_id=987654)
        msgs = chat._build_context_messages(GID)
        joined = "\n".join(m["content"] for m in msgs)
        self.assertIn("(msg:987654)", joined)

    def test_baked_line_is_stable(self):
        """同一消息两次读上下文，内容逐字节一致（前缀缓存前提）"""
        chat.add_context(GID, "user", "[李四] 说: 收到", 0.4, msg_id=42)
        first = chat._chat_contexts[GID][-1]["content"]
        again = chat._build_context_messages(GID)[0]["content"]
        self.assertEqual(first, again)


if __name__ == "__main__":
    unittest.main()
