# Деплой HH Auto Jobs Applier

Бот — Playwright + Chromium, не serverless. Нужен VPS с диском под `data_folder/` (секреты, сессия hh.ru, отчёты).

## Что положить на сервер

Скопируйте репозиторий и **свою** папку `data_folder/` (она не в git):

- `data_folder/secrets/secrets.yaml` — логин hh, ключ OpenRouter (`llm_api_key`), Telegram
- `data_folder/app_config/app_config.yaml` — на сервере `HEADLESS_MODE: true`
- `data_folder/search_config/search_config.yaml`
- `data_folder/browser_session/hh_state.json` — **прогрейте локально** (headed-логин), затем скопируйте файл. С IP датацентра свежий логин часто ловит капчу

Ключ LLM: [OpenRouter](https://openrouter.ai/keys). Fallback моделей задаётся в `FALLBACK_MODELS` и выполняется на стороне OpenRouter.

## Сборка Docker

Образы собираются и под x86 (Hetzner), и под ARM (Oracle Ampere):

```bash
docker compose build
```

Один прогон:

```bash
docker compose run --rm applier
```

Логи: `logs/`. Результаты откликов: `data_folder/output/`.

## Вариант A — Oracle Cloud Always Free (рекомендуемый $0)

1. Аккаунт на [oracle.com/cloud/free](https://www.oracle.com/cloud/free/). Нужна карта для проверки, Always Free не тарифицируется, если не создавать платные ресурсы.
2. Compute → Instance: Ubuntu aarch64, shape **VM.Standard.A1.Flex**, 2–4 OCPU, 8–24 ГБ RAM (квота аккаунта: 4 OCPU / 24 ГБ суммарно).
3. Если «Out of capacity»: другой AD/регион или Pay As You Go **без** платных VM — квота Always Free часто появляется.
4. Security list: SSH (22) только с вашего IP, исходящий HTTPS открыт.
5. На VM: Docker + compose plugin, клон репо, `data_folder`, `HEADLESS_MODE: true`.
6. Playwright на ARM нужен `--no-sandbox` — уже есть в коде.

Idle-инстансы Oracle иногда гасят: таймер бота как раз создаёт активность.

IP датацентра vs hh.ru: капча вероятнее, чем дома. Держите Telegram-топики для капчи и ошибок.

## Вариант B — Hetzner CX22 (~€5/мес)

Проще Oracle: x86, меньше «нет мощности». Та же схема Docker + volume + timer.

## systemd timer (каждые 4 часа)

`/etc/systemd/system/hh-applier.service`:

```ini
[Unit]
Description=HH Auto Jobs Applier
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
WorkingDirectory=/opt/HH_Auto_Jobs_Applier
ExecStart=/usr/bin/docker compose run --rm applier
```

`/etc/systemd/system/hh-applier.timer`:

```ini
[Unit]
Description=Run HH applier every 4 hours

[Timer]
OnBootSec=5min
OnUnitActiveSec=4h
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now hh-applier.timer
sudo systemctl list-timers hh-applier.timer
```

Интервал 4 часа совпадает с лимитом «поднять резюме» на hh.ru.

## Без Docker

Python 3.12, `pip install -r requirements.txt`, `playwright install --with-deps chromium`, `python main.py` из cron.

## Альтернатива $0 без облака

Если Mac почти всегда включён — `launchd` / cron каждые 4 часа локально. Сессия hh.ru стабильнее, чем с VPS.
