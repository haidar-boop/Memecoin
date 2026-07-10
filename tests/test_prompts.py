"""Tests for the analyst system prompt and language guard (Spec Part 16)."""

from meme_intelligence.ai.prompts import ANALYST_SYSTEM_PROMPT, BANNED_PHRASES, check_language


def test_system_prompt_contains_core_rules():
    """The prompt must carry the Part 16 identity, order, and discipline."""
    normalized = " ".join(ANALYST_SYSTEM_PROMPT.split())
    for required in (
        "MEME COIN INTELLIGENCE ANALYST AI",
        "never execute trades",
        "Accuracy over speed",
        "Evidence over hype",
        "Insufficient data available",
        "bull case and a bear case",
        "VERIFIED DATA",
        "Security comes before opportunity",
        "Elite Opportunity / Strong Candidate / Watchlist / Speculative / Avoid",
        "Quick scan",
        "Compare",
        "Update watchlist",
    ):
        assert required in normalized, f"missing from system prompt: {required}"


def test_system_prompt_itself_is_compliant():
    """The prompt quotes banned phrases only as prohibitions; the guard must
    be applied to analysis text, and the prompt's own analytical sections
    must not model hype."""
    # The LANGUAGE DISCIPLINE section legitimately quotes banned phrases;
    # everything before it must be clean.
    analytical_part = ANALYST_SYSTEM_PROMPT.split("LANGUAGE DISCIPLINE")[0]
    assert check_language(analytical_part) == []


def test_banned_phrases_detected():
    assert check_language("This token will pump hard!") == ["will pump"]
    assert "guaranteed" in check_language("A guaranteed winner, risk-free returns.")
    assert "risk-free" in check_language("A guaranteed winner, risk-free returns.")
    assert check_language("it's a Sure  Thing") == ["sure thing"]  # case + spacing


def test_compliant_language_passes():
    compliant = (
        "Current evidence suggests a higher-probability setup. "
        "This remains a speculative opportunity with significant risk; "
        "failure is possible and losses can be total. The guard rails "
        "guarantee nothing about outcomes."  # 'guarantee' as negation still flags?
    )
    # "guarantee nothing" contains the banned word — the guard is deliberately
    # strict: rephrase rather than argue with it.
    assert check_language(compliant) == ["guarantee"]

    clean = ("Current evidence suggests a higher-probability setup. "
             "This remains a speculative opportunity with significant risk.")
    assert check_language(clean) == []


def test_word_boundaries_prevent_false_positives():
    assert check_language("the guarantor clause in the contract") == []
    assert check_language("norisk is a username") == []


def test_generated_reports_are_compliant():
    """The deterministic report generator must obey the same language rules."""
    from tests.test_report_generator import build_all
    from meme_intelligence.ai.report_generator import build_report

    pair, security, onchain, token, risk, master, plan = build_all()
    text = build_report(pair, master, security, onchain=onchain, token=token,
                        risk=risk, plan=plan).text
    assert check_language(text) == []

    pair, security, onchain, token, risk, master, plan = build_all(honeypot=True)
    text = build_report(pair, master, security, onchain=onchain, token=token,
                        risk=risk, plan=plan).text
    assert check_language(text) == []


def test_every_banned_phrase_is_detectable():
    for phrase in BANNED_PHRASES:
        assert check_language(f"analysts say {phrase} today") == [phrase]


def test_negation_does_not_leak_across_sentences():
    """Bug-hunt: 'not' in a PREVIOUS sentence/bullet whitelisted the next
    clause's opening hype claim."""
    assert check_language("Liquidity is not locked.\nGuaranteed strong meme.") == ["guaranteed"]
    assert check_language("Community is organic, not botted. Guaranteed viral.") == ["guaranteed"]


def test_no_doubt_is_not_a_negation():
    assert check_language("There is no doubt this will pump hard.") == ["will pump"]


def test_no_risk_noun_phrases_are_cautionary_not_hype():
    """Bug-hunt: 'no risk assessment/data' is disclaiming language the prompt
    DEMANDS; flagging it discarded whole paid judgments."""
    assert check_language("Snapshot contains no risk assessment data.") == []
    assert check_language("There are no risk signals available.") == []
    # A bare hype 'no risk' still violates.
    assert check_language("This trade carries no risk.") == ["no risk"]
    # And real negated disclaimers still pass.
    assert check_language("This is not guaranteed and is not risk free.") == []
