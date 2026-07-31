"""Tests for two-way Telegram control (Project 2, ROADMAP item 2)."""

import dataclasses
from datetime import datetime, timezone
from types import SimpleNamespace

from meme_intelligence.alerts.telegram_commands import (
    CommandContext,
    TelegramCommandListener,
    classify_address,
)
from meme_intelligence.config.settings import Settings
from meme_intelligence.core.errors import CollectorError
from meme_intelligence.core.models import TokenIdentity
from meme_intelligence.core.rate_limiter import RateLimiter
from meme_intelligence.database.storage import Storage
from meme_intelligence.trading.execution import DryRunExecutor

NOW = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
CHAT_ID = "111222333"
SOL_ADDR = "So1MemeToken111111111111111111111111111111"
EVM_ADDR = "0x" + "ab" * 20
TOKEN = TokenIdentity(chain="solana", address=SOL_ADDR, symbol="MEMA")


def make_settings(**env) -> Settings:
    return Settings.from_env(env=env)


def make_listener(storage, *, settings=None, status=None, check_result="unset",
                  learning=None, executor=None):
    """Listener with a recording transport (no network)."""
    settings = settings or make_settings()

    def status_provider():
        return status if status is not None else {}

    async def check_runner(address, chain):
        return None if check_result == "unset" else check_result

    context = CommandContext(
        storage=storage, settings=settings, status_provider=status_provider,
        check_runner=check_runner, learning_service=learning,
        executor=executor if executor is not None else DryRunExecutor(storage),
    )
    listener = TelegramCommandListener(
        "test-bot-token", CHAT_ID, context,
        rate_limiter=RateLimiter(1000.0, burst=100),
        now_func=lambda: NOW,
    )
    calls: list[tuple[str, dict | None, dict | None]] = []

    async def fake_get_json(path, params=None, *, cache_key=None, cache_ttl=None,
                            headers=None, json_body=None, error_status_as_json=None):
        calls.append((path, params, json_body))
        if "getUpdates" in path:
            return {"ok": True, "result": []}
        return {"ok": True, "result": {}}

    listener._get_json = fake_get_json
    return listener, calls


def message_update(text, chat_id=CHAT_ID, update_id=1):
    return {"update_id": update_id,
            "message": {"chat": {"id": int(chat_id)}, "text": text}}


def callback_update(data, chat_id=CHAT_ID, update_id=1, message_id=None):
    message = {"chat": {"id": int(chat_id)}}
    if message_id is not None:
        message["message_id"] = message_id
    return {"update_id": update_id,
            "callback_query": {"id": "cbq1", "data": data, "message": message}}


def edited_message_update(text, chat_id=CHAT_ID, update_id=1):
    return {"update_id": update_id,
            "edited_message": {"chat": {"id": int(chat_id)}, "text": text}}


def sent_messages(calls):
    return [body for path, _, body in calls if "sendMessage" in path]


def callback_answers(calls):
    return [body for path, _, body in calls if "answerCallbackQuery" in path]


# ---- Address validation (D3) ----

def test_classify_address_accepts_solana_and_evm_only():
    assert classify_address(SOL_ADDR) == "solana"
    assert classify_address(EVM_ADDR) == "evm"
    assert classify_address("DROP TABLE tokens") is None
    assert classify_address("0xZZ" + "a" * 38) is None
    assert classify_address("short") is None
    assert classify_address(None) is None


# ---- Auth (D3): strangers get silence ----

async def test_foreign_chat_is_ignored_silently():
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage)
        await listener._handle_update(message_update("/status", chat_id="999"))
        await listener._handle_update(callback_update("fb:1:" + SOL_ADDR, chat_id="999"))
        assert sent_messages(calls) == []       # no reply to the stranger
        assert callback_answers(calls) == []    # not even a button ack
        assert storage.feedback_summary() == {"up": 0, "down": 0}


# ---- Commands ----

async def test_status_renders_snapshot():
    status = {
        "uptime_seconds": 3700.0, "cycles": 42,
        "last_cycle": {"pools_seen": 30, "candidates": 5, "analyzed": 3,
                       "launches_tracked": 2, "learned": 3, "alerts": 1},
        "networks": ["solana"],
        "layers": {"wallet_intel": True, "ai": False, "learning": True,
                   "pumpfun": False, "jupiter_probe": True, "buy_button": False,
                   "trading_live": False},
        "db": {"tokens": 10, "alerts": 4, "watchlist": 3, "holdings": 1},
    }
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage, status=status)
        await listener._handle_update(message_update("/status"))
    text = sent_messages(calls)[0]["text"]
    assert "uptime 1h 1m" in text and "cycles 42" in text
    assert "3 analyzed" in text and "jupiter probe ON" in text
    assert "trading off" in text and "1 holdings" in text


async def test_why_unknown_token_suggests_check():
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage)
        await listener._handle_update(message_update(f"/why {SOL_ADDR}"))
    assert "Never analyzed" in sent_messages(calls)[0]["text"]


async def test_why_renders_alerts_flags_and_copy_button():
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        storage.upsert_token(TOKEN)
        event = SimpleNamespace(
            priority=SimpleNamespace(value="high"), alert_type="high_priority_opportunity",
            token=TOKEN, title="Strong setup", reasons=("liquidity deep", "volume real"),
            scores={"overall": 88.0}, detected_at=NOW)
        storage.record_alert(event, source="test")
        storage.record_security_facts(TOKEN, {"is_honeypot": False,
                                              "live_sell_route_found": False,
                                              "live_buy_route_found": True})
        listener, calls = make_listener(storage)
        await listener._handle_update(message_update(f"/why {SOL_ADDR}"))
    body = sent_messages(calls)[0]
    assert "Strong setup" in body["text"] and "liquidity deep" in body["text"]
    assert "live sell route NOT found" in body["text"]
    # One-tap copy button rides along (operator request).
    assert body["reply_markup"]["inline_keyboard"][0][0]["copy_text"]["text"] == SOL_ADDR


async def test_why_rejects_bad_address():
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage)
        await listener._handle_update(message_update("/why notanaddress"))
    assert "does not look like a token address" in sent_messages(calls)[0]["text"]


def make_check_result():
    security = SimpleNamespace(
        overall_score=82.0, band="Good", tier=SimpleNamespace(value="acceptable_uncertainty"),
        destructive_findings=(), unknown_fields=("lp_locked_percent",))
    master = SimpleNamespace(final_score=74.0,
                             classification=SimpleNamespace(value="watchlist"),
                             coverage=0.8)
    pair = SimpleNamespace(
        base_token=TOKEN, price_usd=0.0001234, liquidity_usd=90_000.0,
        market_cap=400_000.0, volume_1h=8_000.0, buys_1h=40, sells_1h=15,
        pair_created_at=None)
    return SimpleNamespace(pair=pair, security=security, master=master,
                           momentum=None, security_profile=None)


async def test_check_runs_pipeline_and_attaches_copy_button():
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage, check_result=make_check_result())
        await listener._handle_update(message_update(f"/check {SOL_ADDR}"))
    body = sent_messages(calls)[0]
    assert "CHECK — MEMA" in body["text"]
    assert "security 82/100" in body["text"]
    assert "watchlist" in body["text"]
    # A healthy pool carries no lifecycle banner.
    assert "STATUS:" not in body["text"]
    assert body["reply_markup"]["inline_keyboard"][0][0]["copy_text"]["text"] == SOL_ADDR


# ---- /check lifecycle banner (operator request 2026-07-20: a rugged/dead
# coin was answering zone=early with no hint the pool was already gone) ----

async def test_check_dead_pool_shows_dead_status():
    result = make_check_result()
    result.pair.liquidity_usd = 200.0  # below the $500 dead floor
    result.momentum = SimpleNamespace(
        overall_score=38.0, entry_zone=SimpleNamespace(value="early"))
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage, check_result=result)
        await listener._handle_update(message_update(f"/check {SOL_ADDR}"))
    text = sent_messages(calls)[0]["text"]
    assert "STATUS: DEAD" in text
    # No destructive finding -> no invented rug cause (Rule 8).
    assert "RUGGED" not in text
    # The entry zone is annotated so "early" can't read as an entry signal.
    assert "zone=early (stale — pool is dead)" in text


async def test_check_dead_pool_with_blocked_exit_shows_rugged():
    result = make_check_result()
    result.pair.liquidity_usd = 12.0
    result.security_profile = SimpleNamespace(live_sell_route_found=False)
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage, check_result=result)
        await listener._handle_update(message_update(f"/check {SOL_ADDR}"))
    assert "STATUS: RUGGED" in sent_messages(calls)[0]["text"]


async def test_check_dead_pool_with_destructive_finding_shows_rugged():
    result = make_check_result()
    result.pair.liquidity_usd = 12.0
    result.security = SimpleNamespace(
        overall_score=5.0, band="Critical", tier=SimpleNamespace(value="high_danger"),
        destructive_findings=(SimpleNamespace(message="honeypot: cannot sell"),),
        unknown_fields=())
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage, check_result=result)
        await listener._handle_update(message_update(f"/check {SOL_ADDR}"))
    assert "STATUS: RUGGED" in sent_messages(calls)[0]["text"]


async def test_check_unknown_liquidity_gets_no_lifecycle_banner():
    result = make_check_result()
    result.pair.liquidity_usd = None  # absent data is never a conclusion (Rule 8)
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage, check_result=result)
        await listener._handle_update(message_update(f"/check {SOL_ADDR}"))
    assert "STATUS:" not in sent_messages(calls)[0]["text"]


async def test_check_crashed_price_with_intact_pool_shows_dumped():
    # The operator's live case (DrFy…pump): -86% in 24h, $4.9K liquidity
    # still in the pool — dead to a trader, invisible to the drained-pool rule.
    result = make_check_result()
    result.pair.liquidity_usd = 4_944.0
    result.pair.price_change_24h = -86.13
    result.momentum = SimpleNamespace(
        overall_score=38.0, entry_zone=SimpleNamespace(value="early"))
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage, check_result=result)
        await listener._handle_update(message_update(f"/check {SOL_ADDR}"))
    text = sent_messages(calls)[0]["text"]
    assert "STATUS: DUMPED" in text and "pool still holds" in text
    assert "zone=early (stale — coin already dumped)" in text


async def test_check_moderate_drop_gets_no_dumped_banner():
    result = make_check_result()
    result.pair.price_change_24h = -35.0  # a red day, not a corpse
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage, check_result=result)
        await listener._handle_update(message_update(f"/check {SOL_ADDR}"))
    assert "STATUS:" not in sent_messages(calls)[0]["text"]


async def test_check_dumped_banner_off_when_threshold_zero():
    result = make_check_result()
    result.pair.price_change_24h = -99.0
    settings = make_settings(MEMEINTEL_ALERTS_CHECK_DUMPED_DROP_PERCENT="0")
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage, settings=settings,
                                        check_result=result)
        await listener._handle_update(message_update(f"/check {SOL_ADDR}"))
    assert "STATUS:" not in sent_messages(calls)[0]["text"]


async def test_check_dead_floor_takes_precedence_over_dumped():
    result = make_check_result()
    result.pair.liquidity_usd = 100.0   # drained pool
    result.pair.price_change_24h = -99.0
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage, check_result=result)
        await listener._handle_update(message_update(f"/check {SOL_ADDR}"))
    text = sent_messages(calls)[0]["text"]
    assert "STATUS: DEAD" in text and "DUMPED" not in text


async def test_check_none_result_reports_no_data():
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage, check_result=None)
        await listener._handle_update(message_update(f"/check {SOL_ADDR}"))
    assert "No data" in sent_messages(calls)[0]["text"]


async def test_check_caches_result():
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage, check_result=make_check_result())
        await listener._handle_update(message_update(f"/check {SOL_ADDR}", update_id=1))
        await listener._handle_update(message_update(f"/check {SOL_ADDR}", update_id=2))
    texts = [m["text"] for m in sent_messages(calls)]
    assert len(texts) == 2 and "cached result" in texts[1]


async def test_check_rejects_bad_chain():
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage, check_result=make_check_result())
        await listener._handle_update(message_update(f"/check {SOL_ADDR} bad;chain"))
    assert "Invalid chain id" in sent_messages(calls)[0]["text"]


async def test_holding_unhold_and_holdings_list():
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage)
        await listener._handle_update(message_update(f"/holding {SOL_ADDR}"))
        assert "Holding recorded" in sent_messages(calls)[-1]["text"]
        assert storage.is_holding(TelegramTokenLookup(storage))
        # Review finding 2026-07-30: rechecks iterate the watchlist, so
        # /holding must also ensure the coin is watched — otherwise a held
        # coin the bot never scanned gets no protective monitoring at all.
        watched = {e.token.address for e in storage.get_watchlist()}
        assert SOL_ADDR in watched
        await listener._handle_update(message_update("/holdings"))
        assert "HOLDINGS (1 active)" in sent_messages(calls)[-1]["text"]
        await listener._handle_update(message_update(f"/unhold {SOL_ADDR}"))
        assert "Holding released" in sent_messages(calls)[-1]["text"]
        await listener._handle_update(message_update("/holdings"))
        assert "No active holdings" in sent_messages(calls)[-1]["text"]


def TelegramTokenLookup(storage):
    """The listener stores holdings under the token identity it resolved."""
    token = storage.find_token(SOL_ADDR)
    return token if token is not None else TokenIdentity(chain="solana", address=SOL_ADDR)


async def test_mute_suppresses_and_unmute_restores():
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage)
        await listener._handle_update(message_update(f"/mute {SOL_ADDR}"))
        assert "Muted" in sent_messages(calls)[-1]["text"]
        assert storage.is_muted(TelegramTokenLookup(storage))
        await listener._handle_update(message_update(f"/unmute {SOL_ADDR}"))
        assert "Unmuted" in sent_messages(calls)[-1]["text"]
        assert not storage.is_muted(TelegramTokenLookup(storage))


async def test_mind_off_reports_flag_and_feedback():
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        storage.record_feedback(TOKEN, "up")
        storage.record_feedback(TOKEN, "down")
        storage.record_feedback(TOKEN, "up")
        listener, calls = make_listener(storage, learning=None)
        await listener._handle_update(message_update("/mind"))
    text = sent_messages(calls)[0]["text"]
    assert "Mind layer is off" in text and "2 up / 1 down" in text


async def test_mind_renders_metrics():
    class FakeLearning:
        def get_learning_metrics(self, *, persist=True):
            assert persist is False
            return {"analog_memory_size": 12, "resolved_count": 4,
                    "directional": {"hit_rate": 0.75, "samples": 4},
                    "rug": {"precision": 1.0, "recall": 0.5},
                    "classifier_ready": False}

    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage, learning=FakeLearning())
        await listener._handle_update(message_update("/mind"))
    text = sent_messages(calls)[0]["text"]
    assert "memory: 12 coins" in text and "hit rate: 0.75" in text


async def test_unknown_command_returns_help():
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage)
        await listener._handle_update(message_update("/blorp"))
    assert "Commands:" in sent_messages(calls)[0]["text"]


async def test_handler_exception_is_contained():
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage)

        async def boom(args):
            raise RuntimeError("kaboom")

        listener._handlers["/status"] = boom
        await listener._handle_update(message_update("/status"))
    assert "Command failed" in sent_messages(calls)[0]["text"]


# ---- Callback queries: feedback + buy scaffold ----

async def test_thumbs_up_records_advisory_feedback_and_acks():
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        storage.upsert_token(TOKEN)
        event = SimpleNamespace(
            priority=SimpleNamespace(value="high"), alert_type="high_priority_opportunity",
            token=TOKEN, title="t", reasons=(), scores={}, detected_at=NOW)
        alert_id = storage.record_alert(event, source="test")
        listener, calls = make_listener(storage)
        await listener._handle_update(callback_update(f"fb:1:{SOL_ADDR}"))

        assert storage.feedback_summary() == {"up": 1, "down": 0}
        rows = storage.feedback_for_token(TelegramTokenLookup(storage))
        assert rows[0]["alert_id"] == alert_id       # linked to the recent alert
        acks = callback_answers(calls)
        assert acks and "Feedback recorded" in acks[0]["text"]
        # Advisory only: the measured-outcome column stays untouched (Rule 8).
        assert storage.alert_history(TOKEN, limit=1)[0]["outcome"] is None


async def test_thumbs_down_without_alert_stands_alone():
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage)
        await listener._handle_update(callback_update(f"fb:0:{SOL_ADDR}"))
        assert storage.feedback_summary() == {"up": 0, "down": 1}


async def test_malformed_feedback_payload_rejected():
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage)
        await listener._handle_update(callback_update("fb:2:" + SOL_ADDR))
        await listener._handle_update(callback_update("fb:1:garbage!!"))
        assert storage.feedback_summary() == {"up": 0, "down": 0}
    assert all("Invalid" in a["text"] for a in callback_answers(calls))


async def test_buy_with_flag_off_is_a_hard_noop():
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage)  # defaults: buy_button_enabled False
        await listener._handle_update(callback_update(f"buy:{SOL_ADDR}:0.05"))
        acks = callback_answers(calls)
        assert acks and "off" in acks[0]["text"].lower()
        assert sent_messages(calls) == []                       # no reply message
        assert storage.journal_entries(limit=10) == []          # nothing journaled


async def test_buy_button_on_routes_to_dry_run_when_not_live():
    settings = make_settings(MEMEINTEL_EXECUTION_BUY_BUTTON_ENABLED="true")
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage, settings=settings)  # DryRunExecutor
        await listener._handle_update(callback_update(f"buy:{SOL_ADDR}:0.05"))
        replies = sent_messages(calls)
        assert len(replies) == 1                    # exactly one chat message, not two
        assert "DRY RUN — no real trade executed" in replies[0]["text"]
        assert "0.05 SOL" in replies[0]["text"]
        acks = callback_answers(calls)
        # The popup ack points to the chat for the real outcome; it must NOT
        # assert "Buy sent" (which would contradict a refusal/failure reply).
        assert acks and "see chat" in acks[0]["text"].lower()
        journal = storage.journal_entries(limit=10)
        assert journal and journal[0]["kind"] == "trade_intent"


async def test_dump_button_off_is_refused():
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage)  # buttons off
        await listener._handle_update(callback_update(f"dump:{SOL_ADDR}"))
        assert sent_messages(calls) == []
        acks = callback_answers(calls)
        assert acks and "off" in acks[0]["text"].lower()


async def test_dump_button_on_routes_to_dry_run_when_not_live():
    settings = make_settings(MEMEINTEL_EXECUTION_BUY_BUTTON_ENABLED="true")
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage, settings=settings)
        await listener._handle_update(callback_update(f"dump:{SOL_ADDR}"))
        replies = sent_messages(calls)
        assert replies and "DRY RUN" in replies[0]["text"]


# ---- Idempotency: edited messages and double-tapped buttons (bug-hunt 2026-07-12) ----

async def test_edited_message_is_not_re_executed_as_a_trade():
    """Bug-hunt: editing a prior '/buy ...' message arrives as `edited_message`.
    The dispatcher used to fall back to it, so a single edit fired a SECOND
    real trade with no new user intent. Edits are now ignored outright."""
    settings = make_settings(MEMEINTEL_EXECUTION_BUY_BUTTON_ENABLED="true")
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage, settings=settings)
        await listener._handle_update(edited_message_update(f"/buy {SOL_ADDR} 0.1"))
        assert sent_messages(calls) == []               # no reply
        assert storage.journal_entries(limit=10) == []  # NOT executed


async def test_double_tap_same_buy_button_fires_once():
    """Bug-hunt: two taps of the SAME inline buy button (same message_id +
    data) must execute exactly one trade; the repeat is rejected with an
    'already actioned' ack."""
    settings = make_settings(MEMEINTEL_EXECUTION_BUY_BUTTON_ENABLED="true")
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage, settings=settings)
        update = callback_update(f"buy:{SOL_ADDR}:0.05", message_id=42)
        await listener._handle_update(update)
        await listener._handle_update(update)           # exact same button, tapped twice
        # Exactly one chat reply and one trade_intent journaled.
        assert len(sent_messages(calls)) == 1
        trade_intents = [e for e in storage.journal_entries(limit=10)
                         if e["kind"] == "trade_intent"]
        assert len(trade_intents) == 1
        acks = callback_answers(calls)
        assert len(acks) == 2                            # both taps get a popup
        assert "already actioned" in acks[1]["text"].lower()


async def test_double_tap_same_dump_button_fires_once():
    """Same double-tap guard for the dump button."""
    settings = make_settings(MEMEINTEL_EXECUTION_BUY_BUTTON_ENABLED="true")
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage, settings=settings)
        update = callback_update(f"dump:{SOL_ADDR}", message_id=77)
        await listener._handle_update(update)
        await listener._handle_update(update)
        assert len(sent_messages(calls)) == 1
        acks = callback_answers(calls)
        assert "already actioned" in acks[1]["text"].lower()


async def test_taps_on_different_buttons_both_fire():
    """The dedup key is (message_id, data): two DISTINCT buttons (different
    messages) must each fire -- the guard must not collapse unrelated taps."""
    settings = make_settings(MEMEINTEL_EXECUTION_BUY_BUTTON_ENABLED="true")
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage, settings=settings)
        await listener._handle_update(callback_update(f"buy:{SOL_ADDR}:0.05", message_id=1))
        await listener._handle_update(callback_update(f"buy:{SOL_ADDR}:0.05", message_id=2))
        assert len(sent_messages(calls)) == 2           # both distinct buttons fired


async def test_button_without_message_id_fails_open():
    """A callback lacking a numeric message_id can't be deduped; it must fail
    OPEN (execute) rather than silently swallow a legitimate trade."""
    settings = make_settings(MEMEINTEL_EXECUTION_BUY_BUTTON_ENABLED="true")
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage, settings=settings)
        await listener._handle_update(callback_update(f"buy:{SOL_ADDR}:0.05"))  # no message_id
        assert len(sent_messages(calls)) == 1           # executed, not dropped


async def test_buy_command_parses_amount():
    settings = make_settings(MEMEINTEL_EXECUTION_BUY_BUTTON_ENABLED="true")
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage, settings=settings)
        await listener._handle_update(message_update(f"/buy {SOL_ADDR} 0.1"))
        replies = sent_messages(calls)
        assert len(replies) == 1                    # exactly one reply, not two (bug fix)
        assert "0.1 SOL" in replies[0]["text"]
        # A bad amount is rejected, not executed.
        await listener._handle_update(message_update(f"/buy {SOL_ADDR} lots"))
        assert "must be a number" in sent_messages(calls)[-1]["text"]


async def test_dump_command_sends_exactly_one_reply():
    settings = make_settings(MEMEINTEL_EXECUTION_BUY_BUTTON_ENABLED="true")
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage, settings=settings)
        await listener._handle_update(message_update(f"/dump {SOL_ADDR}"))
        replies = sent_messages(calls)
        assert len(replies) == 1
        assert "DRY RUN" in replies[0]["text"]


async def test_buy_command_guard_off_gives_single_reply_no_trade():
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage)  # buttons off by default
        await listener._handle_update(message_update(f"/buy {SOL_ADDR} 0.1"))
        replies = sent_messages(calls)
        assert len(replies) == 1
        assert "off" in replies[0]["text"].lower()
        assert storage.journal_entries(limit=10) == []


# ---- Poll loop mechanics ----

async def test_offset_advances_past_all_updates():
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage)
        batches = [
            {"ok": True, "result": [message_update("/help", update_id=5),
                                    message_update("/help", update_id=7)]},
            {"ok": True, "result": []},
        ]
        seen_params = []

        async def fake_get_json(path, params=None, *, cache_key=None, cache_ttl=None,
                                headers=None, json_body=None, error_status_as_json=None):
            if "getUpdates" in path:
                seen_params.append(dict(params or {}))
                return batches.pop(0)
            return {"ok": True, "result": {}}

        listener._get_json = fake_get_json
        await listener._poll_once()
        await listener._poll_once()
    assert "offset" not in seen_params[0]
    assert seen_params[1]["offset"] == "8"  # max(update_id) + 1


async def test_poll_loop_backs_off_on_errors_and_recovers():
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage)
        sleeps: list[float] = []
        outcomes = [CollectorError("boom"), CollectorError("boom"), None]

        async def fake_poll_once():
            action = outcomes.pop(0)
            if action is not None:
                raise action
            return 0

        async def fake_sleep(seconds):
            sleeps.append(seconds)
            if not outcomes:               # after the recovery poll: stop
                listener._stopping = True

        listener._poll_once = fake_poll_once
        listener._sleep = fake_sleep
        await listener._poll_forever()
    # Two error backoffs (2s then 4s), then the idle delay after success.
    assert sleeps[0] == 2.0 and sleeps[1] == 4.0
    assert sleeps[2] == listener._idle_delay


async def test_poison_update_does_not_stop_the_batch():
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage)
        batch = {"ok": True, "result": [
            {"update_id": 1, "message": {"chat": {"id": int(CHAT_ID)}}},  # no text
            "garbage",
            message_update("/help", update_id=3),
        ]}

        async def fake_get_json(path, params=None, *, cache_key=None, cache_ttl=None,
                                headers=None, json_body=None, error_status_as_json=None):
            calls.append((path, params, json_body))
            if "getUpdates" in path:
                return batch
            return {"ok": True, "result": {}}

        listener._get_json = fake_get_json
        await listener._poll_once()
    assert listener._offset == 4
    assert any("Commands:" in m["text"] for m in sent_messages(calls))


async def test_start_and_stop_lifecycle():
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage)
        await listener.start()
        task = listener._task
        assert task is not None and not task.done()
        await listener.start()                 # idempotent
        assert listener._task is task
        await listener.stop()
        assert listener._task is None


# ---- Project 3: /mind shows the veto report card line ----

async def test_mind_shows_veto_authority_not_earned():
    class ColdLearning:
        def get_learning_metrics(self, *, persist=True):
            return {"analog_memory_size": 5, "resolved_count": 2,
                    "directional": {"hit_rate": None, "samples": 2},
                    "rug": {"precision": None, "true_positives": 0,
                            "false_positives": 0},
                    "classifier_ready": False}

    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage, learning=ColdLearning())
        await listener._handle_update(message_update("/mind"))
    text = sent_messages(calls)[0]["text"]
    assert "p(rug) veto: off" in text
    assert "not earned yet" in text


async def test_mind_shows_veto_authority_earned_and_flag_state():
    class ProvenLearning:
        def get_learning_metrics(self, *, persist=True):
            return {"analog_memory_size": 500, "resolved_count": 60,
                    "directional": {"hit_rate": 0.7, "samples": 60},
                    "rug": {"precision": 0.82, "true_positives": 14,
                            "false_positives": 3},
                    "classifier_ready": True}

    settings = make_settings(MEMEINTEL_LEARNING_VETO_ENABLED="true")
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage, settings=settings,
                                        learning=ProvenLearning())
        await listener._handle_update(message_update("/mind"))
    text = sent_messages(calls)[0]["text"]
    assert "p(rug) veto: ON" in text
    assert "EARNED — rug precision 0.82 over 17 graded rug calls" in text


# ---- /winners: read-only winners-vs-died comparison (2026-07-28) ----


def _winners_learning():
    """Learning-service double exposing only what /winners touches: .store."""
    from meme_intelligence.learning.models import (
        CoinRecord,
        CoinSnapshot,
        OutcomeBucket,
        OutcomeLabel,
    )

    def record(address, bucket, holders):
        return CoinRecord(
            token=TokenIdentity(chain="solana", address=address, symbol=address[:4]),
            detected_at=NOW, detection_price_usd=0.001,
            snapshots=(CoinSnapshot(age_seconds=30, holder_count=holders,
                                    liquidity_usd=10_000.0),),
            labels=(OutcomeLabel(horizon_hours=6.0, bucket=bucket,
                                 forward_return_percent=200.0),),
        )

    class FakeStore:
        def __init__(self):
            self.calls: list = []

        def resolved_records(self, *, limit=None, bucket=None):
            self.calls.append((limit, bucket))
            if bucket is OutcomeBucket.PUMP:
                return [record(f"W{i}", bucket, 100) for i in range(6)]
            if bucket is OutcomeBucket.RUG:
                return [record(f"L{i}", bucket, 10) for i in range(6)]
            return []

    return SimpleNamespace(store=FakeStore())


async def test_winners_off_when_learning_disabled():
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage, learning=None)
        await listener._handle_update(message_update("/winners"))
    assert "Mind layer is off" in sent_messages(calls)[0]["text"]


async def test_winners_renders_comparison_and_caches():
    learning = _winners_learning()
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage, learning=learning)
        await listener._handle_update(message_update("/winners"))
        first = sent_messages(calls)[0]["text"]
        assert "WINNERS REPORT" in first
        assert "Holders: 100 vs 10" in first
        assert "changes nothing about scanning or alerts" in first
        store_walks = len(learning.store.calls)

        await listener._handle_update(message_update("/winners", update_id=2))
        second = sent_messages(calls)[1]["text"]
        assert "cached result" in second
        # The cached tap must not re-walk the store (Rule 11).
        assert len(learning.store.calls) == store_walks


def _dev_learning():
    """Learning double exposing the three /dev store primitives."""
    from meme_intelligence.learning.models import OutcomeBucket

    class FakeStore:
        def creator_of(self, token):
            return "devWallet" if token.address == SOL_ADDR else None

        def coins_by_creator(self, creator, chain, *, limit=30):
            if creator != "devWallet":
                return []
            return [
                {"address": "CoinA", "symbol": "AAA",
                 "final_bucket": OutcomeBucket.RUG, "detected_at": "2026-07-27"},
                {"address": "CoinB", "symbol": "BBB",
                 "final_bucket": OutcomeBucket.RUG, "detected_at": "2026-07-26"},
                {"address": "CoinC", "symbol": None,
                 "final_bucket": None, "detected_at": "2026-07-28"},
            ]

        def blacklist_entry(self, creator, chain):
            return (2, "2026-07-27T14:02:00") if creator == "devWallet" else None

    return SimpleNamespace(store=FakeStore())


async def test_dev_resolves_coin_to_deployer_rap_sheet():
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage, learning=_dev_learning())
        await listener._handle_update(message_update(f"/dev {SOL_ADDR}"))
    text = sent_messages(calls)[0]["text"]
    assert "DEVELOPER" in text and "deployer of" in text
    assert "coins the bot watched from this wallet: 3" in text
    assert "2 rug | 0 dump | 0 flat | 0 pump" in text
    assert "1 still playing out" in text
    assert "confirmed-rug blacklist: 2 rug(s) on record" in text
    assert "auto-vetoed already" in text


async def test_dev_unknown_address_reports_honestly():
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage, learning=_dev_learning())
        await listener._handle_update(message_update(f"/dev {EVM_ADDR} ethereum"))
    text = sent_messages(calls)[0]["text"]
    assert "No deployer on record" in text


async def test_dev_off_when_learning_disabled():
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage, learning=None)
        await listener._handle_update(message_update(f"/dev {SOL_ADDR}"))
    assert "Mind layer is off" in sent_messages(calls)[0]["text"]


async def test_winners_failure_degrades_without_crashing():
    class BrokenStore:
        def resolved_records(self, *, limit=None, bucket=None):
            raise RuntimeError("db exploded")

    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(
            storage, learning=SimpleNamespace(store=BrokenStore()))
        await listener._handle_update(message_update("/winners"))
    text = sent_messages(calls)[0]["text"]
    assert "unavailable" in text and "nothing was changed" in text


# ---- Project 2 fix: startup backlog is discarded, not replayed ----

async def test_startup_discards_pending_backlog_without_handling():
    """A restart must not replay commands Telegram buffered while we were down:
    the backlog is drained (offset advanced) but NO handler runs."""
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage)
        batches = [
            {"ok": True, "result": [callback_update(f"fb:1:{SOL_ADDR}", update_id=10),
                                    message_update(f"/holding {SOL_ADDR}", update_id=11)]},
            {"ok": True, "result": []},   # drained
        ]

        async def fake_get_json(path, params=None, *, cache_key=None, cache_ttl=None,
                                headers=None, json_body=None, error_status_as_json=None):
            calls.append((path, params, json_body))
            return batches.pop(0)

        listener._get_json = fake_get_json
        await listener._discard_backlog()

        assert listener._offset == 12                # advanced past the whole backlog
        assert storage.feedback_summary() == {"up": 0, "down": 0}   # feedback NOT replayed
        assert storage.get_holdings(active_only=True) == []         # /holding NOT replayed
        assert sent_messages(calls) == []                           # no replies emitted

# ---- Bug-hunt fixes (2026-07-11) ----

async def test_backlog_drain_retries_and_never_falls_through_to_live(monkeypatch):
    """If the first drain poll fails transiently, _poll_forever must RETRY the
    drain — not fall through to live polling with the backlog still pending
    (which would replay a buffered /buy as a real trade)."""
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage)
        # First drain poll raises; second returns a pending /holding backlog;
        # third drains clean. Live polling then runs once and stops.
        drain_batches = [
            CollectorError("transient getUpdates failure"),
            {"ok": True, "result": [message_update(f"/holding {SOL_ADDR}", update_id=20)]},
            {"ok": True, "result": []},
        ]

        async def fake_get_json(path, params=None, *, cache_key=None, cache_ttl=None,
                                headers=None, json_body=None, error_status_as_json=None):
            calls.append((path, params, json_body))
            item = drain_batches.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        live_polls = []

        async def fake_poll_once():
            live_polls.append(True)
            listener._stopping = True
            return 0

        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        listener._get_json = fake_get_json
        listener._poll_once = fake_poll_once
        listener._sleep = fake_sleep
        await listener._poll_forever()

        # The transient failure was retried (one backoff sleep), the backlog
        # was fully drained (offset past update_id 20), the buffered /holding
        # was NOT handled, and live polling only started after the drain
        # succeeded.
        assert sleeps and sleeps[0] == 2.0
        assert listener._offset == 21
        assert storage.get_holdings(active_only=True) == []   # /holding never handled
        assert len(live_polls) == 1


async def test_malformed_drain_response_is_not_treated_as_drained():
    """An ok:false / malformed getUpdates payload must raise (so the drain is
    retried), not be mistaken for an empty, fully-drained backlog."""
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage)

        async def fake_get_json(path, params=None, *, cache_key=None, cache_ttl=None,
                                headers=None, json_body=None, error_status_as_json=None):
            return {"ok": False, "description": "flood wait"}

        listener._get_json = fake_get_json
        raised = False
        try:
            await listener._discard_backlog()
        except CollectorError:
            raised = True
        assert raised
        assert listener._offset is None   # offset NOT advanced past an unknown backlog


async def test_non_finite_buy_amount_is_refused_before_executor():
    settings = make_settings(MEMEINTEL_EXECUTION_BUY_BUTTON_ENABLED="true")
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage, settings=settings)
        for bad in ("nan", "inf", "-inf", "0", "-1"):
            await listener._handle_update(message_update(f"/buy {SOL_ADDR} {bad}"))
        # Every non-finite / non-positive amount is refused with a clean
        # message; NONE reach the executor (no dry-run intent journaled).
        assert storage.journal_entries(limit=10) == []
        assert all("positive number" in m["text"] for m in sent_messages(calls))


async def test_non_finite_buy_amount_button_is_refused():
    settings = make_settings(MEMEINTEL_EXECUTION_BUY_BUTTON_ENABLED="true")
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage, settings=settings)
        await listener._handle_update(callback_update(f"buy:{SOL_ADDR}:nan"))
        assert sent_messages(calls) == []                 # no trade reply
        assert storage.journal_entries(limit=10) == []    # nothing journaled
        acks = callback_answers(calls)
        assert acks and "Invalid buy amount" in acks[0]["text"]


async def test_dump_button_ack_does_not_claim_sent_on_refusal():
    """The popup ack must not assert a trade happened; the chat reply carries
    the real outcome (here a dry-run notice)."""
    settings = make_settings(MEMEINTEL_EXECUTION_BUY_BUTTON_ENABLED="true")
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage, settings=settings)
        await listener._handle_update(callback_update(f"dump:{SOL_ADDR}"))
        acks = callback_answers(calls)
        assert acks and "sent" not in acks[0]["text"].lower()
        assert "see chat" in acks[0]["text"].lower()


# ---- /boost — DexScreener paid-boost lookup (Project 5) ----

async def test_boost_command_reports_amount():
    from meme_intelligence.collectors.market_data import TokenBoost
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, _ = make_listener(storage)

        async def fake_boost(address, chain=None):
            return TokenBoost(chain="solana", token_address=address, total_amount=600.0,
                              url="https://dexscreener.com/solana/x")

        listener._ctx.boost_lookup = fake_boost
        reply = await listener._cmd_boost([SOL_ADDR])
        assert "600" in reply
        # the reply must carry the honest "paid promotion, not endorsement" caveat
        assert "endorsement" in reply.lower()


async def test_boost_command_no_active_boost():
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, _ = make_listener(storage)

        async def fake_boost(address, chain=None):
            return None

        listener._ctx.boost_lookup = fake_boost
        reply = await listener._cmd_boost([SOL_ADDR])
        assert "No active DexScreener boost" in reply


async def test_boost_command_unavailable_when_not_wired():
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, _ = make_listener(storage)  # boost_lookup defaults to None
        reply = await listener._cmd_boost([SOL_ADDR])
        assert "unavailable" in reply.lower()


async def test_boost_command_survives_lookup_error():
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, _ = make_listener(storage)

        async def boom(address, chain=None):
            raise RuntimeError("dexscreener down")

        listener._ctx.boost_lookup = boom
        reply = await listener._cmd_boost([SOL_ADDR])
        assert "try again" in reply.lower()   # never raises into the poll loop


# ---- Percentage-of-balance buy buttons (2026-07-18, operator request) ----

class FakeBalanceExecutor:
    """Executor stand-in with a controllable spendable balance, so the
    percentage-sizing math can be verified independent of a real wallet."""

    def __init__(self, balance):
        self._balance = balance
        self.buy_calls = []

    async def get_spendable_balance_sol(self):
        return self._balance

    async def execute_buy(self, intent):
        self.buy_calls.append(intent.sol_amount)
        return f"DRY RUN — would buy {intent.sol_amount:g} SOL of {intent.token_address}."

    async def execute_sell_all(self, mint, chain="solana"):
        return f"DRY RUN — would dump {mint}."


async def test_percent_buy_button_sizes_against_live_balance():
    settings = make_settings(MEMEINTEL_EXECUTION_BUY_BUTTON_ENABLED="true")
    executor = FakeBalanceExecutor(balance=2.0)
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage, settings=settings, executor=executor)
        await listener._handle_update(callback_update(f"buy:{SOL_ADDR}:pct:50"))
    assert executor.buy_calls == [1.0]                  # 50% of 2.0 SOL
    replies = sent_messages(calls)
    assert replies and "1 SOL" in replies[0]["text"]


async def test_percent_buy_button_100_percent_uses_full_spendable_balance():
    settings = make_settings(MEMEINTEL_EXECUTION_BUY_BUTTON_ENABLED="true")
    executor = FakeBalanceExecutor(balance=0.993)  # already fee-buffer-adjusted
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage, settings=settings, executor=executor)
        await listener._handle_update(callback_update(f"buy:{SOL_ADDR}:pct:100"))
    assert len(executor.buy_calls) == 1 and abs(executor.buy_calls[0] - 0.993) < 1e-9


async def test_percent_buy_button_unavailable_balance_refuses_cleanly():
    """DryRunExecutor honestly reports no balance (no real wallet) — the
    percentage button must refuse with a clear reason, never guess."""
    settings = make_settings(MEMEINTEL_EXECUTION_BUY_BUTTON_ENABLED="true")
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage, settings=settings)  # DryRunExecutor
        await listener._handle_update(callback_update(f"buy:{SOL_ADDR}:pct:50"))
    acks = callback_answers(calls)
    assert acks and "balance is unavailable" in acks[0]["text"].lower()
    assert sent_messages(calls) == []                    # no chat spam on refusal


async def test_percent_buy_button_zero_balance_refuses_cleanly():
    settings = make_settings(MEMEINTEL_EXECUTION_BUY_BUTTON_ENABLED="true")
    executor = FakeBalanceExecutor(balance=0.0)
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage, settings=settings, executor=executor)
        await listener._handle_update(callback_update(f"buy:{SOL_ADDR}:pct:50"))
    acks = callback_answers(calls)
    assert acks and "no spendable sol" in acks[0]["text"].lower()
    assert executor.buy_calls == []


async def test_percent_buy_button_rejects_out_of_range_percent():
    settings = make_settings(MEMEINTEL_EXECUTION_BUY_BUTTON_ENABLED="true")
    executor = FakeBalanceExecutor(balance=1.0)
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage, settings=settings, executor=executor)
        await listener._handle_update(callback_update(f"buy:{SOL_ADDR}:pct:0"))
        await listener._handle_update(callback_update(f"buy:{SOL_ADDR}:pct:150"))
    assert executor.buy_calls == []
    acks = callback_answers(calls)
    assert all("invalid buy percentage" in a["text"].lower() for a in acks)


async def test_percent_buy_button_off_is_a_hard_noop_before_balance_lookup():
    """The trading guard (buttons off) must short-circuit BEFORE any balance
    lookup — mirrors the legacy-format ordering."""
    executor = FakeBalanceExecutor(balance=5.0)
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage, executor=executor)  # buttons off
        await listener._handle_update(callback_update(f"buy:{SOL_ADDR}:pct:50"))
    assert executor.buy_calls == []
    acks = callback_answers(calls)
    assert acks and "off" in acks[0]["text"].lower()


async def test_percent_buy_double_tap_fires_once():
    settings = make_settings(MEMEINTEL_EXECUTION_BUY_BUTTON_ENABLED="true")
    executor = FakeBalanceExecutor(balance=1.0)
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage, settings=settings, executor=executor)
        update = callback_update(f"buy:{SOL_ADDR}:pct:50", message_id=99)
        await listener._handle_update(update)
        await listener._handle_update(update)
    assert executor.buy_calls == [0.5]                   # exactly one trade
    acks = callback_answers(calls)
    assert "already actioned" in acks[1]["text"].lower()


async def test_percent_buy_button_malformed_payload_rejected():
    settings = make_settings(MEMEINTEL_EXECUTION_BUY_BUTTON_ENABLED="true")
    executor = FakeBalanceExecutor(balance=1.0)
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage, settings=settings, executor=executor)
        await listener._handle_update(callback_update(f"buy:{SOL_ADDR}:pct:notanumber"))
        await listener._handle_update(callback_update(f"buy:{SOL_ADDR}:notpct:50"))
    assert executor.buy_calls == []




# ---- /rugwatch kill switch (operator request 2026-07-29) ----


class FakeGuard:
    def __init__(self, armed=True):
        self._armed = armed
        self.disarms = []

    def disarm(self, reason):
        self._armed = False
        self.disarms.append(reason)

    def status(self):
        return {"enabled": True, "auto_sell": self._armed,
                "watching": 2, "exited": 0}


async def _rugwatch(guard, text="/rugwatch"):
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage)
        listener._ctx = dataclasses.replace(listener._ctx, holdings_guard=guard)
        await listener._handle_update(message_update(text))
    return sent_messages(calls)[0]["text"]


async def test_rugwatch_reports_armed_state():
    text = await _rugwatch(FakeGuard(armed=True))
    assert "ARMED" in text and "2 position" in text


async def test_rugwatch_off_disarms_auto_sell():
    """The only thing that spends money without a tap must be stoppable from
    the phone — no SSH."""
    guard = FakeGuard(armed=True)
    text = await _rugwatch(guard, "/rugwatch off")
    assert guard.disarms and not guard._armed
    assert "DISARMED" in text
    assert "restart the service" in text      # re-arming is deliberately manual


async def test_rugwatch_explains_itself_when_not_running():
    text = await _rugwatch(None)
    assert "not running" in text
    assert "MEMEINTEL_RUG_WATCH_ENABLED" in text
