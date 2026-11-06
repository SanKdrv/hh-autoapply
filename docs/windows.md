# Переезд на Windows

Приложение кроссплатформенное: macOS, Windows и Linux. Различия только в
расписании и способе удержания сна — код сам определяет платформу.

## Важно перед переездом

**Останови автоматику на старом компьютере.** Два процесса на одном аккаунте hh
могут откликаться на одно и то же и выглядят подозрительно для площадки.

На macOS:

```bash
cd "/Users/aleks/Documents/Default Project/hh-autoapply"
./scripts/mac_sleep.sh on              # вернуть обычный сон
.venv/bin/python main.py stop --all    # стоп и выгрузка агентов
```

## Что переносится, а что нет

| Переносится | Не переносится |
|---|---|
| `data/state.sqlite3` — история откликов и ответов | `data/browser-profile/` — куки зашифрованы под ОС |
| `data/cache/` — кэш карточек вакансий | |
| `letters/` — готовые письма | |
| `.env`, `config.json`, `profile.json` | |

Профиль браузера придётся создать заново: на Windows нужно один раз войти в
аккаунт hh (`main.py login`). История откликов при этом сохранится, если
перенести базу — иначе она подтянется из списка откликов hh при первом `inbox`.

## Установка

### 1. Python

Скачай Python 3.11+ с python.org (галочка «Add Python to PATH» при установке).

```powershell
py --version
```

### 2. Код

```powershell
cd $HOME\Documents
git clone https://github.com/SanKdrv/hh-autoapply.git
cd hh-autoapply
```

Или скопируй папку с Mac целиком, исключив `.venv`, `data\browser-profile`.

### 3. Окружение

```powershell
py -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

### 4. Ключи

```powershell
copy .env.example .env
notepad .env
```

Заполни `DEEPSEEK_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
`LLM_BASE_URL` и `LLM_MODEL` уже настроены на шлюз OpenCode Go.

Проверка связи с Telegram:

```powershell
.venv\Scripts\python main.py notify-test
```

### 5. Вход в аккаунт hh

Откроется Edge с отдельным профилем — войди в аккаунт (код придёт в SMS).

```powershell
.venv\Scripts\python main.py login
```

### 6. Проверка

```powershell
.venv\Scripts\python main.py selftest
```

Ожидаемо: «12 ок, 0 предупреждений, 0 ошибок».

### 7. Расписание

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\install_tasks.ps1
```

Создаются три задачи:

| Задача | Когда |
|---|---|
| `hh-autoapply-cycle` | 10:00, 14:00, 18:00 — циклы |
| `hh-autoapply-bot` | при входе в систему, с автоперезапуском |
| `hh-autoapply-digest` | 21:00 — анализ логов |

Задачи настроены с флагом **Wake to Run**: Windows может проснуться, чтобы
выполнить запуск. Но удерживать систему во время работы всё равно нужно —
этим занимается само приложение (`SetThreadExecutionState`), пока идёт цикл.

Проверить и запустить вручную:

```powershell
Get-ScheduledTask -TaskName 'hh-autoapply*'
Start-ScheduledTask -TaskName 'hh-autoapply-cycle'
```

Логи задач лежат в папке `logs\`.

Удалить расписание:

```powershell
powershell -File scripts\windows\uninstall_tasks.ps1
```

## Отличия от macOS

| | macOS | Windows |
|---|---|---|
| Расписание | launchd | Task Scheduler |
| Удержание сна | `caffeinate` | `SetThreadExecutionState` |
| Будить по расписанию | `pmset repeat wakeorpoweron` | флаг `WakeToRun` в задаче |
| Сон от сети | `./scripts/mac_sleep.sh off` | Электропитание → «Никогда» |
| Профиль браузера | `data/browser-profile` | `data\browser-profile` (то же поле) |

## Если что-то не работает

- **Edge не найден** — Selenium Manager скачает `msedgedriver` сам при первом
  запуске; нужен интернет.
- **Задача не запускается** — Task Scheduler не может управлять GUI-программами
  без активной сессии, поэтому задачи запускаются «только при входе
  пользователя». Это ожидаемо.
- **Ноутбук уснул посреди цикла** — цикл прервётся, но повторов не будет
  (защита от дублей в базе). При следующем запуске продолжит с того же места.
- **Капча** — придёт уведомление в Telegram, автоматика встанет. Пройди проверку
  вручную и отправь боту `/resume`.
