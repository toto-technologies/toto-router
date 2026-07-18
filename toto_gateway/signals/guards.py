"""RuleGuard — deterministic regex/keyword safety guardrail for Phase 1 routing brain.

SAFETY-CRITICAL: this is guardrail #1. It MUST fail closed — any error or ambiguity
defaults to DOWNGRADE_LOCAL, never ALLOW. Never catch-all silently.

Policy (Gary fold G2):
  - DOWNGRADE_LOCAL = answer on the box, don't refuse. Over-blocking degrades to
    "ran local", which is still a useful answer and never leaks data to frontier.
  - BLOCK = reserved for EGRESS of MNPI — the request combines a strong MNPI phrase
    WITH an outbound-send verb (send/forward/share/route/email/post/upload/transmit).
    "redact MNPI from this memo" → DOWNGRADE_LOCAL (local handling, legit use case).
    "forward this material non-public information to the model" → BLOCK (egress).

Alex will seed a house MNPI lexicon later — see MNPI_TERMS below.
"""

from __future__ import annotations

import re

from ..pipeline import ALLOW, BLOCK, DOWNGRADE_LOCAL, GuardVerdict, Signal
from ..schemas import ChatCompletionRequest

# ---------------------------------------------------------------------------
# MNPI lexicon — extend this list with house deal codenames, fund names, and
# any proprietary pre-release terms. Alex: add your terms here.
# Format: plain substrings (case-insensitive match via .lower()).
# ---------------------------------------------------------------------------
MNPI_TERMS: list[str] = [
    # Regulatory / legal
    "material non-public",
    "mnpi",
    "insider information",
    "insider trading",
    "non-public information",
    # M&A vocabulary
    "acquisition target",
    "merger agreement",
    "lbo",
    "leveraged buyout",
    "going private",
    "take private",
    "definitive agreement",
    "loi",
    "letter of intent",
    "due diligence",
    "term sheet",
    # Earnings / guidance
    "earnings guidance",
    "earnings pre-release",
    "pre-announcement",
    "revenue guidance",
    "not yet announced",
    "not yet public",
    # Confidentiality markers
    "confidential",
    "embargoed",
    "under nda",
    # Fund / deal identity patterns (add house codenames here)
    # "project falcon", "project titan",  # example codenames
]

# Strong MNPI phrases — match signals a BLOCK candidate (pending egress check below).
# These alone are NOT sufficient to block; block requires egress intent too.
# Exception: "insider trading" always blocks (it names the offense, not just the data).
_BLOCK_PHRASES: list[str] = [
    "material non-public information",
    "mnpi",
    "insider information",
    "non-public information",
]

# Always-block phrases: naming the offense itself, regardless of egress verb.
_ALWAYS_BLOCK_PHRASES: list[str] = [
    "insider trading",
]

# Egress verbs: the request wants to SEND the sensitive data out.
# BLOCK only when a _BLOCK_PHRASE AND one of these appear together.
_EGRESS_VERBS: list[str] = [
    "send to",
    "forward to",
    "forward this",
    "share with",
    "share to",
    "route to",
    "email to",
    "post to",
    "upload to",
    "transmit",
    "submit to",
    "pass to",
    "pass along",
    "send it to",
    "send this to",
    "to the model",           # "send this MNPI to the model"
    "to the frontier",        # "forward to the frontier"
    "to the api",
]

# PII patterns — SSN, credit-card-shaped digit runs, email+name combos, account numbers.
# These warrant DOWNGRADE_LOCAL (answer locally, never send to frontier).
_SSN_RE = re.compile(r"\b\d{3}[-\s]\d{2}[-\s]\d{4}\b")
_CC_RE = re.compile(r"\b(?:\d[ -]?){13,16}\b")   # Luhn-shaped: 13-16 digit runs
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_ACCOUNT_RE = re.compile(r"\baccount\s*(?:number|#|no\.?)\s*[:\-]?\s*\d{4,}\b", re.IGNORECASE)

# Jailbreak / prompt-injection markers.
# Policy: DOWNGRADE_LOCAL — the local model can handle/refuse it safely; we don't
# want to send injection attempts to frontier where they might succeed.
_JAILBREAK_PHRASES: list[str] = [
    "ignore previous instructions",
    "ignore your previous instructions",
    "disregard your system prompt",
    "disregard the system prompt",
    "forget your instructions",
    "you are now",
    "act as if you have no restrictions",
    "bypass your restrictions",
    "ignore all prior",
    "ignore all previous",
    "pretend you are",
    "jailbreak",
    "dan mode",
    "developer mode",
]

def _full_text(req: ChatCompletionRequest) -> str:
    """Flatten all messages to a single string for rule matching."""
    return " ".join(m.text() for m in req.messages)


class RuleGuard:
    """Deterministic rule-based safety guard.

    Evaluates the full request (all messages) against regex and keyword rules.
    Returns the most restrictive action found, with human-readable reasons for
    logging to the trace.

    Fail-closed: any exception inside a rule → DOWNGRADE_LOCAL.
    """

    def check(self, req: ChatCompletionRequest, signal: Signal) -> GuardVerdict:
        try:
            return self._check_inner(req)
        except Exception as exc:
            # Fail closed: never let a guard error leak to ALLOW.
            return GuardVerdict(
                action=DOWNGRADE_LOCAL,
                reasons=[f"guard_error: {type(exc).__name__}: {exc}"],
            )

    def _check_inner(self, req: ChatCompletionRequest) -> GuardVerdict:
        # Guard against empty / malformed input — still valid to process, but
        # if we can't extract text at all, fail closed.
        try:
            text = _full_text(req)
        except Exception as exc:
            return GuardVerdict(
                action=DOWNGRADE_LOCAL,
                reasons=[f"text_extraction_error: {exc}"],
            )

        if not text.strip():
            # Empty request — nothing to evaluate; default downgrade (no free pass).
            return GuardVerdict(
                action=DOWNGRADE_LOCAL,
                reasons=["empty_request: no message content"],
            )

        low = text.lower()
        reasons: list[str] = []
        worst_action = ALLOW

        # --- MNPI check -------------------------------------------------------
        # Always-block phrases name the offense itself (e.g. "insider trading").
        always_block_hits = [p for p in _ALWAYS_BLOCK_PHRASES if p in low]
        if always_block_hits:
            return GuardVerdict(
                action=BLOCK,
                reasons=[f"mnpi_always_block: matched {always_block_hits!r}"],
            )

        # Strong MNPI phrases: block whenever combined with egress intent.
        # A local-handling verb (redact/classify/analyze/...) does NOT rescue an
        # egress request — "summarize and email to X" still exfiltrates. Only the
        # no-egress case downgrades, where the analyst processes their doc on-box.
        block_phrase_hits = [p for p in _BLOCK_PHRASES if p in low]
        if block_phrase_hits:
            has_egress = any(v in low for v in _EGRESS_VERBS)
            if has_egress:
                return GuardVerdict(
                    action=BLOCK,
                    reasons=[f"mnpi_egress: phrases={block_phrase_hits!r}"],
                )
            # No egress: downgrade only.
            worst_action = DOWNGRADE_LOCAL
            reasons.append(f"mnpi_strong_terms: matched {block_phrase_hits!r}")

        # Softer MNPI terms → DOWNGRADE_LOCAL.
        mnpi_hits = [t for t in MNPI_TERMS if t in low and t not in block_phrase_hits]
        if mnpi_hits:
            worst_action = DOWNGRADE_LOCAL
            reasons.append(f"mnpi_terms: matched {mnpi_hits!r}")

        # --- PII check --------------------------------------------------------
        pii_reasons: list[str] = []

        if _SSN_RE.search(text):
            pii_reasons.append("ssn_pattern")
        if _CC_RE.search(text):
            pii_reasons.append("credit_card_pattern")
        if _ACCOUNT_RE.search(text):
            pii_reasons.append("account_number_pattern")
        # Email alone is weak — only flag when combined with other PII signals.
        email_hits = _EMAIL_RE.findall(text)
        if email_hits and pii_reasons:
            pii_reasons.append(f"email+pii_combo: {email_hits[:2]!r}")

        if pii_reasons:
            worst_action = DOWNGRADE_LOCAL
            reasons.append(f"pii: {pii_reasons}")

        # --- Jailbreak check --------------------------------------------------
        jb_hits = [p for p in _JAILBREAK_PHRASES if p in low]
        if jb_hits:
            # Policy: downgrade, not block — local model can handle/refuse safely.
            worst_action = DOWNGRADE_LOCAL
            reasons.append(f"jailbreak: matched {jb_hits[:3]!r}")

        return GuardVerdict(action=worst_action, reasons=reasons)
