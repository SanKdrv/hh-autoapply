# Хостинг: где держать систему 24/7

Коротко: **автоотклик требует браузера, живой сессии hh и IP, которому hh доверяет.**
Поэтому варианты сильно неравноценны.

## Сравнение

| Вариант | Поиск и письма | Автоотклик | Цена | Проблемы |
|---|---|---|---|---|
| **Mac + launchd** | ✅ | ✅ | 0 ₽ | Mac должен быть включён |
| **Raspberry Pi / домашний сервер** | ✅ | ✅ | ~5–8 тыс. ₽ один раз | Нужно настроить Linux и один раз залогиниться |
| **Oracle Cloud Free Tier (VM)** | ✅ | ⚠️ | 0 ₽ бессрочно | Датацентровый IP → капчи; вход в аккаунт только через туннель |
| **GitHub Actions / Render / Fly.io** | ⚠️ | ❌ | 0 ₽ | Нет диска и состояния, IP датацентра, headless-логин неудобен |

Почему датацентровые IP плохи: мы это уже видели на практике — `api.hh.ru/vacancies`
отдаёт `403` из-за геоблока, а сайт встречает DDoS-Guard. С домашнего IP такого нет.

## Вариант A. Mac + launchd (рекомендую)

Уже настроено:

```bash
./scripts/install_launchd.sh       # цикл каждые N часов
./scripts/install_bot_launchd.sh   # Telegram-бот постоянно
```

Ограничение: Mac должен быть включён и не спать. Можно разрешить пробуждение по
расписанию (`pmset repeat wake`) или просто держать его подключённым.

## Вариант B. Raspberry Pi или домашний сервер

Тот же код, но Linux и всегда включён:

```bash
sudo apt install -y python3-venv chromium chromium-driver
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env
```

В `config.json` поставь:

```json
"browser": { "name": "chrome", "use_system_profile": false, "headless": true, "debug_port": 9222 }
```

Затем один раз залогинься (см. раздел про вход ниже) и включи systemd-юнит или cron.

## Вариант C. Oracle Cloud Free Tier

Бессрочно-бесплатная VM (ARM Ampere, 4 vCPU / 24 ГБ RAM — для этой задачи с запасом).

```bash
# на сервере
git clone https://github.com/SanKdrv/hh-autoapply.git && cd hh-autoapply
cp .env.example .env && nano .env
docker build -t hh-autoapply .
docker run --rm -it -p 9222:9222 --env-file .env -v "$PWD/data:/app/data" hh-autoapply login
```

**Вход в аккаунт hh на сервере.** Куки Chromium зашифрованы ключом ОС, поэтому
перенести залогиненный профиль с Mac на Linux нельзя — логиниться надо на сервере.
Помогает порт отладки (уже поддержан конфигом `browser.debug_port`):

1. На сервере запускается `login` с `debug_port: 9222` — браузер остаётся открытым.
2. С Mac открываешь SSH-туннель:
   ```bash
   ssh -L 9222:localhost:9222 user@server
   ```
3. В локальном Chrome/Edge открываешь `http://localhost:9222` — там список вкладок
   сервера. Открываешь вкладку hh и входишь в аккаунт руками (код придёт в SMS/на почту).
4. Профиль на сервере сохранён — дальше работает без участия.

Дальше — постоянный запуск:

```bash
docker run -d --name hh --restart unless-stopped --env-file .env \
  -v "$PWD/data:/app/data" -v "$PWD/letters:/app/letters" -v "$PWD/out:/app/out" \
  hh-autoapply
```

Риск: IP Oracle — датацентровый. Поиск и генерация писем будут работать, а вот
автоотклик может регулярно упираться в капчу. Именно поэтому вариант B надёжнее.

## Что бы я выбрал

1. **Сейчас** — вариант A: всё уже работает, ничего доплачивать не нужно.
2. **Если Mac должен выключаться** — Raspberry Pi (вариант B): домашний IP, ни капч,
   ни геоблоков, круглосуточно и почти без энергопотребления.
3. **Облако** — только если Pi нет и хочется «в никуда»: Oracle Free, но с готовностью
   к капчам на откликах.
