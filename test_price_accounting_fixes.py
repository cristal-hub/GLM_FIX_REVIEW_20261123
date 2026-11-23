# tests/test_price_accounting_fixes.py
# ЗАДАЧИ A/B/C — исправление учёта цен BUY/SELL (Tsclp_v2.1_volatile_Samsung,
# фактический инцидент LSRG 23.11.2026: две покупки 23 лота по 858.8/859.8,
# цена входа в журнале 37.3391/37.3826 — занижение ровно в 23 раза).
#
# Основание: аудит фактических структур SDK T-Invest API (orders.proto,
# tinkoff.github.io/investAPI/orders и developer.tinkoff.ru, 23.11.2026):
#   - PostOrderResponse.executed_order_price (MoneyValue) — «Исполненная
#     СРЕДНЯЯ ЦЕНА ОДНОГО ИНСТРУМЕНТА в заявке» — ЦЕНА ЗА ШТУКУ;
#   - OrderState.executed_order_price (MoneyValue) — «Исполненная цена
#     заявки. Произведение средней цены покупки на количество лотов» —
#     СУММА исполнения;
#   - оба содержат executed_commission (MoneyValue) — фактическая комиссия;
#   - OrderState.average_position_price — «Средняя цена ПОЗИЦИИ по сделке» —
#     для цены исполнения заявки НЕ применимо (для SELL это цена входа).
# Одноимённые поля двух ответов имеют РАЗНУЮ семантику — один общий
# извлекатель цены с делением на лоты был причиной дефекта.
#
# Подтверждение по фактическим журналам 23.11.2026 (order_ledger.jsonl,
# research_events.jsonl): событие ORDER_STATE_UNKNOWN непосредственно перед
# POSITION_OPENED — регистрация BUY шла по PostOrderResponse (ветка
# main.py, state=None после FILL), где цена 858.8 делилась на 23 лота.
#
# Обязательные сценарии (задача D): только FakeClient/FakeBroker, без
# сетевых запросов и реальных заявок; стратегия, OrderLedger, P0-5,
# per-order cursor, fail-closed не затронуты.
#
# Запуск из корня сборки: pytest tests/test_price_accounting_fixes.py -v

import asyncio
import csv
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import logger as research_logger
from execution import ExecutionMonitor, PositionState, TrackedPosition
from order_ledger import OrderLedger

UTC = timezone.utc

_IMPORT_ERROR = None
try:
    import main
except Exception as exc:  # noqa: BLE001
    _IMPORT_ERROR = exc

pytestmark = pytest.mark.skipif(
    _IMPORT_ERROR is not None,
    reason=f"main.py недоступен: {_IMPORT_ERROR}",
)

_ORIG_IS_TRADING_ENABLED = main.is_trading_enabled
_ORIG_SAVE_STATE = main._save_state

# Коды статусов T-Invest (проверены по официальной документации):
FILL, REJECTED, CANCELLED, NEW, PARTIALLYFILL = 1, 2, 3, 4, 5


# ---------------------------------------------------------------- fakes

class _Money:
    """MoneyValue/Quotation: units + nano (единицы торговой валюты)."""
    def __init__(self, value):
        units = int(value)
        self.units = units
        self.nano = int(round((value - units) * 1e9))
        self.currency = "rub"


class _Stage:
    """OrderStage: price — цена за 1 инструмент, quantity — количество штук."""
    def __init__(self, price, quantity):
        self.price = _Money(price)
        self.quantity = quantity


class FakePostOrderResponse:
    """PostOrderResponse (ответ на отправку заявки): executed_order_price —
    ЦЕНА ЗА 1 ИНСТРУМЕНТ (семантика PostOrderResponse)."""
    def __init__(self, status_code, lots_executed, price_per_unit=None,
                 commission=None):
        self.execution_report_status = status_code
        self.lots_executed = lots_executed
        self.lots_requested = lots_executed
        self.executed_order_price = _Money(price_per_unit) if price_per_unit else None
        self.executed_commission = _Money(commission) if commission else None


class FakeOrderState:
    """OrderState (отчёт состояния заявки): executed_order_price — СУММА
    исполнения (произведение средней цены на количество лотов)."""
    def __init__(self, status_code, lots_executed, total_price=None,
                 commission=None, stages=None):
        self.execution_report_status = status_code
        self.lots_executed = lots_executed
        self.lots_requested = lots_executed
        self.executed_order_price = _Money(total_price) if total_price else None
        self.executed_commission = _Money(commission) if commission else None
        self.stages = stages or []


class PosData:
    """Минимальный аналог PositionData брокера."""
    def __init__(self, lots, direction):
        self.lots = lots
        self.direction = direction


class FakeBroker:
    """Фейковый брокер: никаких сетевых вызовов и реальных заявок.

    Состояния заявок хранятся по фактическому ключу: production опрашивает
    по EXCHANGE (биржевой ID) либо по REQUEST (клиентский UUID) — фейк
    возвращает заявку под тем ключом, под которым она зарегистрирована."""
    def __init__(self, positions=None, prices=None, post_response=None,
                 post_error=None):
        self.positions = positions if positions is not None else {}
        self.prices = prices if prices is not None else {}
        self.order_states = {}   # ключ (EXCHANGE- или REQUEST-id) -> OrderState
        self.post_response = post_response
        self.post_error = post_error
        self.orders_sent = []    # (figi, lots, direction, order_id)
        self.cancelled = []
        self.uncertain_state = False

    def is_uncertain(self):
        return self.uncertain_state

    async def get_positions(self):
        return self.positions  # None трактуется как недостоверный снимок

    async def get_last_prices(self, figis):
        return {f: self.prices[f] for f in figis if f in self.prices}

    async def get_instrument_info(self, figi):
        return {"lot": 1, "ticker": "TEST"}

    async def get_sandbox_order_state(self, oid, order_id_type=None):
        return self.order_states.get(str(oid))

    async def post_market_order(self, figi, lots, direction, order_id=None):
        self.orders_sent.append((figi, lots, str(direction), order_id))
        if self.post_error is not None:
            # потерянный ответ: state=None, error установлен
            return SimpleNamespace(order_id=order_id, state=None,
                                   exchange_order_id=None,
                                   order_request_id=None,
                                   error=self.post_error)
        exchange_id = f"EX-{order_id}" if order_id else None
        if exchange_id is not None:
            self.order_states[exchange_id] = self.post_response
        return SimpleNamespace(order_id=order_id, state=self.post_response,
                               exchange_order_id=exchange_id,
                               order_request_id=order_id, error=None)

    async def cancel_order(self, oid, order_id_type=None):
        self.cancelled.append(oid)
        return SimpleNamespace(status="OK")


class FakeRM:
    """Минимальный RiskManager: фиксированные SL/TP."""
    def __init__(self, sl=840.0, tp=920.0):
        self.sl = sl
        self.tp = tp
        self.recorded = []

    def calculate_sl_tp(self, price, direction, df):
        return self.sl, self.tp

    async def calculate_position_size(self, figi, price, df):
        return 1

    def record_trade(self, figi):
        self.recorded.append(figi)

    def export_state(self):
        return {"fake": True}


# ---------------------------------------------------------------- helpers

def make_buy_pos(**over):
    base = dict(
        figi="F1", ticker="TEST", direction="BUY", entry_price=858.8,
        quantity=23, sl_price=840.0, tp_price=920.0, lot_size=1,
        state=PositionState.OPEN, trade_id="t1",
        order_send_time=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        entry_order_id="buy-1",
    )
    base.update(over)
    return TrackedPosition(**base)


def proven_buy(ledger, figi="F1", lots=23):
    """Подтверждённая брокером BUY-заявка входа в реестре (терминальный FILL)."""
    assert ledger.record_intent("buy-1", trade_id="t1", figi=figi, ticker="TEST",
                                side="BUY", requested_lots=lots, reason="")
    assert ledger.record_sent("buy-1", figi=figi, ticker="TEST", side="BUY",
                              requested_lots=lots, executed_lots=lots,
                              status_hint="FILL", reason="", source="post_response")


def fresh_monitor(pos, client):
    monitor = ExecutionMonitor(client)
    monitor.positions[pos.figi] = pos
    return monitor


def run(coro):
    return asyncio.run(coro)


def read_trades(tmp_path):
    path = tmp_path / "research_trades.csv"
    with open(path, "r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f, delimiter=";"))


def read_events(tmp_path):
    path = tmp_path / "research_events.jsonl"
    if not path.is_file():
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Изолированная среда: все записи в tmp_path; торговля выключена."""
    monkeypatch.setattr(main, "is_trading_enabled", _ORIG_IS_TRADING_ENABLED)
    monkeypatch.setattr(main, "_save_state", _ORIG_SAVE_STATE)
    monkeypatch.setattr(research_logger, "EVENTS_FILE", str(tmp_path / "research_events.jsonl"))
    monkeypatch.setattr(research_logger, "TRADES_FILE", str(tmp_path / "research_trades.csv"))
    monkeypatch.setattr(main, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(main, "STATE_FILE_TMP", str(tmp_path / "state.json.tmp"))
    monkeypatch.setattr(main, "TRADING_FLAG", str(tmp_path / "trading.flag"))
    monkeypatch.setattr(config, "RECONCILE_CONFIRM_MIN_AGE_SECONDS", 30.0)
    monkeypatch.setattr(main, "_external_close_candidates", {})
    monkeypatch.setattr(main, "reconcile_done", False)
    monkeypatch.setattr(main, "state_save_failed", False)
    monkeypatch.setattr(main, "telegram_bot", None)
    monkeypatch.setattr(main, "top_instruments", [])
    ledger = OrderLedger(str(tmp_path / "order_ledger.jsonl"))
    ledger.load()
    monkeypatch.setattr(main, "ledger_ref", ledger)
    yield SimpleNamespace(tmp_path=tmp_path, ledger=ledger, monkeypatch=monkeypatch)


# ============================================================================
# ЗАДАЧА A: извлечение цены BUY — раздельные семантики PostOrderResponse/OrderState
# ============================================================================

def test_1_buy_post_order_response_price_not_divided_by_lots(tmp_path):
    """Сценарий 1: BUY PostOrderResponse (23 лота, lot_size=1) — цена входа
    равна фактической цене за штуку, а НЕ сумме, делённой на количество
    лотов (инцидент LSRG: 858.8 -> 37.3391)."""
    post = FakePostOrderResponse(FILL, lots_executed=23, price_per_unit=858.8)
    price = ExecutionMonitor._extract_price_from_post_order_response(post, 23)
    assert price == pytest.approx(858.8), \
        f"цена за штуку искажена: {price}"
    assert price != pytest.approx(858.8 / 23), \
        "цена делится на количество лотов (дефект 37.3391)"
    # Регистрация позиции по извлечённой цене: без ПОВТОРНОГО деления.
    ledger = OrderLedger(str(tmp_path / "order_ledger.jsonl"))
    monitor = ExecutionMonitor(FakeBroker())
    rm = FakeRM()
    pos = run(main.register_executed_buy(
        None, monitor, rm, ledger,
        figi="F1", ticker="TEST", order_id="buy-1", trade_id="t1",
        reason="MA(2/3) ↑", signal_time="2026-11-23T10:00:00+00:00",
        price_before_order=858.8, order_send_time="2026-11-23T10:00:00+00:00",
        requested_lots=23, executed_lots=23,
        exec_price=ExecutionMonitor._extract_price_from_post_order_response(
            post, 23),
        lot_size=1, entry_commission=None, partial=False,
        df=object(),  # непустой df: SL/TP по FakeRM -> штатная регистрация OPEN
        source="fill_branch_post_response", ledger_status="FILL",
    ))
    assert pos.entry_price == pytest.approx(858.8), \
        f"entry_price искажён: {pos.entry_price} (ожидалось 858.8)"
    rec = ledger.latest("buy-1")
    assert rec is not None and rec["avg_exec_price"] == pytest.approx(858.8)
    assert rec["status"] == "FILL"


def test_2_buy_order_state_price_extracted_from_total():
    """Сценарий 2: BUY OrderState — executed_order_price является СУММОЙ
    исполнения (произведение средней цены на количество лотов): цена за
    штуку извлекается делением ровно ОДИН раз (lots * lot_size); деления
    дважды нет."""
    state = FakeOrderState(FILL, lots_executed=23, total_price=858.8 * 23)
    price = ExecutionMonitor._extract_price_from_order_state(state, 23, 1)
    assert price == pytest.approx(858.8), f"цена из OrderState искажена: {price}"
    # lot_size > 1: сумма = цена_за_штуку * lots * lot_size
    state10 = FakeOrderState(FILL, lots_executed=23, total_price=858.8 * 23 * 10)
    price10 = ExecutionMonitor._extract_price_from_order_state(state10, 23, 10)
    assert price10 == pytest.approx(858.8), \
        f"lot_size=10: цена искажена: {price10}"
    # Совместимость: прежняя точка входа _extract_price — семантика OrderState.
    assert ExecutionMonitor._extract_price(state, 23, 1) == pytest.approx(858.8)
    # Fallback stages[]: executed_order_price недоступен — фактические сделки.
    st = FakeOrderState(FILL, lots_executed=23, total_price=None,
                        stages=[_Stage(858.0, 230), _Stage(860.0, 0)])
    st.stages = [_Stage(858.0, 10), _Stage(860.0, 13)]
    price_st = ExecutionMonitor._extract_price_from_order_state(st, 23, 1)
    assert price_st == pytest.approx((858.0 * 10 + 860.0 * 13) / 23), \
        f"fallback stages даёт неверную среднюю: {price_st}"


# ============================================================================
# ЗАДАЧА B: регистрация цены/комиссии SELL в close_position
# ============================================================================

def test_3_sell_post_response_fill_price_saved_to_ledger(env):
    """Сценарий 3: SELL PostOrderResponse FILL 23/23 — фактическая цена
    исполнения и комиссия сохраняются в order_ledger (avg_exec_price) и в
    per-order журнале позиции; цена до заявки НЕ подставляется."""
    ledger = env.ledger
    proven_buy(ledger)
    pos = make_buy_pos(state=PositionState.OPEN, close_reason="TP")
    broker = FakeBroker(
        positions={"F1": PosData(0, "LONG")}, prices={"F1": 920.0},
        post_response=FakePostOrderResponse(FILL, lots_executed=23,
                                            price_per_unit=870.5, commission=2.35),
    )
    monitor = fresh_monitor(pos, broker)
    env.monkeypatch.setattr(main, "monitor_ref", monitor)
    run(main.close_position(broker, pos, 920.0))

    assert len(broker.orders_sent) == 1
    req_id = broker.orders_sent[0][3]
    rec = ledger.latest(req_id)
    assert rec is not None, "SELL-запись не найдена в реестре"
    assert rec["status"] == "FILL"
    assert rec["side"] == "SELL"
    assert int(rec["executed_lots"]) == 23
    assert rec["avg_exec_price"] == pytest.approx(870.5), \
        f"цена исполнения SELL не сохранена: {rec['avg_exec_price']}"
    # комиссия — фактическая из отчёта брокера, не выдуманная
    po_rec = pos.close_orders[req_id]
    assert po_rec["commission"] == pytest.approx(2.35)
    # пер-ордер статус и цена позиции пока не финализированы
    # (их применяет apply_close_execution), но цена в записи журнала
    # отсутствовать не должна после apply — здесь проверяем реестр.


def test_4_sell_order_state_fill_price_extracted_and_applied(env):
    """Сценарий 4: SELL OrderState FILL 23/23 — цена правильно извлекается
    из СУММЫ исполнения (total/(lots*lot_size)) и применяется к позиции
    (execution_price, actual_commission, CLOSED, курсор заявки)."""
    ledger = env.ledger
    proven_buy(ledger)
    pos = make_buy_pos(state=PositionState.CLOSING, close_reason="TP",
                       close_order_request_id="sell-1",
                       close_order_id="EX-sell-1",
                       close_requested_lots=23)
    assert ledger.record_intent("sell-1", trade_id="t1", figi="F1", ticker="TEST",
                                side="SELL", requested_lots=23, reason="close:TP")
    broker = FakeBroker(positions={"F1": PosData(0, "LONG")}, prices={"F1": 920.0})
    # OrderState: executed_order_price — СУММА (870.5 * 23), комиссия 1.8
    broker.order_states["EX-sell-1"] = FakeOrderState(
        FILL, lots_executed=23, total_price=870.5 * 23, commission=1.8)
    monitor = fresh_monitor(pos, broker)

    run(monitor.resolve_closing(pos, ledger=ledger))

    assert pos.state == PositionState.CLOSED
    assert pos.quantity == 0
    assert pos.execution_price == pytest.approx(870.5), \
        f"цена исполнения из OrderState искажена: {pos.execution_price}"
    assert pos.exit_price_confirmed is True
    assert pos.actual_commission == pytest.approx(1.8)
    po_rec = pos.close_orders["sell-1"]
    assert po_rec["status"] == "FILL"
    assert po_rec["applied_lots"] == 23
    assert po_rec["confirmed_executed_lots"] == 23
    assert po_rec["avg_exec_price"] == pytest.approx(870.5)
    assert po_rec["commission"] == pytest.approx(1.8)
    rec = ledger.latest("sell-1")
    assert rec["status"] == "FILL"
    assert rec["avg_exec_price"] == pytest.approx(870.5)


# ============================================================================
# ЗАДАЧА C: per-order cursor, частичные исполнения, идемпотентность
# ============================================================================

def test_5_partial_sell_applies_confirmed_delta_only(env):
    """Сценарий 5: частичный SELL 3 из 23 — применяется ТОЛЬКО
    подтверждённая дельта (quantity 23 -> 20, close_applied_lots=3,
    close_applications ровно одна запись delta=3)."""
    ledger = env.ledger
    proven_buy(ledger)
    pos = make_buy_pos(state=PositionState.CLOSING, close_reason="SL",
                       close_order_request_id="sell-p1",
                       close_order_id="EX-sell-p1",
                       close_requested_lots=23)
    assert ledger.record_intent("sell-p1", trade_id="t1", figi="F1", ticker="TEST",
                                side="SELL", requested_lots=23, reason="close:SL")
    broker = FakeBroker(positions={"F1": PosData(20, "LONG")}, prices={"F1": 900.0})
    broker.order_states["EX-sell-p1"] = FakeOrderState(
        PARTIALLYFILL, lots_executed=3, total_price=900.0 * 3, commission=0.3)
    monitor = fresh_monitor(pos, broker)

    run(monitor.resolve_closing(pos, ledger=ledger))

    assert pos.quantity == 20, f"списано не ровно подтверждённые 3 лота: {pos.quantity}"
    assert pos.state == PositionState.UNCERTAIN  # PARTIALLYFILL — заявка жива
    assert pos.close_applied_lots == 3
    assert pos.close_orders["sell-p1"]["applied_lots"] == 3
    assert pos.close_orders["sell-p1"]["status"] == "PARTIALLYFILL"
    assert pos.close_orders["sell-p1"]["avg_exec_price"] == pytest.approx(900.0)
    assert len(pos.close_applications) == 1
    assert pos.close_applications[0]["delta_lots"] == 3


def test_6_repeated_report_does_not_deduct_lots_twice(env):
    """Сценарий 6: повтор того же отчёта (тот же cumulative) не списывает
    лоты второй раз; повтор терминального FILL после частичного применения
    доводит курсор ровно до 23 ровно один раз."""
    ledger = env.ledger
    proven_buy(ledger)
    pos = make_buy_pos(state=PositionState.CLOSING, close_reason="SL",
                       close_order_request_id="sell-r1",
                       close_order_id="EX-sell-r1",
                       close_requested_lots=23)
    assert ledger.record_intent("sell-r1", trade_id="t1", figi="F1", ticker="TEST",
                                side="SELL", requested_lots=23, reason="close:SL")
    broker = FakeBroker(positions={"F1": PosData(20, "LONG")}, prices={"F1": 900.0})
    broker.order_states["EX-sell-r1"] = FakeOrderState(
        PARTIALLYFILL, lots_executed=3, total_price=900.0 * 3)
    monitor = fresh_monitor(pos, broker)

    run(monitor.resolve_closing(pos, ledger=ledger))
    assert pos.quantity == 20 and pos.close_applied_lots == 3

    # тот же отчёт ещё раз — ничего не меняется
    run(monitor.resolve_closing(pos, ledger=ledger))
    assert pos.quantity == 20 and pos.close_applied_lots == 3, \
        "повтор того же отчёта списал лоты повторно"

    # терминальный FILL 23/23: применяется только дельта 20
    broker.order_states["EX-sell-r1"] = FakeOrderState(
        FILL, lots_executed=23, total_price=890.0 * 23)
    run(monitor.resolve_closing(pos, ledger=ledger))
    assert pos.state == PositionState.CLOSED
    assert pos.quantity == 0
    assert pos.close_applied_lots == 23
    assert len(pos.close_applications) == 2, \
        f"применений больше чем дельт: {pos.close_applications}"
    assert pos.close_applications[-1]["from_applied_lots"] == 3
    assert pos.close_applications[-1]["to_applied_lots"] == 23

    # повтор терминального отчёта — заморозка, нулевые изменения
    run(monitor.resolve_closing(pos, ledger=ledger))
    assert pos.quantity == 0 and pos.close_applied_lots == 23
    assert len(pos.close_applications) == 2


# ============================================================================
# PRICE_UNKNOWN и согласованное закрытие через reconcile
# ============================================================================

def test_7_price_unknown_no_fictitious_pnl(env):
    """Сценарий 7: PRICE_UNKNOWN (в реестре цены нет, брокер не отвечает) —
    учёт закрывается БЕЗ фиктивного P&L: execution_price=None,
    exit_price_confirmed=False, CSV-строка с пустыми финансовыми полями."""
    ledger = env.ledger
    proven_buy(ledger)
    pos = make_buy_pos(state=PositionState.UNCERTAIN, close_reason="TP",
                       close_order_request_id="sell-u1",
                       close_order_id=None,  # ответ брокера потерян
                       close_requested_lots=23)
    assert ledger.record_intent("sell-u1", trade_id="t1", figi="F1", ticker="TEST",
                                side="SELL", requested_lots=23, reason="close:TP")
    assert ledger.record_status("sell-u1", "FILL", figi="F1", ticker="TEST",
                                side="SELL", requested_lots=23, executed_lots=23,
                                avg_exec_price=None, source="post_response")
    # брокер не отвечает (опрос None) и позиция отсутствует
    broker = FakeBroker(positions={}, prices={"F1": 900.0})
    monitor = fresh_monitor(pos, broker)
    env.monkeypatch.setattr(main, "monitor_ref", monitor)
    rm = FakeRM()

    action = run(main._try_resolve_absent_position(broker, ledger, pos))
    assert action == "ledger_close"
    assert pos.state == PositionState.CLOSED
    assert pos.quantity == 0
    assert pos.execution_price is None, \
        f"фиктивная цена подставлена: {pos.execution_price}"
    assert pos.exit_price_confirmed is False
    po_rec = pos.close_orders["sell-u1"]
    assert po_rec["status"] == "FILL"          # терминальный статус синхронизирован
    assert po_rec["applied_lots"] == 23        # курсор применён
    assert po_rec["avg_exec_price"] is None    # цена неизвестна — не выдумана

    # CSV-строка без P&L (пишется штатным process_closing при logged=False)
    run(main.process_closing(broker, monitor, rm))
    rows = read_trades(env.tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["gross_pnl_rub"] == "" and row["net_pnl_rub"] == ""
    assert row["exit_price"] == "" or row["exit_price"] == "0.0000"
    assert "PRICE_UNKNOWN" in (row["error_note"] or "")


def test_8_reconcile_close_consistent_state_and_ledger(env):
    """Сценарий 8: закрытие через reconcile — state.json и order_ledger
    согласованы: терминальный статус SELL, per-order cursor, цена
    исполнения, комиссия, состояние позиции, CSV-запись; повторных
    применений нет."""
    ledger = env.ledger
    proven_buy(ledger)
    # Производственный поток: обработчик SL/TP помечает позицию CLOSING
    # ДО вызова close_position (сам close_position состояние не меняет).
    pos = make_buy_pos(state=PositionState.CLOSING, close_reason="TP",
                       entry_price=858.8, price_before_order=858.8)
    broker = FakeBroker(
        positions={"F1": PosData(23, "LONG")}, prices={"F1": 920.0},
        post_response=FakePostOrderResponse(FILL, lots_executed=23,
                                            price_per_unit=870.5, commission=2.35),
    )
    monitor = fresh_monitor(pos, broker)
    env.monkeypatch.setattr(main, "monitor_ref", monitor)
    rm = FakeRM()

    run(main.close_position(broker, pos, 920.0))
    req_id = broker.orders_sent[0][3]
    # Инцидент 23.11.2026: SELL исполнился (позиция распродана), но
    # брокерский опрос состояния заявки в этом цикле НЕ отвечает
    # (eventual consistency песочницы), позиция переходит в UNCERTAIN.
    assert pos.state == PositionState.CLOSING
    broker.order_states.clear()   # латентность опроса: state=None
    broker.positions = {}         # позиция распродана — у брокера отсутствует
    run(monitor.resolve_closing(pos, ledger=ledger))
    assert pos.state == PositionState.UNCERTAIN

    # сверка: позиция отсутствует у брокера (SELL исполнился)
    run(main.reconcile(broker, monitor, ledger, rm))
    assert pos.state == PositionState.CLOSED
    assert pos.quantity == 0
    assert pos.execution_price == pytest.approx(870.5)
    assert pos.actual_commission == pytest.approx(2.35)
    assert pos.exit_price_confirmed is True
    po_rec = pos.close_orders[req_id]
    assert po_rec["status"] == "FILL"
    assert po_rec["applied_lots"] == 23
    assert po_rec["confirmed_executed_lots"] == 23
    assert po_rec["avg_exec_price"] == pytest.approx(870.5)
    assert po_rec["commission"] == pytest.approx(2.35)
    assert pos.close_applied_lots == 23
    assert len(pos.close_applications) == 1

    # state.json (export_state) согласован с реестром
    saved = monitor.export_state()
    saved_pos = saved["positions"]["F1"]
    assert saved_pos["state"] == "CLOSED"
    assert saved_pos["close_orders"][req_id]["status"] == "FILL"
    assert saved_pos["close_orders"][req_id]["applied_lots"] == 23
    assert saved_pos["close_orders"][req_id]["avg_exec_price"] == pytest.approx(870.5)
    assert saved_pos["close_applied_lots"] == 23
    # реестр: FILL с фактической ценой (из PostOrderResponse)
    rec = ledger.latest(req_id)
    assert rec["status"] == "FILL"
    assert rec["avg_exec_price"] == pytest.approx(870.5)

    # CSV-запись с реальным P&L и фактической комиссией
    run(main.process_closing(broker, monitor, rm))
    rows = read_trades(env.tmp_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["exit_price"] == f"{870.5:.4f}"
    assert row["commission_source"] == "mixed"  # вход model, выход actual
    assert float(row["gross_pnl_rub"]) == pytest.approx((870.5 - 858.8) * 23)
    assert float(row["actual_commission_rub"]) == pytest.approx(2.35)
    # повторная сверка/закрытие не создают вторую CSV-строку и не применяют
    # лоты второй раз
    run(main.reconcile(broker, monitor, ledger, rm))
    run(main.process_closing(broker, monitor, rm))
    assert len(read_trades(env.tmp_path)) == 1
    assert len(broker.orders_sent) == 1  # повторной SELL нет
    assert len(pos.close_applications) == 1


def test_9_restart_after_sell_no_resale(env):
    """Сценарий 9: перезапуск после SELL (восстановление state.json в новый
    монитор + перечитывание реестра) не приводит к повторной продаже и
    дублированию CSV."""
    ledger = env.ledger
    proven_buy(ledger)
    pos = make_buy_pos(state=PositionState.CLOSING, close_reason="TP")
    broker = FakeBroker(
        positions={"F1": PosData(23, "LONG")}, prices={"F1": 920.0},
        post_response=FakePostOrderResponse(FILL, lots_executed=23,
                                            price_per_unit=870.5, commission=2.35),
    )
    monitor = fresh_monitor(pos, broker)
    env.monkeypatch.setattr(main, "monitor_ref", monitor)
    rm = FakeRM()

    run(main.close_position(broker, pos, 920.0))
    # Инцидент: позиция распродана, опрос состояния заявки не отвечает
    broker.order_states.clear()
    broker.positions = {}
    run(monitor.resolve_closing(pos, ledger=ledger))  # опрос None -> UNCERTAIN
    run(main.reconcile(broker, monitor, ledger, rm))
    run(main.process_closing(broker, monitor, rm))
    assert pos.state == PositionState.CLOSED
    assert len(broker.orders_sent) == 1
    assert len(read_trades(env.tmp_path)) == 1

    # --- перезапуск: новый монитор из сохранённого состояния, реестр тот же
    saved = monitor.export_state()
    broker2 = FakeBroker(positions={}, prices={"F1": 920.0})
    monitor2 = ExecutionMonitor(broker2)
    monitor2.import_state(saved)
    ledger2 = OrderLedger(str(env.tmp_path / "order_ledger.jsonl"))
    ledger2.load()
    env.monkeypatch.setattr(main, "ledger_ref", ledger2)
    env.monkeypatch.setattr(main, "monitor_ref", monitor2)

    run(main.process_closing(broker2, monitor2, rm))
    run(main.reconcile(broker2, monitor2, ledger2, rm))
    run(main.process_closing(broker2, monitor2, rm))

    assert len(broker2.orders_sent) == 0, \
        f"после перезапуска отправлена повторная SELL: {broker2.orders_sent}"
    assert len(read_trades(env.tmp_path)) == 1, "CSV-строка задублирована"
    restored = monitor2.positions["F1"]
    assert restored.state == PositionState.CLOSED
    assert restored.close_applied_lots == 23
    assert restored.close_orders[restored.close_order_request_id]["status"] == "FILL"


def test_10_broker_error_none_timeout_keeps_uncertain(env):
    """Сценарий 10: ошибка/None/таймаут брокерского ответа не снимает
    защиту UNCERTAIN: позиция не переходит в OPEN, цена не выдумывается,
    повторная SELL не отправляется."""
    ledger = env.ledger
    proven_buy(ledger)
    pos = make_buy_pos(state=PositionState.OPEN, close_reason="TP")
    broker = FakeBroker(positions={"F1": PosData(23, "LONG")},
                        prices={"F1": 920.0},
                        post_error=TimeoutError("broker timeout"))
    monitor = fresh_monitor(pos, broker)
    env.monkeypatch.setattr(main, "monitor_ref", monitor)
    rm = FakeRM()

    run(main.close_position(broker, pos, 920.0))

    assert pos.state == PositionState.UNCERTAIN, \
        f"потерянный ответ не оставил UNCERTAIN: {pos.state}"
    assert len(broker.orders_sent) == 1
    req_id = broker.orders_sent[0][3]
    assert pos.close_order_request_id == req_id
    assert pos.close_order_id is None  # биржевой ID не выдуман
    rec = ledger.latest(req_id)
    assert rec["status"] == "UNCERTAIN"
    assert rec["avg_exec_price"] is None  # цена неизвестна — None, не 920.0

    # повторные циклы с недоступным брокером: защиты не снимаются
    run(main.process_closing(broker, monitor, rm))
    assert pos.state == PositionState.UNCERTAIN
    assert len(broker.orders_sent) == 1, "повторная SELL после потери ответа"
    # resolve_closing при недоступном брокере тоже не снимает UNCERTAIN
    run(monitor.resolve_closing(pos, ledger=ledger))
    assert pos.state == PositionState.UNCERTAIN
    assert pos.execution_price is None
    assert pos.quantity == 23  # исполнение не подтверждено — лоты не списаны
