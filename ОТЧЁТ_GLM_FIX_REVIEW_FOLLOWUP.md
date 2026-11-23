# ОТЧЁТ — дополнительная задача после аудита GLM_FIX_REVIEW_20261123

Дата: 24.11.2026
Коммит проверки: **`aecf1e6d0dfa4faa02a47abd05ba3f78495559cf`** — репозиторий
`cristal-hub/GLM_FIX_REVIEW_20261123`, ветка `main`, единственный коммит
(загружен 23.11.2026; содержит ровно 5 файлов: `main.py`, `execution.py`,
`test_price_accounting_fixes.py`, `price_accounting_fix.diff`,
`remaining_failures.txt`).

Ограничения соблюдены: стратегия, SL/TP, риск-модель и параметры торговли не
менялись; никаких реальных заявок; никаких LIVE-запросов; production-код
`main.py` / `execution.py` остался байт-в-байт равным коммиту `aecf1e6d`
(SHA256 совпадают, см. раздел 5); журналы, `state.json`, `config.py`, `.env`
не тронуты.

---

## 1. diag_order_state.py — добавлен в пакет репозитория

Файл: `diag_order_state.py` (в этом пакете, рядом с отчётом; для загрузки в
репозиторий `GLM_FIX_REVIEW_20261123` в корень). Push из песочницы невозможен
(нет учётных данных GitHub) — файл приложен готовым к загрузке.

Гарантии (проверены автономным сухим прогоном с фейковым клиентом,
6 сценариев — все прошли):

- **Исключительно READ-ONLY**: единственный торговый вызов —
  `sandbox.get_sandbox_order_state(...)` (плюс read-only
  `get_sandbox_orders`, `users.get_accounts`, `instruments.get_instrument_by`).
  Post/cancel/replace/pay_in/создание счетов отсутствуют (в тесте обёртки
  запрещены на уровне AssertionError и ни разу не вызываются).
- **Токен не печатается** — в dry-прогоне добавлена проверка, что маркер
  фейкового токена не появляется в выводе; `account_id` маскируется
  (`acccou...`), ошибкам печатаются только `type(e).__name__`, без `str(e)`.
- **Выводит** по каждой проверяемой заявке: `object_type` (тип объекта ответа),
  `status` (число + символическое имя из официального enum), `direction`,
  `figi`, `order_id` (биржевой) / `order_request_id` (клиентский UUID),
  `lots_requested` / `lots_executed` / `lots_left` / `lots_cancelled`,
  **`executed_order_price` = {units, nano, currency} + Decimal**,
  **`executed_commission` = {units, nano, currency} + Decimal**,
  `initial_order_price`, `average_position_price`, `lot_size`,
  **`stages[]` с price (units/nano/currency) и quantity**.
- **Поиск существующей заявки без отправки новых**: аргументы командной
  строки (`python diag_order_state.py <order_id> [EXCHANGE|REQUEST]`) либо
  авто-поиск в `order_ledger.jsonl` (роль ID известна: `exchange_order_id` →
  EXCHANGE, `order_id`/`order_request_id` → REQUEST), `state.json`
  (`entry_order_id`, `close_order_request_id`, `close_orders[...].close_order_id`),
  `research_events.jsonl`, `research_trades.csv`, затем активные заявки
  песочницы. Лимит — 12 запросов состояния за прогон.
- **ЭТАП 5Б соблюдён**: тип идентификатора передаётся ЯВНО
  (`ORDER_ID_TYPE_EXCHANGE` / `ORDER_ID_TYPE_REQUEST`), UNSPECIFIED не
  используется никогда; UUID без указанного типа → скрипт просит уточнить тип
  и НЕ выполняет запрос (проверено сценарием 3 dry-прогона).
- **Безопасный выход**: если подходящей существующей заявки нет (журналы
  пусты, активных заявок нет, все кандидаты дали `state=None`) — печатается
  «ПОДХОДЯЩЕЙ СУЩЕСТВУЮЩЕЙ ЗАЯВКИ НЕ НАЙДЕНО», пояснение и завершение с
  кодом 0; новая заявка ради диагностики не создаётся.

## 2. Инструкция запуска теста исправлена

Причина: файл `test_price_accounting_fixes.py` в коммите `aecf1e6d` лежит в
**корне** репозитория, но его шапка сама содержала неверную инструкцию
(строка 1 `# tests/test_price_accounting_fixes.py` и строка 28
`# Запуск из корня сборки: pytest tests/test_price_accounting_fixes.py -v`).

Правка — ровно две строки шапки, файл не перемещался, код тестов не менялся
(остальное байт-в-байт):

```diff
-# tests/test_price_accounting_fixes.py
+# test_price_accounting_fixes.py
...
-# Запуск из корня сборки: pytest tests/test_price_accounting_fixes.py -v
+# Запуск из КОРНЯ репозитория (файл лежит в корне, НЕ в tests/): python -m pytest test_price_accounting_fixes.py -v
```

Корректная команда запуска из корня репозитория:

```
python -m pytest test_price_accounting_fixes.py -v
```

После правки 10 новых тестов повторно прогнаны — **10 passed** (вывод в
`RUN_NEW_TESTS_OUTPUT.txt`).

## 3. Тесты на точной версии файлов коммита aecf1e6d

Методика исключения «не той версии»: базовое дерево взято из GitHub main
`cristal-hub-Tsclp_v2.1_volatile_Samsung` @ `118d520e` — именно с ним совпадают
git-blob-хэши «до» из `price_accounting_fix.diff` (`main.py` = 0d71579…,
`execution.py` = f38261b…), поверх наложены три файла из `aecf1e6d` байт-в-байт
(SHA256 в разделе 5). `conftest.py` репозитория сам ставит заглушки SDK при их
отсутствии; Python 3.12.16, pytest 9.0.2, pytest-asyncio 1.3.0.

**10 новых тестов** (`python -m pytest test_price_accounting_fixes.py -v`,
полный вывод — `RUN_NEW_TESTS_OUTPUT.txt`):

```
test_price_accounting_fixes.py::test_1_buy_post_order_response_price_not_divided_by_lots PASSED
test_price_accounting_fixes.py::test_2_buy_order_state_price_extracted_from_total PASSED
test_price_accounting_fixes.py::test_3_sell_post_response_fill_price_saved_to_ledger PASSED
test_price_accounting_fixes.py::test_4_sell_order_state_fill_price_extracted_and_applied PASSED
test_price_accounting_fixes.py::test_5_partial_sell_applies_confirmed_delta_only PASSED
test_price_accounting_fixes.py::test_6_repeated_report_does_not_deduct_lots_twice PASSED
test_price_accounting_fixes.py::test_7_price_unknown_no_fictitious_pnl PASSED
test_price_accounting_fixes.py::test_8_reconcile_close_consistent_state_and_ledger PASSED
test_price_accounting_fixes.py::test_9_restart_after_sell_no_resale PASSED
test_price_accounting_fixes.py::test_10_broker_error_none_timeout_keeps_uncertain PASSED
============================== 10 passed in 0.25s ==============================
```

**Старый набор** (`python -m pytest tests/ -v`, полный вывод —
`RUN_OLD_TESTS_OUTPUT.txt`, список — `OLD_FAILURES_43.txt`):

```
======================== 43 failed, 255 passed in 8.31s ========================
```

43 старых падения **по-элементно совпадают** с эталоном
`remaining_failures.txt` из коммита `aecf1e6d` (сверка `diff` отсортированных
списков: различие только в усечённых суффиксах ошибок эталона, идентификаторы
`file::test` — 43/43 идентичны). Production-код ради прохождения старых тестов
не менялся, проверки не ослаблялись: `main.py` и `execution.py` в прогоне —
точные байты коммита `aecf1e6d`.

Характер падений прежний: комиссии/сводки PnL в `test_main_helpers`
(модель «модельная vs фактическая» комиссия), late-execution и
re-registered-lots в `test_p0_batch2_late_execution` / `test_p0_defect_fixes`,
2 падения многопроцессорной персистентности, 3 ADX-стрика в `test_strategy`,
`test_execution` (частичная SELL + курсор), по одному в `test_fail_closed`,
`test_main`, `test_buy_registration_fix2*` (×3).

## 4. Семантика `OrderState.executed_order_price` при lot_size > 1

### 4.1 Официальный контракт (дословно, orders.proto официального SDK)

Источник: `protos/tinkoff/invest/grpc/orders.proto` официального репозитория
`RussianInvestments/invest-python` (скачан, выдержка —
`orders_proto_excerpt.txt`; тот же proto входит в `t-tech-investments==1.51.0`):

| Поле | Официальное описание |
|---|---|
| `PostOrderResponse.executed_order_price` | «Исполненная **средняя цена одного инструмента** в заявке.» — цена за штуку, делить запрещено (подтверждено инцидентом LSRG: 858.8 → 37.3391 при 23 лотах). |
| `OrderState.executed_order_price` | «Исполненная цена заявки. **Произведение средней цены покупки на количество лотов**.» — формулировка НЕ уточняет лотность. |
| `OrderState.average_position_price` | «Средняя цена позиции по сделке.» — цена за штуку (эталон для сверки). |
| `OrderStage.price` | «Цена **за 1 инструмент**. Для получения стоимости лота требуется умножить на лотность инструмента.» |
| `OrderStage.quantity` | «**Количество лотов**.» |
| `OrderState.initial_order_price` | «Начальная цена заявки. Произведение количества запрошенных лотов на цену.» — та же неопределённость формулировки. |
| `OrderState.executed_commission` | «Фактическая комиссия по итогам исполнения заявки.» |

### 4.2 Формула и две чтения контракта

Обозначим `EOP` = `OrderState.executed_order_price` (MoneyValue → Decimal),
`L` = `lots_executed`, `S` = `lot_size`. Цена за штуку:

- **Чтение R1 (сумма по инструментам)**: `EOP = P_шт × L × S` →
  `P_шт = EOP / (L × S)`. Именно это реализовано в фиксе
  (`_extract_price_from_order_state`: `total / (int(lots_executed) * int(lot_size))`).
- **Чтение R2 (буквальное «на количество лотов»)**: `EOP = P_шт × L` →
  `P_шт = EOP / L`.

Чтения совпадают при `S = 1` и расходятся ровно на множитель `lot_size` при
`S > 1`. Текст контракта сам по себе выбор НЕ решает: формулировки
`executed_order_price` и `initial_order_price` («произведение … на количество
лотов») не уточняют, входит ли лотность в множитель, а соседнее описание
`OrderStage.price` показывает, что «цена» вproto — всегда за 1 инструмент.
ЭТАП 1 (v2.3) это честно фиксировал: «неопределённость для lot_size > 1
задокументирована в docstring и сознательно не решается (по ТЗ)».

### 4.3 Что подтверждено реальными данными (без Fake-объектов)

Из ваших же реальных журналов (`RESET_ARCHIVE_CLEAN_START_20261123_133004/ORIGINALS_MOVED/`,
22.11.2026, 19 сделок, все `lot_size=1`):

- **PostOrderResponse = цена за штуку — подтверждено на 19/19 сделок**:
  записанная старым кодом `avg_exec_price` точно равна
  `price_before_order / lots` (VKCO 205.25/97=2.1160; UWGN 20.96/953=0.02199;
  SMLT 258.0/77=3.3506; WUSH 35.01/570=0.06140; ETLN 21.04/945=0.02220) —
  то есть сырые значения полей были равны цене за штуку (205.25, 20.96, …),
  а деление на лоты порождало инцидент. Это независимо подтверждает и то,
  что исправление Задачи A (не делить PostOrderResponse) верно.
- **OrderState при lot=1**: якорь ЭТАП 1 — 51171.75/(945×1) = 54.15 (контракт
  «сумма исполнения»), деление на лоты подтверждено на реальном прогоне ЭТАП 1.
- **Для lot_size>1 реального OrderState-ответа в доступных материалах НЕТ** —
  все реальные позиции песочницы имели lot_size=1 (VKCO, UWGN, SMLT, WUSH,
  ETLN, LSRG). Следовательно, R1 против R2 на имеющихся данных не
  дискриминируется — это остаточный риск, а не закрытый факт.

### 4.4 Реальный READ-ONLY ответ: почему не получен здесь и как получить

В этой песочнице выполнить реальный READ-ONLY запрос невозможно:
`TINKOFF_API_TOKEN` отсутствует в среде (в `.env` только `DATABASE_URL`), а
SDK `t-tech-investments` недоступен для установки из индекса PyPI
(«versions: none», проект на карантине) — поэтому ни один запрос к брокеру
не выполнялся, заявки не отправлялись. Подтверждение делается на машине
владельца одной командой — для этого и написан `diag_order_state.py`:

```
python diag_order_state.py <order_id> EXCHANGE
```

В песочнице сейчас есть подходящий инструмент: позиция **SBER (BBG004730N88),
1 лот, lot_size=10** (принята reconcile'ом 23.11, событие POSITION_RECOVERED в
`research_events.jsonl`). Биржевой ID её BUY-заявки следует взять из истории
песочницы/терминала и передать явно. Скрипт напечатает всё необходимое и
сразу посчитает три гипотезы (CHECK-блок, в учёт не идёт):

- `H1: EOP/(lots_executed×lot_size)` — чтение R1 (текущий фикс);
- `H2: EOP/lots_executed` — чтение R2;
- `H3: EOP` — цена за штуку (семантика PostOrderResponse);
- плюс **эталон `average_position_price`** (официально «средняя цена за 1
  инструмент») и среднюю по стадиям `Σ(price_i×quantity_i)/Σquantity_i` —
  семантикой признаётся та гипотеза, которая совпала с эталоном.

Правило принятия решения (зафиксировано заранее, чтобы не подгонять под
результат): если на реальном ответе совпала H1 — формула фикса верна,
ничего не меняется; если совпала H2 — `_extract_price_from_order_state`
требует правки делителя на `lots_executed` (это отдельное согласованное
изменение production-кода, до подтверждения ничего не трогается); если H3 —
необходим отдельный фикс. Пока реальный ответ не получен, действующая
формула R1 сохранена как осознанное допущение ЭТАП 1 (при lot_size=1
математически эквивалентна подтверждённому поведению).

Замечание по документации фикса (не функциональный дефект): docstring
`_extract_price_from_order_state` в `execution.py` называет
`OrderStage.quantity` «количеством штук», а proto — «количество лотов».
Fallback-формула `Σ(price_i×quantity_i)/Σquantity_i` инвариантна к этой
разнице (в обоих случаях получается цена за штуку), поэтому результат
расчёта не меняется; несоответствие зафиксировано здесь как неточность
комментария.

## 5. SHA256 и точный commit SHA

Commit: **`aecf1e6d0dfa4faa02a47abd05ba3f78495559cf`**
(репозиторий `GLM_FIX_REVIEW_20261123`, main; полная история — 1 коммит).

| Файл | Версия | SHA256 |
|---|---|---|
| `main.py` | aecf1e6d = итог (не менялся) | `6570d956067df51e8f1785643889ba335b6a53bd7b8009b9ad1810643799077c` |
| `execution.py` | aecf1e6d = итог (не менялся) | `23dda2d9f230406f342acf7f6b0c889379b6a9a1647a974e609b32261b22a206` |
| `test_price_accounting_fixes.py` | aecf1e6d (до правки инструкции) | `dcfd3e2de23afe8d534b4d0465fe0b266b04a988e40ad925e41425782218b9fd` |
| `test_price_accounting_fixes.py` | итог (после правки 2 строк шапки) | `c980229da8dd6b290980befc73796aa3bba7550bdfa1125e0a587e6e9655e965` |
| `diag_order_state.py` | новый файл | `266f1de8c379d46c0c8d2338687194c12425cf99f8f629ee3a41e25c06f8e434` |

Файл `SHA256SUMS.txt` с теми же значениями приложен.

---

## Вывод

1. 10 новых тестов на точных файлах `aecf1e6d`: **10/10 PASS**.
2. Старый набор: **43 падения — точное воспроизведение** эталонного списка,
   production-код не тронут.
3. Инструкция запуска исправлена (2 строки шапки, файл не перемещался);
   корректная команда: `python -m pytest test_price_accounting_fixes.py -v`
   из корня.
4. `diag_order_state.py` готов к добавлению в репозиторий (push из песочницы
   невозможен — файл приложен).
5. Семантика `OrderState.executed_order_price` при lot_size>1 по контракту —
   сумма исполнения (R1, реализована в фиксе), но текст контракта это
   однозначно не закрывает; реальных данных с lot_size>1 не существует.
   Перед контролируемым SANDBOX-тестом выполнить на машине владельца
   READ-ONLY `python diag_order_state.py <SBER_order_id> EXCHANGE`
   (SBER, lot_size=10, позиция есть) и зафиксировать гипотезу по
   `average_position_price`. До этого результата изменение формулы не
   производится.

Готовность к контролируемому SANDBOX-тесту: **готово** — при условии
выполнения одного READ-ONLY запуска diag_order_state.py (без отправки заявок)
для закрытия вопроса lot_size>1; сама стратегия, SL/TP, риск-модель и
параметры торговли не менялись.
