"""Router intent classification — deterministic, no LLM, no network (Phase 7, Layer 1).

The REPL router decides per turn: fresh run | compare-with-Rippling | quit | follow-up.
This is the 'deterministic router — no LLM decides what a command means' contract from
repl.py's docstring. Tests assert each route purely from the text + whether a session
exists — no client, no tools, no network. The pattern table (_RUN_AGAIN_PREFIXES) is the
UX-tunable spot; these tests lock its current behavior in.
"""

from __future__ import annotations

from agent.repl import TurnIntent, classify_turn

# --- fresh runs ------------------------------------------------------------


def test_analyze_prefix_is_fresh():
    ti = classify_turn("analyze gusto.com", has_session=True)
    assert ti.kind == "fresh"
    assert ti.target == "gusto.com"


def test_bare_domain_no_session_is_fresh():
    ti = classify_turn("gusto.com", has_session=False)
    assert ti.kind == "fresh"
    assert ti.target == "gusto.com"


def test_bare_name_no_session_is_fresh():
    ti = classify_turn("Bamboo HR", has_session=False)
    assert ti.kind == "fresh"
    assert ti.target == "Bamboo HR"


def test_run_again_for_named_competitor_is_fresh():
    ti = classify_turn("run again for gusto", has_session=True)
    assert ti.kind == "fresh"
    assert ti.target == "gusto"


def test_run_again_with_no_target_defaults_to_current():
    ti = classify_turn("run again", has_session=True)
    assert ti.kind == "fresh"
    assert ti.target is None  # the REPL re-runs the current session's competitor


def test_run_again_aliases_are_fresh():
    assert classify_turn("again deel", has_session=True) == TurnIntent(kind="fresh", target="deel")
    assert classify_turn("rerun gusto.com", has_session=True) == TurnIntent(
        kind="fresh", target="gusto.com"
    )
    assert classify_turn("redo", has_session=True) == TurnIntent(kind="fresh", target=None)


def test_run_again_strips_optional_for():
    assert classify_turn("again for deel", has_session=True).target == "deel"
    assert classify_turn("rerun for gusto.com", has_session=True).target == "gusto.com"


def test_analyze_rippling_is_fresh_not_compare():
    # "analyze rippling.com" builds the Rippling ledger, not a comparison
    ti = classify_turn("analyze rippling.com", has_session=True)
    assert ti.kind == "fresh"
    assert ti.target == "rippling.com"


def test_run_again_for_rippling_is_fresh_not_compare():
    ti = classify_turn("run again for rippling", has_session=True)
    assert ti.kind == "fresh"
    assert ti.target == "rippling"


# --- compare ---------------------------------------------------------------


def test_compare_with_rippling_is_compare():
    assert classify_turn("compare with rippling", has_session=True).kind == "compare"


def test_compare_natural_phrasing_is_compare():
    for phrase in [
        "what similar strategies do rippling and this company have",
        "how does rippling compare",
        "rippling vs gusto",
        "vs rippling",
        "what strategies does rippling and this company have",
    ]:
        assert classify_turn(phrase, has_session=True).kind == "compare", phrase


def test_compare_without_session_still_classifies_compare():
    # the REPL degrades this ("analyze a competitor first"); the intent is still compare
    assert classify_turn("compare with rippling", has_session=False).kind == "compare"


# --- quit ------------------------------------------------------------------


def test_quit_words():
    for phrase in ["quit", "exit", "q", "EXIT", "Quit"]:
        assert classify_turn(phrase, has_session=True).kind == "quit", phrase


# --- follow-up (the fall-through that lets the user ask any question) ------


def test_plain_followups_route_to_followup():
    for phrase in [
        "dig deeper on the pricing",
        "tell me about their ads",
        "what's their ICP",
        "now what about their recent changes",
    ]:
        assert classify_turn(phrase, has_session=True).kind == "followup", phrase


def test_no_session_unrecognized_text_is_fresh_target():
    # preserves the existing first-turn behavior: any text with no session is a target
    ti = classify_turn("tell me about gusto", has_session=False)
    assert ti.kind == "fresh"
    assert ti.target == "tell me about gusto"


# --- natural run-again phrasings (adversarial prover catch — the prefix list missed these)


def test_run_this_again_for_named_competitor_is_fresh():
    """The user's literal phrasing 'run this again for gusto' — the prefix list missed it
    ('run this again' doesn't start with 'run again'); the regex handles run/it/this/that+again."""
    ti = classify_turn("run this again for gusto", has_session=True)
    assert ti.kind == "fresh"
    assert ti.target == "gusto"


def test_run_it_again_no_target_defaults_to_current():
    assert classify_turn("run it again", has_session=True) == TurnIntent(kind="fresh", target=None)


def test_do_it_again_for_deel_is_fresh():
    assert classify_turn("do it again for deel", has_session=True) == TurnIntent(
        kind="fresh", target="deel"
    )


def test_start_over_for_competitor_is_fresh():
    assert classify_turn("start over for bamboo hr", has_session=True) == TurnIntent(
        kind="fresh", target="bamboo hr"
    )


def test_start_over_no_target_defaults_to_current():
    assert classify_turn("start over", has_session=True) == TurnIntent(kind="fresh", target=None)


def test_reset_is_fresh():
    assert classify_turn("reset", has_session=True) == TurnIntent(kind="fresh", target=None)


def test_run_again_tolerates_extra_whitespace():
    assert classify_turn("re-run  gusto.com", has_session=True).target == "gusto.com"


# --- Rippling heuristic tightening (code-review P1 #2) ----------------------


def test_rippling_mention_without_compare_signal_is_followup():
    """A follow-up that merely references Rippling (no comparative word) stays a follow-up,
    not a fresh comparison. 'the rippling-relevance section' has no compare/similar/vs/and."""
    for phrase in [
        "tell me about the rippling-relevance section",
        "what did the rippling part of the brief say",
    ]:
        assert classify_turn(phrase, has_session=True).kind == "followup", phrase


def test_rippling_and_structure_is_compare():
    """'rippling and <X>' / '<X> and rippling' is a comparative structure even without a
    compare/similar word — catches 'what strategies does rippling and this company have'."""
    assert (
        classify_turn("what strategies does rippling and this company have", has_session=True).kind
        == "compare"
    )
    assert classify_turn("how do gusto and rippling differ", has_session=True).kind == "compare"


# --- new-competitor switch (Phase 7 follow-on — the live-transcript bug) ----
#
# "Now do the same thing for a different company called Workday" — with a Gusto session
# live — used to fall through to followup, producing an unscored, ungrounded, un-persisted
# half-run. These lock in that natural switch phrasings route to a FRESH analyze of the
# named competitor (full pipeline + persisted brief) instead. High-precision: a switch
# requires an explicit "different/new/another company" qualifier, "switch to", or a bare
# domain — topic follow-ups stay follow-ups.


def test_switch_different_company_called_routes_fresh():
    """The user's literal phrasing from the live transcript."""
    ti = classify_turn(
        "Now do the same thing for a different company called Workday",
        has_session=True,
        current_competitor="gusto.com",
    )
    assert ti.kind == "fresh"
    assert ti.target == "Workday"


def test_switch_new_company_called_routes_fresh():
    ti = classify_turn(
        "do the same for a new company called Bamboo HR",
        has_session=True,
        current_competitor="gusto.com",
    )
    assert ti.kind == "fresh"
    assert ti.target == "Bamboo HR"


def test_switch_to_phrasing_routes_fresh():
    for phrase, expected in [
        ("switch to deel", "deel"),
        ("now switch to workday", "workday"),
        ("switch to rippling.com", "rippling.com"),
    ]:
        ti = classify_turn(phrase, has_session=True, current_competitor="gusto.com")
        assert ti.kind == "fresh", phrase
        assert ti.target == expected, phrase


def test_bare_domain_mid_session_routes_fresh():
    """Typing just a domain mid-session is a strong switch signal."""
    ti = classify_turn("workday.com", has_session=True, current_competitor="gusto.com")
    assert ti.kind == "fresh"
    assert ti.target == "workday.com"


def test_now_do_domain_routes_fresh():
    for phrase in ["now do workday.com", "now analyze deel.com", "run bamboohr.com"]:
        ti = classify_turn(phrase, has_session=True, current_competitor="gusto.com")
        assert ti.kind == "fresh", phrase
        assert ti.target is not None and "." in ti.target, phrase


def test_switch_naming_same_competitor_stays_followup():
    """A qualified switch that names the SAME competitor is not a switch — it's a
    re-run-ish follow-up (e.g. 'do the same for a different company called Gusto' while
    already on gusto.com)."""
    ti = classify_turn(
        "do the same thing for a different company called Gusto",
        has_session=True,
        current_competitor="gusto.com",
    )
    assert ti.kind == "followup"


def test_switch_same_bare_domain_stays_followup():
    """Bare domain equal to the current competitor stays a follow-up, not a fresh re-run
    (use 'run again' to re-run the current competitor)."""
    ti = classify_turn("gusto.com", has_session=True, current_competitor="gusto.com")
    assert ti.kind == "followup"


def test_topic_do_the_same_stays_followup():
    """'do the same thing for their pricing' has no switch qualifier and no domain — it's
    a topic follow-up, not a competitor switch. The high-precision guard prevents a
    false fresh-run that would waste ~$0.50 and discard the current session."""
    ti = classify_turn(
        "do the same thing for their pricing",
        has_session=True,
        current_competitor="gusto.com",
    )
    assert ti.kind == "followup"


def test_followups_unaffected_by_current_competitor_arg():
    """Existing follow-up phrasings still route to followup with the new arg present."""
    for phrase in [
        "dig deeper on the pricing",
        "tell me about their ads",
        "now what about their recent changes",
    ]:
        ti = classify_turn(phrase, has_session=True, current_competitor="gusto.com")
        assert ti.kind == "followup", phrase


def test_no_session_bare_text_still_fresh_with_arg():
    """Backward compat: no-session unrecognized text is still a fresh target."""
    ti = classify_turn("tell me about gusto", has_session=False, current_competitor=None)
    assert ti.kind == "fresh"
    assert ti.target == "tell me about gusto"


def test_switch_with_no_current_competitor_routes_fresh():
    """If current_competitor is None (defensive), any switch phrasing routes fresh."""
    ti = classify_turn(
        "do the same thing for a different company called Workday",
        has_session=True,
        current_competitor=None,
    )
    assert ti.kind == "fresh"
    assert ti.target == "Workday"
