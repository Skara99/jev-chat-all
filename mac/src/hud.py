"""Floating HUD: a non-activating panel beside WeChat showing intent, risk and ranked replies.

Design notes
  * NSWindowStyleMaskNonactivatingPanel + floating level: the panel never steals focus
    from WeChat, and window-ID capture means it never appears in our own screenshots.
  * Poll loop: read the chat, hash the newest message, judge only when it changes.
  * The moment a new message is SEEN, both halves start (local pre-judge and the paid
    generation run concurrently, latest-wins); the settle gate then spends the finished
    verdict, waits out whatever generation is still missing, and only the local ranking
    (~0.5 s) is left after it. Candidates display before ranking finishes ("排序中")
    and are re-ordered in place when it lands.
  * The panel positions itself against WeChat's window each tick, so it follows moves,
    resizes and monitor changes without any window-server hooks.
  * Palette is WeChat's light theme (see PALETTE below); the Appearance is pinned to Aqua
    so the title bar and button bezels stay light even when the system is in dark mode.
  * 「填入」 writes through the Accessibility API into WeChat's input box (src/fill.py): no
    synthetic keystrokes, no clipboard, and nothing needs to be frontmost. It needs the
    Accessibility permission; when that is missing the HUD asks for it and reports the
    failure. Candidates cannot appear in the system IME candidate bar.
"""

from __future__ import annotations

import objc
import os
import subprocess
import threading
import time
from pathlib import Path

import AppKit
import sys

from AppKit import (
    NSAppearance,
    NSAttributedString,
    NSBackingStoreBuffered,
    NSBackgroundColorAttributeName,
    NSBezierPath,
    NSBezelStyleRounded,
    NSButton,
    NSColor,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSMutableParagraphStyle,
    NSParagraphStyleAttributeName,
    NSPanel,
    NSPasteboard,
    NSPasteboardTypeString,
    NSPopUpButton,
    NSScreen,
    NSTextAlignmentRight,
    NSTextField,
    NSView,
    NSLineBreakByTruncatingTail,
    NSLineBreakByWordWrapping,
    NSWindowMiniaturizeButton,
    NSWindowStyleMaskBorderless,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskNonactivatingPanel,
    NSWindowStyleMaskTitled,
    NSWindowZoomButton,
    NSWindowCloseButton,
)
from Foundation import NSMakeRect, NSMakeSize, NSMutableAttributedString, NSObject, NSTimer

sys.path.insert(0, str(Path(__file__).parent))
import userconfig  # noqa: E402

userconfig.load()   # ~/.config/jev-jarvis/env -> os.environ (Finder apps inherit none)
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ["HF_HUB_DISABLE_XET"] = "1"

from perception import (  # noqa: E402
    active_app, read_conversation, screen_capture_ok, request_screen_capture,
    set_active_app, warm_ocr, LAYOUTS)
from judge import make_judge  # noqa: E402
from generate import BUILTIN_SOURCE, Generator, load_credentials  # noqa: E402
import styles  # noqa: E402
import fill  # noqa: E402
import cards  # noqa: E402

PANEL_W, PANEL_H = 360, 720   # transcript + 话术 groups + small-print analysis
COLLAPSED_H = 96              # height when the panel is rolled up
# The tick timer fires at FAST_TICK; a read only runs when due. A quiet screen (fingerprint
# match ⇒ no OCR) re-checks every FAST_TICK — a new message surfaces within 0.25 s instead
# of within 1 s. A read that found a change (full capture+OCR paid) first keeps a SHORT
# cadence for a few reads (a burst's next message is noticed in ~0.45 s, not after a full
# SLOW_TICK) and only settles back to SLOW_TICK if the pane keeps moving — that is the
# cadence the old fixed poll had, kept as the CPU guard for a continuously moving screen.
FAST_TICK = 0.25         # re-check cadence while the chat pane is quiet
BURST_TICK = 0.45        # short cadence right after a change: catch the burst's next message
BURST_READS = 3          # how many reads stay on BURST_TICK before falling back to SLOW_TICK
SLOW_TICK = 1.0          # re-check cadence while the chat pane keeps moving
SETTLE_S = 1.2           # upper bound on the settle wait (anti-flood; unchanged by design)
EARLY_SETTLE_S = 0.70    # the gate may open this early …
STABLE_READS = 3         # … but only after this many consecutive unchanged reads
MIN_GAP_S = 2.0          # never restart analysis faster than this
CONTEXT_TURNS = 4        # recent turns the generation half sees
JUDGE_TURNS = 2          # recent turns the judge half sees: shorter prompt, faster forward


# ---------------------------------------------------------------- palette
# WeChat's light theme: a #F7F7F7 surface, near-black body text, #888888 for anything
# secondary, and the brand green/amber/red carrying the risk state.


def _rgb(hex_code: int, alpha: float = 1.0) -> NSColor:
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(
        ((hex_code >> 16) & 0xFF) / 255.0,
        ((hex_code >> 8) & 0xFF) / 255.0,
        (hex_code & 0xFF) / 255.0,
        alpha,
    )


def _rgba(rgba: tuple) -> NSColor:
    r, g, b, a = rgba
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(
        r / 255.0, g / 255.0, b / 255.0, a)


# Jev uses Kaiti so it never looks like a WeChat message (PingFang / system UI).
_JEV_FONT_REGULAR = "STKaitiSC-Regular"
_JEV_FONT_BOLD = "STKaitiSC-Bold"


def _jev_font(size: float, bold: bool = False):
    name = _JEV_FONT_BOLD if bold else _JEV_FONT_REGULAR
    return (NSFont.fontWithName_size_(name, size)
            or (NSFont.boldSystemFontOfSize_(size) if bold
                else NSFont.systemFontOfSize_(size)))


def _paint_card(layout) -> None:
    """Draw one grey Jev card. Called from _BoxesView.drawRect_ on the main thread."""
    r = NSMakeRect(layout.x, layout.y, layout.w, layout.h)
    fill = _rgba(cards.CARD_FILL)
    stroke = _rgba(cards.CARD_STROKE)
    path = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        r, cards.CARD_CORNER, cards.CARD_CORNER)
    fill.set()
    path.fill()
    stroke.set()
    path.setLineWidth_(1.0)
    path.stroke()

    title_font = _jev_font(13, True)
    body_font = _jev_font(12)
    muted_font = _jev_font(11)
    text_color = PALETTE["jev"]
    muted_color = PALETTE["jev_soft"]
    para = NSMutableParagraphStyle.alloc().init()
    para.setLineBreakMode_(NSLineBreakByTruncatingTail)

    x = layout.x + cards.CARD_PAD_X
    # Cocoa y grows up; first line is at the top of the card
    y = layout.y + layout.h - cards.CARD_PAD_Y - cards.CARD_TITLE_H
    max_w = layout.w - 2 * cards.CARD_PAD_X
    title = NSAttributedString.alloc().initWithString_attributes_(
        layout.card.title,
        {NSFontAttributeName: title_font,
         NSForegroundColorAttributeName: text_color})
    title.drawAtPoint_((x, y + 2))
    y -= cards.CARD_LINE_H
    for ln in layout.card.lines:
        if y < layout.y + 2:
            break
        if ln.kind in ("muted",) or layout.card.pending:
            font, color = muted_font, muted_color
        elif ln.kind == "action":
            font, color = muted_font, text_color
        else:
            font, color = body_font, text_color
        s = NSAttributedString.alloc().initWithString_attributes_(
            ln.text,
            {NSFontAttributeName: font,
             NSForegroundColorAttributeName: color,
             NSParagraphStyleAttributeName: para})
        # clip long lines by drawing into a rect instead of a point
        s.drawInRect_(NSMakeRect(x, y, max_w, cards.CARD_LINE_H))
        y -= cards.CARD_LINE_H


PALETTE = {
    "bg": _rgb(0xEDEDED),     # WeChat chat-pane grey
    "text": _rgb(0x191919),   # judged message, intent, candidates, action advice
    "muted": _rgb(0x888888),  # status, sender/context, confidence, percentages, headers
    "green": _rgb(0x07C160),  # WeChat brand green — risk 安全, success feedback
    "green_soft": _rgb(0xE8F9EF),
    "amber": _rgb(0xFA9D3B),  # risk 留神
    "amber_soft": _rgb(0xFFF4E5),
    "red": _rgb(0xFA5151),    # risk 危险, failures
    "red_soft": _rgb(0xFDECEC),
    "me": _rgb(0x95EC69),     # WeChat self-bubble
    "link": _rgb(0x576B95),   # WeChat link blue
    "card": _rgb(0xFFFFFF),   # incoming bubble / intent card
    "white": _rgb(0xFFFFFF),
    # The 话术 dropdown is drawn as a WeChat-style field: a flat light surface with a
    # hairline, because the stock popup bezel brings the system accent colour (a blue
    # chevron) into a panel that has no other system-accent pixel in it.
    # A green-tinted variant of this field was tried to advertise clickability and
    # rejected: next to the all-grey panel it read as a selection state and was jarring.
    # Discoverability is handled by the popup's tooltip instead — zero visual footprint.
    "field": _rgb(0xFFFFFF),
    "edge": _rgb(0xE5E5E5),
    "jev": _rgb(0x8A5A2B),        # ink-brown: Jev is a note, not a chat bubble
    "jev_soft": _rgb(0x6B4A2B),
}

# One-line gloss under the big intent name — WeChat-sized, not a dashboard caption.
INTENT_HINTS = {
    "派活": "对方要你做事",
    "催进度": "对方在催进度",
    "问进度": "对方在问进展",
    "批评": "对方不太满意",
    "要解释": "对方要你说明原因",
    "闲聊": "随便聊聊，没有具体要求",
    "约会议": "对方想约会议或通话",
    "夸奖": "对方在肯定你",
}

# Candidate row geometry. A row is 48 pt tall inside a 56 pt pitch, so rows keep the same
# breathing room as before; prob and buttons share the text's bottom edge.
CAND_BTN_W, CAND_BTN_H, CAND_BTN_GAP = 56, 24, 4
CAND_BTN_X = PANEL_W - 14 - (2 * CAND_BTN_W + CAND_BTN_GAP)   # 230
# Rank/percentage label ("#3 · 100%"): NSTextField's cell insets mean the widest string
# actually consumes 63 px at 11 pt, so the old 48 px frame clipped the "%" off every row.
# 72 px still clears that with 9 px to spare, and the 12 px it gives back go to the
# candidate text — which needs them: at 116 px a 30-character candidate (the generation
# prompt's own cap) lost its last two characters to the 3-line limit.
CAND_PROB_X, CAND_PROB_W = 14, 72                              # 14 .. 86
CAND_TEXT_X = CAND_PROB_X + CAND_PROB_W + 8                    # 94
CAND_TEXT_W = CAND_BTN_X - CAND_TEXT_X - 8                     # 128
CAND_TEXT_H = 48                                                # up to 3 wrapped lines
CAND_ROW_H = 56                                                 # vertical pitch of one row

# 话术 groups. Each group is headed by its dropdown; its candidates sit under it. The panel
# is only as tall as the groups in use, so nothing is reserved for a tone that is switched
# off (that reservation is what used to leave a dead gap in the middle).
TONE_DD_X, TONE_DD_W, TONE_DD_H, TONE_DD_GAP = 14, PANEL_W - 28, 24, 4
ADD_STYLE_H = 28
TONE_DD_INSET = 6         # the popup sits this far inside its field, like text in an input box
# Small-print analysis under the 话术 groups (intent · risk · action).
ANALYSIS_H = 0            # intent now lives in the WeChat-style card at the top
INTENT_CARD_H = 96
TRANSCRIPT_MAX = 8        # visible turns in the Jev window (OCR already caps at 12)
TRANSCRIPT_LINE_H = 16
TRANSCRIPT_PAD = 6
TRANSCRIPT_TEXT_MAX = 42  # one line per turn; longer OCR is truncated with …
TRANSCRIPT_H = 196        # room for buyer turns + Jev small print under each
TONE_DD_FONT = 13         # bigger than the 11 pt labels: it is a control, and it is the one
                          # thing on the panel the user is meant to click
GROUP_GAP = 12            # between one group's rows and the next group's dropdown
BOTTOM_PAD = 18           # below the last group


LOG_PATH = Path.home() / "Library" / "Logs" / "jev-jarvis.log"


def format_transcript(msgs, analyses=None) -> str:
    """OCR turns as a short chat log for the Jev window. Newest last.

    Buyer turns get the Jev card as small print underneath. `analyses` is
    text -> AnalysisCard (the same cache the overlay used to paint).
    Pure string helper so tests can check the layout without starting Cocoa.
    """
    if not msgs:
        return "（还没读到文字气泡）\n打开一个有买家文字的聊天，不要停在会话列表或文件传输助手"
    analyses = analyses or {}
    lines = []
    for m in list(msgs)[-TRANSCRIPT_MAX:]:
        text = (getattr(m, "text", "") or "").replace("\n", " ").strip()
        if len(text) > TRANSCRIPT_TEXT_MAX:
            text = text[: TRANSCRIPT_TEXT_MAX - 1] + "…"
        sender = getattr(m, "sender", None)
        side = getattr(m, "side", "")
        who = sender or {"me": "我", "them": "买家", "unknown": "?"}.get(side, "?")
        mark = "●" if side == "them" else "○"
        lines.append(f"{mark} {who}：{text}")
        if side == "them":
            raw = getattr(m, "text", "") or ""
            for extra in cards.format_card_for_transcript(analyses.get(raw)):
                lines.append(f"    {extra}")
    return "\n".join(lines)


def format_transcript_attributed(msgs, analyses=None):
    """Same log as format_transcript, but Jev notes are Kaiti / ink-brown.

    Chat turns stay in the system UI font so they still read as WeChat
    bubbles; Jev is a handwritten aside, not another speaker.
    """
    raw = format_transcript(msgs, analyses)
    out = NSMutableAttributedString.alloc().init()
    chat_font = NSFont.systemFontOfSize_(12)
    jev_font = _jev_font(13)
    chat_para = NSMutableParagraphStyle.alloc().init()
    chat_para.setLineBreakMode_(NSLineBreakByWordWrapping)
    chat_para.setLineSpacing_(2.0)
    jev_para = NSMutableParagraphStyle.alloc().init()
    jev_para.setLineBreakMode_(NSLineBreakByWordWrapping)
    jev_para.setLineSpacing_(1.0)
    jev_para.setHeadIndent_(18.0)
    lines = raw.split("\n")
    for i, line in enumerate(lines):
        piece = line + ("\n" if i < len(lines) - 1 else "")
        is_jev = line.startswith("    ") or "Jev：" in line
        if is_jev:
            attrs = {
                NSFontAttributeName: jev_font,
                NSForegroundColorAttributeName: PALETTE["jev"],
                NSParagraphStyleAttributeName: jev_para,
            }
        else:
            color = PALETTE["text"]
            if line.startswith("●"):
                color = PALETTE["text"]
            elif line.startswith("○"):
                color = PALETTE["link"]
            attrs = {
                NSFontAttributeName: chat_font,
                NSForegroundColorAttributeName: color,
                NSParagraphStyleAttributeName: chat_para,
            }
        chunk = NSAttributedString.alloc().initWithString_attributes_(piece, attrs)
        out.appendAttributedString_(chunk)
    return out


def _log(msg: str) -> None:
    """One line per stage: to stdout, and into ~/Library/Logs/jev-jarvis.log.

    "It feels slow" is not actionable on its own, so every analysis prints what each stage
    cost; that is the whole point of this function. Deliberately **no message text and no
    candidate text**: this file is meant to be pasted into an issue, and the app's premise
    is that chat content stays on the machine.

    Both destinations on purpose: the .app launcher already redirects stdout into this same
    file, while `./start.command` only shows a terminal — so which place held the evidence
    depended on how the user happened to launch it. The inode check stops the .app case
    from writing every line twice.
    """
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        if os.fstat(sys.stdout.fileno()).st_ino == LOG_PATH.stat().st_ino:
            return                       # stdout already IS that file (the .app case)
    except Exception:
        pass
    try:
        with open(LOG_PATH, "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass                             # a log we cannot write is not worth breaking over


class _BoxesView(NSView):
    """The YOLO overlay's canvas: paints whatever `boxes` last held.

    boxes: [(NSRect, NSColor, line_width, NSAttributedString chip), ...] in view
    coordinates, set from the main thread and followed by setNeedsDisplay_. The view
    owns no data — it only renders the controller's most recent read, which is what
    keeps the overlay honest: what you see boxed is exactly what the pipeline read.

    cards: [CardLayout, ...] — grey Jev analysis cards sitting under incoming
    bubbles. Pure paint, click-through, never written into WeChat.
    """

    def drawRect_(self, rect):
        for box in getattr(self, "boxes", None) or []:
            r, color, lw, chip = box[:4]
            color.set()
            NSBezierPath.setDefaultLineWidth_(lw)
            if len(box) > 4 and box[4]:
                path = NSBezierPath.bezierPathWithRect_(r)
                path.setLineWidth_(lw)
                path.setLineDash_count_phase_([6.0, 4.0], 2, 0)
                path.stroke()
            else:
                NSBezierPath.strokeRect_(r)
            chip.drawAtPoint_((r.origin.x, r.origin.y + r.size.height + 2))
        for layout in getattr(self, "cards", None) or []:
            _paint_card(layout)


class HudController(NSObject):
    def init(self):
        self = objc.super(HudController, self).init()
        if self is None:
            return None
        self.last_seen = None          # newest message text observed
        self._reply_key = None         # (conversation, incoming text), never an outgoing message
        self._reply_epoch = 0          # invalidate even if the same text reappears later
        self._reply_worker = threading.local()
        self.last_change_ts = 0.0      # when it last changed (burst detection)
        self.last_analyze_ts = 0.0     # rate limit for analysis starts
        self.analyzed_text = None      # what the panel currently shows
        self._judged_once = False      # first judge call includes the local model load
        self._read_once = False        # first OCR call includes Vision's own load
        self._last_skip_reason = None
        self.judge = make_judge()
        self.generator = Generator()
        # 话术: per-slot tone selection. A slot on 不用 contributes no request and no rows.
        # Default is one style / one reply; the ＋ 风格 button opens more slots, up to 3.
        self.slot_tones = list(styles.DEFAULT_SLOTS)[:styles.MAX_SLOTS]
        while len(self.slot_tones) < styles.MAX_SLOTS:
            self.slot_tones.append(styles.NONE_LABEL)
        self._dds: list = []
        self._dd_boxes: list = []       # the flat fields the dropdowns are drawn into
        self._remove_btns: list = []    # 移除 next to each extra slot
        self._rows: list = []
        self._fixed: list = []          # (control, x, dy_from_top, w, h) — the rows above
        self._group_top = 0             # where the first group starts, from the top
        self._title_h = 28              # measured right after the panel is built
        self.cand_texts: list[str | None] = [None] * (styles.MAX_SLOTS * styles.PER_TONE)
        self._last_intent = ""          # kept so a tone change can re-rank without re-judging
        # streaming candidates: each generation run bumps this epoch at its start and its
        # streamed lines carry the value, so a late line from a run a tone change or a new
        # message superseded is dropped instead of written into the new run's rows
        self._gen_epoch = 0
        self._stream_rows: dict[int, int] = {}   # slot -> lines already shown, per run

        self._busy = False
        self._next_read_ts = 0.0    # reads before this timestamp are skipped (quiet screen)
        self._fingerprint = None    # last chat-pane fingerprint; equal ⇒ skip OCR entirely
        self._last_full = None      # last OCR'd result, reused while the pane is unchanged
        self._analyzing = False     # judge+generate runs off the tick path
        # Pre-judgment: the local judge starts the moment a new message is seen, and the
        # settle gate consumes the verdict if the text is unchanged — intent/risk land on
        # screen ~1 s earlier and only the (paid) generation half still waits. Single-slot
        # request = latest-wins: a newer text overwrites the slot and retires the verdict.
        self._model_lock = threading.Lock()   # never two local forwards (judge/rank) at once
        self._prejudge_req = None             # (text, context, sender, prev, reply epoch)
        self._prejudge_result = None          # (text, verdict, sender, prev, reply epoch)
        self._prejudging = False              # a pre-judge forward is running right now
        self._prejudge_event = threading.Event()
        threading.Thread(target=self._prejudge_loop, daemon=True).start()
        # Early generation: the paid half starts the moment a message is seen too, with the
        # same latest-wins slot discipline. The settle window (~1 s) then hides the whole
        # generation latency, and only the local ranking is left after the gate opens.
        # Cost: a burst's intermediate messages each fire one discarded API call — cheap at
        # glm-4-flash-class pricing, and superseded results are never consumed.
        self._pregen_req = None              # (text, context, tones tuple, reply epoch)
        self._pregen_result = None           # (text, tones, gen dict, reply epoch)
        self._pregen_running = False         # a pre-generation request is in flight
        self._pregen_event = threading.Event()
        threading.Thread(target=self._pregen_loop, daemon=True).start()
        self._burst_left = BURST_READS       # short-cadence reads left after a change
        self._stable_n = 0                   # consecutive unchanged reads since last change
        self._collapsed = False
        self._expanded_h = None       # full height, captured the first time we collapse
        self._paused = False
        # YOLO overlay default: JEV_BOXES=1 (or true/yes/on) in the env file starts it on;
        # either way the menu-bar item flips it at runtime
        self._show_boxes = userconfig.get("JEV_BOXES").strip().lower() in (
            "1", "true", "yes", "on")
        # Grey cards on WeChat itself are off by default: the analysis now
        # lives as small print under the 话术 groups inside this panel.
        # JEV_CARDS=1 / the menu-bar item turns the overlay back on.
        self._show_cards = userconfig.get("JEV_CARDS").strip().lower() in (
            "1", "true", "yes", "on")
        self._card_cache: dict[str, object] = {}   # text -> AnalysisCard
        self._card_inflight: set[str] = set()
        self._card_event = threading.Event()
        self._card_queue: list = []                # (text, context, epoch)
        self._card_epoch = 0                       # bumped on conversation change
        threading.Thread(target=self._card_loop, daemon=True).start()
        self._last_risk = 0.0         # newest verdict's risk, for the overlay's highlight
        self._chat_title = ""
        self._asked_permission = False
        self._win_wid = None          # sticky chat window id
        self._missing_n = 0           # consecutive WeChat-not-found ticks before hide
        # JEV_APP=dingtalk starts on DingTalk. Default stays WeChat.
        # DingTalk is read-only: no AX walk, no fill, no keystroke.
        self._app = set_active_app(
            "dingtalk" if userconfig.get("JEV_APP").strip().lower() in (
                "dingtalk", "ding", "钉钉") else "wechat")
        self._last_origin = None      # last applied panel origin
        self._pending_origin = None   # candidate origin awaiting confirmation
        self._input_target = None
        self._input_window = None
        self._input_next = 0.0
        self._input_locating = False
        self._build_panel()
        self._build_overlay()
        self._expanded_h = self.panel.frame().size.height
        return self

    # ------------------------------------------------------------------ ui
    @objc.python_method
    def _build_panel(self):
        # Closable/Miniaturizable are what actually CREATE the standard window buttons;
        # NonactivatingPanel alone gives a title bar with no controls at all.
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                 | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskNonactivatingPanel)
        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, PANEL_W, PANEL_H), style, NSBackingStoreBuffered, False)
        self.panel.setLevel_(AppKit.NSFloatingWindowLevel)
        self.panel.setOpaque_(False)
        self.panel.setAlphaValue_(1.0)   # light surfaces go grey/washed out below 1.0
        # The title bar and button bezels are drawn from the appearance, not from the
        # background colour, so pin Aqua: a dark-mode system would otherwise give a dark
        # title bar above a white panel.
        self.panel.setAppearance_(NSAppearance.appearanceNamed_(AppKit.NSAppearanceNameAqua))
        self.panel.setBackgroundColor_(PALETTE["bg"])
        self.panel.setTitle_("微信助手")
        self.panel.setHidesOnDeactivate_(False)
        self.panel.setBecomesKeyOnlyIfNeeded_(True)

        view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, PANEL_W, PANEL_H))
        # Paint the panel colour on the view itself rather than leaning on the window's
        # background colour: _relayout() grows and shrinks this view, and a region that
        # appears after a resize is not reliably covered by the window behind it. It also
        # makes offscreen renders (cacheDisplayInRect_) show what the screen shows — a
        # transparent view renders black there and hides real layout problems.
        view.setWantsLayer_(True)
        view.layer().setBackgroundColor_(PALETTE["bg"].CGColor())
        self.rows: dict[str, NSTextField] = {}

        # Layout order matters: the message being judged is the anchor of the panel,
        # so it sits right under the title in the brightest, largest type.
        # The full-width rows (PANEL_W - 28 = 332 px) cannot clip their widest string:
        # "意图识别率 100%" measures 103 px at 12 pt.
        # Every control is created once and then placed by _relayout(), which is what lets
        # the panel change height when the tone selection changes.
        dy = 10
        for key, size, color, bold, height in (
            ("chat", 13, PALETTE["text"], True, 18),      # 群名 / 联系人
            ("status", 11, PALETTE["muted"], False, 16),
        ):
            tf = self._make_label(16, 0, PANEL_W - 32, height,
                                  size=size, color=color, bold=bold)
            view.addSubview_(tf)
            self.rows[key] = tf
            self._fixed.append((tf, 16, dy, PANEL_W - 32, height))
            dy += height + 2

        # Intent card: WeChat-white bubble with a big intent name + risk pill.
        intent_box = NSView.alloc().initWithFrame_(
            NSMakeRect(12, 0, PANEL_W - 24, INTENT_CARD_H))
        intent_box.setWantsLayer_(True)
        intent_box.layer().setBackgroundColor_(PALETTE["card"].CGColor())
        intent_box.layer().setCornerRadius_(8.0)
        intent_box.layer().setBorderWidth_(0.5)
        intent_box.layer().setBorderColor_(PALETTE["edge"].CGColor())
        view.addSubview_(intent_box)
        self.rows["intent_box"] = intent_box
        self._fixed.append((intent_box, 12, dy, PANEL_W - 24, INTENT_CARD_H))

        kicker = self._make_label(24, 0, 88, 16, size=12, color=PALETTE["jev"], bold=True)
        kicker.setFont_(_jev_font(12, True))
        kicker.setStringValue_("Jev 意图")
        view.addSubview_(kicker)
        self.rows["intent_kicker"] = kicker
        self._fixed.append((kicker, 24, dy + 10, 88, 16))

        risk_pill = self._make_label(PANEL_W - 108, 0, 80, 18,
                                    size=11, color=PALETTE["green"], bold=True)
        risk_pill.setAlignment_(NSTextAlignmentRight)
        risk_pill.setStringValue_("")
        view.addSubview_(risk_pill)
        self.rows["risk_pill"] = risk_pill
        self._fixed.append((risk_pill, PANEL_W - 108, dy + 10, 80, 18))

        intent_name = self._make_label(24, 0, PANEL_W - 48, 32,
                                      size=24, color=PALETTE["jev"], bold=True)
        intent_name.setFont_(_jev_font(24, True))
        intent_name.setStringValue_("等待消息")
        view.addSubview_(intent_name)
        self.rows["intent_name"] = intent_name
        self._fixed.append((intent_name, 24, dy + 28, PANEL_W - 48, 32))

        intent_meta = self._make_label(24, 0, PANEL_W - 48, 18,
                                      size=13, color=PALETTE["jev_soft"])
        intent_meta.setFont_(_jev_font(13))
        intent_meta.setStringValue_("打开一个有对方文字的聊天")
        view.addSubview_(intent_meta)
        self.rows["intent_meta"] = intent_meta
        self._fixed.append((intent_meta, 24, dy + 62, PANEL_W - 48, 18))
        dy += INTENT_CARD_H + 10

        # Transcript: WeChat-white card, newest at the bottom.
        trans_h = TRANSCRIPT_H
        trans_box = NSView.alloc().initWithFrame_(
            NSMakeRect(12, 0, PANEL_W - 24, trans_h))
        trans_box.setWantsLayer_(True)
        trans_box.layer().setBackgroundColor_(PALETTE["card"].CGColor())
        trans_box.layer().setCornerRadius_(8.0)
        trans_box.layer().setBorderWidth_(0.5)
        trans_box.layer().setBorderColor_(PALETTE["edge"].CGColor())
        view.addSubview_(trans_box)
        self.rows["transcript_box"] = trans_box
        self._fixed.append((trans_box, 12, dy, PANEL_W - 24, trans_h))

        transcript = self._make_label(22, 0, PANEL_W - 44, trans_h - 16,
                                      size=12, color=PALETTE["text"])
        transcript.cell().setWraps_(True)
        transcript.setSelectable_(True)
        transcript.setAttributedStringValue_(format_transcript_attributed([]))
        view.addSubview_(transcript)
        self.rows["transcript"] = transcript
        self._fixed.append((transcript, 22, dy + 8, PANEL_W - 44, trans_h - 16))
        dy += trans_h + 10

        # Keep the old keys as hidden fields so pause / applyWaiting_ / the
        # collapse path can still write them without branching.
        for key, size, color, bold, height in (
            ("message", 15, PALETTE["text"], False, 50),
            ("sender", 10, PALETTE["muted"], False, 14),
            ("intent", 21, PALETTE["text"], True, 28),
            ("confidence", 12, PALETTE["muted"], False, 18),
            ("risk", 14, PALETTE["green"], True, 20),
            ("actions", 13, PALETTE["text"], False, 18),
        ):
            tf = self._make_label(14, 0, PANEL_W - 28, height,
                                  size=size, color=color, bold=bold)
            tf.setHidden_(True)
            view.addSubview_(tf)
            self.rows[key] = tf

        # ---- candidates section
        dy += 6
        header = self._make_label(16, 0, PANEL_W - 32, 16,
                                  size=12, color=PALETTE["muted"], bold=True)
        header.setStringValue_("候选回复")
        view.addSubview_(header)
        self.rows["cand_header"] = header
        self._fixed.append((header, 16, dy, PANEL_W - 32, 16))
        dy += 22
        self._group_top = dy

        # ---- 话术 groups: each dropdown heads a group and its candidates sit underneath,
        # so the tone is labelled by the thing that selects it. Every group's controls exist
        # from the start; _relayout() decides which are on screen. The button tags are slot
        # arithmetic (slot * PER_TONE + row) so they never shift when a group's results are
        # still in flight.
        tone_items = styles.labels()
        for slot in range(styles.MAX_SLOTS):
            # the field the popup sits in: a flat surface with a hairline, drawn by us so
            # the control carries no system-accent chrome
            box = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, TONE_DD_W, TONE_DD_H))
            box.setWantsLayer_(True)
            box.layer().setBackgroundColor_(PALETTE["field"].CGColor())
            box.layer().setBorderColor_(PALETTE["edge"].CGColor())
            box.layer().setBorderWidth_(1.0)
            box.layer().setCornerRadius_(6.0)
            view.addSubview_(box)
            self._dd_boxes.append(box)

            pop = NSPopUpButton.alloc().initWithFrame_pullsDown_(
                NSMakeRect(0, 0, TONE_DD_W - 2 * TONE_DD_INSET, TONE_DD_H), False)
            pop.setBordered_(False)          # <- no bezel, no accent-coloured chevron
            pop.setToolTip_("点这里换话术（每种风格一条回复）")
            pop.setFont_(NSFont.systemFontOfSize_(TONE_DD_FONT))
            pop.addItemsWithTitles_(tone_items)
            if self.slot_tones[slot] in styles.PRESETS:
                pop.selectItemWithTitle_(self.slot_tones[slot])
            pop.setTarget_(self)
            pop.setAction_("toneChanged:")
            view.addSubview_(pop)
            self._dds.append(pop)

            rm = self._make_button(0, 0, 52, TONE_DD_H, "移除", "removeStyle:", slot)
            rm.setHidden_(True)
            view.addSubview_(rm)
            self._remove_btns.append(rm)

            slot_rows = []
            for row in range(styles.PER_TONE):
                tag = slot * styles.PER_TONE + row
                bubble = NSView.alloc().initWithFrame_(
                    NSMakeRect(0, 0, PANEL_W - 28, CAND_TEXT_H + 8))
                bubble.setWantsLayer_(True)
                bubble.layer().setBackgroundColor_(PALETTE["card"].CGColor())
                bubble.layer().setCornerRadius_(8.0)
                view.addSubview_(bubble)
                prob = self._make_label(CAND_PROB_X, 0, CAND_PROB_W, 14,
                                        size=11, color=PALETTE["muted"])
                text = self._make_label(CAND_TEXT_X, 0, CAND_TEXT_W, CAND_TEXT_H,
                                        size=13, color=PALETTE["text"])
                text.cell().setWraps_(True)
                copy_btn = self._make_button(CAND_BTN_X, 0, CAND_BTN_W, CAND_BTN_H,
                                             "复制", "copyCandidate:", tag)
                fill_btn = self._make_fill_button(CAND_BTN_X + CAND_BTN_W + CAND_BTN_GAP, 0,
                                                  CAND_BTN_W, CAND_BTN_H, tag)
                for c in (prob, text, copy_btn, fill_btn):
                    view.addSubview_(c)
                slot_rows.append({"bubble": bubble, "prob": prob, "text": text,
                                  "btn": copy_btn, "fill_btn": fill_btn})
            self._rows.append(slot_rows)

        add_btn = self._make_add_style_button()
        view.addSubview_(add_btn)
        self.rows["add_style"] = add_btn

        analysis = self._make_label(14, 0, PANEL_W - 28, ANALYSIS_H,
                                    size=11, color=PALETTE["muted"])
        analysis.cell().setWraps_(True)
        analysis.setStringValue_("等待买家消息…")
        view.addSubview_(analysis)
        self.rows["analysis"] = analysis

        self.panel.setContentView_(view)
        self._title_h = self.panel.frame().size.height - PANEL_H   # measured, not assumed
        self._relayout()
        self.rows["status"].setStringValue_("等待微信消息…")
        self._wire_window_controls()
        self._install_status_item()

    @objc.python_method
    def _build_overlay(self):
        """A transparent, click-through window aligned to WeChat: the YOLO-style view.

        Pure visualization of what perception already returns — every message's bounding
        box and its real OCR confidence, the judged one carrying intent+risk on its chip.
        Three properties keep it safe: it is OFF by default (menu-bar toggle); clicks pass
        through (`ignoresMouseEvents`), so WeChat never gets blocked; and perception
        captures by window ID, so this window can never pollute our own OCR.
        Coordinate mapping assumes the 1x nominal capture's pixel size equals the window's
        point size — that is exactly what kCGWindowImageNominalResolution promises.
        """
        self._ov_panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 200, 200), NSWindowStyleMaskBorderless,
            NSBackingStoreBuffered, False)
        self._ov_panel.setLevel_(AppKit.NSFloatingWindowLevel)
        self._ov_panel.setOpaque_(False)
        self._ov_panel.setHasShadow_(False)
        self._ov_panel.setIgnoresMouseEvents_(True)   # never steal a click meant for WeChat
        self._ov_panel.setHidesOnDeactivate_(False)
        self._ov_panel.setBackgroundColor_(NSColor.clearColor())
        view = _BoxesView.alloc().init()
        view.boxes = []
        view.cards = []
        self._ov_panel.setContentView_(view)

    @objc.python_method
    def _slot_active(self, slot: int) -> bool:
        return self.slot_tones[slot] in styles.PRESETS

    @objc.python_method
    def _relayout(self):
        """Place every control for the current tone selection and size the panel to fit.

        Two things are computed here rather than at build time. Positions are measured from
        the TOP, so when the panel grows or shrinks nothing above the change moves — only the
        bottom edge does. And the height follows the groups in use: a slot on 不用 reserves
        neither a dropdown's worth of rows nor its candidates, which is what removes the dead
        space a fixed-height panel left in the middle.
        """
        dy = self._group_top
        placements = []          # (control, x, dy_from_top, w, h)
        shown = 0
        for slot in range(styles.MAX_SLOTS):
            active = self._slot_active(slot)
            rm = self._remove_btns[slot]
            if not active:
                self._dd_boxes[slot].setHidden_(True)
                self._dds[slot].setHidden_(True)
                rm.setHidden_(True)
                for row in range(styles.PER_TONE):
                    for c in self._row_controls(slot, row):
                        c.setHidden_(True)
                continue
            extra = shown > 0
            dd_w = TONE_DD_W - (60 if extra else 0)
            placements.append((self._dd_boxes[slot], TONE_DD_X, dy, dd_w, TONE_DD_H))
            placements.append((self._dds[slot], TONE_DD_X + TONE_DD_INSET, dy,
                               dd_w - 2 * TONE_DD_INSET, TONE_DD_H))
            self._dd_boxes[slot].setHidden_(False)
            self._dds[slot].setHidden_(False)
            if extra:
                placements.append((rm, TONE_DD_X + dd_w + 8, dy, 52, TONE_DD_H))
                rm.setHidden_(False)
            else:
                rm.setHidden_(True)
            dy += TONE_DD_H + TONE_DD_GAP
            for row in range(styles.PER_TONE):
                r = self._rows[slot][row]
                # row height is reserved whether or not the candidates have arrived, so
                # nothing jumps when results land mid-generation
                placements += [
                    (r["bubble"], 14, dy - 2, PANEL_W - 28, CAND_TEXT_H + 6),
                    (r["text"], CAND_TEXT_X, dy, CAND_TEXT_W, CAND_TEXT_H),
                    (r["prob"], CAND_PROB_X, dy + 34, CAND_PROB_W, 14),
                    (r["btn"], CAND_BTN_X, dy + 24, CAND_BTN_W, CAND_BTN_H),
                    (r["fill_btn"], CAND_BTN_X + CAND_BTN_W + CAND_BTN_GAP, dy + 24,
                     CAND_BTN_W, CAND_BTN_H),
                ]
                dy += CAND_ROW_H
            shown += 1
            dy += GROUP_GAP

        add = self.rows.get("add_style")
        if add is not None:
            if shown < styles.MAX_SLOTS:
                placements.append((add, TONE_DD_X, dy, PANEL_W - 28, ADD_STYLE_H))
                add.setHidden_(False)
                dy += ADD_STYLE_H + 6
            else:
                add.setHidden_(True)

        if ANALYSIS_H:
            placements.append((self.rows["analysis"], 16, dy, PANEL_W - 32, ANALYSIS_H))
            dy += ANALYSIS_H + 4
        else:
            self.rows["analysis"].setHidden_(True)

        content_h = dy + BOTTOM_PAD
        view = self.panel.contentView()
        view.setFrameSize_(NSMakeSize(PANEL_W, content_h))
        for ctrl, x, top, w, h in placements + self._fixed:
            ctrl.setFrame_(NSMakeRect(x, content_h - top - h, w, h))

        # resize the window with its TOP edge pinned: growing downwards is what the eye
        # expects here, and _position_near() anchors the panel to WeChat's top anyway
        f = self.panel.frame()
        top = f.origin.y + f.size.height
        frame_h = content_h + self._title_h
        self.panel.setFrame_display_(
            NSMakeRect(f.origin.x, top - frame_h, PANEL_W, frame_h), True)
        self._expanded_h = frame_h

    @objc.python_method
    def _wire_window_controls(self):
        """Native traffic lights, mapped to this app's actions.

        red    -> quit. A hidden panel would otherwise be unreachable: LSUIElement apps
                  have no Dock icon, so a plain order-out looks like a crash.
        yellow -> roll the panel up instead of miniaturizing, for the same reason.
        green  -> hidden: the HUD has a fixed size and nothing to zoom.
        """
        close = self.panel.standardWindowButton_(NSWindowCloseButton)
        mini = self.panel.standardWindowButton_(NSWindowMiniaturizeButton)
        zoom = self.panel.standardWindowButton_(NSWindowZoomButton)
        if close:
            close.setTarget_(self)
            close.setAction_("quitApp:")
            close.setToolTip_("退出 jev-jarvis")
        if mini:
            mini.setTarget_(self)
            mini.setAction_("collapsePanel:")
            mini.setToolTip_("收起 / 展开面板")
        if zoom:
            zoom.setHidden_(True)

    @objc.python_method
    def _install_status_item(self):
        """Menu-bar item — the standard place for a background helper's controls."""
        bar = AppKit.NSStatusBar.systemStatusBar()
        self.status_item = bar.statusItemWithLength_(AppKit.NSVariableStatusItemLength)
        self.status_item.button().setTitle_("J")
        self.status_item.button().setToolTip_("jev-jarvis · 意图助手")

        menu = AppKit.NSMenu.alloc().init()
        for title, action, key in (
            ("显示 / 收起面板", "collapsePanel:", ""),
            ("暂停读屏", "togglePause:", ""),
            ("YOLO 检测框", "toggleBoxes:", ""),
            ("微信上叠灰卡（默认关）", "toggleCards:", ""),
            ("读取钉钉（只读）", "toggleApp:", ""),
            ("立即重新分析", "reanalyze:", ""),
        ):
            menu.addItemWithTitle_action_keyEquivalent_(title, action, key)
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        menu.addItemWithTitle_action_keyEquivalent_("退出 jev-jarvis", "quitApp:", "q")
        for item in menu.itemArray():
            item.setTarget_(self)
        self.pause_item = menu.itemArray()[1]
        self.boxes_item = menu.itemArray()[2]
        self.cards_item = menu.itemArray()[3]
        self.app_item = menu.itemArray()[4]
        self.app_item.setState_(
            AppKit.NSOnState if self._app == "dingtalk" else AppKit.NSOffState)
        self.boxes_item.setState_(
            AppKit.NSOnState if self._show_boxes else AppKit.NSOffState)
        self.cards_item.setState_(
            AppKit.NSOnState if self._show_cards else AppKit.NSOffState)
        self.status_item.setMenu_(menu)

    @objc.python_method
    def _make_label(self, x, y, w, h, size=13, color=None, bold=False):
        tf = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
        tf.setStringValue_("")
        tf.setBezeled_(False)
        tf.setDrawsBackground_(False)
        tf.setEditable_(False)
        tf.setSelectable_(True)
        tf.setTextColor_(PALETTE["text"] if color is None else color)
        tf.setFont_(NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size))
        return tf

    @objc.python_method
    def _make_button(self, x, y, w, h, title, action, tag):
        """A native rounded bezel with a WeChat-green label — reads correctly on #F7F7F7."""
        btn = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
        btn.setTitle_(title)
        btn.setBezelStyle_(NSBezelStyleRounded)
        btn.setFont_(NSFont.systemFontOfSize_(11))
        btn.setContentTintColor_(PALETTE["green"])
        btn.setTarget_(self)
        btn.setAction_(action)
        btn.setTag_(tag)
        btn.setHidden_(True)
        return btn

    @objc.python_method
    def _make_add_style_button(self):
        """Dashed WeChat-style add row: opens the next empty 话术 slot, up to 3."""
        btn = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, PANEL_W - 28, ADD_STYLE_H))
        btn.setBordered_(False)
        btn.setBezelStyle_(NSBezelStyleRounded)
        btn.setFont_(NSFont.systemFontOfSize_(13))
        btn.setWantsLayer_(True)
        btn.layer().setBackgroundColor_(PALETTE["card"].CGColor())
        btn.layer().setCornerRadius_(8.0)
        btn.layer().setBorderWidth_(1.0)
        btn.layer().setBorderColor_(PALETTE["green"].colorWithAlphaComponent_(0.45).CGColor())
        attr = NSAttributedString.alloc().initWithString_attributes_(
            "＋ 增加回复风格",
            {NSFontAttributeName: NSFont.systemFontOfSize_(13),
             NSForegroundColorAttributeName: PALETTE["green"]})
        btn.setAttributedTitle_(attr)
        btn.setToolTip_("再加一种话术，最多三种，每种一条回复")
        btn.setTarget_(self)
        btn.setAction_("addStyle:")
        return btn

    @objc.python_method
    def _make_fill_button(self, x, y, w, h, tag):
        """WeChat-green primary action — same job as 发送, never auto-sends."""
        btn = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
        btn.setTitle_("填入")
        btn.setBordered_(False)
        btn.setBezelStyle_(NSBezelStyleRounded)
        btn.setFont_(NSFont.boldSystemFontOfSize_(12))
        btn.setWantsLayer_(True)
        btn.layer().setBackgroundColor_(PALETTE["green"].CGColor())
        btn.layer().setCornerRadius_(6.0)
        btn.setContentTintColor_(PALETTE["white"])
        attr = NSAttributedString.alloc().initWithString_attributes_(
            "填入",
            {NSFontAttributeName: NSFont.boldSystemFontOfSize_(12),
             NSForegroundColorAttributeName: PALETTE["white"]})
        btn.setAttributedTitle_(attr)
        btn.setTarget_(self)
        btn.setAction_("fillCandidate:")
        btn.setTag_(tag)
        btn.setHidden_(True)
        return btn

    @objc.python_method
    def _show(self):
        if not self.panel.isVisible():
            self.panel.orderFrontRegardless()

    @objc.python_method
    def _refresh_transcript(self, msgs) -> None:
        """Paint the OCR'd turns into the Jev window. Newest at the bottom."""
        self.rows["transcript"].setAttributedStringValue_(
            format_transcript_attributed(msgs, self._card_cache))

    @objc.python_method
    def _set_analysis(self, text: str, color=None) -> None:
        # Hidden leftover row; the live intent lives in the WeChat-style card.
        self.rows["analysis"].setStringValue_(text)
        self.rows["analysis"].setTextColor_(color or PALETTE["muted"])

    @objc.python_method
    def _paint_intent_card(self, name: str, meta: str, accent=None, fill=None,
                           pill: str = "") -> None:
        accent = accent or PALETTE["muted"]
        fill = fill or PALETTE["card"]
        self.rows["intent_name"].setStringValue_(name or "—")
        self.rows["intent_name"].setFont_(_jev_font(24, True))
        self.rows["intent_name"].setTextColor_(PALETTE["jev"])
        self.rows["intent_meta"].setStringValue_(meta or "")
        self.rows["intent_meta"].setFont_(_jev_font(13))
        self.rows["intent_meta"].setTextColor_(PALETTE["jev_soft"])
        self.rows["intent_kicker"].setFont_(_jev_font(12, True))
        self.rows["intent_kicker"].setTextColor_(PALETTE["jev"])
        self.rows["risk_pill"].setStringValue_(pill)
        self.rows["risk_pill"].setFont_(_jev_font(12, True))
        self.rows["risk_pill"].setTextColor_(accent)
        box = self.rows["intent_box"]
        box.layer().setBackgroundColor_(fill.CGColor())
        if accent is PALETTE["green"]:
            box.layer().setBorderColor_(PALETTE["green"].colorWithAlphaComponent_(0.25).CGColor())
        elif accent is PALETTE["amber"]:
            box.layer().setBorderColor_(PALETTE["amber"].colorWithAlphaComponent_(0.30).CGColor())
        elif accent is PALETTE["red"]:
            box.layer().setBorderColor_(PALETTE["red"].colorWithAlphaComponent_(0.30).CGColor())
        else:
            box.layer().setBorderColor_(PALETTE["edge"].CGColor())

    @objc.python_method
    def _analysis_from_verdict(self, v: dict) -> None:
        intent = v.get("intent") or "—"
        conf = float(v.get("confidence") or 0.0)
        risk = int(round(float(v.get("risk") or 0)))
        label = "安全" if risk <= 3 else ("留神" if risk <= 6 else "危险")
        color = PALETTE["green"] if risk <= 3 else (
            PALETTE["amber"] if risk <= 6 else PALETTE["red"])
        fill = PALETTE["green_soft"] if risk <= 3 else (
            PALETTE["amber_soft"] if risk <= 6 else PALETTE["red_soft"])
        hint = INTENT_HINTS.get(intent, "")
        actions = " · ".join(v.get("actions") or [])
        meta_bits = [hint] if hint else []
        if conf:
            meta_bits.append(f"把握 {conf:.0%}")
        if actions:
            meta_bits.append(actions)
        self._paint_intent_card(intent, "  ·  ".join(meta_bits), color, fill,
                                pill=f"{label} {risk}/9")
        line = f"{intent} {conf:.0%} · {label} {risk}/9"
        if actions:
            line += f"\n{actions}"
        self.rows["analysis"].setStringValue_(line)
        self.rows["analysis"].setTextColor_(color)

    @objc.python_method
    def _context_line(self, sender, prev: str) -> str:
        parts = []
        if sender:
            parts.append(f"来自 {sender}")
        if prev:
            parts.append(f"上文：{prev[:26]}")
        return " · ".join(parts)

    @objc.python_method
    def _render(self, key: str, text: str, color: NSColor | None = None):
        tf = self.rows[key]
        tf.setStringValue_(text)
        if color is not None:
            tf.setTextColor_(color)

    @objc.python_method
    def _row_controls(self, slot: int, row: int):
        r = self._rows[slot][row]
        return (r["bubble"], r["prob"], r["text"], r["btn"], r["fill_btn"])

    @objc.python_method
    def _render_groups(self, payload: list):
        """payload: [(slot, tone, [{"text","prob"}, ...]), ...] — one entry per active tone.

        Rows the model did not fill are emptied and their buttons hidden, but the row keeps
        its space: the panel's height is decided by the tone selection, not by how many lines
        came back, so a late result cannot resize the panel under the cursor.
        """
        wanted = set()
        for slot, _tone, items in payload:
            for row in range(styles.PER_TONE):
                if row < len(items):
                    it = items[row]
                    wanted.add((slot, row))
                    r = self._rows[slot][row]
                    prob = "排序中" if it["prob"] is None else f"{it['prob'] * 100:.0f}%"
                    r["prob"].setStringValue_(prob)
                    r["text"].setStringValue_(it["text"])
                    for c in self._row_controls(slot, row):
                        c.setHidden_(not self._slot_active(slot))
                    self.cand_texts[slot * styles.PER_TONE + row] = it["text"]
        for slot in range(styles.MAX_SLOTS):
            for row in range(styles.PER_TONE):
                if (slot, row) not in wanted and self._slot_active(slot):
                    r = self._rows[slot][row]
                    r["prob"].setStringValue_("")
                    r["text"].setStringValue_("")
                    r["btn"].setHidden_(True)
                    r["fill_btn"].setHidden_(True)
                    r["bubble"].setHidden_(True)
                    self.cand_texts[slot * styles.PER_TONE + row] = None
        self._relayout()

    @objc.python_method
    def _clear_candidates(self):
        for slot in range(styles.MAX_SLOTS):
            for row in range(styles.PER_TONE):
                r = self._rows[slot][row]
                r["prob"].setStringValue_("")
                r["text"].setStringValue_("")
                r["btn"].setHidden_(True)
                r["fill_btn"].setHidden_(True)
                r["bubble"].setHidden_(True)
                self.cand_texts[slot * styles.PER_TONE + row] = None

    @objc.python_method
    def _display_height(self) -> float:
        """Height of the display whose origin is (0,0) — the Quartz<->Cocoa flip constant.

        Taking this from the *target* screen is wrong on multi-display setups: a screen
        placed above the main one has origin.y > 0 and the flip must still use the
        primary display's height.
        """
        for scr in NSScreen.screens():
            f = scr.frame()
            if f.origin.x == 0 and f.origin.y == 0:
                return f.size.height
        return NSScreen.mainScreen().frame().size.height

    @objc.python_method
    def _position_near(self, win: dict | None):
        """Dock the panel beside WeChat, on the screen WeChat is actually on.

        Uses global Cocoa coordinates throughout. NSScreen.mainScreen() must NOT be used:
        it follows whichever display holds the key window, so relying on it made the panel
        hop ~1369 px between displays a few times a minute.
        """
        flip = self._display_height()
        panel_h = self.panel.frame().size.height or PANEL_H
        panel_w = self.panel.frame().size.width or PANEL_W
        screens = list(NSScreen.screens())
        primary = next((s for s in screens
                        if s.frame().origin.x == 0 and s.frame().origin.y == 0), screens[0])

        if win:
            # CGWindow bounds are top-left origin global pixels -> Cocoa bottom-left
            wx, wy = win["x"], win["y"]
            ww, wh = win["w"], win["h"]
            cx_win = wx + ww / 2.0
            cyan = flip - (wy + wh / 2.0)
            host = next((s for s in screens
                         if s.frame().origin.x <= cx_win <= s.frame().origin.x + s.frame().size.width
                         and s.frame().origin.y <= cyan <= s.frame().origin.y + s.frame().size.height),
                        primary)
            sf = host.frame()
            # dock right of WeChat if it fits on that screen, else left, else its right edge
            x = wx + ww + 8
            if x + panel_w > sf.origin.x + sf.size.width:
                x = wx - panel_w - 8
            if x < sf.origin.x:
                x = sf.origin.x + sf.size.width - panel_w - 12
            y = flip - wy - panel_h
            y = max(sf.origin.y + 40, min(y, sf.origin.y + sf.size.height - panel_h - 40))
        else:
            sf = primary.frame()
            x = sf.size.width - panel_w - 12
            y = sf.size.height - panel_h - 60

        # dead-band: ignore sub-2pt corrections and one-off blips, so WeChat's own window
        # animations (and our own numeric noise) stop nudging the panel around
        target = (round(x), round(y))
        last = self._last_origin
        if last is None:                    # first placement: apply without debounce
            self._last_origin = target
            self._pending_origin = target
            self.panel.setFrameOrigin_(target)
            return
        if abs(target[0] - last[0]) <= 2 and abs(target[1] - last[1]) <= 2:
            return
        if target != self._pending_origin:
            self._pending_origin = target
            return  # require the same target on two consecutive ticks before moving
        self._last_origin = target
        self.panel.setFrameOrigin_(target)

    # ------------------------------------------------------------ actions
    def copyCandidate_(self, sender):
        text = self.cand_texts[sender.tag()] if 0 <= sender.tag() < len(self.cand_texts) else None
        if not text:
            return
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(text, NSPasteboardTypeString)
        self._render("status", "已复制", PALETTE["green"])

    def fillCandidate_(self, sender):
        """Write the candidate into WeChat's input box (src/fill.py).

        DingTalk never reaches the write: the mode is capture-and-OCR only.
        """
        if self._app == "dingtalk":
            self._render("status", "钉钉只读，不填入", PALETTE["muted"])
            return
        idx = sender.tag()
        text = self.cand_texts[idx] if 0 <= idx < len(self.cand_texts) else None
        if not text:
            return
        # The status line is painted before the call because writing into WeChat takes a
        # beat; the click should look instant even though the write has not happened yet.
        self._render("status", "填入中…", PALETTE["muted"])
        self.panel.displayIfNeeded()
        if not fill.has_accessibility():
            # First click is the moment to ask: the system dialog is the only way in.
            fill.request_accessibility()
        target = getattr(self, "_input_target", None)
        if target is None or (target["box"] is None and not target.get("visual_rect")):
            self._render("status", "填入失败：" + (target["reason"] if target else "等待输入框定位"), PALETTE["red"])
            return
        ok, reason = fill.fill_text(text, target=target)
        if ok:
            self._render("status", reason, PALETTE["green"])
        else:
            self._render("status", f"填入失败：{reason}", PALETTE["red"])

    def toneChanged_(self, sender):
        """A 话术 dropdown moved: the verdict is still valid, only the writing changes."""
        picked = []
        for i, p in enumerate(self._dds):
            if self.slot_tones[i] not in styles.PRESETS:
                picked.append(styles.NONE_LABEL)
            else:
                picked.append(p.titleOfSelectedItem() or styles.NONE_LABEL)
        if picked == self.slot_tones:
            return
        self.slot_tones = picked
        # the panel is sized by how many slots are in use, so re-lay-out *before* the new
        # candidates arrive: the empty rows appear at once and nothing jumps later
        self._clear_candidates()
        self._stream_rows = {}     # the run _regenerate starts streams into fresh rows
        self._regenerate()

    def addStyle_(self, sender):
        """Open the next empty slot with a still-unused 话术, up to MAX_SLOTS."""
        used = {t for t in self.slot_tones if t in styles.PRESETS}
        if len(used) >= styles.MAX_SLOTS:
            self._render("status", "最多三种风格", PALETTE["muted"])
            return
        nxt = next((i for i, t in enumerate(self.slot_tones) if t not in styles.PRESETS), None)
        if nxt is None:
            return
        pick = next((lab for lab in styles.labels() if lab not in used), None)
        if not pick:
            self._render("status", "没有更多话术可加", PALETTE["muted"])
            return
        self.slot_tones[nxt] = pick
        self._dds[nxt].selectItemWithTitle_(pick)
        self._clear_candidates()
        self._stream_rows = {}
        self._regenerate()

    def removeStyle_(self, sender):
        """Close one extra 话术 slot. The first remaining style stays on."""
        slot = int(sender.tag())
        if slot < 0 or slot >= styles.MAX_SLOTS:
            return
        if sum(1 for t in self.slot_tones if t in styles.PRESETS) <= 1:
            self._render("status", "至少保留一种风格", PALETTE["amber"])
            return
        self.slot_tones[slot] = styles.NONE_LABEL
        self._clear_candidates()
        self._stream_rows = {}
        self._regenerate()

    @objc.python_method
    def _regenerate(self):
        """Re-run just the generation half for the message on screen.

        No re-judging and no re-reading of the screen: the intent and risk do not depend on
        the tone, and re-running them would make a dropdown click feel like a new analysis.
        """
        self._relayout()
        text = self.analyzed_text
        if not text:
            self._render("status", "话术已选 · 下条消息生效", PALETTE["muted"])
            return
        active = [t for t in self.slot_tones if t in styles.PRESETS]
        if not active:
            self._render("status", "没选话术 · 至少选一个", PALETTE["amber"])
            return
        self._render("status", f"换话术中…（{'、'.join(active)}）", PALETTE["muted"])
        self.rows["cand_header"].setStringValue_("候选回复 · 生成中…")
        threading.Thread(target=self._reply_task,
                         args=(self._reply_epoch, self._regen_work,
                               text, self._last_intent, list(self.slot_tones)),
                         daemon=True).start()

    @objc.python_method
    def _payload_from_gen(self, gen: dict):
        """Generation result -> unranked [(slot, tone, items)] (prob=None ⇒ 待排序).

        None when nothing usable came back — the caller shows gen's error then.
        """
        groups = [g for g in (gen.get("groups") or []) if g.get("texts")]
        if not groups:
            return None
        return [(g["slot"], g["tone"], [{"text": t, "prob": None} for t in g["texts"]])
                for g in groups]

    @objc.python_method
    def _rank_payload(self, payload: list, message: str, intent: str) -> list:
        """Score and reorder each group's candidates. One ranking pass covers every
        candidate the requests produced, so `#1`/`#2` inside a group means "the better of
        these two", not "whichever line the model wrote first" — one forward pass, not one
        per tone. Ranking failure leaves probabilities at 0 rather than dropping rows.
        """
        texts = [it["text"] for _s, _t, items in payload for it in items]
        scores: dict[str, float] = {}
        if intent and texts:
            try:
                with self._model_lock:   # never two local forwards at once
                    ranked = self.judge.rank_candidates(message, intent, texts)
                scores = {r["text"]: r["prob"] for r in ranked}
            except Exception:
                scores = {}
        out = []
        for slot, tone, items in payload:
            scored = [{"text": it["text"], "prob": scores.get(it["text"], 0.0)}
                      for it in items]
            scored.sort(key=lambda x: -x["prob"])
            out.append((slot, tone, scored))
        return out

    @objc.python_method
    def _stream_hook(self, t0: float, label: str = ""):
        """The on_candidate callback for the generation run starting now.

        Shared by all three run starters (_analyze, _run_generation, _regen_work) so the
        streaming lines follow one epoch/rows discipline no matter which path produced
        them. The callback runs on the run's worker thread; it hops to the main thread for
        every UI touch, and the first line it sees logs the latency that streaming is here
        for. The epoch check inside applyStreamLine_ is what makes a superseded run's late
        lines harmless.
        """
        reply_epoch = getattr(self._reply_worker, "epoch", self._reply_epoch)
        self._gen_epoch += 1
        epoch = self._gen_epoch
        prefix = f"{label} " if label else ""
        first_line = {"shown": False}

        def on_candidate(slot: int, _tone: str, text: str) -> None:
            if not first_line["shown"]:
                first_line["shown"] = True
                _log(f"{prefix}首条候选上屏 {(time.perf_counter() - t0) * 1000:.0f}ms（未排序）")
            self._push_reply("applyStreamLine:", (epoch, slot, text), reply_epoch)
        return on_candidate

    @objc.python_method
    def _regen_work(self, text: str, intent: str, slot_tones: list[str]):
        t0 = time.perf_counter()
        try:
            if not self._reply_current():
                return
            gen = self.generator.generate(text, intent, slot_tones, None,
                                          self._stream_hook(t0, "换话术"))
            groups = gen.get("groups") or []
            failed = [f"{g['tone']}({g['error'][:40]})" for g in groups if g.get("error")]
            _log(f"换话术 生成 {gen.get('elapsed_s', 0) * 1000:.0f}ms · {len(groups)} 个话术"
                 + (f" · 失败: {'; '.join(failed)}" if failed else ""))
            payload = self._payload_from_gen(gen)
            if payload is None:
                err = (gen.get("error") or "空结果")[:60]
                _log(f"换话术无可用候选: {err}")
                self._push("applyError:", f"候选生成失败: {err}")
                return
            # streamed endpoints already showed the lines; this push only matters for the
            # non-streaming shape (anthropic), which has no applyStreamLine_ at all
            self._push("applyTones:", payload)
            ranked = self._rank_payload(payload, text, intent)
            _log(f"换话术 端到端 {(time.perf_counter() - t0) * 1000:.0f}ms")
            self._push("applyTones:", ranked)
        except Exception as e:
            _log(f"换话术失败 {type(e).__name__}: {str(e)[:60]}")
            self._push("applyError:", f"换话术失败: {type(e).__name__}: {str(e)[:40]}")

    @objc.python_method
    def _payload_current(self, payload) -> bool:
        """False when the tone selection moved on — a late result must not repaint it.

        Generation+ranking now pushes twice (unranked, then ranked); a dropdown click
        between the two would otherwise bring back the tone the user just switched away
        from. Same guard for a 换话术 result racing a second click.
        """
        return all(self.slot_tones[slot] == tone for slot, tone, _items in payload)

    @objc.python_method
    def _cand_header(self, payload) -> str:
        pending = any(it["prob"] is None for _s, _t, items in payload for it in items)
        return "候选回复 · 排序中…" if pending else "候选回复（按合适度排序）"

    def applyTones_(self, payload):
        if not self._payload_current(payload):
            return
        self.rows["cand_header"].setStringValue_(self._cand_header(payload))
        total = sum(len(items) for _s, _t, items in payload)
        self._render("status", f"已换话术 · {total} 条", PALETTE["muted"])
        self._render_groups(payload)

    # ------------------------------------------------------------ controls
    def collapsePanel_(self, sender):
        self._set_collapsed(not self._collapsed)

    def togglePause_(self, sender):
        self._paused = not self._paused
        self.pause_item.setTitle_("继续读屏" if self._paused else "暂停读屏")
        if self._paused:
            self._prejudge_req = None        # a paused app judges nothing further
            self._prejudge_result = None
            self._pregen_req = None          # …and generates nothing further
            self._pregen_result = None
            if self._ov_panel.isVisible():   # frozen boxes would lie about "realtime"
                self._ov_panel.orderOut_(None)
            self._render("status", "已暂停 · 不再读屏", PALETTE["amber"])
            self._render("message", "", PALETTE["text"])
            self._render("sender", "", PALETTE["muted"])
            self._render("intent", "—", PALETTE["muted"])
            self._render("confidence", "", PALETTE["muted"])
            self._render("risk", "", PALETTE["muted"])
            self._render("actions", "", PALETTE["text"])
            self._set_analysis("已暂停")
            self._paint_intent_card("已暂停", "不再读屏", PALETTE["amber"], PALETTE["amber_soft"],
                                    pill="暂停")
            self.rows["cand_header"].setStringValue_("")
            self._clear_candidates()
        else:
            self._prejudge_result = None
            self.last_seen = None      # force a fresh read of whatever is on screen
            self.analyzed_text = None
            self._render("status", "已恢复 · 读屏中", PALETTE["muted"])

    def reanalyze_(self, sender):
        self._prejudge_req = None      # "re-analyze" means re-run, not reuse the pre-judge
        self._prejudge_result = None
        self._pregen_req = None        # …and not reuse the early generation either
        self._pregen_result = None
        self.last_seen = None
        self.analyzed_text = None
        self._render("status", "重新分析中…", PALETTE["muted"])
        self._paint_intent_card("重新分析", "正在重新判断这条消息", PALETTE["green"], PALETTE["green_soft"])

    def quitApp_(self, sender):
        AppKit.NSApplication.sharedApplication().terminate_(None)

    @objc.python_method
    def _set_collapsed(self, collapsed: bool):
        """Roll the panel up to a title+status strip, or back to full height."""
        self._collapsed = collapsed
        controlled = ["transcript", "analysis", "cand_header",
                      "intent_box", "intent_kicker", "intent_name",
                      "intent_meta", "risk_pill", "transcript_box"]
        for key in controlled:
            self.rows[key].setHidden_(collapsed)
        add = self.rows.get("add_style")
        if add is not None:
            add.setHidden_(collapsed)
        for slot in range(styles.MAX_SLOTS):
            self._dds[slot].setHidden_(collapsed)
            self._dd_boxes[slot].setHidden_(collapsed)
            self._remove_btns[slot].setHidden_(collapsed)
            for row in range(styles.PER_TONE):
                has = self.cand_texts[slot * styles.PER_TONE + row] is not None
                for c in self._row_controls(slot, row):
                    c.setHidden_(collapsed or not has)
        if not collapsed:
            # re-expanding puts every control back where _relayout() wants it, and re-hides
            # the slots that are switched off — the collapse above cannot know that
            self._relayout()
            self._last_origin = None      # let the next tick re-dock cleanly
            return

        rect = self.panel.frame()
        # _expanded_h is maintained by _relayout() (it changes with the tone selection), so
        # expanding reads the current full height rather than a value captured at startup
        new_h = COLLAPSED_H if collapsed else (self._expanded_h or PANEL_H)
        self.panel.setFrame_display_(
            NSMakeRect(rect.origin.x, rect.origin.y + (rect.size.height - new_h),
                       rect.size.width, new_h), True)
        self._last_origin = None      # let the next tick re-dock cleanly

    # --------------------------------------------------------------- loop
    def tick_(self, timer):
        if self._paused or self._busy or time.time() < self._next_read_ts:
            return  # paused, a previous read is still running, or not due yet
        self._busy = True
        threading.Thread(target=self._work, daemon=True).start()

    @objc.python_method
    def _work(self):
        try:
            self._work_inner()
        finally:
            self._busy = False

    @objc.python_method
    def _work_inner(self):
        if not screen_capture_ok():
            if not self._asked_permission:
                self._asked_permission = True
                request_screen_capture()      # opens the system prompt
                _log("需要屏幕录制权限")
            self._push("applyError:", "需要屏幕录制权限 · 系统设置 › 隐私与安全性")
            self._next_read_ts = time.time() + SLOW_TICK
            return
        try:
            res = read_conversation(previous_wid=self._win_wid,
                                    prev_fingerprint=self._fingerprint)
        except Exception as e:
            _log(f"读取失败 {type(e).__name__}: {str(e)[:60]}")
            self._push("applyError:", f"读取失败: {type(e).__name__}: {str(e)[:40]}")
            self._next_read_ts = time.time() + SLOW_TICK
            return
        if not res["ok"]:
            # WeChat gone (quit / minimised / other Space) -> hide with it.
            # A capture error with the window still there stays on screen.
            err = res.get("error") or "微信窗口未找到"
            missing = "window not found" in err.lower() or "窗口未找到" in err
            if self._last_skip_reason != err:
                self._last_skip_reason = err
                _log(f"读屏失败 · {err}")
            if missing:
                self._missing_n += 1
                self._win_wid = None
                self._fingerprint = None
                # WeChat 4.x occasionally drops out of the window list for one
                # tick; hide only after two misses so the HUD does not flicker.
                if self._missing_n >= 2:
                    self._push("applyHidden:", err)
                self._next_read_ts = time.time() + FAST_TICK
            else:
                self._push("applyError:", err)
                self._next_read_ts = time.time() + SLOW_TICK
            return

        # Same fingerprint ⇒ same pixels ⇒ the messages are exactly what we last read.
        # Cadence follows the screen: quiet checks back in FAST_TICK (capture+hash only,
        # ~30 ms); a change first keeps BURST_TICK for a few reads so the burst's NEXT
        # message is noticed quickly (this also feeds _stable_n, the early-settle signal),
        # and only a pane that keeps moving settles back to SLOW_TICK like the old poll.
        # Blank OCR must not freeze the skip-hash: otherwise a later real chat
        # is treated as "same empty picture" forever.
        self._fingerprint = res.get("fingerprint") if (
            res.get("unchanged") or res.get("n_blocks") or res.get("messages")
        ) else None
        if res["unchanged"]:
            self._stable_n += 1
            self._burst_left = BURST_READS
            self._next_read_ts = time.time() + FAST_TICK
        elif self._burst_left > 0:
            self._burst_left -= 1
            self._stable_n = 0
            self._next_read_ts = time.time() + BURST_TICK
        else:
            self._stable_n = 0
            self._next_read_ts = time.time() + SLOW_TICK

        # position immediately: analysis takes seconds, and a delayed correction
        # showed up as a visible jump after the verdict landed. Pushed on unchanged
        # frames too — the window can move while its pixels stay identical.
        live_window = res["window"]
        self._win_wid = res["window"]["wid"]
        self._missing_n = 0
        self._push("applyPosition:", res["window"])
        self._push("applyVisible:", None)
        if res["unchanged"] and self._last_full is not None:
            # the settle/analyze gate below still runs every read; an unchanged frame
            # just skips re-deriving the messages it would act on
            res = self._last_full
        else:
            self._last_full = res
            self._push("applyChat:", res.get("chat_title") or "")

        res = dict(res, window=live_window)
        # AX traversal of WeChat's tree can stall for seconds (measured: the
        # read worker sat in AXUIElementCopyAttributeValue and `_busy` never
        # cleared, so the HUD showed「等待微信消息」with no 读屏 log). Locate
        # the input box off this thread; OCR / transcript must not wait.
        self._schedule_input_locate(res["window"])
        msgs = res["messages"]
        thems = [m for m in msgs if m.side == "them"]
        newest = thems[-1] if thems else None
        prev_text = thems[-2].text if len(thems) > 1 else ""

        key = (res.get("chat_title") or "", newest.text) if newest else None
        if key != self._reply_key:
            self._reply_epoch += 1
            self._reply_key = key
            self.last_seen = None
            self.analyzed_text = None
            self._prejudge_req = self._prejudge_result = None
            self._pregen_req = self._pregen_result = None
            self._gen_epoch += 1
            # conversation (or target message) changed: drop in-flight cards so
            # a late verdict from the previous chat cannot paint on this one.
            # Finished cards stay in _card_cache — same wording in a new chat
            # is the same judgment.
            self._card_epoch += 1
            self._card_inflight.clear()

        # Jev analysis for every visible buyer bubble, even when the WeChat
        # overlay is off — the result is painted under the turn in this panel.
        self._enqueue_cards(msgs, newest)
        self._push("applyTranscript:", msgs)
        if self._show_boxes or self._show_cards:
            self._push("applyBoxes:", (res["window"], msgs,
                                       newest.text if newest else None))
        if newest is None:
            t = res.get("timing_ms") or {}
            n_blocks = int(res.get("n_blocks") or 0)
            title = (res.get("chat_title") or "").strip()
            skip = f"no-them:{title}:{len(msgs)}:{n_blocks}"
            if not self._read_once or self._last_skip_reason != skip:
                self._read_once = True
                self._last_skip_reason = skip
                path = t.get("capture_path") or ""
                covering_log = (res.get("occluded_by") or "").strip()
                _log(f"读屏 抓取 {t.get('capture', 0):.0f}ms + OCR {t.get('ocr', 0):.0f}ms"
                     f" = {t.get('total', 0):.0f}ms · 读到 {len(msgs)} 条（对方 0 条）"
                     f" · OCR {n_blocks} 块"
                     + (f" · {title}" if title else "")
                     + (f" · {path}" if path else "")
                     + (f" · 被{covering_log}挡住" if covering_log else ""))
            covering = (res.get("occluded_by") or "").strip()
            if covering:
                hint = f"微信被「{covering}」挡住了，点一下微信窗口"
            elif title and len(msgs) == 0:
                hint = f"{title} · 没有文字气泡，换一个有买家文字的聊天"
            elif n_blocks < 3:
                hint = "打开一个有文字气泡的聊天窗口"
            else:
                hint = "聊天区没有识别为买家的气泡（可能在会话列表）"
            self._push("applyWaiting:", hint)
            return
        now = time.time()

        # --- anti-flood: track arrivals, never analyze mid-burst
        if newest.text != self.last_seen:
            self.last_seen = newest.text
            self.last_change_ts = now
            # only on arrival: this function runs every second, and a per-tick line would
            # bury the timing that matters
            t = res.get("timing_ms") or {}
            first_read = not self._read_once
            self._read_once = True
            # Vision loads on the first call and costs ~2x steady state; saying so keeps a
            # one-off from being read as a regression (same reason the judge line does it)
            note = "（首次，含 Vision 加载）" if first_read and t.get("ocr", 0) > 400 else ""
            # say when the fast in-process capture was refused: otherwise a permanent
            # fallback looks like ordinary slowness instead of something to report
            slow_cap = " · 抓屏走了子进程（进程内被抓图接口拒绝）" \
                if t.get("capture_path") == "subprocess" else ""
            n_blocks = int(res.get("n_blocks") or 0)
            _log(f"读屏 抓取 {t.get('capture', 0):.0f}ms + OCR {t.get('ocr', 0):.0f}ms"
                 f" = {t.get('total', 0):.0f}ms · 读到 {len(msgs)} 条（对方 {len(thems)} 条）"
                 f" · OCR {n_blocks} 块{note}{slow_cap}")
            _log(f"新消息 · 预判+生成先跑，停稳 {SETTLE_S}s（连续 {STABLE_READS} 跳不变最早 "
                 f"{EARLY_SETTLE_S}s）后上屏（两次完整分析最小间隔 {MIN_GAP_S}s）")
            # latest-wins: overwrite the slot, retire the old verdict — only the newest
            # text's judgment can ever be consumed, and only by the settle gate below
            self._prejudge_req = (newest.text, self._context_text(msgs, newest, JUDGE_TURNS),
                                  newest.sender, prev_text, self._reply_epoch)
            self._prejudge_result = None
            self._prejudge_event.set()
            # same discipline for the generation half: fire now, supersede on the next
            # arrival, spend at settle. Tones are captured here — a dropdown click during
            # the window invalidates the result at consumption time (checked in _take_pregen)
            self._pregen_req = (newest.text, self._context_text(msgs, newest),
                                tuple(self.slot_tones), self._reply_epoch)
            self._pregen_result = None
            self._pregen_event.set()
            # keep the previous verdict readable; just badge that something new landed
            self._push("applyIncoming:", (newest.text, newest.sender, prev_text))

        # Anti-flood, two signals: the blind wait (SETTLE_S, unchanged upper bound) or a
        # content-stability early open — the pane went quiet for STABLE_READS consecutive
        # reads spanning at least EARLY_SETTLE_S, which is itself evidence the burst is
        # over. A burst keeps resetting _stable_n, so mid-burst opens cannot happen.
        elapsed = now - self.last_change_ts
        settled = elapsed >= SETTLE_S or (elapsed >= EARLY_SETTLE_S
                                          and self._stable_n >= STABLE_READS)
        cooled = (now - self.last_analyze_ts) >= MIN_GAP_S
        pr = self._prejudge_result
        pre_hit = pr is not None and pr[0] == newest.text and pr[4] == self._reply_epoch
        # A pre-judged verdict needs no cooling: its cost was already paid per arrival.
        # Only the full path (no usable pre-judgment) still waits MIN_GAP_S out.
        if (newest.text != self.analyzed_text and settled and not self._analyzing
                and not self._prejudging and (pre_hit or cooled)):
            self.last_analyze_ts = now
            self.analyzed_text = newest.text
            self._prejudge_result = None      # spent: a verdict is shown exactly once
            self._analyzing = True
            if pre_hit:
                # Judgment already ran inside the settle window; go straight to the
                # verdict on screen and start only the generation half.
                _log(f"停稳 · 用预判结论上屏 · 这条消息出现到现在 {now - self.last_change_ts:.1f}s")
                self._push("applyJudgment:", (pr[1], pr[2], pr[3]))
                threading.Thread(target=self._reply_task,
                                 args=(self._reply_epoch, self._run_generation,
                                       newest, msgs, pr[1]), daemon=True).start()
            else:
                _log(f"开始分析 · 这条消息出现到现在 {now - self.last_change_ts:.1f}s")
                self._push("applyPending:", (newest.text, newest.sender, prev_text))
                # off the tick path on purpose: judge+generate+rank takes over a second, and
                # while it runs the loop must keep reading — a message landing mid-analysis
                # used to wait the whole analysis out before anyone even saw it
                threading.Thread(target=self._reply_task,
                                 args=(self._reply_epoch, self._run_analysis,
                                       newest, msgs, prev_text), daemon=True).start()
        elif newest.text != self.analyzed_text:
            # the wait is deliberate; say so once per arrival change so "it feels slow" can
            # be told apart from "it is still waiting out the burst window"
            why = ("消息还在变" if not settled else
                   "上一条还在分析" if self._analyzing else
                   "预判还在跑" if self._prejudging else
                   f"距上次分析不足 {MIN_GAP_S}s")
            if self._last_skip_reason != why:
                self._last_skip_reason = why
                _log(f"暂不分析（{why}）")
        else:
            self._last_skip_reason = None

    @objc.python_method
    def _run_analysis(self, newest, msgs, prev_text: str):
        try:
            if not self._reply_current():
                return
            self._analyze(newest, msgs, prev_text)
        except Exception as e:
            _log(f"分析失败 {type(e).__name__}: {str(e)[:60]}")
            self._push("applyError:", f"分析失败: {type(e).__name__}: {str(e)[:40]}")
        finally:
            self._analyzing = False

    @objc.python_method
    def _prejudge_loop(self):
        """Judge a message the moment it is seen, so the settle gate can skip the wait.

        One resident worker serializes the passes (a forward takes ~1 s). The request slot
        holds only the newest text, so a burst queues one judgment, not one per tick, and a
        verdict survives only if its text is still the newest when the pass ends
        (latest-wins — checked before and after). Nothing is drawn here: the settle gate in
        _work_inner is the only place a verdict reaches the panel, so a stale conclusion
        cannot be shown no matter how the timing lands.
        """
        while True:
            try:
                self._prejudge_event.wait()
                self._prejudge_event.clear()
                req = self._prejudge_req
                self._prejudge_req = None
                if req is None:
                    continue
                text, context, sender, prev, epoch = req
                if self._paused or text != self.last_seen or epoch != self._reply_epoch:
                    continue          # superseded while queued: only the newest text counts
                self._prejudging = True
                try:
                    t0 = time.perf_counter()
                    tmpl = cards.match_template(text)
                    extra_q = cards.extra_questions(tmpl) if tmpl else None
                    with self._model_lock:
                        verdict = self.judge.judge(
                            text, context=context, extra_questions=extra_q)
                    ms = (time.perf_counter() - t0) * 1000
                    first = not self._judged_once
                    self._judged_once = True
                    backend = verdict.get("backend") or "?"
                    note = "（首次）" if first else ""
                    _log(f"预判 {ms:.0f}ms → {verdict.get('intent', '?')}"
                         f" 把握 {verdict.get('confidence', 0):.0%}"
                         f" 风险 {verdict.get('risk', '?')} · {backend}{note}（待停稳上屏）")
                except Exception as e:
                    _log(f"预判失败 {type(e).__name__}: {str(e)[:60]}")
                    verdict = None
                finally:
                    self._prejudging = False
                if (verdict is not None and not self._paused and text == self.last_seen
                        and epoch == self._reply_epoch):
                    self._prejudge_result = (text, verdict, sender, prev, epoch)
                    self._remember_card(text, verdict)
            except Exception:
                pass                  # a resident worker must not die on one bad request

    @objc.python_method
    def _pregen_loop(self):
        """Generate the moment a message is seen — the paid half of the pre-judge trick.

        Same resident-worker, latest-wins shape as _prejudge_loop. Generation is a network
        call, so it holds no _model_lock and truly overlaps the local judge. A burst each
        time overwrites the slot, so one generation per arrival, not one per tick, and a
        result survives only if its text is still the newest when the call returns.
        """
        while True:
            try:
                self._pregen_event.wait()
                self._pregen_event.clear()
                req = self._pregen_req
                self._pregen_req = None
                if req is None:
                    continue
                text, context, tones, epoch = req
                if self._paused or text != self.last_seen or epoch != self._reply_epoch:
                    continue          # superseded while queued: only the newest text counts
                self._pregen_running = True
                gen = None
                try:
                    gen = self.generator.generate(text, "", list(tones), context)
                except Exception:
                    gen = None        # a failed early run just means the settle path regenerates
                # store BEFORE clearing _pregen_running, so _take_pregen never observes
                # "not running" without the result already visible
                if (gen is not None and not self._paused and text == self.last_seen
                        and epoch == self._reply_epoch):
                    self._pregen_result = (text, tones, gen, epoch)
                self._pregen_running = False
            except Exception:
                self._pregen_running = False

    @objc.python_method
    def _take_pregen(self, text: str, tones: tuple) -> tuple[dict | None, float]:
        """Collect the early generation: (gen, waited_ms). gen=None ⇒ caller generates.

        A stored result counts only when BOTH the text and the tone selection match — the
        text because a newer message retired it, the tones because a dropdown click during
        the window changed what should be generated. While a matching request is in flight
        we wait for it (it started ~1 s ago at detection, so what is left is usually a few
        hundred ms — still cheaper than a fresh call, and free of a second TLS handshake).
        """
        t0 = time.perf_counter()
        deadline = time.time() + 30   # generation's own timeout; never wait longer
        while time.time() < deadline:
            if not self._reply_current():
                return None, (time.perf_counter() - t0) * 1000
            r = self._pregen_result
            if (r is not None and r[0] == text and r[1] == tones
                    and r[3] == self._reply_epoch):
                self._pregen_result = None      # spent: each result is consumed exactly once
                return r[2], (time.perf_counter() - t0) * 1000
            if (not self._pregen_running
                    and (self._pregen_req is None or self._pregen_req[0] != text)):
                return None, (time.perf_counter() - t0) * 1000
            time.sleep(0.03)
        return None, (time.perf_counter() - t0) * 1000

    @objc.python_method
    def _gen_with_pregen(self, text: str, context: str | None,
                         on_candidate=None) -> dict:
        """Generate, preferring an early run already in flight or finished (full path).

        The hook only reaches the fresh call: an early-run hit already has all its lines,
        and _finish_generate paints them the moment the verdict lands.
        """
        tones = tuple(self.slot_tones)
        gen, _waited = self._take_pregen(text, tones)
        if not self._reply_current():
            return {"groups": []}
        if gen is None:
            gen = self.generator.generate(text, "", list(tones), context, on_candidate)
        return gen

    @objc.python_method
    def _run_generation(self, newest, msgs, verdict: dict):
        """The pre-judged path's second half: collect generation + rank, judgment shown.

        The early run usually finished inside the settle window, so what is left here is
        the wait-remainder plus ranking. Only a miss (superseded mid-burst, tone changed
        during the window, network failure) starts a fresh call — and that one streams,
        so it gets the hook. A hit never creates a hook, so no epoch is bumped and any
        in-flight 换话术 stream keeps its slot on screen.
        """
        t0 = time.perf_counter()
        try:
            if not self._reply_current():
                return
            context = self._context_text(msgs, newest)
            gen, wait_ms = self._take_pregen(newest.text, tuple(self.slot_tones))
            if not self._reply_current():
                return
            note = f"（早跑命中，停稳后仅等 {wait_ms:.0f}ms）" if gen is not None else ""
            if gen is None:
                gen = self.generator.generate(newest.text, "", list(self.slot_tones),
                                              context, self._stream_hook(t0))
            self._finish_generate(gen, newest, t0, verdict, note)
        except Exception as e:
            _log(f"生成失败 {type(e).__name__}: {str(e)[:60]}")
            self._push("applyError:", f"候选生成失败: {type(e).__name__}: {str(e)[:40]}")
        finally:
            self._analyzing = False

    @objc.python_method
    def _remember_card(self, text: str, verdict: dict) -> None:
        """Stash a finished grey card and hop to the main thread to repaint.

        Called from the pre-judge / analysis workers (any thread). The overlay
        only reads `_card_cache` on the main thread inside applyBoxes_.
        """
        if not text:
            return
        extra = verdict.get("extra") if isinstance(verdict, dict) else None
        self._card_cache[text] = cards.card_from_verdict(text, verdict, extra)
        self._card_inflight.discard(text)
        # bound the cache: overlay only paints the last MAX_CARDS incoming
        # bubbles, but the same wording in an old chat should still hit
        if len(self._card_cache) > 40:
            extra_keys = list(self._card_cache)[:-32]
            for k in extra_keys:
                self._card_cache.pop(k, None)
        if self._last_full:
            msgs = self._last_full.get("messages") or []
            self._push("applyTranscript:", msgs)
            if self._show_cards:
                newest = next((m for m in reversed(msgs) if m.side == "them"), None)
                win = self._last_full.get("window")
                if win:
                    self._push("applyBoxes:", (win, msgs, newest.text if newest else None))

    @objc.python_method
    def _enqueue_cards(self, msgs, newest) -> None:
        """Queue a Jev judgment for every visible incoming bubble.

        Newest is already covered by the main pre-judge path; older bubbles
        get their own background pass so every buyer turn in the Jev window
        can show analysis (not just the latest one).
        """
        incoming = [m for m in msgs if getattr(m, "side", "") == "them" and m.text]
        incoming = incoming[-cards.MAX_CARDS:]
        for m in incoming:
            cached = self._card_cache.get(m.text)
            if cached is not None and not getattr(cached, "pending", False):
                continue          # finished card, reuse it
            if cached is None:
                self._card_cache[m.text] = cards.pending_card(m.text)
            if newest is not None and m.text == newest.text:
                continue          # the main pre-judge / analysis path owns this one
            if m.text in self._card_inflight:
                continue
            self._card_inflight.add(m.text)
            ctx = self._context_text(msgs, m, JUDGE_TURNS)
            self._card_queue.append((m.text, ctx, self._card_epoch))
            self._card_event.set()

    @objc.python_method
    def _card_loop(self):
        """Background worker: fill older bubbles' grey cards.

        Serial on purpose — shares `_model_lock` with pre-judge/rank so the
        local model never runs two forwards at once. Cloud Jev still benefits
        from the lock as a simple in-flight cap. A conversation change bumps
        `_card_epoch` and the queued items from the old chat are dropped.
        """
        while True:
            try:
                self._card_event.wait()
                self._card_event.clear()
                while self._card_queue:
                    text, context, epoch = self._card_queue.pop(0)
                    if epoch != self._card_epoch or self._paused:
                        self._card_inflight.discard(text)
                        continue
                    if text in self._card_cache and not getattr(
                            self._card_cache[text], "pending", True):
                        self._card_inflight.discard(text)
                        continue
                    tmpl = cards.match_template(text)
                    extra_q = cards.extra_questions(tmpl) if tmpl else None
                    try:
                        t0 = time.perf_counter()
                        with self._model_lock:
                            verdict = self.judge.judge(
                                text, context=context, extra_questions=extra_q)
                        _log(f"买家分析 {(time.perf_counter() - t0) * 1000:.0f}ms"
                             f" → {verdict.get('intent', '?')}"
                             f" 风险 {verdict.get('risk', '?')}"
                             f" · {verdict.get('backend', '?')}")
                    except Exception as e:
                        _log(f"买家分析失败 {type(e).__name__}: {str(e)[:60]}")
                        self._card_inflight.discard(text)
                        continue
                    if epoch != self._card_epoch:
                        self._card_inflight.discard(text)
                        continue
                    self._remember_card(text, verdict)
            except Exception:
                pass

    @objc.python_method
    def _context_text(self, msgs, newest, turns: int = CONTEXT_TURNS) -> str | None:
        """The last few turns, each prefixed with who said it — shared by both halves.

        The names are the point. The judge used to receive a jumble of lines with no
        speaker, which in a group chat throws away the most useful clue available: who is
        talking, and whether the last thing said was mine. One-to-one chats render no name
        above the bubble, so 我/对方 stands in.

        The halves take different depths: generation needs the conversational thread
        (CONTEXT_TURNS), while the judge's prompt is paid per forward — two turns carry
        most of the signal at roughly half the added prefill (JUDGE_TURNS).

        The message under judgment is excluded **by identity**, not by position: `newest` is
        the last message from the other side, which is not the same as the last element of
        `msgs` (my own replies come after it).
        """
        prior = [m for m in msgs if m is not newest][-turns:]
        if not prior:
            return None
        return "\n".join(
            f"{m.sender or {'me': '我', 'them': '对方'}.get(m.side, '方向未确认')}: {m.text}"
            for m in prior)

    @objc.python_method
    def _analyze(self, newest, msgs, prev_text: str = ""):
        """Judge and generate in parallel, then rank. Judgment lands on screen first.

        Runs on its own thread (started by _work_inner): it takes over a second and must
        not hold the read loop hostage.
        """
        import concurrent.futures as cf

        t0 = time.perf_counter()
        context = self._context_text(msgs, newest)
        with cf.ThreadPoolExecutor(max_workers=2) as ex:
            # generation does not need the intent, so it runs while judging; it prefers an
            # early run that started at detection time (_gen_with_pregen) — only a miss
            # streams, and only that fresh call takes the hook
            gen_future = ex.submit(self._reply_task, self._reply_worker.epoch,
                                   self._gen_with_pregen, newest.text, context,
                                   self._stream_hook(t0))
            verdict = None
            t_judge = time.perf_counter()
            try:
                with self._model_lock:   # never two local forwards at once
                    tmpl = cards.match_template(newest.text)
                    extra_q = cards.extra_questions(tmpl) if tmpl else None
                    verdict = self.judge.judge(
                        newest.text,
                        context=self._context_text(msgs, newest, JUDGE_TURNS),
                        extra_questions=extra_q)
                ms = (time.perf_counter() - t_judge) * 1000
                first = not self._judged_once
                self._judged_once = True
                # the model load happens on the first call and is seconds, not milliseconds —
                # without saying so the first verdict looks like a performance regression
                note = "（首次，含本地模型加载）" if first else ""
                _log(f"判断 {ms:.0f}ms → {verdict.get('intent', '?')}"
                     f" 把握 {verdict.get('confidence', 0):.0%}"
                     f" 风险 {verdict.get('risk', '?')}{note}")
                self._push("applyJudgment:", (verdict, newest.sender, prev_text))
            except Exception as e:
                _log(f"判断失败 {type(e).__name__}: {str(e)[:60]}")
                self._push("applyError:", f"判断失败: {type(e).__name__}: {str(e)[:40]}")

            try:
                gen = gen_future.result()
            except Exception as e:
                _log(f"生成失败 {type(e).__name__}: {str(e)[:60]}")
                self._push("applyError:", f"候选生成失败: {type(e).__name__}: {str(e)[:40]}")
                return
            self._finish_generate(gen, newest, t0, verdict)

    @objc.python_method
    def _finish_generate(self, gen: dict, newest, t0: float, verdict: dict | None,
                         note: str = ""):
        """Log the generation, push candidates unranked, rank, push the ordered version.

        Shared by both analysis paths — the full one (judge ran here) and the pre-judged
        one (the verdict was computed during the settle window) — so the second half of
        the pipeline has exactly one implementation. Candidates reach the screen BEFORE
        ranking (prob reads 排序中) and are re-ordered in place when the local forward
        lands, so the ~0.5 s rank never delays first paint. On streamed endpoints the
        lines are usually already up (applyStreamLine_) and this first push just re-renders
        them; on non-streaming ones (anthropic shape) it IS the first paint. Without an
        intent (judge failed) ranking is a no-op and the early push is skipped.
        """
        if not self._reply_current():
            return
        groups = gen.get("groups") or []
        failed = [f"{g['tone']}({g['error'][:40]})" for g in groups if g.get("error")]
        _log(f"生成 {gen.get('elapsed_s', 0) * 1000:.0f}ms{note} · {len(groups)} 个话术并发"
             f" → {sum(len(g['texts']) for g in groups)} 条候选"
             + (f" · 失败: {'; '.join(failed)}" if failed else ""))
        intent = verdict["intent"] if verdict else ""
        payload = self._payload_from_gen(gen)
        if payload is None:
            err = (gen.get("error") or "空结果")[:60]
            _log(f"生成无可用候选: {err}")
            self._push("applyError:", f"候选生成失败: {err}")
            return
        if intent:
            self._push("applyCandidates:", payload)
        t_rank = time.perf_counter()
        ranked = self._rank_payload(payload, newest.text, intent) if intent else payload
        rank_ms = (time.perf_counter() - t_rank) * 1000
        if intent:
            _log(f"排序 {rank_ms:.0f}ms（本地模型，一次前向）")
        _log(f"端到端 {(time.perf_counter() - t0) * 1000:.0f}ms"
             f" · 从分析开始到候选上屏")
        self._push("applyCandidates:", ranked)

    @objc.python_method
    def _push(self, selector: str, payload=None):
        if selector in {"applyIncoming:", "applyPending:", "applyJudgment:",
                        "applyCandidates:", "applyStreamLine:", "applyWaiting:", "applyError:"}:
            epoch = getattr(self._reply_worker, "epoch", self._reply_epoch)
            self._push_reply(selector, payload, epoch)
            return
        self.performSelectorOnMainThread_withObject_waitUntilDone_(selector, payload, False)

    @objc.python_method
    def _reply_task(self, epoch, callback, *args):
        self._reply_worker.epoch = epoch
        try:
            return callback(*args)
        finally:
            del self._reply_worker.epoch

    @objc.python_method
    def _reply_current(self):
        return (self._reply_key is not None and not self._paused
                and getattr(self._reply_worker, "epoch", self._reply_epoch) == self._reply_epoch)

    @objc.python_method
    def _push_reply(self, selector, payload, epoch):
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "applyReplyUpdate:", (epoch, selector, payload), False)

    def applyReplyUpdate_(self, update):
        epoch, selector, payload = update
        if epoch != self._reply_epoch:
            return
        if selector not in {"applyWaiting:", "applyError:"} and not self._reply_current():
            return
        getattr(self, selector.replace(":", "_"))(payload)

    def applyWaiting_(self, payload):
        self._show()
        self._last_intent = ""
        self._last_risk = 0.0
        self._clear_candidates()
        self._stream_rows = {}
        for key in ("message", "sender", "intent", "confidence", "risk", "actions"):
            self._render(key, "", PALETTE["muted"])
        self.rows["cand_header"].setStringValue_("候选回复")
        hint = payload if isinstance(payload, str) and payload else "等待可确认的买家消息…"
        self._render("status", hint, PALETTE["muted"])
        self._set_analysis(hint)
        self._paint_intent_card("等待消息", hint, PALETTE["muted"], PALETTE["card"])
        # The big transcript box is what the user looks at; don't leave it on
        # the generic empty line when we already know why it's empty.
        current = self.rows["transcript"].stringValue()
        if isinstance(current, str) and "●" not in current:
            self.rows["transcript"].setAttributedStringValue_(
                format_transcript_attributed([]))

    # --- main-thread callbacks (AppKit is not thread safe)
    def applyChat_(self, title):
        self._chat_title = title
        self._render("chat", title, PALETTE["text"])

    def applyTranscript_(self, msgs):
        self._refresh_transcript(msgs or [])

    def applyIncoming_(self, payload):
        # a new message landed but we are not analysing yet (burst in progress):
        # keep the previous verdict visible, just badge it
        text, sender, prev = payload
        self._show()
        self._render("status", "有新消息 · 等消息停稳…", PALETTE["muted"])
        self._render("message", text, PALETTE["muted"])   # grey: not analysed yet
        self._render("sender", self._context_line(sender, prev), PALETTE["muted"])
        self._set_analysis("新消息 · 停稳后分析")
        self._paint_intent_card("新消息", "等气泡停稳后再判断", PALETTE["muted"], PALETTE["card"])

    def applyPending_(self, payload):
        text, sender, prev = payload
        self._show()
        self._render("status", "分析中…", PALETTE["muted"])
        self._render("message", text, PALETTE["text"])    # inked: this is the one
        self._render("sender", self._context_line(sender, prev), PALETTE["muted"])
        self._set_analysis("分析中…")
        self._paint_intent_card("分析中", "正在判断这条消息", PALETTE["green"], PALETTE["green_soft"])
        self._clear_candidates()
        self._stream_rows = {}     # a new run starts at line zero in every slot
        self.rows["cand_header"].setStringValue_("候选回复 · 等待判断…")

    def applyJudgment_(self, payload):
        v, sender, prev = payload
        self._remember_card(v.get("message") or self.analyzed_text or "", v)
        self._show()
        # kept so a 话术 change can re-rank the new candidates against the same verdict
        self._last_intent = v.get("intent", "")
        self._last_risk = v.get("risk", 0)   # and so the overlay can badge the message
        self._render("message", v["message"], PALETTE["text"])
        self._render("sender", self._context_line(sender, prev), PALETTE["muted"])
        backend = v.get("backend", "")
        if backend.startswith("jev/"):
            self._render("status", f"分析完成 · Jev · {backend.split('/', 1)[-1]}", PALETTE["muted"])
        elif backend.startswith("jev-error") or backend.startswith("heuristic"):
            detail = backend.split("(", 1)[-1].rstrip(")") if "(" in backend else "连不上"
            self._render("status", f"Jev 连不上 · 将重试 · {detail[:18]}", PALETTE["amber"])
        elif backend.startswith("local"):
            detail = backend.split("(", 1)[-1].rstrip(")") if "(" in backend else backend
            self._render("status", f"Jev 连不上 · {detail[:22]}", PALETTE["amber"])
        elif backend:
            self._render("status", f"分析完成 · {backend}", PALETTE["muted"])
        else:
            self._render("status", "分析完成", PALETTE["muted"])
        self._render("intent", v["intent"], PALETTE["text"])
        # the intent recognition rate, read off the judged intent — same muted slot
        self._render("confidence", f"意图识别率 {v['confidence']:.0%}", PALETTE["muted"])
        # Rounded, so the panel does not claim a precision it has: the judge reports a
        # mean like 4.7 out of a 10-level distribution, and "4.7/9" reads as a measurement
        # while "5/9" reads as the estimate it is. Deliberately the mean and not the most
        # likely level — measured on 8 real messages, this model's top level never exceeds
        # 0.4 and the argmax jumps 1/3/6 across near-identical criticism messages, while the
        # mean holds (派活 2.0–2.4, 批评 3.0–4.0, 闲聊 1.7).
        risk = int(round(float(v.get("risk", 0))))
        label = "安全" if risk <= 3 else ("留神" if risk <= 6 else "危险")
        color = PALETTE["green"] if risk <= 3 else (
            PALETTE["amber"] if risk <= 6 else PALETTE["red"])
        self._render("risk", f"● {label}  {risk}/9", color)
        self._render("actions", " · ".join(v.get("actions", [])), PALETTE["text"])
        self._analysis_from_verdict(v)
        self.rows["cand_header"].setStringValue_("候选回复 · 生成中…")
        # the verdict landing starts a new candidate run: without this reset, the streamed
        # line counters left over from the previous message would eat every new line
        # (applyStreamLine_ drops rows beyond PER_TONE) — only applyPending_ and
        # toneChanged_ used to reset it, and the pre-judged path goes through neither
        self._stream_rows = {}

    def applyCandidates_(self, payload):
        if not self._payload_current(payload):
            return
        self.rows["cand_header"].setStringValue_(self._cand_header(payload))
        self._render_groups(payload)

    def applyStreamLine_(self, payload):
        """One streamed candidate line, shown the moment it completes (not ranked yet).

        applyCandidates_ re-fills every row with scores when the full result lands, so the
        "#n" here is only "nth line of this tone" and the score slot reads as pending. A
        superseded run's lines are dropped by the epoch check — a tone change or a new
        message starting mid-stream must not write into the new run's rows.
        """
        epoch, slot, text = payload
        if epoch != self._gen_epoch or not self._slot_active(slot):
            return
        row = self._stream_rows.get(slot, 0)
        if row >= styles.PER_TONE:
            return                       # the prompt asks for PER_TONE lines; extras stray
        self._stream_rows[slot] = row + 1
        self.cand_texts[slot * styles.PER_TONE + row] = text
        if self._collapsed:
            return      # collapse keeps the data; _set_collapsed(False) puts it back up
        r = self._rows[slot][row]
        r["prob"].setStringValue_("生成中")
        r["text"].setStringValue_(text)
        for c in self._row_controls(slot, row):
            c.setHidden_(False)

    def applyError_(self, text):
        self._show()                       # never vanish without telling the user why
        self._render("status", text, PALETTE["red"])
        self._set_analysis(text, PALETTE["red"])
        self._paint_intent_card("读屏失败", text, PALETTE["red"], PALETTE["red_soft"],
                                pill="失败")

    def applyHidden_(self, reason):
        # WeChat gone or unreadable -> take the panel away (the app "opens with WeChat")
        self._render("status", reason, PALETTE["muted"])
        if self.panel.isVisible():
            self.panel.orderOut_(None)
        if self._ov_panel.isVisible():
            self._ov_panel.orderOut_(None)

    def applyVisible_(self, _payload):
        """WeChat is back on screen — bring the HUD with it."""
        self._show()

    def applyPosition_(self, win):
        self._position_near(win)
        if not self.panel.isVisible():
            self._show()

    @objc.python_method
    def _schedule_input_locate(self, win: dict) -> None:
        """Refresh the Fill target without blocking the OCR / transcript loop.

        WeChat 4.x's AX tree can stall for many seconds inside
        AXUIElementCopyAttributeValue. Doing that on the read worker used to
        freeze `_busy` and leave the panel on「等待微信消息」with no 读屏 log.
        """
        if self._app == "dingtalk":
            self._input_target = {
                "box": None, "rect": None, "window": win,
                "reason": "钉钉只读，不定位输入框",
            }
            self._input_window = dict(win)
            return
        now = time.monotonic()
        if self._input_locating:
            return
        if (win == getattr(self, "_input_window", None)
                and now < getattr(self, "_input_next", 0)):
            return
        self._input_locating = True
        threading.Thread(target=self._locate_input_bg, args=(dict(win),),
                         daemon=True).start()

    @objc.python_method
    def _locate_input_bg(self, win: dict) -> None:
        try:
            target = fill.locate_input(win)
            if target["box"] is None:
                from input_region import locate_visual_input
                target["visual_rect"] = locate_visual_input(win)
                if target["visual_rect"]:
                    from visual_fill import chat_signature
                    target["chat_signature"] = chat_signature(win, target["visual_rect"])
            self._input_target = target
            self._input_window = dict(win)
            self._input_next = time.monotonic() + 2.0
        except Exception as e:
            _log(f"定位输入框失败 {type(e).__name__}: {str(e)[:60]}")
        finally:
            self._input_locating = False

    # --- YOLO overlay callbacks (visual only; see _build_overlay)
    def applyBoxes_(self, payload):
        """Repaint the overlay from the last read's window geometry + messages."""
        if not self._show_boxes and not self._show_cards:
            return
        win, msgs, newest_text = payload
        W, H = win["w"], win["h"]
        flip = self._display_height()
        # top-left (Quartz) -> bottom-left (Cocoa), covering WeChat exactly
        self._ov_panel.setFrame_display_(
            NSMakeRect(win["x"], flip - win["y"] - H, W, H), False)
        font = (NSFont.fontWithName_size_("Menlo-Bold", 10)
                or NSFont.boldSystemFontOfSize_(10))
        judged = (newest_text is not None and newest_text == self.analyzed_text
                  and bool(self._last_intent))
        risk = int(round(float(self._last_risk)))
        boxes = []
        if self._show_boxes:
            for m in msgs:
                if m.w <= 0:
                    continue               # pre-overlay geometry: nothing to draw
                who = m.sender or {"them": "对方", "me": "我"}.get(m.side, "方向未确认")
                label = f"{who} {m.conf:.2f}"
                if judged and m.side == "them" and m.text == newest_text:
                    color = (PALETTE["green"] if risk <= 3 else
                             PALETTE["amber"] if risk <= 6 else PALETTE["red"])
                    lw = 2.5
                    label += f" · {self._last_intent} 风险{risk}/9"
                else:
                    color = _rgb(0x576B95) if m.side == "me" else PALETTE["green"]
                    lw = 1.5
                chip = NSAttributedString.alloc().initWithString_attributes_(
                    label,
                    {NSFontAttributeName: font,
                     NSForegroundColorAttributeName: NSColor.whiteColor(),
                     NSBackgroundColorAttributeName: color.colorWithAlphaComponent_(0.85)})
                y = H - (m.y + m.h) * H     # normalized top-origin -> view bottom-origin
                boxes.append((NSMakeRect(m.x * W, y, m.w * W, m.h * H), color, lw, chip))
        if self._show_boxes:
            target = getattr(self, "_input_target", None)
            if target and target["window"] == win and target["rect"]:
                x, y, w, h = target["rect"]
                color = _rgb(0x2478DD)
                rect = NSMakeRect(x-win["x"], H-(y-win["y"])-h, w, h)
                label = target["reason"]
            elif target and target["window"] == win and target.get("visual_rect"):
                x, y, w, h = target["visual_rect"]
                color = PALETTE["amber"]
                rect = NSMakeRect(x-win["x"], H-(y-win["y"])-h, w, h)
                label = "虚线：视觉输入区 · 点击填入后校验（不发送）"
            else:
                color = PALETTE["amber"]
                rect = NSMakeRect(12, 12, 0, 0)
                label = "输入框：" + (target["reason"] if target else "定位中…")
            chip = NSAttributedString.alloc().initWithString_attributes_(label, {
                NSFontAttributeName: font, NSForegroundColorAttributeName: NSColor.whiteColor(),
                NSBackgroundColorAttributeName: color.colorWithAlphaComponent_(0.85)})
            boxes.append((rect, color, 2.0, chip, bool(target and target.get("visual_rect") and not target["rect"])))
        view = self._ov_panel.contentView()
        view.boxes = boxes if self._show_boxes else []
        if self._show_cards:
            lay = LAYOUTS.get(self._app, LAYOUTS["wechat"])
            view.cards = cards.layout_cards(
                msgs, self._card_cache, W, H, lay["chat_x"], lay["input_y"])
        else:
            view.cards = []
        view.setNeedsDisplay_(True)
        if not self._ov_panel.isVisible():
            self._ov_panel.orderFrontRegardless()

    def toggleApp_(self, sender):
        """Menu-bar switch between WeChat (fill enabled) and read-only DingTalk.

        Drops the sticky window and the last frame so the next tick cannot reuse
        a WeChat fingerprint against a DingTalk capture.
        """
        self._app = set_active_app("wechat" if self._app == "dingtalk" else "dingtalk")
        self.app_item.setState_(
            AppKit.NSOnState if self._app == "dingtalk" else AppKit.NSOffState)
        self._win_wid = None
        self._fingerprint = None
        self._last_full = None
        self._input_target = None
        self._input_window = None
        label = "钉钉（只读）" if self._app == "dingtalk" else "微信"
        self._render("status", f"已切换到{label}", PALETTE["muted"])

    def toggleBoxes_(self, sender):
        """Menu-bar switch; JEV_BOXES=1 in the env file makes it start on instead."""
        self._show_boxes = not self._show_boxes
        self.boxes_item.setState_(
            AppKit.NSOnState if self._show_boxes else AppKit.NSOffState)
        if not self._show_boxes and not self._show_cards and self._ov_panel.isVisible():
            self._ov_panel.orderOut_(None)

    def toggleCards_(self, sender):
        """Menu-bar switch; overlay cards start off. JEV_CARDS=1 turns them on."""
        self._show_cards = not self._show_cards
        self.cards_item.setState_(
            AppKit.NSOnState if self._show_cards else AppKit.NSOffState)
        if not self._show_cards and not self._show_boxes and self._ov_panel.isVisible():
            self._ov_panel.orderOut_(None)
        elif self._show_cards and self._last_full:
            msgs = self._last_full.get("messages") or []
            newest = next((m for m in reversed(msgs) if m.side == "them"), None)
            self._enqueue_cards(msgs, newest)
            win = self._last_full.get("window")
            if win:
                self._push("applyBoxes:", (win, msgs, newest.text if newest else None))

    # --------------------------------------------------------------- warm-up
    @objc.python_method
    def _warm(self):
        """Pay the one-off loads in the background: Vision OCR first, then the judge model.

        The first real message used to carry both costs: Vision's ~0.7 s first OCR and
        decider-2b's 9-15 s load inside its first judge(). Starting both here, right after
        launch, moves them to idle time — the fast one first so it is ready within a
        second, the slow one after. If a message does land mid-warm-up nothing breaks:
        its judge() blocks on the model's load lock until the warm-up finishes, and the
        OCR warm-up is independent of WeChat entirely (a blank canvas, not a window).
        """
        t0 = time.perf_counter()
        ocr_ms = warm_ocr()
        if ocr_ms >= 0:
            self._read_once = True    # Vision's one-off load is paid; first read is steady-state
            _log(f"预热 OCR 就绪 · {ocr_ms:.0f}ms")
        else:
            _log("预热 OCR 失败 · 首次读屏会稍慢，不影响使用")

        try:
            self.judge.warm()
        except Exception as e:
            _log(f"预热判断模型失败 {type(e).__name__}: {str(e)[:60]}")
        else:
            self._judged_once = True  # same: the load is paid, the first judge is steady-state
            _log(f"预热 判断模型就绪 · 总耗时 {(time.perf_counter() - t0) * 1000:.0f}ms")


def warn_if_no_generation_key() -> None:
    """Say it out loud at launch when the candidate half has no key behind it.

    The judgment half runs locally and needs nothing, so a panel with an empty candidate
    area reads as "the app is broken" rather than "I never configured this". One dialog at
    launch is the cheapest way to tell the two apart — it cannot be missed the way a line
    of grey text in a floating panel can.

    OPENAI_* and ANTHROPIC_* are two ways to configure the same generation layer, so this
    fires only when NEITHER is set: either one on its own is a complete configuration.
    A packaged build also carries a shared default (src/builtin.py), so this dialog only
    appears when that default was deliberately emptied out. TypeSafe is not checked — it
    has a local fallback, so it is never missing, only different.

    Drawn with osascript rather than NSAlert, which was measured to not work here: an
    accessory app cannot activate itself (NSApp.isActive stays False after
    activateIgnoringOtherApps_), and an NSAlert stayed isVisible=False even inside its own
    modal session — so the user would get nothing to click while the app sat in a modal
    loop, i.e. an app that looks hung. osascript's dialog belongs to a process that can
    activate, and Popen does not wait, so a dialog nobody dismisses cannot stall us.
    """
    if load_credentials()[1]:
        return
    path = str(userconfig.ENV_FILE).replace(str(Path.home()), "~")
    # AppleScript string escapes (\n) work inside the literal; keep it free of double quotes
    script = (
        'display alert "生成层还没配 Key，候选回复会是空的" message "'
        "意图和风险判断不受影响 —— 那部分跑在本地模型上，不需要 Key。\\n\\n"
        f"在下面的文件里填这两组中的任意一组（二选一即可），然后重启本应用：\\n{path}\\n\\n"
        "    OPENAI_API_KEY      （任意 OpenAI 兼容端点，如 DeepSeek）\\n"
        '    ANTHROPIC_API_KEY   （任意 Anthropic 兼容端点，如智谱）" as informational'
    )
    try:
        subprocess.Popen(["osascript", "-e", script],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass          # no osascript: the panel still shows the hint in the candidate area


def main() -> None:
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    warn_if_no_generation_key()
    controller = HudController.alloc().init()
    # First line of every run: which backends are actually in play. Support requests
    # always need it, and it proves the log is live before the first message arrives.
    _base, _key, _model, _src, _api = load_credentials()
    _log(f"启动 · 判断层 "
         f"{'TypeSafe Jev' if userconfig.get('TYPESAFE_API_KEY') else '本地 decider-2b'}"
         f" · 生成层 {(_base + ' / ' + _model) if _key else '未配置（候选区会是空的）'}"
         + ("（内置默认）" if _src == BUILTIN_SOURCE else "")
          + (" · YOLO 框开" if controller._show_boxes else "")
          + (" · 气泡卡开" if controller._show_cards else "")
          + (" · 钉钉只读" if controller._app == "dingtalk" else " · 微信"))
    controller._show()
    # Warm the heavy one-off loads (Vision OCR, judge model) while the panel is idle, so
    # the user's first message pays only steady-state costs. With TypeSafe Jev configured
    # warm() is a no-op — the network path has nothing to load.
    threading.Thread(target=controller._warm, daemon=True).start()
    timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        FAST_TICK, controller, "tick:", None, True)
    AppKit.NSRunLoop.currentRunLoop().addTimer_forMode_(timer, AppKit.NSDefaultRunLoopMode)
    app.run()


if __name__ == "__main__":
    main()
