# halooglasi

Монитор новых квартир на [halooglasi.com](https://www.halooglasi.com): раз в N секунд смотрит выдачу и постит свежие объявления в Telegram-группу.

Фильтры живут в `config.py`. Токен бота и id чата — в `.env` (или тоже в `config.py`).

## Настройка

1. Создай бота у [@BotFather](https://t.me/BotFather), добавь его в группу, дай право писать.
2. Узнай id группы: напиши что-нибудь в чат, затем открой `https://api.telegram.org/bot<TOKEN>/getUpdates` и возьми `chat.id` (для групп это отрицательное число, часто `-100…`).
3. Скопируй `.env.example` → `.env` и заполни:
   ```
   TELEGRAM_BOT_TOKEN=123456:ABC...
   TELEGRAM_CHAT_ID=-1001234567890
   ```
4. В `config.py` выставь поиск. Самый простой путь — открыть halooglasi, накликать фильтры и вставить получившийся URL в `SEARCH_URL`. Либо задай поля:
   - `PRICE_FROM` / `PRICE_TO` — евро
   - `AREA_FROM` / `AREA_TO` — м²
   - `ROOMS_FROM` / `ROOMS_TO` — 1.0, 1.5, 2.0, …
   - `ADVERTISERS` — `["vlasnik"]` / `["agencija"]`
   - `FURNISHED` — `["namešteno"]` и т.п.

Первый прогон только запоминает текущую выдачу (`SEED_ON_FIRST_RUN = True`), чтобы не залить группу старыми объявлениями.

## Запуск локально

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python main.py --once --dry-run   # проверить парсер без Telegram
python main.py                    # крутиться и постить
```

## Docker

```bash
docker compose up -d --build
docker compose logs -f
```

Состояние уже виденных объявлений хранится в `data/seen.json`.
