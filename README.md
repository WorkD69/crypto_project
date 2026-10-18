# Crypto Research Platform — feasibility spike и MVP 0.1–0.3

**18.10.2026:** один учебный материал прошёл ручной импорт → SQLite-событие → выбор инструментов Hermes → карточка с 7 правилами → черновик → SQLite-журнал. [Итоговый отчёт](docs/hermes-first-analytical-cycle-report.md), [карта адаптера бота](docs/crypto-illia-bot-map.md). Автоматический сбор выключен; 4 логических запроса Luna с частично измеренным расходом. Это учебный результат, торговой сделки нет.

**Текущее дополнение, 17.10.2026:** Stage 0 переработан в лёгкий Windows Telegram Watcher с проверкой свежести видимого UI, повторным использованием медиа и SQLite-очередью событий. Один собственный текстовый тест прошёл через установленный Hermes CLI; автоматическая передача авторских публикаций и расписание выключены. [Инструкция](docs/browser-collector.md), [отчёт Hermes-first](docs/hermes-first-watcher-report.md), [первый отчёт Stage 0](docs/stage-0-report.md), [границы архитектуры](docs/architecture-constraints.md). Описание ниже относится к историческим MVP.

Исследование от 12 октября 2026 года. Это отдельный локальный проект для проверки технических компонентов, а не торговый бот или работающий сборщик каналов. MVP 0.1 создал график и черновик из реальных закрытых Bybit-свечей, но пост не публиковал. Реальных ордеров, торговых API-ключей и фоновых сервисов нет.

MVP 0.2–0.3 связал публичный урок по ликвидности с закрытыми BTCUSDT-свечами Bybit 4H и контекстом 1D. Созданы датированный график, тестовый пост и SQLite-дневник. Один пост после явного подтверждения владельца опубликован в его собственном Telegram «Избранном»; подробности в [отчёте](docs/mvp-0.2-0.3-report.md).

## Результаты

- [MVP 0.1: реальные проверки браузера, Qwen, видео, Bybit, графика и черновика](docs/mvp-0.1-report.md)
- [MVP 0.2–0.3: связанный эксперимент](docs/mvp-0.2-0.3-report.md)
- [Карточка методики](docs/method-card-liquidity.md)
- [Следующие шаги](docs/mvp-next-steps.md)
- [Общий отчёт](docs/feasibility-report.md)
- [Доступ к источникам и права](docs/source-access.md)
- [Обзор библиотек](docs/open-source-review.md)
- [Ресурсы и очереди](docs/resource-estimates.md)
- [Риски и блокеры](docs/risks-and-blockers.md)

## Установка

Требуется Python 3.12. Из PowerShell в каталоге проекта:

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-mvp.txt -r requirements-collector.txt
.venv\Scripts\python.exe -m playwright install chromium
if (-not (Test-Path config\collector.sources.json)) {
    Copy-Item config\collector.sources.example.json config\collector.sources.json
}
$env:COLLECTOR_BROWSER_TESTS = '1'
.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py'
```

Для видео отдельно нужен `ffmpeg` в PATH; для локальной Qwen — установленный Ollama и нужная модель. Они не нужны для автоматических тестов. Необязательный MTProto-пробник требует `pip install telethon==1.45.0`; браузерный Watcher работает через Playwright.

Реальный `config/collector.sources.json` игнорируется Git и сохраняется только локально. В примере включены лишь собственные тестовые материалы; каналы выключены и требуют локальной настройки и разрешений. Не заменяйте существующую рабочую конфигурацию примером. Приватные инвайты, идентификаторы и сессии не должны попадать в коммиты.

В репозитории сохранены код и SQLite-схемы в `scripts/collector/store.py` и `scripts/idea_journal.py`; живые базы, медиа и веса моделей исключены. Исторические отчёты опубликованы в очищенной редакции. См. [границы публикации](docs/publication-safety.md).

## Воспроизведение безопасных проверок

Из PowerShell в каталоге проекта:

```powershell
python scripts/probe_public.py
python scripts/confluence_demo.py
python scripts/media_probe.py
python scripts/unreadable_vision_probe.py
python -m unittest discover -s tests -p 'test_mvp_scenario.py'
python scripts/run_mvp.py
python scripts/video_probe.py
python -m scripts.run_research
python -m scripts.journal_cli status <idea-id>
python -m scripts.journal_cli check <idea-id>
python -m unittest discover -s tests -p 'test_*.py'
```

`probe_public.py` делает небольшие публичные GET-запросы к Telegraph и Bybit. Следует соблюдать лимиты Bybit; повторный запуск слишком часто не нужен. `media_probe.py` создаёт собственный график из последних закрытых BTCUSDT-свечей и делает один вызов установленной локальной Qwen. Изображение и тестовое видео находятся в игнорируемом `artifacts/`.

`run_mvp.py` делает два публичных GET-запроса Bybit, сохраняет тестовый график `artifacts/mvp_btcusdt_1h.png`, машинный результат и `artifacts/telegram_draft.txt`. `video_probe.py` проверяет ffmpeg и локальный faster-whisper на собственном коротком MP4. `local_content_probe.py --image <путь>` или `--text-stdin` отправляет разрешённый материал только в локальную Qwen через Ollama.

`run_research.py` делает три публичных GET Bybit (4H, 1D, инструмент), пишет датированные файлы в `artifacts/mvp_02_03/` и создаёт новую неизменяемую запись идеи. Повторный запуск создаёт новую идею; для прежней используйте `journal_cli check <idea-id>`. Команды `status`, `check`, `cancel`, `assume-fill`, `complete`, `publish` не выставляют биржевых ордеров. `telegram_video_probe.py <разрешённый-файл.mp4>` делает локальную русскую транскрипцию и кадры, не читая Telegram-сессию.

Проверенные версии Python-зависимостей указаны в [requirements-mvp.txt](requirements-mvp.txt); для видео отдельно нужен `ffmpeg` в PATH. SQLite входит в стандартную библиотеку Python.

Сейчас проверен уже авторизованный Telegram Web в браузере Codex, без API ID/hash и Telethon. Исходный MTProto-пробник остаётся отдельным вариантом на будущее и требует вашего **локального интерактивного** входа:

```powershell
.venv\Scripts\python.exe scripts\telegram_own_source_probe.py --peer me --watch-seconds 30
```

Скрипт ничего не отправляет и не нажимает. Секреты запрашиваются скрытым вводом. Файл сессии хранится вне репозитория в `%LOCALAPPDATA%\CryptoResearchProbe\own_source.session`; его нельзя передавать Hermes или загружать в Git. Передача доступа к собственному каналу возможна отдельным запуском с `--peer <username>` после проверки прав. Скрипт выводит только метаданные последних 20 сообщений и ID новых событий без текста и медиа.

## Границы

Hermes расположен в `%USERPROFILE%/Projects\hermes-platform` и использован только для чтения документации и одной CLI-проверки собственного изображения. Игровой бот не затронут. Подробные воспроизводимые действия и ограничения указаны в отчёте.
