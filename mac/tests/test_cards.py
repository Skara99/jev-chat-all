"""Offline regressions for the in-chat Jev overlay cards.

No Cocoa, no screen, no API. Run: python -B -m unittest tests.test_cards -v
"""
import sys
import unittest
from types import SimpleNamespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import cards  # noqa: E402


def msg(text, side="them", x=0.36, y=0.20, w=0.28, h=0.06):
    return SimpleNamespace(text=text, side=side, x=x, y=y, w=w, h=h, sender=None, conf=1.0)


SCREENSHOT = [
    "在吗？有个小需求。",
    "做个像淘宝一样的，简单点就行。",
    "可以，顺便加个 AI。",
    "都要。明天能上线吧？",
    "行，你看着安排。",
]


class MatchTests(unittest.TestCase):
    def test_screenshot_messages_hit_the_mock_templates(self):
        expected = ["small_ask", "keep_it_simple", "by_the_way", "ship_tomorrow", "you_decide"]
        got = [cards.match_template(t).id for t in SCREENSHOT]
        self.assertEqual(got, expected)

    def test_by_the_way_beats_add_ai_on_the_screenshot_line(self):
        # 「顺便加个 AI」must hang the 「顺便」card, not the AI-weight card
        t = cards.match_template("可以，顺便加个 AI。")
        self.assertEqual(t.id, "by_the_way")

    def test_simple_beats_taobao_on_the_screenshot_line(self):
        t = cards.match_template("做个像淘宝一样的，简单点就行。")
        self.assertEqual(t.id, "keep_it_simple")

    def test_generic_chat_has_no_template(self):
        self.assertIsNone(cards.match_template("下午开会记得带材料"))

    def test_empty_text_is_none(self):
        self.assertIsNone(cards.match_template(""))
        self.assertIsNone(cards.match_template(None))


class CardTextTests(unittest.TestCase):
    def test_pending_template_card_has_ellipsis_not_numbers(self):
        c = cards.pending_card("在吗？有个小需求。")
        blob = cards.format_lines_for_test(c)
        self.assertTrue(c.pending)
        self.assertIn("Jev：", c.title)
        self.assertIn("“小需求”真的小吗？", blob)
        self.assertIn("是：…", blob)
        self.assertNotIn("%", blob)

    def test_pending_generic_card_quotes_the_message(self):
        c = cards.pending_card("下午开会记得带材料")
        blob = cards.format_lines_for_test(c)
        self.assertIn("下午开会记得带材料", blob)
        self.assertIn("正在判断", blob)

    def test_small_ask_card_matches_the_mock(self):
        v = {"intent": "派活", "confidence": 0.9, "risk": 9.0,
             "actions": ["问清交付标准和期限"]}
        extra = {
            "trap": {"type": "noul", "noul": 0.02},
            "impact": {"type": "choice",
                       "probabilities": {"改个颜色": 0.03, "重写半个项目": 0.97}},
        }
        c = cards.card_from_verdict("在吗？有个小需求。", v, extra)
        blob = cards.format_lines_for_test(c)
        self.assertIn("“小需求”真的小吗？", blob)
        self.assertIn("- 是：2%", blob)
        self.assertIn("- 否：98%", blob)
        self.assertIn("预计影响", blob)
        self.assertIn("- 改个颜色：3%", blob)
        self.assertIn("- 重写半个项目：97%", blob)
        self.assertIn("危险等级：9 / 10", blob)
        self.assertIn("先确认范围", blob)
        self.assertFalse(c.pending)

    def test_keep_it_simple_card_matches_the_mock(self):
        v = {"intent": "派活", "confidence": 0.8, "risk": 7.0, "actions": []}
        extra = {"trap": {"type": "choice",
                          "probabilities": {"功能简单": 0.01, "预算简单": 0.99}}}
        c = cards.card_from_verdict("做个像淘宝一样的，简单点就行。", v, extra)
        blob = cards.format_lines_for_test(c)
        self.assertIn("“简单点”指什么？", blob)
        self.assertIn("- 功能简单：1%", blob)
        self.assertIn("- 预算简单：99%", blob)
        self.assertIn("先确认范围", blob)

    def test_by_the_way_card_matches_the_mock(self):
        v = {"intent": "派活", "confidence": 0.9, "risk": 8.0, "actions": []}
        extra = {"trap": {"type": "noul", "noul": 1.0},
                 "impact": {"probabilities": {"可忽略": 0.0, "已超光速": 1.0}}}
        c = cards.card_from_verdict("可以，顺便加个 AI。", v, extra)
        blob = cards.format_lines_for_test(c)
        self.assertIn("“顺便”是否属于需求？", blob)
        self.assertIn("- 是：100%", blob)
        self.assertIn("已超光速：100%", blob)
        self.assertIn("询问优先级", blob)

    def test_ship_tomorrow_card_matches_the_mock(self):
        v = {"intent": "催进度", "confidence": 0.8, "risk": 8.0, "actions": []}
        extra = {"trap": {"probabilities": {
            "正常开发": 0.0, "连夜跑路": 0.12, "做个演示版": 0.88}}}
        c = cards.card_from_verdict("都要。明天能上线吧？", v, extra)
        blob = cards.format_lines_for_test(c)
        self.assertIn("正在计算可行方案", blob)
        self.assertIn("- 正常开发：0%", blob)
        self.assertIn("- 连夜跑路：12%", blob)
        self.assertIn("- 做个演示版：88%", blob)
        self.assertIn("明确「演示版」", blob)

    def test_you_decide_card_matches_the_mock(self):
        v = {"intent": "派活", "confidence": 0.7, "risk": 6.0, "actions": []}
        extra = {"trap": {"noul": 0.08}}
        c = cards.card_from_verdict("行，你看着安排。", v, extra)
        blob = cards.format_lines_for_test(c)
        self.assertIn("危机是否解除？", blob)
        self.assertIn("- 是：8%", blob)
        self.assertIn("- 只是存档了：92%", blob)
        self.assertIn("截图留证", blob)

    def test_local_backend_without_extra_still_fills_percentages(self):
        v = {"intent": "派活", "confidence": 0.9, "risk": 9.0, "actions": []}
        c = cards.card_from_verdict("在吗？有个小需求。", v, extra=None)
        blob = cards.format_lines_for_test(c)
        self.assertIn("%", blob)
        self.assertIn("危险等级：9 / 10", blob)
        self.assertNotIn("：…", blob)

    def test_generic_message_falls_back_to_intent_and_risk(self):
        v = {"intent": "约会议", "confidence": 0.86, "risk": 2.4,
             "intent_probs": {"约会议": 0.86, "派活": 0.09, "闲聊": 0.03},
             "actions": ["确认时间", "说清议程"]}
        c = cards.card_from_verdict("下午开会记得带材料", v)
        blob = cards.format_lines_for_test(c)
        self.assertIn("意图：约会议（86%）", blob)
        self.assertIn("- 派活：9%", blob)
        self.assertIn("危险等级：2 / 10", blob)
        self.assertIn("确认时间", blob)


class ExtraQuestionsTests(unittest.TestCase):
    def test_noul_template_emits_trap_and_impact(self):
        t = cards.match_template("有个小需求")
        q = cards.extra_questions(t)
        self.assertEqual(q["trap"]["type"], "noul")
        self.assertEqual(q["impact"]["type"], "choice")
        self.assertIn("改个颜色", q["impact"]["criteria"])

    def test_choice_template_emits_trap_only(self):
        t = cards.match_template("简单点就行")
        q = cards.extra_questions(t)
        self.assertEqual(q["trap"]["type"], "choice")
        self.assertNotIn("impact", q)
        self.assertIn("功能简单", q["trap"]["criteria"])


class LayoutTests(unittest.TestCase):
    def test_cards_sit_under_incoming_bubbles_not_own(self):
        them = msg("在吗？有个小需求。", y=0.20, h=0.05, x=0.36, w=0.30)
        me = msg("在的，您说。", side="me", y=0.40, h=0.05, x=0.62, w=0.22)
        cache = {them.text: cards.pending_card(them.text)}
        laid = cards.layout_cards([them, me], cache, view_w=400, view_h=800, chat_x_min=0.32)
        self.assertEqual(len(laid), 1)
        bubble_bottom = 800 - (them.y + them.h) * 800
        self.assertLess(laid[0].y + laid[0].h, bubble_bottom + 0.5)
        self.assertGreaterEqual(laid[0].x, 0.32 * 400)

    def test_two_incoming_cards_do_not_overlap(self):
        a = msg("在吗？有个小需求。", y=0.12, h=0.05)
        b = msg("简单点就行。", y=0.42, h=0.05)
        cache = {a.text: cards.pending_card(a.text),
                 b.text: cards.pending_card(b.text)}
        laid = cards.layout_cards([a, b], cache, 400, 800, input_y_min=0.0)
        self.assertEqual(len(laid), 2)
        first, second = laid
        self.assertTrue(first.y + first.h <= second.y + 0.5
                        or second.y + second.h <= first.y + 0.5)

    def test_tight_gap_still_paints_the_card_to_match_the_mock(self):
        # Real WeChat spacing is too tight for a 6-line card. Dropping it
        # would hide the screenshot's whole point, so we overlay anyway.
        a = msg("在吗？有个小需求。", y=0.20, h=0.05)
        b = msg("简单点就行。", y=0.27, h=0.05)
        cache = {a.text: cards.pending_card(a.text),
                 b.text: cards.pending_card(b.text)}
        laid = cards.layout_cards([a, b], cache, 400, 800, input_y_min=0.0)
        keys = {item.card.key for item in laid}
        self.assertIn(a.text, keys)
        self.assertIn(b.text, keys)

    def test_input_area_clips_the_bottom_of_a_card(self):
        # y=0.62 → bubble bottom at 264pt, input floor at 192pt: room for a
        # clipped card but not the full ~130pt pending card.
        m = msg("在吗？有个小需求。", y=0.62, h=0.05)
        cache = {m.text: cards.pending_card(m.text)}
        laid = cards.layout_cards([m], cache, 400, 800, input_y_min=0.24)
        self.assertEqual(len(laid), 1)
        self.assertGreaterEqual(laid[0].y, 0.24 * 800 - 0.5)
        self.assertTrue(laid[0].clipped)

    def test_transcript_card_uses_small_print(self):
        pending = cards.pending_card("在吗？有个小需求。")
        self.assertEqual(cards.format_card_for_transcript(pending), ["Jev：分析中…"])
        v = {"intent": "派活", "confidence": 0.9, "risk": 9.0,
             "actions": ["问清交付标准和期限"]}
        extra = {"trap": {"type": "noul", "noul": 0.02}}
        done = cards.card_from_verdict("在吗？有个小需求。", v, extra)
        lines = cards.format_card_for_transcript(done)
        self.assertTrue(lines[0].startswith("Jev："))
        self.assertTrue(any("是：2%" in ln or "否：98%" in ln for ln in lines))

    def test_own_messages_never_get_a_card(self):
        me = msg("演示版。", side="me", y=0.50)
        cache = {me.text: cards.pending_card(me.text)}
        laid = cards.layout_cards([me], cache, 400, 800)
        self.assertEqual(laid, [])


if __name__ == "__main__":
    unittest.main()
