# Feasibility report — 12 октября 2026

> Публичная редакция: локальные идентификаторы и приватные сведения удалены; исходный отчёт хранится только локально.

**Исторический снимок первоначального spike.** Последующая проверка уже авторизованного Telegram Web, локальной Qwen, демонстрационного сценария на реальных свечах и черновика описана в [MVP 0.1](mvp-0.1-report.md); статусы Telegram ниже относятся к моменту первого spike.

## Вывод

Лёгкий исследовательский MVP технически возможен: публичные Bybit V5 данные и Telegraph читаются с Windows и VPS; Hermes Luna и уже установленная локальная Qwen прочитали собственный Bybit-график; локальный video pipeline извлёк аудио, кадр и таймкоды. Telegram MTProto ещё не прошёл авторизованный runtime-тест. Главный внешний блокер для анализа авторских материалов — разрешения и условия Telegram/правообладателей, а не отсутствие библиотеки. Это не доказательство качества торговых сигналов или авторской стратегии.

## Аудит Hermes

Из `hermes-platform/README.md`, `PROJECT_CONTEXT.md`, `docs/tools-and-security.md`, `docs/model-routing.md`, `docs/local-models-2026-10-11.md` и живой read-only проверки:

- Hermes Agent v0.21.6 на VPS с Luna через Codex OAuth; Sol доступна вручную. Общий shell/host files и медиа-инструменты выключены. Есть web, отдельный workspace/Python MCP, очередь Qwen и браузер Windows через Tailscale. Текущий `local-jobs` принимает только короткие текстовые `classify/facts/summary`, не изображения/видео.
- SSH-контроль: Bybit и Telegraph с VPS ответили HTTP 200; `free -m` показал 938 MiB available, `df -h /` 5.3 GB свободно, Hermes 361.4 MiB и два проверенных sidecar 83.68/61.52 MiB. Существующие контейнеры не перенастроены, игровой бот не затронут.
- В Hermes CLI обнаружен `--image`. Собственный PNG временно передан в контейнер и обработан `hermes chat -Q --image ... -m gpt-6-luna`. Ответ назвал BTCUSDT, 1H и тестовую зону 82,958.6–83,096.0 USDT. Запись конкретной сессии `LOCAL_SESSION_OMITTED` в `state.db`: `model=gpt-6-luna`, `tool_call_count=0`, 1 основной API-вызов, 11,767 input / 38 output tokens; title generation ещё 268/15 tokens. Значит сработал прямой мультимодальный ввод модели, отдельный vision tool не вызывался. Telegram-доставка изображения в Hermes не проверена.
- Локальный `/api/show` Ollama для Qwen3.5-4B показал `vision` capability. На том же PNG реальный `/api/chat` при `think=false` прочёл BTC/USDT, 1H, TEST ZONE и обе границы за 4.34 с, 769 prompt / 103 output tokens. Первая попытка с лимитом 160 output tokens дала пустой content; режим генерации и бюджет важны. На отдельном собственном графике без актива, таймфрейма и ценовых подписей модель ответила, что точные данные не указаны/нечитаемы (60 prompt / 176 output tokens), без выдуманных уровней. Два примера не доказывают устойчивость на произвольных скриншотах. Значения первой зоны привязаны к 20 реальным закрытым свечам в `media_probe.py`, но это тестовая область, не торговый сетап.

## Матрица проверок

| Компонент | Статус | Что реально проверено / граница вывода |
| --- | --- | --- |
| Telegraph page + `getPage` | **PASSED** | HTTP 200 и `ok=true`, 35 top-level nodes, 7,495 символов извлечённого текста, 10 image+caption пар, порядок Node сохранён. Неверный path: `ok=false`, `PAGE_NOT_FOUND`. Полный урок не сохранён. |
| Bybit Windows | **PASSED** | 898 торгуемых linear, BTC/ETH 15m/1H/4H по 5 свечей, descending timestamps, первая незакрыта; BTC tick size `0.10`, min qty `0.001`, funding interval 480 мин; funding и OI по 2 записи. |
| Bybit VPS | **PASSED** | `curl` `/market/time` и BTC 15m kline HTTP 200 из VPS. Список/все интервалы на VPS отдельно не прогонялись. |
| История Bybit/лимиты | **PARTIAL** | BTC 4H данные за 2021/2022/2024 есть; неделя января 2020 пустая. Один запрос вернул HTTP 200, `retCode=10006`; последующий повтор 2020 дал `retCode=0`, пустой список. Точную первую доступную свечу и устойчивую нагрузку не искали. |
| Telethon библиотека | **PARTIAL** | Установлена 1.45.0 в `.venv`, импортированы Client/NewMessage/Album, проверены методы history/download. Без входа фактические сообщения, скорость/RAM и reconnect не проверены. |
| Telegram account/media/bot | **NOT TESTED** | Владелец согласился на локальный вход, но результат ещё не получен. Нет чтения своего канала, медиа, альбома, кнопок, бота или новых событий. |
| Hermes текст | **PASSED ранее / PARTIAL здесь** | В документации Hermes зафиксированы вызовы Luna web/workspace/Python и текстовой Qwen-очереди. В этом spike отдельный новый документ через Hermes не прогонялся. |
| Hermes график | **PASSED для одного ясного PNG** | Реальный Luna CLI `--image`, подтверждён ответ и ноль tool calls. Telegram image path и статистическая надёжность не проверены. |
| Локальная Qwen vision | **PASSED на двух тестовых PNG / PARTIAL надёжность** | `/api/show` vision и два реальных `/api/chat`: ясный график распознан, на графике без данных модель отказалась называть точные уровни. Нужен корпус с эталонной разметкой. |
| Собственное видео | **PASSED локально / PARTIAL Hermes** | 9.953-секундный MP4 111,786 B из собственного графика и TTS; ffmpeg извлёк WAV и кадры t=1/5/8 с, faster-whisper tiny.en дал три сегмента, сопоставленные по ближайшей середине с кадрами 1/5/8. Hermes v0.21.6 в текущем контуре не имеет проверенного video ingestion/transcription tool. Английская tiny.en не проверяет русскую речь; позже нужна малая multilingual-модель и собственный русский тест. |
| Confluence mechanics | **PASSED синтетически** | 5 assertions: пересечение цен, LONG/SHORT конфликт, просрочка, зависимые группы, несовпадающие области; `WATCHLIST/CANDIDATE/REJECTED`. Не торговая стратегия. |
| Автопостинг/торговля | **NOT TESTED** | Вне scope; ничего не публиковалось и ордера не создавались. |

## Telegram: возможности и пробелы

У [Telethon stable client API](https://docs.telethon.dev/en/stable/modules/client.html) есть `iter_messages`, `min_id`, `download_media`; [events](https://docs.telethon.dev/en/stable/modules/events.html) включает `NewMessage` и `Album`. У Message имеются ID, дата, sender, text/caption, photo/video/document, `grouped_id`, buttons. Публичная ссылка строится по username канала и message ID; в приватном источнике публичной ссылки нет. Для инкрементальности использовать live updates плюс последнюю подтверждённую ID и catch-up `min_id` после разрыва, с дедупликацией; нельзя полагаться только на бесконечный polling всей истории. Это обоснование по API, **не** измерение реального аккаунта. Нажатие кнопок и отправка команд сторонним ботам не выполнялись. Подробности доступа — в [source-access.md](source-access.md).

## Telegraph

`probe_public.py` действительно прочитал JSON-дерево указанной страницы и рекурсивно посчитал текст, сохранив порядок узлов. `figure` содержит `img` и `figcaption`, поэтому связь подписи с картинкой доступна структурно. API-ответ на неверный path показывает, что проверять надо JSON `ok`, а не только HTTP. Публичная страница не означает лицензии на хранение или AI-переработку; см. [официальный API](https://telegra.ph/api).

## Bybit

Официальный [V5 Kline API](https://bybit-exchange.github.io/docs/v5/market/kline) документирует обратный порядок и то, что `closePrice` незакрытой свечи является последней сделкой. Скрипт сравнивает `startTime + interval` с текущим UTC и исключает незакрытую свечу при построении графика. Количество 898 — снимок на момент запроса, включая все linear `Trading` записи страницы без курсора, не гарантированное постоянное число. Список `instruments-info` и [лимиты Bybit](https://bybit-exchange.github.io/docs/v5/rate-limit) надо пересматривать при реализации; единичный `10006` не объяснён официальным 600/5s IP пределом и требует аккуратного backoff/наблюдения. Регионального блока в этих двух сетях не обнаружено.

## Минимальный Confluence Engine

`scripts/confluence_demo.py` принимает синтетические `Factor(kind, low, high, direction, timeframe, detected_at, rule_version, dependency_group)`. Общая область — `max(low)..min(high)`. Конфликт направления, устаревший/будущий фактор и отсутствие общей области дают `REJECTED`; две независимые группы — `WATCHLIST`, три — `CANDIDATE`. Liquidity и Fibonacci от одного swing остаются одной зависимой группой. Число групп — технический счётчик, **не вероятность выигрыша**. Следующий шаг: адаптер только закрытых Bybit-свечей и кандидаты факторов с provenance `(symbol, timeframe, candle ID, rule version, source swing)`; сами формулы авторских паттернов определять только после разрешённого изучения уроков и отдельной валидации. ТВХ/SL/TP/R:R пока не рассчитываются.

## Следующий этап

Приоритет: завершить локальную Telegram-проверку на собственных сообщениях, установить права на выбранные учебные источники, формализовать определения автора, затем в Codex Plan Mode спроектировать минимальный collector + replayable факторный движок + валидацию без публикации и торговли. Ресурсы, очереди и квоты описаны в [resource-estimates.md](resource-estimates.md), выбор библиотек — в [open-source-review.md](open-source-review.md), решения и блокеры — в [risks-and-blockers.md](risks-and-blockers.md).
