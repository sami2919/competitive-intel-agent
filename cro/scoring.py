"""Deterministic variant rubric. The LLM writes copy; this decides if it may ship.

Same shape as ledger/confidence.py: fixed weights, a hard gate, and a trace that
records WHY — never a model judging its own output.

Two GATES (categorical rejection, not score penalties):
  multivariate      -> changed more than one element. Not a test, a redesign; its
                       result is unreadable, so there is no score worth computing.
  unsourced_claim   -> a comparative assertion with no ledger/first-party backing.
                       Computed in cro/compliance.py and passed in here.

Five WEIGHTED checks (sum 100):
  message_match 25 · specificity 25 · length 20 · readability 15 · segment_fit 15

Specificity is a POSITIVE test — it counts concrete referents (numerals, named
products, integrations, jurisdictions), not banned phrases. A blocklist is
permanently incomplete and dodged by synonym substitution: ban "seamless" and you
get "frictionless". Worse, "Better payroll for growing teams" contains no banned
phrase and asserts nothing. Requiring concrete referents cannot be gamed without
actually adding specifics, which is the behaviour we want.

Known LLM tics are reported in the trace as a NOTE and carry no score weight —
useful signal, not a measurement.
"""

from __future__ import annotations

import re

from cro.models import CRO_SCORE_GATE, Hypothesis, ScoredVariant, Variant

W_MESSAGE_MATCH = 25
W_SPECIFICITY = 25
W_LENGTH = 20
W_READABILITY = 15
W_SEGMENT_FIT = 15

# Layout constraints — a variant that breaks the grid cannot ship regardless of copy.
MAX_HEADLINE_CHARS = 60
MAX_SUBHEAD_CHARS = 140
MAX_CTA_WORDS = 4

# Specificity thresholds: at least one concrete referent in the headline, and at
# least two across headline+subhead. Strict enough to bite, loose enough that a
# good short headline is not punished for being short.
MIN_HEADLINE_REFERENTS = 1
MIN_TOTAL_REFERENTS = 2

# SMB buyer, not a compliance officer.
READABILITY_BAND = (6.0, 10.0)

# Reported in the trace only. No score impact — see module docstring.
LLM_TICS = frozenset({"seamless", "effortless", "frictionless", "supercharge", "empower"})

# Concrete nouns a Rippling variant may anchor to. Injectable so tests and future
# competitors are not hostage to this list.
RIPPLING_VOCABULARY = frozenset(
    {
        "payroll",
        "onboarding",
        "offboarding",
        "benefits",
        "mdm",
        "sso",
        "device",
        "devices",
        "contractor",
        "contractors",
        "eor",
        "entity",
        "entities",
        "state",
        "states",
        "country",
        "countries",
        "app",
        "apps",
        "integration",
        "integrations",
        "workflow",
        "workflows",
    }
)

_STOPWORDS = frozenset(
    {
        "the", "a", "an", "and", "or", "but", "for", "to", "of", "in", "on", "with",
        "your", "you", "our", "we", "is", "are", "be", "that", "this", "it", "as",
        "at", "by", "from", "all", "can", "will", "not", "no", "up", "out", "more",
    }
)  # fmt: skip

_WORD = re.compile(r"[a-z0-9%$][a-z0-9%$'-]*")
_HAS_DIGIT = re.compile(r"\d")


def score_variant(
    variant: Variant,
    hypothesis: Hypothesis,
    canonical_statement: str,
    unsourced_claims: list[str] | None = None,
    vocabulary: frozenset[str] = RIPPLING_VOCABULARY,
) -> ScoredVariant:
    """Apply the two gates, then the five weighted checks. Never raises."""
    unsourced = unsourced_claims or []

    if unsourced:
        return _rejected(
            variant, "unsourced_claim", f"unsourced comparative claim(s): {'; '.join(unsourced)}"
        )
    if len(set(variant.changed_elements)) > 1:
        changed = ", ".join(sorted(set(variant.changed_elements)))
        return _rejected(
            variant,
            "multivariate",
            f"changed {len(set(variant.changed_elements))} elements ({changed}) — "
            f"result cannot be attributed to a single variable",
        )

    parts: list[tuple[int, str]] = [
        _score_message_match(variant.headline, canonical_statement),
        _score_specificity(variant, vocabulary),
        _score_length(variant),
        _score_readability(variant),
        _score_segment_fit(variant.segment, hypothesis.segment),
    ]
    total = sum(points for points, _ in parts)
    trace = " · ".join(note for _, note in parts)

    tics = sorted(t for t in LLM_TICS if t in f"{variant.headline} {variant.subhead}".lower())
    if tics:
        trace += f" · NOTE llm-tic: {', '.join(tics)} (unscored)"

    if total < CRO_SCORE_GATE:
        return ScoredVariant(
            variant=variant,
            score=total,
            trace=f"{trace} → {total}/100 below gate {CRO_SCORE_GATE}",
            shippable=False,
            reject_reason="below_gate",
        )
    return ScoredVariant(
        variant=variant, score=total, trace=f"{trace} → {total}/100", shippable=True
    )


def count_concrete_referents(text: str, vocabulary: frozenset[str]) -> tuple[int, list[str]]:
    """Concrete referents = numerals or vocabulary nouns. Returns count + what matched.

    Returning the matches (not just a count) is what makes the specificity score
    auditable — the trace can say which words earned it.
    """
    matched: list[str] = []
    for word in _WORD.findall(text.lower()):
        if (_HAS_DIGIT.search(word) or word in vocabulary) and word not in matched:
            matched.append(word)
    return len(matched), matched


def _score_message_match(headline: str, canonical_statement: str) -> tuple[int, str]:
    """Headline must carry a content word from the claim it counters (ad->LP scent).

    Crude stemming (strip s/ing/ed) rather than a real lemmatizer — this is a
    keyword-overlap heuristic and the trace says so.
    """
    claim_terms = _content_stems(canonical_statement)
    headline_terms = _content_stems(headline)
    overlap = sorted(claim_terms & headline_terms)
    if not overlap:
        return 0, "message_match 0/25 (headline shares no content term with source claim)"
    points = W_MESSAGE_MATCH if len(overlap) >= 2 else W_MESSAGE_MATCH // 2
    return points, f"message_match {points}/25 (shares {', '.join(overlap[:3])})"


def _score_specificity(variant: Variant, vocabulary: frozenset[str]) -> tuple[int, str]:
    head_n, head_hits = count_concrete_referents(variant.headline, vocabulary)
    total_n, total_hits = count_concrete_referents(
        f"{variant.headline} {variant.subhead}", vocabulary
    )
    if head_n >= MIN_HEADLINE_REFERENTS and total_n >= MIN_TOTAL_REFERENTS:
        return W_SPECIFICITY, f"specificity 25/25 ({', '.join(total_hits[:4])})"
    if total_n >= MIN_TOTAL_REFERENTS:
        half = W_SPECIFICITY // 2
        return half, f"specificity {half}/25 (concrete, but headline itself is vague)"
    return 0, (
        f"specificity 0/25 (only {total_n} concrete referent(s)"
        f"{': ' + ', '.join(total_hits) if total_hits else ''} — asserts nothing checkable)"
    )


def _score_length(variant: Variant) -> tuple[int, str]:
    """Each present element gets an equal share of W_LENGTH; over-limit gets none."""
    checks = [
        ("headline", len(variant.headline) <= MAX_HEADLINE_CHARS, len(variant.headline)),
        ("subhead", len(variant.subhead) <= MAX_SUBHEAD_CHARS, len(variant.subhead)),
        ("cta", len(variant.cta.split()) <= MAX_CTA_WORDS, len(variant.cta.split())),
    ]
    present = [c for c in checks if c[2] > 0]
    if not present:
        return 0, "length 0/20 (no copy)"
    share = W_LENGTH / len(present)
    points = int(round(sum(share for _, ok, _ in present if ok)))
    over = [name for name, ok, _ in present if not ok]
    detail = f"over limit: {', '.join(over)}" if over else "all within layout limits"
    return points, f"length {points}/20 ({detail})"


def _score_readability(variant: Variant) -> tuple[int, str]:
    text = f"{variant.headline}. {variant.subhead}".strip()
    grade = flesch_kincaid_grade(text)
    low, high = READABILITY_BAND
    if low <= grade <= high:
        return W_READABILITY, f"readability {W_READABILITY}/15 (grade {grade:.1f}, in band)"
    distance = low - grade if grade < low else grade - high
    points = max(0, int(round(W_READABILITY - distance * 3)))
    return points, f"readability {points}/15 (grade {grade:.1f}, outside {low:.0f}-{high:.0f})"


def _score_segment_fit(variant_segment: str, hypothesis_segment: str) -> tuple[int, str]:
    """Weighted, deliberately NOT a gate.

    Segment boundaries are fuzzy ("30-200 employee migration" vs "growing teams"
    genuinely overlap). Hard gates are reserved for things categorically wrong —
    an unsourceable claim, an unreadable test design — not things arguably wrong.
    """
    v_terms = _content_stems(variant_segment)
    h_terms = _content_stems(hypothesis_segment)
    if not v_terms or not h_terms:
        return 0, "segment_fit 0/15 (segment not declared)"
    overlap = v_terms & h_terms
    ratio = len(overlap) / len(h_terms)
    points = int(round(W_SEGMENT_FIT * min(1.0, ratio)))
    return points, f"segment_fit {points}/15 ({len(overlap)}/{len(h_terms)} segment terms)"


def flesch_kincaid_grade(text: str) -> float:
    """FK grade level. Heuristic syllable counting — no NLP dependency."""
    words = _WORD.findall(text.lower())
    if not words:
        return 0.0
    sentences = max(1, len([s for s in re.split(r"[.!?]+", text) if s.strip()]))
    syllables = sum(_syllables(w) for w in words)
    return round(0.39 * (len(words) / sentences) + 11.8 * (syllables / len(words)) - 15.59, 2)


def _syllables(word: str) -> int:
    """Vowel-group count with a silent-e correction. Approximate by design."""
    groups = re.findall(r"[aeiouy]+", word)
    count = len(groups)
    if word.endswith("e") and count > 1 and not word.endswith(("le", "ee")):
        count -= 1
    return max(1, count)


def _content_stems(text: str) -> set[str]:
    """Lowercased content words, crudely stemmed. Stopwords and 1-char tokens dropped."""
    return {_stem(w) for w in _WORD.findall(text.lower()) if w not in _STOPWORDS and len(w) > 2}


def _stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def _rejected(variant: Variant, reason: str, detail: str) -> ScoredVariant:
    return ScoredVariant(
        variant=variant,
        score=0,
        trace=f"REJECTED ({reason}): {detail}",
        shippable=False,
        reject_reason=reason,  # type: ignore[arg-type]
    )
