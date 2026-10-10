"""图片审核防误判测试 — python tests/test_image_mod_guard.py

线上案例（2026-08-17，群 1084012604）：
群友分享自己的微信支付截图被判 advertising（82%/75%）→ 撤回 + 禁言 1 小时。
同类截图日志里也有判 normal 的（"支付页面报错截图，无推销信息"），说明模型
在支付凭证截图上随机摇摆，需要确定性防护兜底。
"""
import asyncio
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _run(coro):
    return asyncio.run(coro)


# ── 线上真实文案 ──
INCIDENT_DESC = (
    "这是一张 PC 端浏览器的网页截图，内容为一个「微信扫码支付」收款页面。"
    "页面中央是收款二维码，下方显示金额与订单信息。"
)
AD_SOLICIT_TEXT = "加微信秒到账，花呗白条套现，日赚500，佣金返利，扫码进群"

VERDICT_ADVERTISING = (
    '{"is_violation": true, "confidence": 0.82, "violation_type": "advertising", '
    '"ocr_text": "微信扫码支付 收款页面 ¥30.00", '
    '"reason": "第三方收款二维码引流"}'
)


class TestPaymentReceiptGuard(unittest.TestCase):

    def setUp(self):
        from bot import image_mod
        self.im = image_mod

    def test_payment_screenshot_downgraded(self):
        """支付凭证特征明显且无引流话术 → advertising 降级为 normal（15:34 案例）"""
        result = {
            "is_violation": True, "confidence": 0.75,
            "violation_type": "advertising",
            "ocr_text": "微信支付 ¥30.00 支付页面", "reason": "仿冒微信支付页面",
        }
        out = self.im._payment_receipt_guard(result, "支付页面报错截图")
        self.assertFalse(out["is_violation"])
        self.assertLess(out["confidence"], 0.7)

    def test_incident_desc_downgraded(self):
        """15:19 事件的图片描述文案命中防误判（无套现字样时）"""
        result = {
            "is_violation": True, "confidence": 0.82,
            "violation_type": "advertising", "ocr_text": "", "reason": "收款二维码引流",
        }
        out = self.im._payment_receipt_guard(result, INCIDENT_DESC)
        self.assertFalse(out["is_violation"])

    def test_payment_with_solicitation_kept(self):
        """支付字样 + 引流话术 → 维持违规判定（真广告不放行）"""
        result = {
            "is_violation": True, "confidence": 0.9,
            "violation_type": "advertising", "ocr_text": "", "reason": "引流",
        }
        out = self.im._payment_receipt_guard(result, "扫码支付 " + AD_SOLICIT_TEXT)
        self.assertTrue(out["is_violation"])

    def test_cashout_ad_kept(self):
        """套现广告即使带支付字样也维持违规"""
        result = {
            "is_violation": True, "confidence": 0.9,
            "violation_type": "advertising", "ocr_text": "", "reason": "套现",
        }
        out = self.im._payment_receipt_guard(result, "扫码支付 花呗套现 白条套现 秒到")
        self.assertTrue(out["is_violation"])

    def test_non_advertising_type_untouched(self):
        """非 advertising 类型（赌博/色情）不降级"""
        result = {"is_violation": True, "confidence": 0.95,
                  "violation_type": "gambling", "ocr_text": "扫码支付 四码中特", "reason": "赌博"}
        out = self.im._payment_receipt_guard(result, "扫码支付赌博网站")
        self.assertTrue(out["is_violation"])


class TestReviewImagePath(unittest.TestCase):
    """事件回放：缓存描述路径下 AI 判 advertising 82% → 复核后应为 normal"""

    def test_review_with_cached_desc_downgraded(self):
        from bot import image_mod
        resp = MagicMock()
        resp.content = [MagicMock(text=VERDICT_ADVERTISING)]

        async def fake_llm_create(*a, **kw):
            return resp

        with patch("bot.llm_call.get_client", return_value=object()), \
             patch("bot.memory.load_model_settings", return_value={"vision_model": "test-vision"}), \
             patch("bot.media_reader.get_cached_description", return_value=INCIDENT_DESC), \
             patch("bot.llm_call.llm_create", new=fake_llm_create):
            out = _run(image_mod.review_image_with_ai(bot=None, file_id="", image_url="http://x/1.jpg"))

        self.assertIsNotNone(out)
        self.assertFalse(out["is_violation"], f"支付截图仍被判违规: {out}")


if __name__ == "__main__":
    unittest.main()
