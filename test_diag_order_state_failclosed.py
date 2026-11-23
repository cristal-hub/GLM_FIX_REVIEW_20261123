# tests/test_diag_order_state_failclosed.py
# Tsclp_v2.1_volatile_Samsung — fail-closed тесты diag_order_state
# (исправление после аудита GLM_FIX_REVIEW_20261123).
#
# ПРОВЕРЯЕТСЯ: _query_state() и main() НИКОГДА не вызывают
# get_sandbox_order_state без ЯВНО переданного order_id_type:
#   1) enum OrderIdType отсутствует в SDK -> API-вызов НЕ выполняется;
#   2) нужный член enum отсутствует       -> API-вызов НЕ выполняется;
#   3) корректный режим EXCHANGE          -> API вызывается С ЯВНЫМ типом;
#   4) корректный режим REQUEST           -> API вызывается С ЯВНЫМ типом;
#   5) неверный режим                     -> API-вызов НЕ выполняется.
#
# ТОЛЬКО fake/mock-объекты: без сети, без токенов, без реального брокера,
# без создания/отмены заявок. Производственный код не изменяется.

import enum

import pytest

import diag_order_state as diag


class _FakeOrderIdType(enum.Enum):
    """Модель исправной сборки SDK: оба нужных члена на месте."""

    ORDER_ID_TYPE_UNSPECIFIED = 0
    ORDER_ID_TYPE_EXCHANGE = 1
    ORDER_ID_TYPE_REQUEST = 2


class _PartialOrderIdType(enum.Enum):
    """Модель дефектной сборки SDK: члены EXCHANGE/REQUEST отсутствуют."""

    ORDER_ID_TYPE_UNSPECIFIED = 0


class _FakeSandbox:
    """Записывает kwargs каждого вызова get_sandbox_order_state."""

    def __init__(self, state=None):
        self.calls = []
        self._state = state

    async def get_sandbox_order_state(self, **kwargs):
        self.calls.append(dict(kwargs))
        return self._state


class _FakeClient:
    """Минимальный клиент: только sandbox.get_sandbox_order_state."""

    def __init__(self, state=None):
        self.sandbox = _FakeSandbox(state=state)


class _FakeState:
    """Минимальный OrderState-подобный ответ (не сетевой объект)."""

    figi = "BBG000000000"


# ---------------------------------------------------------------------------
# 1. enum OrderIdType отсутствует в SDK -> API-вызов НЕ выполняется
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["EXCHANGE", "REQUEST"])
async def test_enum_missing_api_not_called(monkeypatch, kind):
    monkeypatch.setattr(diag, "OrderIdType", None)
    client = _FakeClient(state=_FakeState())

    with pytest.raises(diag.OrderIdTypeUnavailable):
        await diag._query_state(client, "acc-id", "some-order-id", kind)

    # Запрос физически не выполнялся: ни одного вызова API.
    assert client.sandbox.calls == []


# ---------------------------------------------------------------------------
# 2. Нужный член enum отсутствует -> API-вызов НЕ выполняется
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["EXCHANGE", "REQUEST"])
async def test_member_missing_api_not_called(monkeypatch, kind):
    monkeypatch.setattr(diag, "OrderIdType", _PartialOrderIdType)
    client = _FakeClient(state=_FakeState())

    with pytest.raises(diag.OrderIdTypeUnavailable):
        await diag._query_state(client, "acc-id", "some-order-id", kind)

    assert client.sandbox.calls == []


# ---------------------------------------------------------------------------
# 3/4. Корректные режимы -> API вызывается РОВНО с ЯВНЫМ типом
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_exchange_mode_calls_api_with_explicit_type(monkeypatch):
    monkeypatch.setattr(diag, "OrderIdType", _FakeOrderIdType)
    client = _FakeClient(state=_FakeState())

    state = await diag._query_state(
        client, "acc-id", "some-order-id", "EXCHANGE")

    assert state is not None
    assert client.sandbox.calls == [{
        "account_id": "acc-id",
        "order_id": "some-order-id",
        "order_id_type": _FakeOrderIdType.ORDER_ID_TYPE_EXCHANGE,
    }]
    # Явный тип — не None и не UNSPECIFIED.
    sent = client.sandbox.calls[0]["order_id_type"]
    assert sent is not None
    assert sent is not _FakeOrderIdType.ORDER_ID_TYPE_UNSPECIFIED


@pytest.mark.asyncio
async def test_request_mode_calls_api_with_explicit_type(monkeypatch):
    monkeypatch.setattr(diag, "OrderIdType", _FakeOrderIdType)
    client = _FakeClient(state=_FakeState())

    state = await diag._query_state(
        client, "acc-id", "client-uuid-0000", "REQUEST")

    assert state is not None
    assert client.sandbox.calls == [{
        "account_id": "acc-id",
        "order_id": "client-uuid-0000",
        "order_id_type": _FakeOrderIdType.ORDER_ID_TYPE_REQUEST,
    }]


# ---------------------------------------------------------------------------
# 5. Неверный режим -> API-вызов НЕ выполняется
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("bad_kind", ["FOO", "", "UNSPECIFIED", 123, None.__class__])
async def test_invalid_mode_api_not_called(monkeypatch, bad_kind):
    monkeypatch.setattr(diag, "OrderIdType", _FakeOrderIdType)
    client = _FakeClient(state=_FakeState())

    with pytest.raises(diag.OrderIdTypeUnavailable):
        await diag._query_state(client, "acc-id", "some-order-id", bad_kind)

    assert client.sandbox.calls == []


# ---------------------------------------------------------------------------
# kind=None: авто-перебор EXCHANGE -> REQUEST, оба вызова ЯВНЫЕ
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_kind_none_tries_both_modes_with_explicit_types(monkeypatch):
    monkeypatch.setattr(diag, "OrderIdType", _FakeOrderIdType)
    client = _FakeClient(state=None)  # оба режима возвращают None

    result = await diag._query_state(client, "acc-id", "some-order-id", None)

    assert result is None
    assert len(client.sandbox.calls) == 2
    assert client.sandbox.calls[0]["order_id_type"] \
        is _FakeOrderIdType.ORDER_ID_TYPE_EXCHANGE
    assert client.sandbox.calls[1]["order_id_type"] \
        is _FakeOrderIdType.ORDER_ID_TYPE_REQUEST


@pytest.mark.asyncio
async def test_kind_none_exchange_hit_no_second_call(monkeypatch):
    monkeypatch.setattr(diag, "OrderIdType", _FakeOrderIdType)
    client = _FakeClient(state=_FakeState())  # EXCHANGE сразу находит

    state = await diag._query_state(client, "acc-id", "some-order-id", None)

    assert state is not None
    assert len(client.sandbox.calls) == 1
    assert client.sandbox.calls[0]["order_id_type"] \
        is _FakeOrderIdType.ORDER_ID_TYPE_EXCHANGE


@pytest.mark.asyncio
async def test_kind_none_enum_missing_api_not_called(monkeypatch):
    """Даже авто-перебор при отсутствии enum не выполняет ни одного вызова."""
    monkeypatch.setattr(diag, "OrderIdType", None)
    client = _FakeClient(state=_FakeState())

    with pytest.raises(diag.OrderIdTypeUnavailable):
        await diag._query_state(client, "acc-id", "some-order-id", None)

    assert client.sandbox.calls == []


# ---------------------------------------------------------------------------
# main(): fail-fast ДО подключения при отсутствии enum
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_main_fails_fast_without_connect_when_enum_missing(monkeypatch):
    """enum отсутствует -> main завершается (код 2) ДО создания клиента:
    подключение и любые API-вызовы физически невозможны."""
    monkeypatch.setattr(diag, "OrderIdType", None)
    monkeypatch.setattr(diag.config, "validate", lambda: None)
    monkeypatch.setattr(diag.config, "USE_SANDBOX", True)

    entered = {"constructed": 0, "entered": 0}

    class _ForbiddenClient:
        def __init__(self, *a, **kw):
            entered["constructed"] += 1

        async def __aenter__(self):
            entered["entered"] += 1
            raise AssertionError("подключение не должно выполняться")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(diag, "AsyncSandboxClient", _ForbiddenClient)

    with pytest.raises(SystemExit) as ei:
        await diag.main()

    assert ei.value.code == 2
    # Клиент ни создан, ни открыт: ни одного обращения к API.
    assert entered == {"constructed": 0, "entered": 0}


# ---------------------------------------------------------------------------
# main(): отсутствует член enum при явной заявке -> прерывание БЕЗ запроса
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_main_member_missing_mid_loop_no_api_call(monkeypatch):
    """Явная заявка с EXCHANGE при enum без нужного члена: main прерывается
    (код 2), get_sandbox_order_state не вызывается ни разу."""
    monkeypatch.setattr(diag, "OrderIdType", _PartialOrderIdType)
    monkeypatch.setattr(diag.config, "validate", lambda: None)
    monkeypatch.setattr(diag.config, "USE_SANDBOX", True)
    monkeypatch.setattr(diag.config, "TINKOFF_ACCOUNT_ID", "acc-test")

    calls = []

    class _FakeSandboxCtx:
        async def get_sandbox_order_state(self, **kwargs):
            calls.append(dict(kwargs))
            return None

    class _FakeCtxClient:
        def __init__(self, *a, **kw):
            self.sandbox = _FakeSandboxCtx()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(diag, "AsyncSandboxClient", _FakeCtxClient)
    monkeypatch.setattr(
        diag.sys, "argv",
        ["diag_order_state.py", "some-order-id", "EXCHANGE"],
    )

    with pytest.raises(SystemExit) as ei:
        await diag.main()

    assert ei.value.code == 2
    assert calls == []  # API физически не вызывался
