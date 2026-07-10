"""Tests for two-way Telegram control (Project 2, ROADMAP item 2)."""

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


def callback_update(data, chat_id=CHAT_ID, update_id=1):
    return {"update_id": update_id,
            "callback_query": {"id": "cbq1", "data": data,
                               "message": {"chat": {"id": int(chat_id)}}}}


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
    assert body["reply_markup"]["inline_keyboard"][0][0]["copy_text"]["text"] == SOL_ADDR


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
        assert replies and "DRY RUN — no real trade executed" in replies[0]["text"]
        assert "0.05 SOL" in replies[0]["text"]
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


async def test_buy_command_parses_amount():
    settings = make_settings(MEMEINTEL_EXECUTION_BUY_BUTTON_ENABLED="true")
    with Storage(":memory:", now_func=lambda: NOW) as storage:
        listener, calls = make_listener(storage, settings=settings)
        await listener._handle_update(message_update(f"/buy {SOL_ADDR} 0.1"))
        assert "0.1 SOL" in sent_messages(calls)[0]["text"]
        # A bad amount is rejected, not executed.
        await listener._handle_update(message_update(f"/buy {SOL_ADDR} lots"))
        assert "must be a number" in sent_messages(calls)[-1]["text"]


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
