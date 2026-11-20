<p align="center">
  <img src="docs/logo.png" alt="Логотип Telegram Digest" width="144">
</p>

<h1 align="center">Telegram Digest</h1>

<p align="center">
  Бот собирает публикации из веб-лент и публичных Telegram-каналов и присылает два дайджеста со ссылками — «Сбор с сайтов» и «Сбор с Telegram».
</p>

## Быстрый старт

Нужен **Python 3.12+**. Бот использует стандартную библиотеку Python.

1. Создайте бота через [@BotFather](https://t.me/BotFather).
2. Скопируйте [`.env.example`](.env.example) в `.env` и заполните `TELEGRAM_BOT_TOKEN` и `ADMIN_USERNAME`.
3. Из корня проекта запустите:

   ```sh
   python3 deploy/run_bot.py
   ```

4. В личном чате с ботом отправьте `/start`, затем `/subscribe`.

## Настройки

| Переменная | Что задаёт |
| --- | --- |
| `SOURCE_EXTRA_URLS` | Адреса RSS, Atom или JSON Feed через запятую |
| `TG_CHANNELS` | Имена публичных Telegram-каналов через запятую |
| `SUBSCRIPTION_INTERVAL_MINUTES` | Интервал проверки источников в минутах (по умолчанию 60) |
| `DIGEST_INTERVAL_HOURS` | Интервал отправки дайджестов в часах (по умолчанию 10) |

После изменения `.env` перезапустите бота. Очередь и история ссылок хранятся в SQLite по пути `DB_PATH`.

## Команды

| Команда | Действие |
| --- | --- |
| `/start` | Привязать аккаунт владельца |
| `/subscribe` · `/unsubscribe` | Включить или приостановить автоматический сбор и отправку |
| `/digest` | Сразу отправить оба накопленных дайджеста |
| `/fetch` · `/fetchtg` | Сразу собрать публикации с сайтов или из Telegram |
| `/status` | Посмотреть расписание, очередь и доступность источников |
| `/help` | Показать команды и статус подписки |

## Запуск на сервере

### systemd
Разместите проект в `/opt/telegram-digest`, сохраните настройки в `/etc/telegram-digest.env` с правами `0600` и укажите `DB_PATH=/var/lib/telegram-digest/rblog.sqlite3`. Установите [юнит службы](deploy/telegram-digest.service):

```sh
sudo install -m 644 deploy/telegram-digest.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-digest
```

Проверьте статус через `systemctl status telegram-digest.service`
Логи можно проверить командой `journalctl -u telegram-digest` или в `logs/bot.log` в корне проекта.

### run

Если `systemctl` на сервере недоступен, возможно будет работать команда `run --help`.

Для запуска скрипта достаточно выполнить `run python3 deploy/run_bot.py`.
Проверить статус можно командой `run --list` или `run status`. 


