# Промпт для ИИ-агента: развёртывание на Windows

Скопируй текст ниже целиком в агента на Windows-машине.

---

## Задача

Разверни проект `hh-autoapply` на этом компьютере (Windows) и доведи до состояния,
когда `python main.py selftest` показывает **12 ок / 0 предупреждений / 0 ошибок**.

Проект — автоматизация поиска вакансий и откликов на hh.ru: поиск вакансий,
генерация сопроводительных писем через LLM, отправка откликов через Selenium+Edge,
чтение ответов работодателей, Telegram-бот, запуск по расписанию.

## Что уже есть

- Архив проекта: `%USERPROFILE%\Desktop\hh-autoapply-transfer-*.zip`
  (внутри код, `config.json`, `profile.json`, `.env` с ключами, `letters/`,
  `data\state.sqlite3` с историей откликов).
- Проект поддержан на Python 3.11+.

## Важные факты

- **Профиль браузера НЕ переносится** — куки зашифрованы под ОС. Нужен один
  ручной вход в аккаунт hh (шаг 5).
- Кириллица в логах: код сам включает UTF-8, но запускай через `-X utf8`.
- Задачи планировщика работают только при активном входе пользователя.
- hh меняет вёрстку: не редактируй селекторы без нужды, они уже проверены.

## Шаги

**Важно: шаги 4 и 5 требуют человека.** Шаг 4 — заполнение ключей, если `.env`
не пришёл в архиве. Шаг 5 — вход в hh: появится окно браузера, нужно ввести
телефон и код из SMS. Не пытайся автоматизировать вход — просто запусти команду
и попроси пользователя завершить вход в открывшемся окне. Не закрывай окно сам.

### 1. Распаковать

```powershell
$zip = Get-ChildItem "$env:USERPROFILE\Desktop\hh-autoapply-transfer-*.zip" | Select-Object -First 1
Expand-Archive -Path $zip.FullName -DestinationPath "$env:USERPROFILE\hh-autoapply" -Force
cd "$env:USERPROFILE\hh-autoapply"
Get-ChildItem
```

Ожидается: `main.py`, `hh_autoapply\`, `scripts\`, `config.json`, `.env`, `data\`.

### 2. Проверить Python

```powershell
py --version
```

Если нет — остановись и попроси пользователя установить Python 3.11+ с python.org
с галочкой «Add Python to PATH». Дальше без него нельзя.

### 3. Настроить окружение

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\setup.ps1
```

Скрипт создаст `.venv`, поставит `selenium`, сделает `.env` при отсутствии и
проверит наличие Edge.

### 4. Проверить ключи (может потребоваться человек)

```powershell
Get-Content .env | Select-String "DEEPSEEK_API_KEY|TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID" |
  ForEach-Object { ($_ -split '=')[0] + " = " + $(if (($_ -split '=',2)[1].Trim()) {"<задано>"} else {"<ПУСТО>"}) }
```

Если что-то пусто — попроси пользователя открыть `.env` в блокноте и вписать
значения, затем повтори проверку. Значения не выводи в чат.

```powershell
.venv\Scripts\python -X utf8 main.py notify-test
```

Ожидается: «отправлено».

### 5. Вход в аккаунт hh (делает человек)

```powershell
.venv\Scripts\python -X utf8 main.py login
```

Откроется Edge с отдельным профилем. **Попроси пользователя:** ввести телефон или
почту, получить код и ввести его. Ждать до 15 минут. После успеха в выводе будет
«авторизация успешна, профиль сохранён».

### 6. Самопроверка (критерий приёмки)

```powershell
.venv\Scripts\python -X utf8 main.py selftest
```

**Готово считается только если:** `Итого: 12 ок, 0 предупреждений, 0 ошибок`.

Если есть ошибки — покажи их пользователю целиком и не переходи дальше.

### 7. Расписание

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\install_tasks.ps1
Get-ScheduledTask -TaskName 'hh-autoapply*' | Select-Object TaskName, State
```

Ожидается три задачи: `hh-autoapply-cycle`, `hh-autoapply-bot`,
`hh-autoapply-digest` в состоянии `Ready` (или `Running` для bot).

### 8. Финальный отчёт

Сообщи пользователю:

1. Путь, куда развёрнут проект.
2. Результат `selftest` (строку «Итого: …»).
3. Список созданных задач планировщика и их состояние.
4. Что дальше произойдёт само: циклы в 10:00, 14:00, 18:00; разбор логов в 21:00;
   бот отвечает в Telegram на `/status`, `/funnels`, `/queue`, `/drafts`.
5. Что отклики отправляются **только при питании от сети** (настройка
   `schedule.apply_only_on_ac`), чтобы не жечь батарею.

## Чего делать НЕ надо

- Не запускай `main.py apply` без явной просьбы пользователя: это отправляет
  реальные отклики работодателям.
- Не меняй `data\state.sqlite3` — там история, по ней работает защита от
  повторных откликов.
- Не отправляй ничего в переписку с работодателем про Ozon API
  (вакансия 138387513) — там идёт подготовка к собеседованию.
- Не логируй и не выводи значения ключей из `.env`.
- Не переписывай селекторы Selenium: они проверены на живой странице.

## Если что-то не работает

| Симптом | Причина и решение |
|---|---|
| `UnicodeEncodeError` в логах | запускай с `-X utf8`; код уже настраивает UTF-8 |
| Edge driver не найден | Selenium Manager скачает сам, нужен интернет |
| Задача не запускается | планировщик не управляет GUI без активной сессии — задачи запускаются «при входе пользователя», это нормально |
| `selftest` ругается на браузер | проверь, что Edge установлен и вход в hh выполнен (шаг 5) |
| `selftest` ругается на LLM | проверь `DEEPSEEK_API_KEY` и что `LLM_BASE_URL=https://opencode.ai/inference/go/v1` |

## Полезные команды

```powershell
.venv\Scripts\python -X utf8 main.py stats       # состояние базы
.venv\Scripts\python -X utf8 main.py drafts      # черновики ответов
.venv\Scripts\python -X utf8 main.py run --no-apply --max-detail 5   # безопасный прогон
.venv\Scripts\python -X utf8 main.py stop        # аварийный стоп автоматики
.venv\Scripts\python -X utf8 main.py resume      # снять стоп
```
