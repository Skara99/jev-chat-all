"""In-chat Jev analysis cards — visual overlay only, never written into WeChat.

The mock in the product screenshot hangs a grey "Jev:" card under every incoming
bubble. We cannot inject into WeChat, so the cards live on the existing
click-through overlay (`src/hud.py` `_BoxesView`). This module owns:

  * matching a message against workplace-trap templates (the screenshot's
    「小需求 / 简单点 / 顺便 / 明天上线」family)
  * turning a judge verdict (intent + risk, plus optional extra Jev answers)
    into the lines the overlay paints
  * laying those cards out under each incoming bubble without colliding

Nothing here talks to WeChat. Nothing here changes `judge.INTENTS`.
"""

from __future__ import annotations

from dataclasses import dataclass

from judge import ACTION_MAP, INTENTS


# Grey card matching the mock: #EDEDED fill, #333333 body, 12-pt system.
CARD_FILL = (0xED, 0xED, 0xED, 0.94)
CARD_STROKE = (0xD8, 0xD8, 0xD8, 1.0)
CARD_TEXT = (0x33, 0x33, 0x33, 1.0)
CARD_MUTED = (0x66, 0x66, 0x66, 1.0)
CARD_PAD_X = 10.0
CARD_PAD_Y = 8.0
CARD_LINE_H = 16.0
CARD_TITLE_H = 18.0
CARD_GAP_AFTER_BUBBLE = 4.0
CARD_CORNER = 6.0
CARD_MAX_W_PT = 260.0
CARD_MIN_W_PT = 168.0
# Leave this much of the chat pane empty under a card so the next bubble
# (and the input box) stay clickable / readable.
CARD_BOTTOM_MARGIN = 8.0
# Overlay cards for at most this many incoming messages per frame.
MAX_CARDS = 6


@dataclass(frozen=True)
class TrapOption:
    label: str
    key: str


@dataclass(frozen=True)
class TrapTemplate:
    """A workplace-trap question the screenshot hangs under a bubble.

    `needles` are substrings; matching is case-insensitive on the raw OCR text.
    `kind` is "noul" (yes/no, shown as 是/否) or "choice" (named options).
    """
    id: str
    needles: tuple[str, ...]
    quote: str                 # the phrase the card puts in 「」
    question: str              # the line under the quote
    kind: str                  # "noul" | "choice"
    options: tuple[TrapOption, ...]
    impact: tuple[TrapOption, ...] = ()
    action: str = ""
    instructions: str = ""     # extra Jev question text (optional)
    criteria: dict | tuple | None = None


# Screenshot-faithful templates first; broader workplace traps after.
# Order is match priority: first hit wins, one card per message.
TEMPLATES: tuple[TrapTemplate, ...] = (
    TrapTemplate(
        id="small_ask",
        needles=("小需求", "小改动", "小功能", "顺便改一下", "就改一点点"),
        quote="小需求",
        question="“小需求”真的小吗？",
        kind="noul",
        options=(TrapOption("是", "yes"), TrapOption("否", "no")),
        impact=(TrapOption("改个颜色", "tiny"), TrapOption("重写半个项目", "rewrite")),
        action="先确认范围，别说「好的」。",
        instructions="这句话里的「小需求 / 小改动」是不是真的小？",
        criteria={"true": "改动面小、不影响现有模块",
                  "false": "听起来小，实际会动到核心或半个项目"},
    ),
    TrapTemplate(
        id="keep_it_simple",
        needles=("简单点", "简单就行", "做个简单的", "别做太复杂", "差不多就行"),
        quote="简单点",
        question="“简单点”指什么？",
        kind="choice",
        options=(TrapOption("功能简单", "feature"), TrapOption("预算简单", "budget")),
        action="先确认范围，别说「好的」。",
        instructions="对方说「简单点」时，更可能指功能范围小，还是预算/工期紧？",
        criteria={"功能简单": "只要核心功能、可以砍需求",
                  "预算简单": "钱少、人少、时间紧，功能并不能少"},
    ),
    TrapTemplate(
        id="by_the_way",
        needles=("顺便", "顺手", "加个", "再加一个", "也加上"),
        quote="顺便",
        question="“顺便”是否属于需求？",
        kind="noul",
        options=(TrapOption("是", "yes"), TrapOption("否", "no")),
        impact=(TrapOption("可忽略", "ignore"), TrapOption("已超光速", "ftl")),
        action="询问优先级。",
        instructions="对方用「顺便 / 顺手 / 加个」塞进来的，算不算正式需求？",
        criteria={"true": "算需求，不做会被追问",
                  "false": "只是随口一提，可以不当作交付项"},
    ),
    TrapTemplate(
        id="ship_tomorrow",
        needles=("明天能", "今天能上", "今晚上线", "马上上线", "立刻上线",
                 "明天上线", "这周上线", "能不能上"),
        quote="明天能上线",
        question="正在计算可行方案……",
        kind="choice",
        options=(TrapOption("正常开发", "normal"),
                 TrapOption("连夜跑路", "flee"),
                 TrapOption("做个演示版", "demo")),
        action="明确「演示版」。",
        instructions="对方要立刻/明天/今晚上线时，现实可行方案更接近哪一种？",
        criteria={"正常开发": "范围已经做完，按正常节奏能发",
                  "连夜跑路": "根本做不完，硬撑会出事",
                  "做个演示版": "只能先给可看的演示，正式上线另排时间"},
    ),
    TrapTemplate(
        id="you_decide",
        needles=("你看着安排", "你看着办", "你定", "你决定", "随你"),
        quote="你看着安排",
        question="危机是否解除？",
        kind="noul",
        options=(TrapOption("是", "yes"), TrapOption("只是存档了", "filed")),
        action="截图留证，立即停止追加承诺。",
        instructions="对方说「你看着安排 / 你看着办」之后，事情是真的交给你了，还是只是把压力存档？",
        criteria={"true": "范围、期限都清楚，可以按自己的节奏推进",
                  "false": "只是口头放权，事后仍可能被追责"},
    ),
    TrapTemplate(
        id="like_taobao",
        needles=("像淘宝", "做成淘宝", "对标淘宝", "跟淘宝一样", "跟京东一样",
                 "做成抖音", "对标"),
        quote="像淘宝一样",
        question="对标的是产品，还是体量？",
        kind="choice",
        options=(TrapOption("外观像一下", "looks"),
                 TrapOption("功能对标大厂", "parity")),
        action="把「像」拆成可交付的第一版范围。",
        instructions="对方说「像淘宝 / 对标某某」时，更可能只要外观像，还是要功能体量对标？",
        criteria={"外观像一下": "只要看起来像那个产品",
                  "功能对标大厂": "功能、流量、完整度都要对上"},
    ),
    TrapTemplate(
        id="add_ai",
        needles=("加个 ai", "加个ai", "加点 ai", "接入大模型", "上个 gpt",
                 "整个智能", "智能客服"),
        quote="顺便加个 AI",
        question="「加个 AI」的真实重量？",
        kind="choice",
        options=(TrapOption("贴个入口", "button"),
                 TrapOption("重做产品", "rebuild")),
        action="先问要用在哪一步、有没有现成数据。",
        instructions="对方说「加个 AI」时，更接近在现有流程上贴一个入口，还是要把产品重做一遍？",
        criteria={"贴个入口": "现有流程不变，只加一个调用",
                  "重做产品": "数据、流程、交互都要为 AI 重做"},
    ),
)


@dataclass
class CardLine:
    text: str
    kind: str = "body"   # title | quote | body | muted | action


@dataclass
class AnalysisCard:
    """One grey card ready to paint under a bubble."""
    key: str
    title: str
    lines: list[CardLine]
    pending: bool = False
    template_id: str | None = None
    risk: float | None = None

    def texts(self) -> list[str]:
        return [ln.text for ln in self.lines]


@dataclass
class CardLayout:
    """Pixel (view) coordinates for one card, origin = overlay view bottom-left."""
    x: float
    y: float           # bottom edge
    w: float
    h: float
    card: AnalysisCard
    clipped: bool = False


def match_template(text: str) -> TrapTemplate | None:
    """First matching trap template, or None for a generic intent/risk card."""
    raw = (text or "").strip()
    if not raw:
        return None
    folded = raw.lower().replace(" ", "")
    for t in TEMPLATES:
        for needle in t.needles:
            if needle.lower().replace(" ", "") in folded:
                return t
    return None


def extra_questions(template: TrapTemplate) -> dict:
    """TypeSafe question map to send alongside intent/risk. Empty for local judge."""
    if template.kind == "noul":
        q: dict = {
            "type": "noul",
            "instructions": template.instructions or template.question,
        }
        if isinstance(template.criteria, dict):
            q["criteria"] = template.criteria
        out = {"trap": q}
        if template.impact:
            out["impact"] = {
                "type": "choice",
                "instructions": "如果当真去做，更可能落到哪一种影响？",
                "criteria": {opt.label: None for opt in template.impact},
            }
        return out
    q = {
        "type": "choice",
        "instructions": template.instructions or template.question,
        "criteria": template.criteria or {opt.label: None for opt in template.options},
    }
    return {"trap": q}


def _pct(p: float) -> str:
    p = max(0.0, min(1.0, float(p)))
    return f"{round(p * 100):.0f}%"


def _noul_pair(p_yes: float, yes="是", no="否") -> list[CardLine]:
    p_yes = max(0.0, min(1.0, float(p_yes)))
    return [
        CardLine(f"- {yes}：{_pct(p_yes)}", "body"),
        CardLine(f"- {no}：{_pct(1.0 - p_yes)}", "body"),
    ]


def pending_card(text: str) -> AnalysisCard:
    """Skeleton shown the moment a bubble is seen, before the judge returns."""
    t = match_template(text)
    if t is None:
        snippet = (text or "").replace("\n", " ").strip()
        if len(snippet) > 18:
            snippet = snippet[:18] + "…"
        return AnalysisCard(
            key=_card_key(text),
            title="Jev：",
            lines=[CardLine(f"「{snippet}」", "quote"),
                   CardLine("正在判断意图与风险…", "muted")],
            pending=True,
        )
    lines = [CardLine(t.question, "quote")]
    for opt in t.options:
        lines.append(CardLine(f"- {opt.label}：…", "muted"))
    if t.impact:
        lines.append(CardLine("预计影响", "muted"))
        for opt in t.impact:
            lines.append(CardLine(f"- {opt.label}：…", "muted"))
    return AnalysisCard(
        key=_card_key(text),
        title="Jev：",
        lines=lines,
        pending=True,
        template_id=t.id,
    )


def card_from_verdict(text: str, verdict: dict,
                      extra: dict | None = None) -> AnalysisCard:
    """Build the screenshot-style card from a judge verdict + optional extra answers.

    `extra` is the raw TypeSafe `answers` dict (keys `trap` / `impact`) when the
    cloud backend ran the template questions; local backend leaves it empty and
    the card falls back to intent / risk / action.
    """
    t = match_template(text)
    intent = verdict.get("intent") or "闲聊"
    risk = float(verdict.get("risk") or 0.0)
    risk_i = int(round(risk))
    actions = verdict.get("actions") or ACTION_MAP.get(intent, [])
    extra = extra or {}

    if t is None:
        conf = float(verdict.get("confidence") or 0.0)
        intent_probs = verdict.get("intent_probs") or {}
        lines = [
            CardLine(f"意图：{intent}（{_pct(conf)}）", "quote"),
        ]
        # show the next-best intent when the distribution is not a slam dunk
        ranked = sorted(intent_probs.items(), key=lambda kv: -float(kv[1]))
        for name, p in ranked[1:3]:
            if float(p) >= 0.08 and name in INTENTS:
                lines.append(CardLine(f"- {name}：{_pct(p)}", "body"))
        lines.append(CardLine(f"危险等级：{risk_i} / 10", "body"))
        if actions:
            lines.append(CardLine(f"建议动作：{' · '.join(actions[:2])}", "action"))
        return AnalysisCard(
            key=_card_key(text),
            title="Jev：",
            lines=lines,
            template_id=None,
            risk=risk,
        )

    lines: list[CardLine] = [CardLine(t.question, "quote")]
    trap_ans = extra.get("trap") or {}
    if t.kind == "noul":
        p_yes = trap_ans.get("noul")
        if p_yes is None:
            # local fallback: map risk / intent onto 是/否 so the card still
            # looks like the mock instead of going blank
            p_yes = _noul_from_verdict(t, verdict)
        yes_label = t.options[0].label if t.options else "是"
        no_label = t.options[1].label if len(t.options) > 1 else "否"
        lines.extend(_noul_pair(float(p_yes), yes_label, no_label))
    else:
        probs = trap_ans.get("probabilities") or {}
        if not probs:
            probs = _choice_from_verdict(t, verdict)
        for opt in t.options:
            p = probs.get(opt.label)
            if p is None:
                # gateway may echo the chosen label only
                chosen = trap_ans.get("choice")
                p = trap_ans.get("confidence", 0.0) if chosen == opt.label else 0.0
                if not trap_ans:
                    p = probs.get(opt.label, 0.0)
            lines.append(CardLine(f"- {opt.label}：{_pct(float(p))}", "body"))

    if t.impact:
        lines.append(CardLine("预计影响", "muted"))
        impact_ans = extra.get("impact") or {}
        probs = impact_ans.get("probabilities") or {}
        if not probs:
            probs = _impact_from_verdict(t, verdict)
        for opt in t.impact:
            p = probs.get(opt.label)
            if p is None:
                chosen = impact_ans.get("choice")
                p = impact_ans.get("confidence", 0.0) if chosen == opt.label else 0.0
                if not impact_ans:
                    p = probs.get(opt.label, 0.0)
            lines.append(CardLine(f"- {opt.label}：{_pct(float(p))}", "body"))

    lines.append(CardLine(f"危险等级：{risk_i} / 10", "body"))
    action = t.action or (" · ".join(actions[:2]) if actions else "")
    if action:
        lines.append(CardLine(f"建议动作：{action}", "action"))
    return AnalysisCard(
        key=_card_key(text),
        title="Jev：",
        lines=lines,
        template_id=t.id,
        risk=risk,
    )


def _noul_from_verdict(t: TrapTemplate, verdict: dict) -> float:
    """Heuristic yes-probability when the cloud trap question did not run."""
    intent = verdict.get("intent") or ""
    risk = float(verdict.get("risk") or 0.0)
    if t.id == "small_ask":
        # high risk / 派活 → "否，并不小"
        return max(0.02, min(0.98, 1.0 - (0.08 + risk * 0.10)))
    if t.id == "by_the_way":
        return 0.97 if intent in {"派活", "催进度"} else max(0.55, min(0.97, 0.40 + risk * 0.08))
    if t.id == "you_decide":
        # "危机是否解除" — almost never
        return max(0.04, min(0.40, 0.35 - risk * 0.04))
    return 0.5


def _choice_from_verdict(t: TrapTemplate, verdict: dict) -> dict[str, float]:
    intent = verdict.get("intent") or ""
    risk = float(verdict.get("risk") or 0.0)
    labels = [o.label for o in t.options]
    if t.id == "keep_it_simple":
        # 预算简单 dominates the meme
        return {labels[0]: 0.01, labels[1]: 0.99} if len(labels) >= 2 else {labels[0]: 1.0}
    if t.id == "ship_tomorrow":
        # 正常开发 collapses as risk climbs
        p_normal = max(0.0, 0.25 - risk * 0.04)
        p_flee = min(0.20, 0.04 + risk * 0.02)
        p_demo = max(0.0, 1.0 - p_normal - p_flee)
        vals = [p_normal, p_flee, p_demo][:len(labels)]
        return {lab: v for lab, v in zip(labels, vals)}
    if t.id == "like_taobao":
        p_parity = 0.85 if intent == "派活" else 0.55
        return {labels[0]: 1.0 - p_parity, labels[1]: p_parity} if len(labels) >= 2 else {labels[0]: 1.0}
    if t.id == "add_ai":
        p_rebuild = min(0.92, 0.45 + risk * 0.07)
        return {labels[0]: 1.0 - p_rebuild, labels[1]: p_rebuild} if len(labels) >= 2 else {labels[0]: 1.0}
    n = max(1, len(labels))
    return {lab: 1.0 / n for lab in labels}


def _impact_from_verdict(t: TrapTemplate, verdict: dict) -> dict[str, float]:
    risk = float(verdict.get("risk") or 0.0)
    labels = [o.label for o in t.impact]
    if not labels:
        return {}
    if t.id == "small_ask":
        p_tiny = max(0.02, 0.20 - risk * 0.03)
        return {labels[0]: p_tiny, labels[1]: 1.0 - p_tiny} if len(labels) >= 2 else {labels[0]: 1.0}
    if t.id == "by_the_way":
        return {labels[0]: 0.02, labels[1]: 0.98} if len(labels) >= 2 else {labels[0]: 1.0}
    n = max(1, len(labels))
    return {lab: 1.0 / n for lab in labels}


def _card_key(text: str) -> str:
    return (text or "").strip()


def card_size(card: AnalysisCard, max_w: float) -> tuple[float, float]:
    """Point size of the grey card for the given max width."""
    n = 1 + len(card.lines)          # title + body lines
    h = CARD_PAD_Y * 2 + CARD_TITLE_H + CARD_LINE_H * len(card.lines)
    longest = max([card.title] + card.texts(), key=len)
    # ~7.2 pt per CJK char at 12 pt; keep inside max_w
    w = min(max_w, max(CARD_MIN_W_PT, 24 + len(longest) * 7.2))
    return w, h


def layout_cards(messages, cards: dict[str, AnalysisCard],
                 view_w: float, view_h: float,
                 chat_x_min: float = 0.32,
                 input_y_min: float = 0.24) -> list[CardLayout]:
    """Place cards under incoming bubbles, top-origin message y/h → view coords.

    `messages` are `perception.Message` (or duck types with .text/.side/.x/.y/.w/.h).

    WeChat will not reflow for us, so a full-height card almost always
    overlaps whatever sits below it. That overlap is the closest we can get
    to the mock (where the cards *are* the space between bubbles). We only
    clip against the input box / window bottom, never drop a card just
    because the next bubble is close. Clicks still pass through.
    """
    incoming = [m for m in messages if getattr(m, "side", "") == "them" and m.w > 0]
    incoming = incoming[-MAX_CARDS:]
    laid: list[CardLayout] = []
    chat_left = chat_x_min * view_w
    floor_y = max(CARD_BOTTOM_MARGIN, input_y_min * view_h)

    def _view_top(m) -> float:
        return view_h - m.y * view_h

    def _view_bottom(m) -> float:
        return _view_top(m) - m.h * view_h

    for m in incoming:
        card = cards.get(_card_key(m.text))
        if card is None:
            continue
        bubble_left = m.x * view_w
        bubble_bottom = _view_bottom(m)
        max_w = min(CARD_MAX_W_PT, max(CARD_MIN_W_PT, view_w - chat_left - 16))
        cw, ch = card_size(card, max_w)
        cx = max(chat_left + 8, min(bubble_left, view_w - cw - 8))
        card_top = bubble_bottom - CARD_GAP_AFTER_BUBBLE
        max_h = card_top - floor_y
        min_h = CARD_TITLE_H + CARD_PAD_Y * 2
        if max_h < min_h:
            continue
        clipped = False
        if ch > max_h:
            ch = max_h
            clipped = True
        cy = card_top - ch
        laid.append(CardLayout(x=cx, y=cy, w=cw, h=ch, card=card, clipped=clipped))
    return laid


def format_lines_for_test(card: AnalysisCard) -> str:
    """Stable text dump used by unit tests."""
    body = "\n".join(ln.text for ln in card.lines)
    return f"{card.title}\n{body}"


def format_card_for_transcript(card: AnalysisCard | None) -> list[str]:
    """Small-print Jev lines that sit under a buyer turn in the HUD transcript.

    Overlay cards stay optional; this is how the same judgment is shown inside
    the Jev window when WeChat itself is left alone.
    """
    if card is None or card.pending:
        return ["Jev：分析中…"]
    out: list[str] = []
    for i, ln in enumerate(card.lines):
        text = (ln.text or "").strip()
        if not text:
            continue
        out.append(f"Jev：{text}" if i == 0 else text)
        if len(out) >= 6:
            break
    return out or ["Jev：分析中…"]


if __name__ == "__main__":
    import json
    import sys

    sample = sys.argv[1] if len(sys.argv) > 1 else "在吗？有个小需求。"
    t = match_template(sample)
    print(f"template: {t.id if t else '(generic)'}")
    print("--- pending ---")
    print(format_lines_for_test(pending_card(sample)))
    fake = {"intent": "派活", "confidence": 0.9, "risk": 8.0,
            "actions": ["问清交付标准和期限"],
            "intent_probs": {"派活": 0.9, "催进度": 0.05}}
    print("--- local fallback ---")
    print(format_lines_for_test(card_from_verdict(sample, fake)))
    if t:
        print("--- extra questions ---")
        print(json.dumps(extra_questions(t), ensure_ascii=False, indent=2))
