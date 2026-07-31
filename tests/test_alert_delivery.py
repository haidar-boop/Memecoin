"""Tests for Part 29: sinks, message format, ranking, history, performance."""

from datetime import datetime, timezone

import pytest

from meme_intelligence.alerts.notification_engine import (
    AlertEvent,
    NotificationEngine,
    rank_alert,
)
from meme_intelligence.alerts.sinks import (
    DiscordSink,
    TelegramSink,
    channel_for,
    format_alert,
    parse_routes,
)
from meme_intelligence.ai.prompts import check_language
from meme_intelligence.config.settings import (
    AlertDeliverySettings,
    AlertEngineSettings,
    Settings,
)
from meme_intelligence.core.cache import TTLCache
from meme_intelligence.core.enums import AlertPriority
from meme_intelligence.core.errors import CollectorError, ConfigurationError
from meme_intelligence.core.models import TokenIdentity
from meme_intelligence.core.rate_limiter import RateLimiter
from meme_intelligence.database.storage import Storage

TOKEN = TokenIdentity(chain="solana", address="TokenAddr1", name="Meme", symbol="MEME")


def make_event(priority=AlertPriority.HIGH, alert_type="high_priority_opportunity",
               **overrides) -> AlertEvent:
    defaults = dict(
        priority=priority, alert_type=alert_type, token=TOKEN,
        title="All review gates passed (score 88)",
        reasons=("classification: strong_candidate",),
        scores={"master": 88.0, "security": 92.0},
        why_it_matters="Every measurable gate passed with data.",
        monitoring=("track holder growth",),
        detected_at=datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return AlertEvent(**defaults)


# ---- Section 7 message format ----

def test_format_alert_has_every_section7_field():
    text = format_alert(make_event())
    for expected in (
        "HIGH IMPORTANCE UPDATE",          # Section 2 output header
        "HIGH PRIORITY OPPORTUNITY",       # alert type
        "Name: Meme", "Symbol: MEME", "Contract: TokenAddr1", "Chain: solana",
        "Time detected: 2026-07-08 12:00 UTC",
        "Event summary", "All review gates passed",
        "Why it matters", "Evidence", "classification: strong_candidate",
        "Current scores", "master: 88/100",
        "Risk assessment: High",
        "Recommended monitoring", "track holder growth",
    ):
        assert expected in text, f"missing: {expected}"
    assert check_language(text) == []


def test_priority_headers_and_risk_mapping():
    critical = format_alert(make_event(priority=AlertPriority.CRITICAL,
                                       alert_type="emergency_review"))
    assert "CRITICAL RISK EVENT" in critical and "Risk assessment: High" in critical
    low = format_alert(make_event(priority=AlertPriority.LOW, alert_type="info"))
    assert "INFORMATION UPDATE" in low and "Risk assessment: Low" in low


# ---- Section 8 channel routing ----

def test_channel_categories():
    assert channel_for(make_event()) == "discoveries"
    assert channel_for(make_event(alert_type="whale_exit")) == "smart_money"
    assert channel_for(make_event(alert_type="security_change")) == "security"
    assert channel_for(make_event(alert_type="momentum")) == "momentum"
    assert channel_for(make_event(alert_type="something_future")) == "reports"


def test_boost_channel_is_isolated_from_discoveries():
    """A boost is unscreened paid promotion; it must never share a channel
    with vetted opportunity alerts (early_opportunity, strong_candidate,
    high_priority_opportunity, new_token_discovery) or its higher volume
    drowns out the alerts that actually matter — regression guard for the
    bug where boost briefly rode in on "discoveries"."""
    boost_channel = channel_for(make_event(alert_type="boost"))
    assert boost_channel != "discoveries"
    for opportunity_type in (
        "early_opportunity", "strong_candidate",
        "high_priority_opportunity", "new_token_discovery",
    ):
        assert channel_for(make_event(alert_type=opportunity_type)) == "discoveries"


def test_parse_routes_validates_categories():
    routes = parse_routes("security=-100123, momentum=-100456")
    assert routes == {"security": "-100123", "momentum": "-100456"}
    assert parse_routes("") == {}
    with pytest.raises(ValueError):
        parse_routes("secruity=-100123")  # typo must fail loudly
    with pytest.raises(ValueError):
        parse_routes("security=")


# ---- Sinks ----

def make_telegram(**kwargs) -> TelegramSink:
    return TelegramSink("BOT_TOKEN", "-100999",
                        rate_limiter=RateLimiter(100.0, burst=10), cache=TTLCache(),
                        **kwargs)


async def test_telegram_sends_formatted_message(monkeypatch):
    sink = make_telegram()
    calls = []

    async def fake_get_json(path, params=None, *, cache_key=None, cache_ttl=None,
                            headers=None, json_body=None):
        calls.append({"path": path, "body": json_body})
        return {"ok": True}

    monkeypatch.setattr(sink, "_get_json", fake_get_json)
    await sink.send(make_event())
    assert len(calls) == 1
    assert calls[0]["path"] == "botBOT_TOKEN/sendMessage"
    assert calls[0]["body"]["chat_id"] == "-100999"
    assert "HIGH IMPORTANCE UPDATE" in calls[0]["body"]["text"]


async def test_telegram_routes_by_category(monkeypatch):
    sink = make_telegram(routes={"security": "-100SEC"})
    calls = []

    async def fake_get_json(path, params=None, *, cache_key=None, cache_ttl=None,
                            headers=None, json_body=None):
        calls.append(json_body["chat_id"])
        return {"ok": True}

    monkeypatch.setattr(sink, "_get_json", fake_get_json)
    await sink.send(make_event(alert_type="risk_warning"))     # security category
    await sink.send(make_event(alert_type="momentum", priority=AlertPriority.MEDIUM))
    assert calls == ["-100SEC", "-100999"]


async def test_min_priority_filters_low_noise(monkeypatch):
    sink = make_telegram(min_priority=AlertPriority.MEDIUM)
    calls = []

    async def fake_get_json(*a, **k):
        calls.append(1)
        return {"ok": True}

    monkeypatch.setattr(sink, "_get_json", fake_get_json)
    await sink.send(make_event(priority=AlertPriority.LOW, alert_type="info"))
    assert calls == []  # phones don't buzz for background information
    await sink.send(make_event(priority=AlertPriority.CRITICAL,
                               alert_type="emergency_review"))
    assert len(calls) == 1


async def test_delivery_failure_never_raises(monkeypatch):
    sink = make_telegram()

    async def broken(*a, **k):
        raise CollectorError("telegram: unexpected status 502")

    monkeypatch.setattr(sink, "_get_json", broken)
    await sink.send(make_event())  # must not raise (Rule 7)


async def test_discord_appends_wait_and_wraps_content(monkeypatch):
    sink = DiscordSink("https://discord.com/api/webhooks/1/abc",
                       rate_limiter=RateLimiter(100.0, burst=10), cache=TTLCache())
    calls = []

    async def fake_get_json(path, params=None, *, cache_key=None, cache_ttl=None,
                            headers=None, json_body=None):
        calls.append({"path": path, "body": json_body})
        return {"id": "1"}

    monkeypatch.setattr(sink, "_get_json", fake_get_json)
    await sink.send(make_event())
    assert calls[0]["path"].endswith("?wait=true")
    assert calls[0]["body"]["content"].startswith("```")


# ---- Section 10 ranking ----

def test_ranking_orders_critical_and_novel_first():
    critical = make_event(priority=AlertPriority.CRITICAL, alert_type="emergency_review")
    medium = make_event(priority=AlertPriority.MEDIUM, alert_type="momentum")
    assert rank_alert(critical, is_novel=True) > rank_alert(medium, is_novel=True)
    # Novelty moves the rank (10% weight) but never outranks priority.
    assert rank_alert(medium, is_novel=True) > rank_alert(medium, is_novel=False)
    assert rank_alert(medium, is_novel=True) < rank_alert(critical, is_novel=False)


class RecordingSink:
    def __init__(self):
        self.events = []

    async def send(self, event):
        self.events.append(event)


async def test_dispatch_ranks_and_stamps_detected_at():
    sink = RecordingSink()
    engine = NotificationEngine([sink], AlertEngineSettings())
    medium = make_event(priority=AlertPriority.MEDIUM, alert_type="momentum",
                        detected_at=None)
    critical = make_event(priority=AlertPriority.CRITICAL, alert_type="emergency_review",
                          detected_at=None)
    delivered = await engine.dispatch([medium, critical])  # arrives unordered
    assert [e.priority for e in delivered] == [AlertPriority.CRITICAL, AlertPriority.MEDIUM]
    assert all(e.detected_at is not None for e in sink.events)


class FailingSink:
    """Raises on every send — simulates a broken/misconfigured sink."""

    async def send(self, event):
        raise RuntimeError("sink exploded")


async def test_one_failing_sink_does_not_eat_the_rest_of_the_batch():
    """Bug-hunt: an unhandled exception from one sink propagated out of
    dispatch() entirely, aborting delivery to every remaining sink for
    that event AND every subsequent event in the same batch."""
    good = RecordingSink()
    engine = NotificationEngine([FailingSink(), good], AlertEngineSettings())
    first = make_event(alert_type="momentum", detected_at=None)
    second = make_event(alert_type="whale_exit", priority=AlertPriority.HIGH, detected_at=None)
    delivered = await engine.dispatch([first, second])
    # both events still reached the surviving sink and both count delivered
    assert len(good.events) == 2
    assert len(delivered) == 2


async def test_total_delivery_outage_does_not_permanently_lose_the_alert():
    """Bug-hunt: cooldown was stamped BEFORE any sink was attempted, so a
    total delivery outage (all sinks failing) still marked the alert as
    'sent' and it could never be retried within the cooldown window."""
    engine = NotificationEngine([FailingSink()], AlertEngineSettings(cooldown_seconds=900.0))
    event = make_event(detected_at=None)
    delivered = await engine.dispatch([event])
    assert delivered == []  # correctly not counted as delivered
    # cooldown must NOT have been stamped — the same event tried again
    # immediately (as the next scan cycle would) is not suppressed
    key = (event.token.chain, event.token.address.lower(), event.alert_type, event.priority.value)
    assert key not in engine._last_sent


def test_format_alert_sanitizes_injection_in_token_name():
    """Bug-hunt: an on-chain token name with backticks/newlines broke out of
    Discord's code fence and injected live markdown (incl. mention pings)."""
    evil = TokenIdentity(chain="solana", address="Mint1",
                         name="```@everyone\nCLICK", symbol="p\u200bump\n`x`")
    text = format_alert(make_event(token=evil))
    name_line = next(line for line in text.splitlines() if line.strip().startswith("Name:"))
    assert "```" not in name_line
    assert "\n" not in name_line.replace("Name:", "")
    assert "@everyone" in name_line  # kept as inert text, just defanged of markdown
    # The whole rendered message carries no stray backticks from identity.
    assert text.count("`") == 0


async def test_console_success_does_not_mask_failed_phone_delivery():
    """Bug-hunt: Telegram/Discord swallowed their delivery failures and the
    always-successful console counted as delivery — a lost phone alert was
    cooldown-stamped, recorded as delivered, and never retried. When external
    sinks are configured, only THEY decide delivery."""
    from meme_intelligence.alerts.notification_engine import ConsoleSink

    class FailingExternalSink:
        external = True

        def __init__(self):
            self.calls = 0

        async def send(self, event):
            self.calls += 1
            return False  # delivery failed (e.g. Telegram down / bad token)

    failing = FailingExternalSink()
    engine = NotificationEngine([ConsoleSink(), failing],
                                AlertEngineSettings(cooldown_seconds=900.0),
                                time_func=lambda: 0.0)
    event = make_event(detected_at=None)
    delivered = await engine.dispatch([event])
    assert delivered == []          # console printing is not phone delivery
    await engine.dispatch([event])
    assert failing.calls == 2       # cooldown not stamped -> retried

    class OkExternalSink:
        external = True

        async def send(self, event):
            return True

    engine_ok = NotificationEngine([ConsoleSink(), OkExternalSink()],
                                   AlertEngineSettings(), time_func=lambda: 0.0)
    delivered = await engine_ok.dispatch([make_event(detected_at=None)])
    assert len(delivered) == 1      # a real external success still delivers


# ---- Sections 11-12: history and performance ----

def make_master(score: float):
    """Minimal MasterAssessment stand-in for snapshot recording."""
    from meme_intelligence.analyzers.scoring_engine import MasterAssessment
    from meme_intelligence.core.enums import Classification, ConfidenceLevel
    from meme_intelligence.core.models import CategoryScores

    return MasterAssessment(
        token=TOKEN, generated_at=datetime.now(timezone.utc),
        category_scores=CategoryScores(security=score), final_score=score,
        coverage=0.15, classification=Classification.WATCHLIST,
        overrides=(), decision_trace=(), confidence=ConfidenceLevel.MEDIUM,
    )


def test_alert_history_and_performance(tmp_path):
    ticks = iter(datetime(2026, 7, 8, 12, minute, tzinfo=timezone.utc)
                 for minute in range(0, 60, 10))
    with Storage(str(tmp_path / "t.sqlite3"), now_func=lambda: next(ticks)) as storage:
        storage.record_snapshot(make_master(70.0), source="test")       # t0: baseline
        storage.record_alert(make_event(scores={"master": 70.0}), source="test")  # t1
        storage.record_snapshot(make_master(84.0), source="test")       # t2: follow-up

        history = storage.alert_history()
        assert len(history) == 1
        assert history[0]["alert_type"] == "high_priority_opportunity"
        assert history[0]["score_at_alert"] == pytest.approx(70.0)
        assert history[0]["symbol"] == "MEME"

        performance = storage.alert_performance()
        assert len(performance) == 1
        row = performance[0]
        assert row["alerts_measured"] == 1
        assert row["avg_score_drift"] == pytest.approx(14.0)  # 84 after vs 70 at alert
        assert row["improved_count"] == 1


def test_alert_performance_empty_without_followups(tmp_path):
    with Storage(str(tmp_path / "t.sqlite3")) as storage:
        storage.record_alert(make_event(scores={"master": 70.0}), source="test")
        assert storage.alert_performance() == []  # no snapshot after the alert yet


# ---- Settings ----

def test_delivery_settings_validated_and_loaded():
    with pytest.raises(ConfigurationError):
        AlertDeliverySettings(external_min_priority="loud")
    settings = Settings.from_env(env={
        "MEMEINTEL_TELEGRAM_BOT_TOKEN": "123:abc",
        "MEMEINTEL_TELEGRAM_CHAT_ID": "-100777",
        "MEMEINTEL_DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/1/x",
        "MEMEINTEL_ALERT_DELIVERY_TELEGRAM_ROUTES": "security=-100SEC",
        "MEMEINTEL_ALERT_DELIVERY_EXTERNAL_MIN_PRIORITY": "high",
    })
    assert settings.telegram_bot_token == "123:abc"
    assert settings.alert_delivery.telegram_routes == "security=-100SEC"
    assert settings.alert_delivery.external_min_priority == "high"


def test_build_sinks_activates_on_secrets():
    from meme_intelligence.__main__ import build_sinks

    assert len(build_sinks(Settings.from_env(env={}))) == 1  # console only
    sinks = build_sinks(Settings.from_env(env={
        "MEMEINTEL_TELEGRAM_BOT_TOKEN": "123:abc",
        "MEMEINTEL_TELEGRAM_CHAT_ID": "-100777",
        "MEMEINTEL_DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/1/x",
    }))
    assert [type(s).__name__ for s in sinks] == ["ConsoleSink", "TelegramSink", "DiscordSink"]


# ---- Community-aware rules (gate now fed by the live collector) ----

def make_rule_result(community, overall=88.0):
    """Minimal PipelineResult stand-in for targeted rule tests."""
    from types import SimpleNamespace

    security = SimpleNamespace(
        overall_score=90.0, is_destructive=False,
        sub_scores={"liquidity": 85.0}, findings=(),
    )
    return SimpleNamespace(
        # Deep liquidity + no AI judgment: the strong-candidate vetoes
        # (depth floor, AI-confidence floor) stay out of these gate tests.
        pair=SimpleNamespace(base_token=TOKEN, liquidity_usd=90_000.0),
        security=security,
        onchain=SimpleNamespace(overall_score=80.0),
        community=community,
        ai_judgment=None,
        master=SimpleNamespace(
            final_score=overall,
            classification=SimpleNamespace(value="strong_candidate"),
        ),
    )


def test_opportunity_gate_uses_live_community_score():
    from meme_intelligence.alerts.notification_engine import AutomationRules
    from meme_intelligence.config.settings import AlertThresholds
    from types import SimpleNamespace

    rules = AutomationRules(AlertThresholds(), AlertEngineSettings())

    # Community verified and passing -> full HIGH qualification.
    strong = SimpleNamespace(overall_score=78.0, is_artificial=False, findings=())
    event = rules._opportunity_rule(make_rule_result(strong))
    assert event.alert_type == "high_priority_opportunity"
    assert event.priority is AlertPriority.HIGH

    # Community verified and failing -> no opportunity alert at all.
    weak = SimpleNamespace(overall_score=40.0, is_artificial=False, findings=())
    assert rules._opportunity_rule(make_rule_result(weak)) is None

    # No community data but overall clears the strong bar (88) -> HIGH
    # strong_candidate (the fresh-launch tier).
    event = rules._opportunity_rule(make_rule_result(None, overall=88.0))
    assert event.alert_type == "strong_candidate"
    assert event.priority is AlertPriority.HIGH
    assert any("community" in r or "unverified" in r.lower()
               for r in (event.title,) + event.reasons)

    # No community data and below the strong bar -> MEDIUM provisional.
    event = rules._opportunity_rule(make_rule_result(None, overall=86.0))
    assert event.alert_type == "early_opportunity"
    assert event.priority is AlertPriority.MEDIUM


def test_fake_community_fires_high_alert():
    from meme_intelligence.alerts.notification_engine import AutomationRules
    from meme_intelligence.config.settings import AlertThresholds
    from meme_intelligence.analyzers.common import Finding
    from meme_intelligence.core.enums import RiskTier
    from types import SimpleNamespace

    rules = AutomationRules(AlertThresholds(), AlertEngineSettings())
    fake = SimpleNamespace(
        overall_score=0.0, is_artificial=True,
        findings=(Finding("growth", RiskTier.DESTRUCTIVE,
                          "62% bot followers: fake community"),),
    )
    event = rules._community_rule(make_rule_result(fake))
    assert event is not None
    assert event.alert_type == "community_fake"
    assert event.priority is AlertPriority.HIGH
    assert channel_for(event) == "security"


# ---- Project 2: inline keyboards (feedback / copy / buy scaffold) ----

def test_feedback_keyboard_has_thumbs_and_copy_but_no_buy_by_default():
    from meme_intelligence.alerts.sinks import feedback_keyboard

    addr = "So1MemeToken111111111111111111111111111111"
    markup = feedback_keyboard(addr)
    rows = markup["inline_keyboard"]
    assert rows[0][0]["callback_data"] == f"fb:1:{addr}"
    assert rows[0][1]["callback_data"] == f"fb:0:{addr}"
    assert rows[1][0]["copy_text"]["text"] == addr    # one-tap copy (operator request)
    assert all("buy:" not in str(row) for row in rows)


def test_feedback_keyboard_adds_buy_and_dump_when_percents_given():
    from meme_intelligence.alerts.sinks import feedback_keyboard

    addr = "So1MemeToken111111111111111111111111111111"
    rows = feedback_keyboard(addr, buy_percents=(20, 50, 75, 100))["inline_keyboard"]
    flat = [b for row in rows for b in row]
    buys = [b for b in flat if b.get("callback_data", "").startswith("buy:")]
    assert [b["callback_data"] for b in buys] == [
        f"buy:{addr}:pct:20", f"buy:{addr}:pct:50",
        f"buy:{addr}:pct:75", f"buy:{addr}:pct:100",
    ]
    assert [b["text"] for b in buys] == ["Buy 20%", "Buy 50%", "Buy 75%", "Buy 100%"]
    dumps = [b for b in flat if b.get("callback_data") == f"dump:{addr}"]
    assert dumps and dumps[0]["text"] == "💥 Dump all"


def test_feedback_keyboard_no_trade_buttons_without_percents():
    from meme_intelligence.alerts.sinks import feedback_keyboard

    addr = "So1MemeToken111111111111111111111111111111"
    rows = feedback_keyboard(addr)["inline_keyboard"]
    flat = str(rows)
    assert "buy:" not in flat and "dump:" not in flat


def test_feedback_keyboard_refuses_oversized_address():
    from meme_intelligence.alerts.sinks import feedback_keyboard

    assert feedback_keyboard("x" * 80) is None   # would exceed 64-byte callback_data
    assert feedback_keyboard("") is None


def test_copy_keyboard_shape():
    from meme_intelligence.alerts.sinks import copy_keyboard

    addr = "So1MemeToken111111111111111111111111111111"
    markup = copy_keyboard(addr)
    assert markup["inline_keyboard"][0][0]["copy_text"]["text"] == addr
    assert copy_keyboard("") is None


async def test_telegram_alert_carries_feedback_keyboard(monkeypatch):
    sink = make_telegram()
    calls = []

    async def fake_get_json(path, params=None, *, cache_key=None, cache_ttl=None,
                            headers=None, json_body=None):
        calls.append(json_body)
        return {"ok": True}

    monkeypatch.setattr(sink, "_get_json", fake_get_json)
    await sink.send(make_event())
    markup = calls[0]["reply_markup"]
    data = str(markup)
    assert "fb:1:" in data and "copy_text" in data and "buy:" not in data


async def test_telegram_alert_shows_trade_buttons_with_percents(monkeypatch):
    sink = make_telegram(buy_button_percents=(20, 50, 75, 100))
    calls = []

    async def fake_get_json(path, params=None, *, cache_key=None, cache_ttl=None,
                            headers=None, json_body=None):
        calls.append(json_body)
        return {"ok": True}

    monkeypatch.setattr(sink, "_get_json", fake_get_json)
    await sink.send(make_event())
    data = str(calls[0]["reply_markup"])
    assert "buy:" in data and "dump:" in data


# ---- Rug-guard alert routing + cooldown distinctness (2026-07-31 port) ----


def test_rug_watch_alerts_route_to_the_security_channel():
    """The system's only auto-sell alerts must never fall through to the
    low-attention "reports" channel."""
    warn = make_event(priority=AlertPriority.HIGH, alert_type="rug_watch_warning")
    exit_ = make_event(priority=AlertPriority.CRITICAL, alert_type="rug_watch_exit")
    assert channel_for(warn) == "security"
    assert channel_for(exit_) == "security"


async def test_a_warn_can_never_cool_down_the_exit():
    """The cooldown key includes alert_type AND priority, so an escalating
    rug (WARN then EXIT on the same coin, seconds apart) delivers BOTH —
    pinned here because the guard's whole point dies if the exit alert is
    suppressed by its own warning."""
    sink = RecordingSink()
    engine = NotificationEngine([sink], AlertEngineSettings(cooldown_seconds=900.0),
                                time_func=lambda: 1000.0)
    warn = make_event(priority=AlertPriority.HIGH, alert_type="rug_watch_warning")
    exit_ = make_event(priority=AlertPriority.CRITICAL, alert_type="rug_watch_exit")
    await engine.dispatch([warn])
    await engine.dispatch([exit_])
    assert [e.alert_type for e in sink.events] == ["rug_watch_warning", "rug_watch_exit"]


async def test_a_repeat_exit_inside_the_window_is_cooled_down():
    """The other half of the contract: the SAME stage re-firing within the
    cooldown stays suppressed, so a drain that keeps draining does not spam
    a CRITICAL every poll."""
    sink = RecordingSink()
    engine = NotificationEngine([sink], AlertEngineSettings(cooldown_seconds=900.0),
                                time_func=lambda: 1000.0)
    exit_ = make_event(priority=AlertPriority.CRITICAL, alert_type="rug_watch_exit")
    await engine.dispatch([exit_])
    await engine.dispatch([exit_])
    assert [e.alert_type for e in sink.events] == ["rug_watch_exit"]
