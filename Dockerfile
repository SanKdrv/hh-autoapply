# Образ для запуска hh-autoapply на сервере (headless).
#
# ВАЖНО: hh агрессивно защищается от датацентровых IP — возможны капчи и
# ограничения. Для стабильной работы лучше домашняя машина или домашний сервер.
#
#   docker build -t hh-autoapply .
#   docker run -d --name hh --env-file .env -v "$PWD/data:/app/data" \
#       -v "$PWD/letters:/app/letters" -v "$PWD/out:/app/out" hh-autoapply
#
# Перед первым запуском нужно один раз залогиниться в аккаунт hh внутри
# того же профиля браузера:
#   docker run --rm -it --env-file .env -v "$PWD/data:/app/data" hh-autoapply login

FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium chromium-driver ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# В контейнере используем Chromium и headless-режим.
RUN python - <<'PY'
import json, pathlib
p = pathlib.Path("config.json")
cfg = json.loads(p.read_text(encoding="utf-8"))
cfg["browser"]["name"] = "chrome"
cfg["browser"]["use_system_profile"] = False
cfg["browser"]["headless"] = True
p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
PY

VOLUME ["/app/data", "/app/letters", "/app/out"]

CMD ["python", "main.py", "bot"]
