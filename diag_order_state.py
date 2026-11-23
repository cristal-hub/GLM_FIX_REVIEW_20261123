# diag_order_state.py
# Tsclp_v2.1_volatile_Samsung — READ-ONLY диагностика состояния СУЩЕСТВУЮЩЕЙ
# Sandbox-заявки (пакет GLM_FIX_REVIEW_20261123, продолжение price-accounting fix).
#
# НАЗНАЧЕНИЕ: исключительно READ-ONLY запрос состояния уже существующей
#             Sandbox-заявки (get_sandbox_order_state) и печать полей,
#             необходимых для проверки семантики цен учёта:
#               - тип объекта ответа (type().__name__);
#               - execution_report_status (статус);
#               - lots_executed;
#               - executed_order_price (units/nano/currency);
#               - stages[] и их price/quantity;
#               - executed_commission (units/nano/currency);
#             плюс контекст: figi, direction, lots_requested/lots_left/
#             lots_cancelled, order_id/order_request_id, initial_order_price,
#             average_position_price, lot_size инструмента.
#
# ЗАПРЕЩЕНО: отправлять заявки (post), отменять (cancel), изменять,
#            открывать/пополнять счета (pay_in), менять позиции и учёт.
# ТОЛЬКО ЧТЕНИЕ. ТОЛЬКО SANDBOX.
#
# НЕ ПЕЧАТАЕТ токен; account_id и order_id кандидатов маскируются
# (полный ID печатается только для проверяемой заявки — он не секрет и
# присутствует в локальных журналах в открытом виде).
# Ошибки выводятся ТОЛЬКО типом исключения (type(e).__name__), без str(e).
#
# Запуск (из корня репозитория, где лежит config.py):
#   python diag_order_state.py                     # авто-поиск заявки
#   python diag_order_state.py <order_id>          # конкретная заявка
#   python diag_order_state.py <order_id> EXCHANGE # тип ID указан явно
#   python diag_order_state.py <order_id> REQUEST  # клиентский UUID явно
#
# Поиск кандидатов (без расхода лимитов API): order_ledger.jsonl
# (exchange_order_id -> EXCHANGE; order_id/order_request_id -> REQUEST),
# state.json (entry_order_id, close_orders), research_events.jsonl,
# research_trades.csv, затем активные заявки get_sandbox_orders (EXCHANGE).
#
# ЭТАП 5Б (важно): тип идентификатора передаётся ЯВНО
# (ORDER_ID_TYPE_EXCHANGE / ORDER_ID_TYPE_REQUEST). Запрос с
# UNSPECIFIED по клиентскому UUID — запрещён (порождение ложных
# "не найдено"). Если тип ID неизвестен — сначала EXCHANGE, затем
# REQUEST; оба запроса явные.
#
# Если подходящей существующей заявки нет — скрипт завершается
# безопасно с понятным сообщением (код 0). НЕ создавать новую заявку
# ради диагностики.

import asyncio
import csv
import json
import logging
import os
import sys
from decimal import Decimal
from typing import List, Optional, Tuple

# SDK импортируется по образцу tinkoff_client.py (путь OrderIdType
# различается между сборками SDK; отсутствие enum не должно ронять
# скрипт — в этом случае используется прежний запрос без order_id_type
# с предупреждающей печатью).
try:
    from t_tech.invest import AsyncClient  # noqa: F401 — как в tinkoff_client
except Exception:
    AsyncClient = None
try:
    from t_tech.invest.schemas import OrderIdType
except ImportError:
    try:
        from t_tech.invest import OrderIdType
    except Exception:
        OrderIdType = None
try:
    from t_tech.invest.sandbox.async_client import AsyncSandboxClient
except Exception:
    AsyncSandboxClient = None
try:
    from t_tech.invest.utils import quotation_to_decimal
except Exception:
    quotation_to_decimal = None

import config

logging.basicConfig(level=logging.WARNING,
                    format='%(asctime)s - %(levelname)s - %(message)s')

# Ограничение числа запросов get_sandbox_order_state за прогон.
MAX_STATE_REQUESTS = 12

# Разделитель research_trades.csv — подтверждено по logger.py: delimiter=";".
TRADES_DELIMITER = ";"

# Коды execution_report_status по официальному контракту T-Invest API
# (OrderExecutionReportStatus, gRPC). Используются только для ЧЕЛОВЕКОЧИТАЕМОЙ
# печать рядом с сырым значением.
STATUS_NAMES = {
    0: "UNSPECIFIED",
    1: "FILL",
    2: "REJECTED",
    3: "CANCELLED",
    4: "NEW",
    5: "PARTIALLYFILL",
    6: "EXPIRED",
}


def _safe_error(e: Exception) -> str:
    """Только тип исключения, без str(e) — чтобы не вывести чувствительные детали."""
    return type(e).__name__


def _mask_account(acc: Optional[str]) -> str:
    """Маскировка account_id: первые 6 символов + '...'."""
    if not acc:
        return "<None>"
    return acc[:6] + "..." if len(acc) > 6 else "***"


def _mask_order_id(oid: Optional[str]) -> str:
    """Маскировка order_id в списках кандидатов: первые 8 символов + '...'."""
    if not oid:
        return "<None>"
    return oid[:8] + "..." if len(oid) > 8 else oid


def _order_id_type_member(kind):
    """'EXCHANGE'/'REQUEST' -> член SDK OrderIdType; None -> (None, True).

    fail-closed как в tinkoff_client: если тип запрошен, а SDK не
    экспортирует enum — возвращает (None, False), вызывающий код
    НЕ выполняет запрос с неверной семантикой."""
    if kind is None:
        return None, True
    if OrderIdType is None:
        return None, False
    if isinstance(kind, str):
        member = getattr(OrderIdType, f"ORDER_ID_TYPE_{kind.upper()}", None)
        return member, member is not None
    return kind, True


def _money_to_dict(money) -> Optional[dict]:
    """MoneyValue/Quotation -> {'units', 'nano', 'currency'} без округления."""
    if money is None:
        return None
    try:
        return {
            "units": int(getattr(money, "units", 0)),
            "nano": int(getattr(money, "nano", 0)),
            "currency": getattr(money, "currency", None),
        }
    except Exception:
        return None


def _money_to_decimal_str(money) -> Optional[str]:
    """Строковое представление Decimal без потери точности."""
    if money is None:
        return None
    d = _money_to_dict(money)
    if d is None:
        return None
    try:
        return str(Decimal(d["units"]) + Decimal(d["nano"]) / Decimal(10 ** 9))
    except Exception:
        return None


def _direction_name(raw) -> str:
    """OrderDirection: 0=UNSPECIFIED, 1=BUY, 2=SELL (официальный контракт)."""
    if raw is None:
        return "None"
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return f"unknown({raw!r})"
    return {0: "UNSPECIFIED", 1: "BUY", 2: "SELL"}.get(value, f"unknown({value})")


def _status_name(raw) -> str:
    """Человекочитаемое имя статуса рядом с сырым значением."""
    if raw is None:
        return "None"
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return f"unknown({raw!r})"
    return STATUS_NAMES.get(value, f"unknown({value})")


def _collect_from_ledger(path: str) -> List[Tuple[str, str]]:
    """order_ledger.jsonl -> [(id, 'EXCHANGE'|'REQUEST')], свежие — в конце."""
    found: List[Tuple[str, str]] = []
    seen = set()
    if not os.path.isfile(path):
        return found
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if not isinstance(rec, dict):
                    continue
                pairs = [
                    (rec.get("exchange_order_id"), "EXCHANGE"),
                    (rec.get("close_order_id"), "EXCHANGE"),
                    (rec.get("order_request_id"), "REQUEST"),
                    (rec.get("order_id"), "REQUEST"),
                ]
                for val, kind in pairs:
                    if isinstance(val, str) and val and val not in seen:
                        seen.add(val)
                        found.append((val, kind))
    except Exception as e:
        print(f"  warning: order_ledger.jsonl: {_safe_error(e)}")
    return found


def _collect_from_state(path: str) -> List[Tuple[str, str]]:
    """state.json -> [(id, 'EXCHANGE'|'REQUEST')].

    positions[*].entry_order_id — клиентский UUID BUY;
    positions[*].close_order_request_id и ключи close_orders — клиентские
    UUID SELL; close_orders[oid].close_order_id (если заполнен) —
    биржевой ID SELL."""
    found: List[Tuple[str, str]] = []
    seen = set()

    def add(val, kind):
        if isinstance(val, str) and val and val not in seen:
            seen.add(val)
            found.append((val, kind))

    if not os.path.isfile(path):
        return found
    try:
        with open(path, "r", encoding="utf-8") as f:
            st = json.load(f)
        for pos in (st.get("positions", {}) or {}).values():
            if not isinstance(pos, dict):
                continue
            add(pos.get("entry_order_id"), "REQUEST")
            add(pos.get("close_order_request_id"), "REQUEST")
            close_orders = pos.get("close_orders") or {}
            if isinstance(close_orders, dict):
                for req_id, rec in close_orders.items():
                    add(req_id, "REQUEST")
                    if isinstance(rec, dict):
                        add(rec.get("close_order_id"), "EXCHANGE")
                        add(rec.get("exchange_order_id"), "EXCHANGE")
    except Exception as e:
        print(f"  warning: state.json: {_safe_error(e)}")
    return found


def _collect_from_events(path: str) -> List[Tuple[str, str]]:
    """research_events.jsonl -> [(id, None)] — тип ID неизвестен."""
    found: List[Tuple[str, str]] = []
    seen = set()
    keys = ("order_id", "entry_order_id", "close_order_id", "orderId",
            "exchange_order_id")
    if not os.path.isfile(path):
        return found
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                sources = [rec]
                if isinstance(rec, dict) and isinstance(rec.get("payload"), dict):
                    sources.append(rec["payload"])
                for src in sources:
                    if not isinstance(src, dict):
                        continue
                    for key in keys:
                        val = src.get(key)
                        if isinstance(val, str) and val and val not in seen:
                            seen.add(val)
                            found.append((val, None))
    except Exception as e:
        print(f"  warning: research_events.jsonl: {_safe_error(e)}")
    return found


def _collect_from_trades(path: str) -> List[Tuple[str, str]]:
    """research_trades.csv (delimiter ';') -> [(id, None)] — тип неизвестен."""
    found: List[Tuple[str, str]] = []
    seen = set()
    if not os.path.isfile(path):
        return found
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f, delimiter=TRADES_DELIMITER)
            for row in reader:
                for col in ("order_id", "exchange_order_id"):
                    val = (row or {}).get(col)
                    if isinstance(val, str) and val and val not in seen:
                        seen.add(val)
                        found.append((val, None))
    except Exception as e:
        print(f"  warning: research_trades.csv: {_safe_error(e)}")
    return found


async def _fetch_active_orders(client, account_id: str) -> List[Tuple[str, str]]:
    """Активные заявки (read-only): orders[].order_id — БИРЖЕВОЙ ID
    (официальный контракт GetOrders: order_id — биржевой идентификатор;
    order_request_id — клиентский)."""
    found: List[Tuple[str, str]] = []
    try:
        resp = await client.sandbox.get_sandbox_orders(account_id=account_id)
        for o in (getattr(resp, "orders", []) or []):
            oid = getattr(o, "order_id", None)
            if isinstance(oid, str) and oid:
                found.append((oid, "EXCHANGE"))
            req_id = getattr(o, "order_request_id", None)
            if isinstance(req_id, str) and req_id:
                found.append((req_id, "REQUEST"))
    except Exception as e:
        print(f"  warning: get_sandbox_orders: {_safe_error(e)}")
    return found


async def _get_lot_size(client, figi: str) -> Optional[int]:
    """lot_size инструмента через instruments.get_instrument_by (read-only)."""
    try:
        from t_tech.invest import InstrumentIdType
        resp = await client.instruments.get_instrument_by(
            id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_FIGI, id=figi,
        )
        lot = getattr(resp.instrument, "lot", None)
        return int(lot) if lot is not None else None
    except Exception as e:
        print(f"  warning: lot_size({figi}): {_safe_error(e)}")
        return None


def _print_order_state(state, lot_size: Optional[int]) -> bool:
    """Печатает обязательные поля диагностики. True — есть исполнение."""
    object_type = type(state).__name__
    status = getattr(state, "execution_report_status", None)
    lots_exec = getattr(state, "lots_executed", None)
    lots_req = getattr(state, "lots_requested", None)
    lots_left = getattr(state, "lots_left", None)
    lots_cancelled = getattr(state, "lots_cancelled", None)
    figi = getattr(state, "figi", None)
    direction = getattr(state, "direction", None)
    order_id = getattr(state, "order_id", None)
    order_request_id = getattr(state, "order_request_id", None)

    print(f"  object_type = {object_type}")
    print(f"  status = {status} ({_status_name(status)})")
    print(f"  direction = {_direction_name(direction)}")
    print(f"  figi = {figi}")
    print(f"  order_id (exchange) = {order_id}")
    print(f"  order_request_id (client uuid) = {order_request_id}")
    print(f"  lots_requested = {lots_req}")
    print(f"  lots_executed = {lots_exec}")
    print(f"  lots_left = {lots_left}")
    print(f"  lots_cancelled = {lots_cancelled}")

    # --- Обязательное поле аудита: executed_order_price (units/nano/currency)
    eop = getattr(state, "executed_order_price", None)
    print(f"  executed_order_price = {_money_to_dict(eop)}")
    print(f"  executed_order_price (Decimal) = {_money_to_decimal_str(eop)}")

    # --- Обязательное поле аудита: executed_commission
    comm = getattr(state, "executed_commission", None)
    print(f"  executed_commission = {_money_to_dict(comm)}")
    print(f"  executed_commission (Decimal) = {_money_to_decimal_str(comm)}")

    # --- Контекст для проверки семантики (не является обязательным полем)
    print(f"  initial_order_price = "
          f"{_money_to_decimal_str(getattr(state, 'initial_order_price', None))}")
    print(f"  average_position_price = "
          f"{_money_to_decimal_str(getattr(state, 'average_position_price', None))}")
    print(f"  lot_size = {lot_size}")

    # --- Обязательное поле аудита: stages и их price/quantity
    stages = getattr(state, "stages", None) or []
    print(f"  stages: {len(stages)}")
    for i, st in enumerate(stages):
        st_price = _money_to_dict(getattr(st, "price", None))
        st_qty = getattr(st, "quantity", None)
        print(f"    stage[{i}]: price = {st_price} quantity = {st_qty}")

    # --- Арифметическая подсказка (только печать, никаких выводов в учёт).
    # Официальный контракт (orders.proto, RussianInvestments/invest-python):
    #   OrderState.executed_order_price — «Исполненная цена заявки.
    #   Произведение средней цены покупки на количество лотов»;
    #   OrderStage.price — «Цена за 1 инструмент»;
    #   OrderStage.quantity — «Количество лотов»;
    #   average_position_price — «Средняя цена позиции по сделке» (за штуку).
    # Формулировка OrderState.executed_order_price не уточняет лотность:
    # при lot_size=1 все три гипотезы различимы только по сумме; при
    # lot_size>1 решает сравнение с average_position_price (эталон цены
    # за штуку) и с суммой стадий Σ(price_i × quantity_i × lot_size).
    eop_dec = _money_to_decimal_str(eop)
    app = getattr(state, "average_position_price", None)
    app_dec = _money_to_decimal_str(app)
    if eop_dec is not None and lots_exec and int(lots_exec) > 0:
        total = Decimal(eop_dec)
        le = Decimal(int(lots_exec))
        ls = Decimal(int(lot_size)) if lot_size else Decimal(0)
        print(f"  CHECK (для ручной сверки, в учёт не идёт):")
        if ls > 0:
            print(f"    H1: executed_order_price / (lots_executed * lot_size) "
                  f"= {total / (le * ls)}")
        if le > 0:
            print(f"    H2: executed_order_price / lots_executed "
                  f"= {total / le}")
        print(f"    H3: executed_order_price как есть = {total}")
        if app_dec is not None:
            print(f"    average_position_price (эталон цены за 1 инструмент) "
                  f"= {app_dec}")
            print(f"    -> семантика OrderState.executed_order_price та, "
                  f"чья гипотеза совпала с average_position_price; "
                  f"при lot_size=1 по вашему инциденту H1/H2 совпадают, "
                  f"для lot_size>1 решает именно эта сверка.")
        # Сверка со стадиями: Σ(price_i × quantity_i) — цена за штуку,
        # Σ(price_i × quantity_i × lot_size) — денежная сумма исполнения.
        stages = getattr(state, "stages", None) or []
        if stages:
            stage_money = Decimal(0)
            stage_qty = Decimal(0)
            ok_stages = True
            for st in stages:
                sp = _money_to_decimal_str(getattr(st, "price", None))
                sq = getattr(st, "quantity", None)
                if sp is None or sq is None:
                    ok_stages = False
                    break
                stage_money += Decimal(sp) * Decimal(int(sq))
                stage_qty += Decimal(int(sq))
            if ok_stages and stage_qty > 0:
                print(f"    стадии: Σ(price_i × quantity_i) = {stage_money}, "
                      f"Σ quantity_i = {stage_qty} (ЛОТОВ по контракту), "
                      f"средняя цена за штуку по стадиям = "
                      f"{stage_money / stage_qty}")

    has_execution = lots_exec is not None and int(lots_exec) > 0
    if has_execution:
        print(f"  >>> ЕСТЬ ИСПОЛНЕНИЕ: lots_executed={lots_exec}")
    return has_execution


def _load_candidates_from_files() -> List[Tuple[str, str]]:
    """Кандидаты из локальных журналов (свежие — в конце списков)."""
    found: List[Tuple[str, str]] = []
    seen = set()
    for loader in (_collect_from_ledger, _collect_from_state,
                   _collect_from_events, _collect_from_trades):
        for oid, kind in loader(os.path.join(os.getcwd(), _source_name(loader))):
            if (oid, kind) not in seen and (oid, None) not in seen:
                seen.add((oid, kind))
                found.append((oid, kind))
    # Свежие записи реестра — первыми (журнал append-only: реверс).
    found.reverse()
    return found


def _source_name(loader) -> str:
    """Имя файла для загрузчика (по функции-коллектору)."""
    name = {
        "_collect_from_ledger": "order_ledger.jsonl",
        "_collect_from_state": "state.json",
        "_collect_from_events": "research_events.jsonl",
        "_collect_from_trades": "research_trades.csv",
    }.get(loader.__name__)
    return name or ""


async def _query_state(client, account_id: str, oid: str,
                       kind) -> Optional[object]:
    """READ-ONLY get_sandbox_order_state с ЯВНЫМ типом идентификатора.

    kind: 'EXCHANGE' | 'REQUEST' | None. При kind=None сначала пробуется
    EXCHANGE, затем REQUEST (оба запроса явные); UNSPECIFIED не используется
    никогда. При недоступном enum в SDK — прежний запрос без order_id_type
    с предупреждением (диагностика, не production-учёт)."""
    if kind is None:
        for try_kind in ("EXCHANGE", "REQUEST"):
            state = await _query_state(client, account_id, oid, try_kind)
            if state is not None:
                return state
        return None
    member, supported = _order_id_type_member(kind)
    kwargs = {"account_id": account_id, "order_id": oid}
    if kind is not None:
        if not supported:
            print(f"  warning: SDK не экспортирует OrderIdType — запрос без "
                  f"order_id_type (ненадёжно для UUID, только диагностика)")
        else:
            kwargs["order_id_type"] = member
    try:
        return await client.sandbox.get_sandbox_order_state(**kwargs)
    except Exception as e:
        print(f"  warning: get_sandbox_order_state({_mask_order_id(oid)}, "
              f"{kind}): {_safe_error(e)}")
        return None


async def main() -> None:
    print("=" * 70)
    print("READ-ONLY ДИАГНОСТИКА СОСТОЯНИЯ СУЩЕСТВУЮЩЕЙ SANDBOX-ЗАЯВКИ")
    print("ТОЛЬКО ЧТЕНИЕ. НИКАКИХ ЗАЯВОК. ТОЛЬКО SANDBOX.")
    print("=" * 70)

    config.validate()

    if not getattr(config, "USE_SANDBOX", False):
        print("USE_SANDBOX=False: скрипт работает только в песочнице.")
        return
    if AsyncSandboxClient is None:
        print("SDK t_tech.invest недоступен: установите t-tech-investments "
              "(см. requirements.txt). Ничего не отправлялось.")
        return

    # --- Разбор аргументов командной строки
    args = [a for a in sys.argv[1:] if a]
    explicit_id = None
    explicit_kind = None
    if args:
        explicit_id = args[0]
        if len(args) > 1:
            kind = args[1].upper()
            if kind not in ("EXCHANGE", "REQUEST"):
                print("Второй аргумент должен быть EXCHANGE или REQUEST.")
                return
            explicit_kind = kind
        elif "-" not in explicit_id and len(explicit_id) != 36:
            # Явно указан не-UUID идентификатор — это биржевой ID.
            explicit_kind = "EXCHANGE"
        else:
            # UUID без указания типа: тип не угадываем — оператор обязан
            # указать EXCHANGE/REQUEST вторым аргументом (ЭТАП 5Б).
            print("Указан UUID без типа идентификатора. Клиентский UUID "
                  "запрашивается с order_id_type=REQUEST, биржевой ID — с "
                  "EXCHANGE. Укажите тип вторым аргументом, например:")
            print(f"  python diag_order_state.py {explicit_id} REQUEST")
            return

    # --- Подключение к песочнице
    async with AsyncSandboxClient(config.TINKOFF_API_TOKEN) as client:
        account_id = config.TINKOFF_ACCOUNT_ID
        if not account_id:
            accounts = await client.users.get_accounts()
            if not accounts.accounts:
                print("В песочнице нет счетов. Диагностика невозможна "
                      "(счёт НЕ создаётся).")
                return
            account_id = accounts.accounts[0].id
            print(f"TINKOFF_ACCOUNT_ID не задан — используется первый счёт: "
                  f"{_mask_account(account_id)}")
        else:
            print(f"Счёт: {_mask_account(account_id)}")

        # --- Кандидаты
        if explicit_id is not None:
            candidates = [(explicit_id, explicit_kind)]
            print(f"Режим явной заявки: {_mask_order_id(explicit_id)} "
                  f"({explicit_kind})")
        else:
            candidates = _load_candidates_from_files()
            print(f"Кандидаты из локальных журналов: {len(candidates)}")
            for i, (oid, kind) in enumerate(candidates[:20]):
                print(f"  {i + 1}. {_mask_order_id(oid)} [{kind or '?'}]")
            if len(candidates) > 20:
                print(f"  ... и ещё {len(candidates) - 20}")

            active = await _fetch_active_orders(client, account_id)
            print(f"Активные заявки (get_sandbox_orders): {len(active)}")
            for oid, kind in active:
                if all(oid != c_oid for c_oid, _ in candidates):
                    candidates.append((oid, kind))
                print(f"  active: {_mask_order_id(oid)} [{kind}]")

        if not candidates:
            print()
            print("=" * 70)
            print("ПОДХОДЯЩЕЙ СУЩЕСТВУЮЩЕЙ ЗАЯВКИ НЕ НАЙДЕНО")
            print("=" * 70)
            print("В order_ledger.jsonl, state.json, research_events.jsonl,")
            print("research_trades.csv и активных заявках песочницы нет ни")
            print("одного идентификатора заявки. Диагностировать нечего.")
            print("НОВАЯ заявка НЕ создаётся (создание заявок запрещено).")
            print("Скрипт завершён безопасно (код 0).")
            return

        # --- Диагностика (лимит запросов)
        to_check = candidates[:MAX_STATE_REQUESTS]
        skipped = len(candidates) - len(to_check)
        print()
        print("=" * 70)
        print(f"ЗАПРОС СОСТОЯНИЯ (к проверке: {len(to_check)}, "
              f"пропущено: {skipped}, лимит {MAX_STATE_REQUESTS})")
        print("=" * 70)

        diagnosed = 0
        executed_found = False
        for idx, (oid, kind) in enumerate(to_check, 1):
            print(f"\n--- [{idx}/{len(to_check)}] order_id: {oid} "
                  f"({kind or 'тип неизвестен'}) ---")
            state = await _query_state(client, account_id, oid, kind)
            if state is None:
                print("  state=None: заявка не найдена или ответ не получен "
                      "(ошибка напечатана выше по типу исключения).")
                continue
            diagnosed += 1
            lot_size = None
            figi = getattr(state, "figi", None)
            if figi:
                lot_size = await _get_lot_size(client, figi)
            if _print_order_state(state, lot_size):
                executed_found = True
                break  # заявка с исполнением найдена — перебор остановлен
            else:
                # Просматриваем ещё несколько кандидатов: у NEW/CANCELLED
                # без исполнения цены нет, для диагностики нужен FILL.
                if idx >= MAX_STATE_REQUESTS:
                    break

        print()
        print("=" * 70)
        if executed_found:
            print("OK: найдена существующая заявка с исполнением — вывод выше.")
        elif diagnosed > 0:
            print("Заявки получены, но lots_executed=0 у всех проверенных "
                  "(цены исполнения нет).")
            print("НОВАЯ заявка НЕ создаётся: повторите диагностику после "
                  "естественного исполнения существующей заявки.")
        else:
            print("ПОДХОДЯЩЕЙ СУЩЕСТВУЮЩЕЙ ЗАЯВКИ НЕ НАЙДЕНО: ни один "
                  "кандидат не дал состояния (state=None у всех).")
            print("Проверьте порядок ID (EXCHANGE/REQUEST) и наличие заявки "
                  "в песочнице. НОВАЯ заявка НЕ создаётся.")
        print("Скрипт завершён безопасно (код 0). Ничего не отправлялось.")
        print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
