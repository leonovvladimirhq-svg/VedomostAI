# Деплой Ведомость AI на Yandex Cloud

## Где работает
- **VM** `vedomost-ai-bot` (project4-vedomost-ai, folder `b1guqlfk525se8bvag90`), Ubuntu 22.04,
  публичный IP `111.88.251.184`. Код в `~/VedomostAI`, запуск — systemd-сервис `vedomost-bot`
  (long-polling, `Restart=always`, автозапуск).
- SSH: `ssh -i ~/.ssh/vedomost_vm yc-user@111.88.251.184`

## ⚠️ Критично: Telegram заблокирован из Yandex Cloud
Из YC исходящие к `api.telegram.org` фильтруются: DNS отдаёт IP `149.154.166.110`, который
**заблокирован**. Но IP `149.154.167.220` (тоже Bot API) из YC **доступен**. Поэтому на VM
принудительно прибиваем хост к рабочему IP:

```bash
echo "149.154.167.220 api.telegram.org" | sudo tee -a /etc/hosts
```
Без этой строки бот падает с `TelegramNetworkError: Request timeout` и крэш-лупит.
(aiohttp/aiogram резолвят через системный getaddrinfo, который читает /etc/hosts.)

## Первичная установка (что уже сделано)
```bash
sudo apt-get update && sudo apt-get install -y python3-venv python3-pip git
git clone https://github.com/leonovvladimirhq-svg/VedomostAI.git ~/VedomostAI
cd ~/VedomostAI && python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
# .env создать вручную на VM (НЕ в git): TELEGRAM_BOT_TOKEN, DATABASE_URL, YC_FOLDER_ID
echo "149.154.167.220 api.telegram.org" | sudo tee -a /etc/hosts
# systemd unit /etc/systemd/system/vedomost-bot.service -> ExecStart=.venv/bin/python -m bot.main
sudo systemctl enable --now vedomost-bot
```

## Обновление кода
```bash
ssh -i ~/.ssh/vedomost_vm yc-user@111.88.251.184
cd ~/VedomostAI && git pull && ./.venv/bin/pip install -r requirements.txt
sudo systemctl restart vedomost-bot
sudo journalctl -u vedomost-bot -n 20 --no-pager
```

## Важно
- Одновременно НЕ запускать бота ещё где-то (локально) — Telegram разрешает один поллинг (ошибка 409).
- `.env` и ключи в git не коммитятся (см. `.gitignore`).
