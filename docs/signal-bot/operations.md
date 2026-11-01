# Эксплуатация

Адрес сервера в репозитории не хранится. Он лежит в локальном игнорируемом `deploy/server.env`, а на новом ноутбуке берётся из панели AdminVPS.

## Сервер

- Ubuntu 24.04, 1 CPU, 2 ГБ RAM, около 4.5 ГБ свободного диска. Рядом работают контейнеры Hermes и другие сервисы, их не менять.
- Служба бота `crypto-signals`, пользователь `cryptosignals`, описание в [deploy/crypto-signals.service](../../deploy/crypto-signals.service).

| Что | Где |
| --- | --- |
| Код | `/opt/crypto-signals/signal_bot` |
| Прошлая версия кода | `/opt/crypto-signals/signal_bot.prev` |
| Окружение Python | `/opt/crypto-signals/.venv` |
| База и сессия чтения | `/var/lib/crypto-signals` |
| Настройки | `/etc/crypto-signals/secrets.env`, `/etc/crypto-signals/sources.json` |
| Спул картинок | `/var/lib/crypto-signals/spool` |
| Состояние брокера | `/var/lib/crypto-hermes-broker` |

## Брокер Hermes

Служба `crypto-hermes-broker`, описание в [deploy/crypto-hermes-broker.service](../../deploy/crypto-hermes-broker.service), код в `signal_bot/broker.py`. Брокер работает под отдельным пользователем `crypto-hermes` в группе `docker`. У бота доступа к Docker нет: он говорит с брокером через сокет `/run/crypto-hermes/broker.sock`, куда пускают только группу `cryptosignals`.

Брокер принимает только `{prompt, image_sha256}` и запускает одну фиксированную команду, без инструментов модели. Картинка берётся из `/var/lib/crypto-signals/spool/<sha>.jpg`, на время вызова копируется в спул контейнера и сразу удаляется.

Ограничения: один вызов за раз, 120 секунд на вызов, 300 вызовов в сутки (`BROKER_DAILY_LIMIT`). При признаках лимита брокер делает паузу на час. Счётчик и пауза лежат в `/var/lib/crypto-hermes-broker/state.json`.

Первая установка выполняется из Git Bash после `deploy.sh`, повторный запуск безопасен:

```bash
bash deploy/setup-broker.sh
```

## Эталон

Посты с разметкой лежат на сервере в `/var/lib/crypto-signals/golden/` и в Git не попадают. Команды на сервере, `collect` выполняется при остановленной службе `crypto-signals`, потому что сессия общая:

```bash
systemd-run --wait --pipe -p User=cryptosignals -p Group=cryptosignals     -p EnvironmentFile=/etc/crypto-signals/secrets.env -p WorkingDirectory=/opt/crypto-signals     /opt/crypto-signals/.venv/bin/python -m signal_bot.golden collect
```

Для `run` и `score` команда та же, секреты им не нужны. `run` читает `labels.json` и продолжает с места остановки, а `score` печатает таблицу и проценты.

## Уроки

Список адресов лежит в `/etc/crypto-signals/lessons.json`, формат показан в [deploy/lessons.example.json](../../deploy/lessons.example.json). Читаются только страницы Telegraph из этого списка. Тексты, правила и памятка лежат на сервере в `/var/lib/crypto-signals/lessons/` и в Git не попадают.

Запуск той же командой, что и для эталона: `python -m signal_bot.lessons fetch`, затем `rules` и `digest`. `rules` продолжает с места остановки. Цитаты правил и числа памятки проверяются кодом. Файл `digest.txt` подключается к разбору постов автоматически.

## Подключение

Команды ниже рассчитаны на Git Bash. Первая строка подставляет адрес и ключ из `deploy/server.env`.

```bash
source deploy/server.env
ssh -i "$SSH_KEY" -o UserKnownHostsFile="$HOME/.ssh/known_hosts" "$SERVER"
```

## Деплой

Сначала полный набор тестов, затем:

```bash
bash deploy/deploy.sh
```

Скрипт загружает `signal_bot`, сохраняет прошлую версию в `signal_bot.prev`, перезапускает службу и показывает её состояние, число рестартов, память и последние строки журнала. Нужный результат: `active` и `NRestarts=0`.

Зависимости и файл службы скрипт не трогает. Если они изменились, обновить их на сервере отдельно: `.venv/bin/pip install -r requirements-signal-bot.txt` и `systemctl daemon-reload`.

## Откат

Команды на сервере:

```bash
cd /opt/crypto-signals
rm -rf signal_bot.bad
mv signal_bot signal_bot.bad
mv signal_bot.prev signal_bot
systemctl restart crypto-signals
systemctl is-active crypto-signals
```

Откат возвращает только код. Если новая версия изменила схему базы, перед откатом проверить, что старый код с ней работает.

## Логи и состояние

```bash
systemctl status crypto-signals
systemctl show crypto-signals -p NRestarts -p MemoryCurrent
journalctl -u crypto-signals -n 100 --no-pager
journalctl -u crypto-signals -f
```

Норма: память ниже 300 МБ, 0 рестартов, в журнале нет токена. Логи `httpx` приглушены, потому что в адресах Bot API есть токен.

## Сессия Telethon

Сессией нельзя пользоваться из двух процессов. Перед разовым чтением той же сессии остановить службу, после чтения запустить:

```bash
systemctl stop crypto-signals
# разовая работа с сессией
systemctl start crypto-signals
```

Вход в аккаунт выполняется один раз и вручную на сервере через [deploy/telegram_login.py](../../deploy/telegram_login.py).

## Секреты

Значения хранятся только в `/etc/crypto-signals/secrets.env` на сервере, права `600`. Имена:

- `BOT_TOKEN`: токен Telegram-бота;
- `TELEGRAM_API_ID`: ID приложения Telegram;
- `TELEGRAM_API_HASH`: хеш приложения Telegram.

Необязательные настройки: `BOT_DATA_DIR` и `BOT_CONFIG_DIR`. По умолчанию это пути из таблицы выше.

Смена значения выполняется в локальном окне PowerShell со скрытым вводом. Значение уходит по SSH через stdin и не попадает ни в чат, ни в историю команд. В примере меняется `BOT_TOKEN`, вместо `<SERVER>` подставить значение из `deploy/server.env`:

```powershell
$secure = Read-Host 'Новое значение' -AsSecureString
$value = [Net.NetworkCredential]::new('', $secure).Password
$remote = @'
v=$(LC_ALL=C tr -cd 'A-Za-z0-9:_-'); sed -i '/^BOT_TOKEN=/d' /etc/crypto-signals/secrets.env; printf 'BOT_TOKEN=%s\n' $v >> /etc/crypto-signals/secrets.env; systemctl restart crypto-signals
'@
$value | ssh -i "$HOME\.ssh\id_ed25519_stellar_bot" <SERVER> $remote
Remove-Variable value, secure
```

`tr -cd` оставляет только символы, допустимые в этих трёх секретах: PowerShell может дописать к значению невидимый BOM и перевод строки. Двойные кавычки в `$remote` не использовать: Windows PowerShell 5.1 портит их при передаче в ssh.

## Источники

Список каналов лежит в `/etc/crypto-signals/sources.json`, формат показан в [deploy/sources.example.json](../../deploy/sources.example.json). После правки перезапустить службу. Настоящий файл в Git не попадает.
