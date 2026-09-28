"""Judge: local Jev-shaped model reads a Chinese message and returns intent + risk.

One forward pass answers both slots (decider's documented multi-question layout:
append further `Question k: ... Answer k: (` blocks and read logits at each slot).

Measured on 22 real Chinese workplace messages, zero-shot: 86% intent accuracy
against a 13.6% majority baseline.
"""

from __future__ import annotations

import os
import threading

import numpy as np

# 描述保持这个长度是有实测依据的，别为了省 prefill 时间去瘦身：两轮压缩措辞
# （保语义锚点、每条砍 ~1/3 字符）在 22 条回归上分别是 81.8% 和 77.3%，都低于
# 原文的 86.4%——批评/要解释 的边界对措辞极敏感。省下的 ~100 ms 判断又藏在
# 停稳窗口里基本不可见，不划算（2026-09 实测，judge_zh_test.py 已改为直接
# import 这份 INTENTS，改这里必须重跑回归）。
INTENTS = {
    "派活": "对方要我做一件事或接一个任务",
    "催进度": "对方在催促我尽快完成某个已在办的事",
    "问进度": "对方在询问某件事的进展或状态",
    "批评": "对方对我的工作或结果表达不满、指出错误",
    "要解释": "对方要求我说明原因或给出解释",
    "闲聊": "对方只是在聊天、分享或表达感受，没有具体要求",
    "约会议": "对方想安排一次会议或通话",
    "夸奖": "对方在肯定、称赞我的成果",
}

RISK_LEVELS = [
    "完全没风险，怎么回都行",
    "基本没风险",
    "平淡，正常回就好",
    "需要稍微留神",
    "有点敏感，措辞注意",
    "需要谨慎，可能被挑刺",
    "比较危险，容易得罪人或踩坑",
    "很危险，说错要出问题",
    "非常危险，涉及责任或利益",
    "极度危险，先别回，想清楚再说",
]

# huggingface_hub reads HF_ENDPOINT at import. Mainland networks cannot reach
# huggingface.co (this HUD hung on「分析中」retrying it). The community mirror
# serves the same repo paths. An explicit HF_ENDPOINT still wins.
HF_MIRROR = "https://hf-mirror.com"
_download_lock = threading.Lock()
_download_started = False


def ensure_hf_mirror() -> str:
    """Pin downloads at hf-mirror.com unless the user already set HF_ENDPOINT."""
    current = (os.environ.get("HF_ENDPOINT") or "").strip()
    if not current:
        os.environ["HF_ENDPOINT"] = HF_MIRROR
        current = HF_MIRROR
    # huggingface_hub 1.32 still calls hf_xet when the package is installed;
    # that hits cas-bridge.xethub.hf.co and bypasses HF_ENDPOINT. Force HTTP
    # both in the environment (must land before hub import) and on the already
    # imported modules (warm() imports transformers first).
    os.environ["HF_HUB_DISABLE_XET"] = "1"
    try:
        from huggingface_hub import constants
        from huggingface_hub.utils import _runtime
        import huggingface_hub.file_download as file_download
        constants.HF_HUB_DISABLE_XET = True
        constants.ENDPOINT = current.rstrip("/")
        constants.HUGGINGFACE_CO_URL_TEMPLATE = (
            constants.ENDPOINT + "/{repo_id}/resolve/{revision}/{filename}")
        _runtime.is_xet_available = lambda: False
        file_download.is_xet_available = lambda: False
    except Exception:
        pass
    return current


ensure_hf_mirror()


# V0: actions are a static derivation, no generation involved
ACTION_MAP = {
    "派活": ["接住", "问清交付标准和期限", "先给个时间点"],
    "催进度": ["先给当前状态", "给明确的完成时间", "别解释太多"],
    "问进度": ["直接说事实", "给下个节点", "有卡点就说卡点"],
    "批评": ["先认下来", "别急着辩解", "给补救方案"],
    "要解释": ["说清原因", "别找借口", "给改进措施"],
    "闲聊": ["轻松回应", "可以互动", "不用当真"],
    "约会议": ["确认时间", "说清议程", "准备好材料"],
    "夸奖": ["接住并感谢", "别过度谦虚", "可以顺带提下一步"],
}


class Judge:
    """Wraps a decoder-only decision model; lazy-loads on first use."""

    def __init__(self, repo: str = "Mapika/decider-2b", device: str | None = None):
        import torch

        self.torch = torch
        if device is None:
            device = "mps" if torch.backends.mps.is_available() else "cpu"
        self.device = device
        self.repo = repo
        self.temperature = 1.3
        self._loaded = False
        # RLock, not Lock: warm() holds it across the whole dummy forward, and judge()
        # inside that same call re-enters _load(). One lock guards both the load and the
        # first forward, so a warm-up and a real judgment can never run a forward at the
        # same time — they queue up instead.
        self._load_lock = threading.RLock()

    def _load(self):
        if self._loaded:
            return
        # Double-checked: the warm-up thread and the first real message can both get here
        # at once, and two concurrent from_pretrained calls would load the model twice.
        # The loser of the race just waits on the lock until the winner is done.
        with self._load_lock:
            if self._loaded:
                return
            t = self.torch
            ensure_hf_mirror()
            from transformers import AutoModelForCausalLM, AutoTokenizer
            # Cache hit: load offline so we never pay a huggingface.co revision check.
            # Cache miss: do not download on this thread — that used to pin the HUD
            # on「分析中」for minutes. Kick a background pull from hf-mirror.com and
            # let FallbackJudge use the heuristic until the weights land.
            try:
                self.tok = AutoTokenizer.from_pretrained(self.repo, local_files_only=True)
            except Exception:
                self._start_mirror_download()
                raise RuntimeError("local model missing; downloading from hf-mirror.com")
            # float16, not bfloat16: MPS takes the slow path for bf16 (limited op coverage) and
            # it costs exactly 2x here — measured on this model, same prompt, three runs each:
            # bf16 1352/1393/1467 ms vs fp16 734/745/827 ms. The judge is the single biggest
            # steady-state cost in the pipeline, so this is the difference between a ~3 s and a
            # ~4 s reply. CPU has no fp16 win, so it stays fp32.
            dtype = t.float16 if self.device == "mps" else t.float32
            try:
                self.model = AutoModelForCausalLM.from_pretrained(
                    self.repo, dtype=dtype, local_files_only=True).to(self.device).eval()
            except Exception:
                self._start_mirror_download()
                raise RuntimeError("local model missing; downloading from hf-mirror.com")
            self._letters = [self.tok.encode(c, add_special_tokens=False)[0]
                             for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"]
            self._loaded = True

    def _start_mirror_download(self) -> None:
        """One background from_pretrained against hf-mirror.com; never blocks judge()."""
        global _download_started
        with _download_lock:
            if _download_started:
                return
            _download_started = True
        repo = self.repo

        def _pull():
            try:
                ensure_hf_mirror()
                from transformers import AutoModelForCausalLM, AutoTokenizer
                AutoTokenizer.from_pretrained(repo)
                AutoModelForCausalLM.from_pretrained(repo)
            except Exception:
                global _download_started
                with _download_lock:
                    _download_started = False

        threading.Thread(target=_pull, daemon=True).start()

    def warm(self) -> None:
        """Load the model and run one real-shaped forward, so no real message pays for it.

        decider-2b's first load costs 9-15 s and lands inside whichever judge() call gets
        there first — the HUD starts this in the background right after launch, so that
        call is ours, not the user's first message. The whole thing runs under the load
        lock: if a real message arrives mid-warm-up, its judge() blocks here until the
        warm-up is done, then runs at steady state.
        """
        with self._load_lock:
            self._load()
            self.judge("预热")

    def _slot_probs(self, logits_by_slot: list, n_options: int, slot: int) -> np.ndarray:
        logits = logits_by_slot[slot]
        ids = self._letters[:n_options]
        probs = self.torch.softmax(logits[ids].float() / self.temperature, -1)
        return probs.cpu().numpy()

    def _forward(self, prompt: str, n_slots: int):
        """One forward pass; returns (logits, [token index per 'Answer: (' slot]).

        logits[i] is the distribution for position i+1, so reading at the token that
        contains "(" gives the letter distribution for that slot.
        """
        import re

        ids = self.tok(prompt, return_tensors="pt", return_offsets_mapping=True).to(self.device)
        offsets = ids.pop("offset_mapping")[0].tolist()
        with self.torch.no_grad():
            out = self.model(**ids)
        slot_token_idx = []
        for m in re.finditer(r"Answer: \(", prompt):
            char_pos = m.start() + len("Answer: ")
            for i, (s, e) in enumerate(offsets):
                if s <= char_pos < e:
                    slot_token_idx.append(i)
                    break
        if len(slot_token_idx) < n_slots:
            raise RuntimeError(f"expected {n_slots} answer slots, found {len(slot_token_idx)}")
        return out.logits[0], slot_token_idx

    def rank_candidates(self, message: str, intent: str,
                        candidates: list[str]) -> list[dict]:
        """Rank reply candidates by asking which one fits best.

        The candidates are the options, so one forward pass yields the distribution the
        phone demo shows as 89% / 9% / 2%.
        """
        self._load()
        letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        prompt = f"Context:\n收到：「{message}」\n判断出的意图：{intent}\n\n"
        prompt += "Question: 哪一条回复最合适？\nOptions:\n"
        for i, c in enumerate(candidates):
            prompt += f"({letters[i]}) {c}\n"
        prompt += "Answer: ("

        logits, slots = self._forward(prompt, 1)
        probs = self._slot_probs([logits[slots[0]]], len(candidates), 0)
        ranked = sorted(
            ({"text": c, "prob": float(p)} for c, p in zip(candidates, probs)),
            key=lambda r: -r["prob"])
        return ranked

    def judge(self, message: str, context: str | None = None,
              extra_questions: dict | None = None) -> dict:
        # extra_questions is accepted so the overlay card path can call either
        # backend with the same signature; the local model cannot answer
        # arbitrary noul/choice items, so they are ignored here.
        del extra_questions
        self._load()
        intents = list(INTENTS)
        letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

        prompt = f"Context:\n{context + chr(10) + chr(10) if context else ''}{message}\n\n"
        # slot 0: intent
        prompt += "Question: 这句话的真实意图是什么？\nOptions:\n"
        for i, name in enumerate(intents):
            prompt += f"({letters[i]}) {name} - {INTENTS[name]}\n"
        prompt += "Answer: ("
        # slot 1: risk
        prompt += "\n\nQuestion: 如果直接回复这句话，风险有多大？\nOptions:\n"
        for i, lv in enumerate(RISK_LEVELS):
            prompt += f"({letters[i]}) {lv}\n"
        prompt += "Answer: ("

        logits, slot_token_idx = self._forward(prompt, 2)

        intent_probs = self._slot_probs([logits[slot_token_idx[0]]], len(intents), 0)
        risk_probs = self._slot_probs([logits[slot_token_idx[1]]], len(RISK_LEVELS), 0)

        intent_idx = int(np.argmax(intent_probs))
        risk_value = float((np.arange(len(RISK_LEVELS)) * risk_probs).sum())

        return {
            "intent": intents[intent_idx],
            "confidence": float(intent_probs[intent_idx]),
            "intent_probs": {n: float(p) for n, p in zip(intents, intent_probs)},
            "risk": round(risk_value, 1),
            "risk_probs": {str(i): float(p) for i, p in enumerate(risk_probs)},
            "actions": ACTION_MAP.get(intents[intent_idx], []),
            "message": message,
            "backend": "local/decider-2b",
        }


if __name__ == "__main__":
    import json
    import sys

    j = Judge()
    msg = sys.argv[1] if len(sys.argv) > 1 else "这个需求你今天跟一下"
    print(json.dumps(j.judge(msg), ensure_ascii=False, indent=1))


def heuristic_verdict(message: str, reason: str = "") -> dict:
    """Last-resort intent so the HUD can leave「分析中」when both Jev and local fail.

    Keyword matching is coarse; the generation half still writes the replies.
    """
    text = message or ""
    intent = "闲聊"
    for name, needles in (
        ("催进度", ("赶紧", "尽快", "怎么还", "催一下", "还没好")),
        ("问进度", ("怎么样了", "进展", "进度", "弄好了吗")),
        ("派活", ("你去", "帮我", "跟一下", "处理一下", "看一下这个")),
        ("批评", ("怎么搞的", "又错", "不行", "离谱")),
        ("要解释", ("为什么", "怎么回事", "解释一下")),
        ("约会议", ("开会", "会议", "约个时间", "通话")),
        ("夸奖", ("辛苦了", "不错", "厉害", "感谢")),
    ):
        if any(n in text for n in needles):
            intent = name
            break
    tag = reason.strip() or "Jev 不可用"
    return {
        "intent": intent,
        "confidence": 0.2,
        "intent_probs": {intent: 0.2},
        "risk": 2.0,
        "risk_probs": {"2": 1.0},
        "actions": ACTION_MAP.get(intent, []),
        "message": message,
        "backend": f"heuristic ({tag[:40]})",
    }


class FallbackJudge:
    """Always try TypeSafe Jev first. Local / heuristic only cover this one call.

    The previous version switched permanently to the local model after the first
    Jev failure, so a single TLS timeout made the HUD read「本地兜底」forever.
    Ranking still uses the local model: it is on-device, fast, and does not
    need another Jev round-trip.
    """

    def __init__(self):
        import judge_jev
        self.primary = judge_jev.JevJudge()
        self.local = None
        self.reason = ""

    def _fallback(self):
        if self.local is None:
            self.local = Judge()
        return self.local

    def judge(self, message: str, context: str | None = None,
              extra_questions: dict | None = None) -> dict:
        try:
            return self.primary.judge(message, context,
                                      extra_questions=extra_questions)
        except Exception as e:
            self.reason = f"{type(e).__name__}: {str(e)[:80]}"
            try:
                print(f"[judge] Jev 判断失败，将重试 · {self.reason}", flush=True)
            except Exception:
                pass
            # Do not switch to the local model: the HUD is a Jev product, and a
            # one-off TLS timeout must not become「本地兜底」. Keyword guess keeps
            # generation moving; the next message tries Jev again.
            out = heuristic_verdict(message, self.reason)
            out["backend"] = f"jev-error ({self.reason})"
            return out

    def rank_candidates(self, message: str, intent: str, candidates: list[str]) -> list[dict]:
        try:
            return self._fallback().rank_candidates(message, intent, candidates)
        except Exception:
            n = max(len(candidates), 1)
            return [{"text": c, "prob": 1.0 / n} for c in candidates]

    def warm(self) -> None:
        # Jev has nothing to load. Local ranking still needs the weights, so
        # kick the hf-mirror pull at launch; judgment itself stays on Jev.
        try:
            self._fallback()._load()
        except Exception:
            pass


def make_judge():
    """Jev when a key is configured, otherwise the local decider-2b."""
    try:
        import judge_jev
        if judge_jev.jev_configured():
            return FallbackJudge()
    except Exception:
        pass
    return Judge()
