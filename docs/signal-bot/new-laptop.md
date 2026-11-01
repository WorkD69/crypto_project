# Продолжение на другом ноутбуке

Вся работа лежит в GitHub, а данные бота лежат на сервере. На ноутбук переносить нечего. Нужны только код, Python и новый SSH-ключ.

## 1. Код и тесты

Нужны Git, Git Bash и [uv](https://docs.astral.sh/uv/). Команды для PowerShell:

```powershell
git clone https://github.com/WorkD69/crypto_project.git
cd crypto_project
git switch feat/signal-bot
uv python install 3.12
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements-mvp.txt -r requirements-collector.txt -r requirements-signal-bot.txt
.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

Должно быть `OK`. Браузерные тесты пропускаются, пока не задана переменная `COLLECTOR_BROWSER_TESTS`.

## 2. Новый SSH-ключ

Ключи между ноутбуками не копировать. На сервер пускают только по ключу.

Создать ключ в Git Bash на новом ноутбуке:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_stellar_bot
cat ~/.ssh/id_ed25519_stellar_bot.pub
```

Добавить публичный ключ одной строкой в `/root/.ssh/authorized_keys` на сервере:

- с действующего ноутбука по SSH;
- или через консоль в панели AdminVPS, если действующего ноутбука нет.

Если старый ноутбук утрачен, удалить его строку из `authorized_keys`.

## 3. Адрес сервера

Адрес берётся из панели AdminVPS.

```bash
cp deploy/server.env.example deploy/server.env
```

В `deploy/server.env` указать адрес сервера и проверить вход:

```bash
source deploy/server.env
ssh -i "$SSH_KEY" -o UserKnownHostsFile="$HOME/.ssh/known_hosts" "$SERVER" systemctl is-active crypto-signals
```

Должно быть `active`.

## 4. Что не в GitHub

Это нормально, так и задумано: репозиторий публичный.

| Что | Где лежит | Как получить |
| --- | --- | --- |
| Адрес сервера | панель AdminVPS | записать в `deploy/server.env` |
| Секреты, база, сессия Telegram | сервер | не переносятся |
| Эталон: посты, картинки, разметка, ответы модели | сервер, `/var/lib/crypto-signals/golden/` | по SSH, если нужен локально |
| Уроки и профили авторов | сервер | по SSH, если нужны локально |

Локальная папка `private/` на старом ноутбуке это только копии с сервера и страницы для проверки разметки. Если она потеряется, ничего важного не пропадёт.

Работа идёт в ветке `feat/signal-bot`, а не в `main`, поэтому шаг с `git switch` обязателен.

## 5. Куда смотреть дальше

Открыть [STATUS.md](STATUS.md): там записано, на чём остановилась работа и какой следующий шаг. Правила лежат в [CLAUDE.md](../../CLAUDE.md), работа с сервером описана в [operations.md](operations.md).
