# Исправление fail-open в `diag_order_state.py` (аудит GLM_FIX_REVIEW_20261123)

Дата: 2026-11-24. Ветка: `fix/diag-order-state-failclosed`.
Коммит с фиксом: `4b28f9fb0618f4ab59dfd4c9d992002d0bc4875d`
Базовый коммит (текущая followup-версия как точка отсчёта): `fea1f86`

## Проблема

В `_query_state()` followup-версии при отсутствии в SDK `OrderIdType` или
нужного члена enum печаталось предупреждение, но запрос всё равно
выполнялся **без** `order_id_type` (fail-open):

```python
if not supported:
    print("warning: SDK не экспортирует OrderIdType ...")
else:
    kwargs["order_id_type"] = member
return await client.sandbox.get_sandbox_order_state(**kwargs)
```

Для клиентского UUID такой запрос недопустим: без явного типа
идентификатора семантика `order_id` не гарантирована.

## Исправление (точечное, только `diag_order_state.py`)

1. **`_query_state()`** — полностью fail-closed:
   - enum отсутствует -> `OrderIdTypeUnavailable`, запрос НЕ выполняется;
   - член enum отсутствует/неоднозначен -> `OrderIdTypeUnavailable`, запрос
     НЕ выполняется;
   - неверный режим (не `EXCHANGE`/`REQUEST`) -> `OrderIdTypeUnavailable`,
     запрос НЕ выполняется;
   - `kwargs` ВСЕГДА содержат явный `order_id_type` (реальный член enum);
   - `kind=None` перебирает `EXCHANGE` -> `REQUEST`, оба вызова явные,
     `UNSPECIFIED` не используется никогда.
2. **`main()`** — fail-fast ДО подключения к API: если SDK не экспортирует
   `OrderIdType`, диагностика немедленно завершается с понятной ошибкой
   (код выхода 2); цикл диагностики прерывается по `OrderIdTypeUnavailable`
   с сообщением «запрос к API не выполнялся, ничего не отправлялось».
3. Новый класс `OrderIdTypeUnavailable(RuntimeError)` и константа
   `ORDER_ID_TYPE_KINDS = ("EXCHANGE", "REQUEST")`.

## Изменённые файлы (полный перечень)

| Файл | Изменение |
| --- | --- |
| `diag_order_state.py` | fail-closed фикс `_query_state` + защиты `main()` (+109/−33) |
| `tests/test_diag_order_state_failclosed.py` | НОВЫЙ файл, 16 fake-тестов |

Никакие другие файлы не изменялись: `main.py`, `execution.py`,
`strategy.py`, `risk_manager.py`, `config.py`, торговая логика, расчёт цен,
комиссий, P&L и учёт исполнения — НЕ затронуты. Операции записи (POST,
отмена заявок, pay_in) не добавлялись — в скрипте по-прежнему единственный
API-вызов: read-only `get_sandbox_order_state`.

## Результат запуска новых тестов

`python -m pytest tests/test_diag_order_state_failclosed.py -v`
**16 passed** (полный вывод — `TEST_RUN_OUTPUT.txt`).

Сценарии (все на fake/mock-клиенте, без сети, токенов и заявок):

| Требуемый сценарий | Тест | Статус |
| --- | --- | --- |
| enum отсутствует -> API не вызывается | `test_enum_missing_api_not_called[EXCHANGE/REQUEST]` | passed |
| член enum отсутствует -> API не вызывается | `test_member_missing_api_not_called[EXCHANGE/REQUEST]` | passed |
| корректный EXCHANGE -> вызов с ЯВНЫМ типом | `test_exchange_mode_calls_api_with_explicit_type` | passed |
| корректный REQUEST -> вызов с ЯВНЫМ типом | `test_request_mode_calls_api_with_explicit_type` | passed |
| неверный режим -> API не вызывается | `test_invalid_mode_api_not_called[FOO/''/UNSPECIFIED/123/NoneType]` | passed |
| kind=None: оба режима явные | `test_kind_none_tries_both_modes_with_explicit_types`, `test_kind_none_exchange_hit_no_second_call` | passed |
| kind=None при отсутствии enum: 0 вызовов | `test_kind_none_enum_missing_api_not_called` | passed |
| main() fail-fast ДО подключения (код 2, клиент не создаётся) | `test_main_fails_fast_without_connect_when_enum_missing` | passed |
| main(): член enum отсутствует -> 0 вызовов API | `test_main_member_missing_mid_loop_no_api_call` | passed |

## Подтверждение отсутствия вызова API

Подтверждается двумя независимыми уровнями:
1. **Уровень `_query_state`**: в сценариях 1, 2, 5 тесты утверждают
   `assert client.sandbox.calls == []` — fake-клиент не зафиксировал ни
   одного вызова `get_sandbox_order_state`.
2. **Уровень `main()`**: при отсутствии enum клиент даже не создаётся и не
   открывается (`entered == {"constructed": 0, "entered": 0}`), при
   отсутствии члена enum внутри цикла — `calls == []`.

Механическая проверка кода: единственная точка вызова
`client.sandbox.get_sandbox_order_state(**kwargs)` в скрипте — строка 556,
достижима только после успешного разрешения явного `order_id_type`;
`kwargs` формируются с обязательным ключом `order_id_type`.

## SHA256 (после проверки)

```
c5c2253a22866acc61ff77da6b4efa85446a45f59845fc99d7a2e29489cdd446  diag_order_state.py
add44d4533ab2ca626b3f85a2f9eff06acb46e52c3e477ba14c3ec4ab194dd26  tests/test_diag_order_state_failclosed.py
95a2832773e0247bbb2ea3bd0d98029f267b105781c15ffc80b90e214af07f91  diag_order_state_failclosed.diff
```

Точный commit SHA: `4b28f9fb0618f4ab59dfd4c9d992002d0bc4875d`
(ветка `fix/diag-order-state-failclosed`, репозиторий
`cristal-hub/cristal-hub-Tsclp_v2.1_volatile_Samsung`).

## Ограничения соблюдены

- Стратегия, SL/TP, риск-модель и торговые параметры не менялись.
- Никаких реальных заявок, никаких LIVE-запросов; тесты — только fake.
- Токены и секреты не выводятся (ошибки — только `type(e).__name__`,
  идентификаторы маскируются).
- Исходные followup-файлы не перезаписаны (фикс выполнен в отдельной
  ветке/коммитах; исходная версия сохранена в git-истории и в пакете
  `glmfix_review_20261123_followup`).
- Запуск бота и реальные запросы к брокеру не выполнялись (до отдельного
  разрешения владельца).
