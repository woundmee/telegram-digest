# Технологический стек

- Python 3.12+ и стандартная библиотека.
- Облачный [Telegram Bot API](https://core.telegram.org/bots/api) по HTTPS. Бот получает обновления через `getUpdates`, отправляет сообщения через `sendMessage` и очищает старую очередь через `deleteWebhook`.
- RSS 2.0, RSS 1.0, Atom и публичные HTTP-эндпоинты в формате [JSON Feed](https://www.jsonfeed.org/version/1.1/). Ленты по умолчанию доступны у [Хабра](https://habr.com/ru/docs/help/lenta/), [OpenNET](https://www.opennet.ru/opennews/opennews_all.rss) и [Linux.org.ru](https://www.linux.org.ru/section-rss.jsp?section=1).
- SQLite через `sqlite3`, с журналом WAL для локального состояния.
- `zoneinfo` и системная база часовых поясов IANA.
- Linux VPS и systemd. Служба берёт настройки из файла окружения и пишет в journald.
- `unittest` для бизнес-логики и проверки сценариев с подменным Telegram API.

- Публичные Telegram-каналы читаются из HTML веб-лент `t.me/s/username` через `urllib.request` и `html.parser`.
