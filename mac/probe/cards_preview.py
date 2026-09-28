"""Render a screenshot-faithful chat + overlay cards to PNG.

Uses the real `cards.card_from_verdict` / `layout_cards` path. No WeChat,
no HUD, no network. Run:

    .venv/bin/python probe/cards_preview.py [out.png]
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import cards  # noqa: E402

from AppKit import (  # noqa: E402
    NSAffineTransform,
    NSAttributedString,
    NSBezierPath,
    NSBitmapImageFileTypePNG,
    NSBitmapImageRep,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSGraphicsContext,
    NSImage,
    NSMutableParagraphStyle,
    NSParagraphStyleAttributeName,
)
from Foundation import NSMakeRect  # noqa: E402

W, H = 390, 844  # iPhone-ish, matching the mock aspect


def msg(text, side, y, h=0.045, x=None, w=0.62):
    if x is None:
        x = 0.14 if side == "them" else 0.24
    return SimpleNamespace(text=text, side=side, x=x, y=y, w=w, h=h, sender=None, conf=1.0)


# Vertical positions chosen so each grey card has room under its bubble,
# the way the mock (not real WeChat spacing) lays them out.
MESSAGES = [
    msg("在吗？有个小需求。", "them", 0.08, w=0.52),
    msg("在的，您说。", "me", 0.28, x=0.42, w=0.40),
    msg("做个像淘宝一样的，简单点就行。", "them", 0.36, w=0.70),
    msg("第一版只做商品展示，支付和物流后续再加，可以吗？", "me", 0.54, x=0.22, w=0.64, h=0.07),
    msg("可以，顺便加个 AI。", "them", 0.66, w=0.50),
    msg("那商品展示和 AI，您希望先做哪个？", "me", 0.82, x=0.22, w=0.64, h=0.06),
]


VERDICTS = {
    "在吗？有个小需求。": (
        {"intent": "派活", "confidence": 0.9, "risk": 9.0, "actions": []},
        {"trap": {"noul": 0.02},
         "impact": {"probabilities": {"改个颜色": 0.03, "重写半个项目": 0.97}}},
    ),
    "做个像淘宝一样的，简单点就行。": (
        {"intent": "派活", "confidence": 0.9, "risk": 7.0, "actions": []},
        {"trap": {"probabilities": {"功能简单": 0.01, "预算简单": 0.99}}},
    ),
    "可以，顺便加个 AI。": (
        {"intent": "派活", "confidence": 0.9, "risk": 8.0, "actions": []},
        {"trap": {"noul": 1.0},
         "impact": {"probabilities": {"可忽略": 0.0, "已超光速": 1.0}}},
    ),
}


def _rgb(hex_code, a=1.0):
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(
        ((hex_code >> 16) & 0xFF) / 255.0,
        ((hex_code >> 8) & 0xFF) / 255.0,
        (hex_code & 0xFF) / 255.0, a)


def _rgba(rgba):
    r, g, b, a = rgba
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(r / 255.0, g / 255.0, b / 255.0, a)


def _draw_text(s, x, y, font, color, w=None):
    para = NSMutableParagraphStyle.alloc().init()
    attrs = {NSFontAttributeName: font, NSForegroundColorAttributeName: color,
             NSParagraphStyleAttributeName: para}
    ns = NSAttributedString.alloc().initWithString_attributes_(s, attrs)
    if w is None:
        ns.drawAtPoint_((x, y))
    else:
        ns.drawInRect_(NSMakeRect(x, y, w, 36))


def paint_card(layout):
    r = NSMakeRect(layout.x, layout.y, layout.w, layout.h)
    path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        r, cards.CARD_CORNER, cards.CARD_CORNER)
    _rgba(cards.CARD_FILL).set(); path.fill()
    _rgba(cards.CARD_STROKE).set(); path.setLineWidth_(1.0); path.stroke()
    title_font = NSFont.boldSystemFontOfSize_(12)
    body_font = NSFont.systemFontOfSize_(12)
    muted_font = NSFont.systemFontOfSize_(11)
    text_color = _rgba(cards.CARD_TEXT)
    muted_color = _rgba(cards.CARD_MUTED)
    para = NSMutableParagraphStyle.alloc().init()
    x = layout.x + cards.CARD_PAD_X
    y = layout.y + layout.h - cards.CARD_PAD_Y - cards.CARD_TITLE_H
    max_w = layout.w - 2 * cards.CARD_PAD_X
    NSAttributedString.alloc().initWithString_attributes_(
        layout.card.title,
        {NSFontAttributeName: title_font, NSForegroundColorAttributeName: text_color}
    ).drawAtPoint_((x, y + 2))
    y -= cards.CARD_LINE_H
    for ln in layout.card.lines:
        if y < layout.y + 2:
            break
        font, color = body_font, text_color
        if ln.kind in ("muted",) or layout.card.pending:
            font, color = muted_font, muted_color
        elif ln.kind == "action":
            font, color = muted_font, text_color
        NSAttributedString.alloc().initWithString_attributes_(
            ln.text,
            {NSFontAttributeName: font, NSForegroundColorAttributeName: color,
             NSParagraphStyleAttributeName: para}
        ).drawInRect_(NSMakeRect(x, y, max_w, cards.CARD_LINE_H))
        y -= cards.CARD_LINE_H


def render(dest: Path) -> Path:
    cache = {}
    for text, (verdict, extra) in VERDICTS.items():
        cache[text] = cards.card_from_verdict(text, verdict, extra)

    laid = cards.layout_cards(MESSAGES, cache, W, H, chat_x_min=0.10, input_y_min=0.04)

    img = NSImage.alloc().initWithSize_((W, H))
    img.lockFocus()
    _rgb(0xEDEDED).set(); NSBezierPath.fillRect_(NSMakeRect(0, 0, W, H))
    # nav bar
    _rgb(0xF7F7F7).set(); NSBezierPath.fillRect_(NSMakeRect(0, H - 56, W, 56))
    _draw_text("老板", 170, H - 42, NSFont.boldSystemFontOfSize_(16), _rgb(0x191919))
    _draw_text("18:01", 16, H - 22, NSFont.systemFontOfSize_(12), _rgb(0x888888))

    # bubbles (Cocoa y = bottom-origin; Message.y is top-origin)
    for m in MESSAGES:
        bx, by = m.x * W, H - (m.y + m.h) * H
        bw, bh = m.w * W, m.h * H
        fill = _rgb(0x95EC69) if m.side == "me" else _rgb(0xFFFFFF)
        path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            NSMakeRect(bx, by, bw, bh), 6, 6)
        fill.set(); path.fill()
        color = _rgb(0x191919)
        _draw_text(m.text, bx + 10, by + 8, NSFont.systemFontOfSize_(13), color, bw - 20)

    for layout in laid:
        paint_card(layout)

    # caption
    _draw_text(f"真实 cards.py 布局 · {len(laid)} 张灰卡（点击穿透 overlay，不是微信消息）",
               12, 10, NSFont.systemFontOfSize_(10), _rgb(0x888888), W - 24)
    img.unlockFocus()

    tiff = img.TIFFRepresentation()
    rep = NSBitmapImageRep.imageRepWithData_(tiff)
    data = rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, None)
    dest.parent.mkdir(parents=True, exist_ok=True)
    data.writeToFile_atomically_(str(dest), True)
    return dest


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("docs/cards-preview.png")
    p = render(out)
    print(f"wrote {p} ({p.stat().st_size} bytes)")
