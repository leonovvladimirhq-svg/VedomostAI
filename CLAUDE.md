# CLAUDE.md

Guidance for Claude Code when working in this repository. Комментарии и общение по проекту — **на русском** (пользователь ведёт проект на русском).

## Что это

**Ведомость AI** — Telegram-бот для преподавателей ШК НИУ ВШЭ: ведение ведомостей оценок.
В перспективе — один из инструментов («субагент») внутри большого бота преподавателя.
Заказчик: Лара Грязева (ОП «Интегрированные коммуникации»).

Сквозной сценарий: преподаватель присылает боту свой **ПУД файлом** → бот извлекает
формулу оценивания и элементы контроля → создаёт ведомость → преподаватель вводит оценки
(кнопки; далее текст/голос) → детерминированный движок считает итог → выгрузка в Excel
(далее — Яндекс.Таблицы).

## Архитектурные принципы (важно соблюдать)

- **Доменное ядро `core/` вызывается только через `core/services/`.** Бот — тонкий слой:
  парсит апдейты Telegram и вызывает сервисы. Никакой бизнес-логики в `bot/`. Это условие
  превращения бота в субагента без переписывания.
- **Расчётный движок детерминирован, без LLM** (`core/services/grading_service.py`). LLM —
  только на границах (разбор ввода, извлечение ПУД). Любое число проходит через тестируемый код.
- **Оценки append-only** (`GradeEntry`): не перезаписываются. Правка = новая запись; актуальна
  последняя (для элементов `aggregation="single"`) либо среднее (`"average"`).
- **Статус ведомости — конечный автомат** (`core/statuses.py`): Черновик→Заполняется→Закрыта→…
- **Секреты не коммитим** (`.gitignore`): `.env`, `*.json` (ключи YC), `*.db`, `.venv`.

## Структура

```
core/
  models.py              # SQLAlchemy: Teacher, Group, Student, Statement, GradingScheme,
                         #   ControlElement, GradeEntry, StudentStatus, ConsentRecord, Feedback
  legal/consent_vedomost_v1.md  # текст согласия на обработку ПДн (152-ФЗ), версионируется
  statuses.py            # конечный автомат статусов ведомости
  db.py                  # engine/session (SQLite сейчас, Postgres позже — только DATABASE_URL)
  services/
    statement_service.py # СЕРВИСНЫЙ СЛОЙ: создание ведомости, append-only ввод, пересчёт,
                         #   мост БД<->движок (build_engine_scheme, student_total, active_statement)
    grading_service.py   # ДВИЖОК: compute(Scheme, entries)->GradeResult; округления, блокирующие;
                         #   шкала GRADE_MIN/GRADE_MAX + element_max (валидация «вне диапазона»)
    reminder_service.py  # контур 4: find_stale() — неактивные ведомости для напоминаний
    consent_service.py   # согласие ПДн (152-ФЗ): версия, summary, SHA-256, needs/record, forget_me
    feedback_service.py  # обратная связь преподавателя (👍/👎 + комментарий), агрегаты
  parsing/
    pud_ingest.py        # извлечение текста из файла ПУД (PDF/DOCX/HTML/TXT) + поиск формулы
    pud_parser.py        # разбор строки формулы «Актив*0.1+...» -> Scheme (детерминированно)
    text_parser.py       # ЗАГЛУШКА: разбор текста-потока оценок (Qwen) — TODO
    voice.py             # ЗАГЛУШКА: транскрипция голоса (SpeechKit) — TODO
  export/excel.py        # генерация .xlsx (build_ledger_from_statement)
bot/main.py              # aiogram 3: FSM-поток (ПУД -> подтверждение -> ввод -> экспорт)
seed/test_group.py       # 20 выдуманных студентов (без ПДн)
tests/test_grading.py    # тесты движка (в т.ч. сверка с реальным Excel «число-в-число»)
```

## Команды

```bash
# Локальная разработка (из папки app/)
python -m venv .venv && ./.venv/Scripts/pip install -r requirements.txt   # Windows
cp .env.example .env            # заполнить TELEGRAM_BOT_TOKEN
python -m bot.main              # запуск бота (long-polling)

# Тесты движка (самый ответственный модуль)
./.venv/Scripts/python -m pytest tests/ -q

# Валидация движка против реальной Excel-ведомости
./.venv/Scripts/python -m scripts.validate_engine
```

⚠️ **Не запускать локальный поллинг, пока бот работает на VM** — Telegram допускает один
getUpdates (ошибка 409). Разработка локально — без запуска поллера (тестировать сервисы/скриптами).

## Деплой (подробно — `DEPLOY.md`)

- Прод: VM `vedomost-ai-bot` в Yandex Cloud (project4-vedomost-ai), systemd-сервис `vedomost-bot`.
- **Критично:** из YC `api.telegram.org` (149.154.166.110) заблокирован; в `/etc/hosts` на VM
  прибит рабочий IP `149.154.167.220`. Без этого бот крэш-лупит с TelegramNetworkError.
- Обновление: `ssh` на VM → `cd ~/VedomostAI && git pull && sudo systemctl restart vedomost-bot`.

## Технологии / инфраструктура

- Python 3.10+, aiogram 3, SQLAlchemy 2, openpyxl, pypdf, python-docx.
- Yandex Cloud (project4, folder `b1guqlfk525se8bvag90`): доступны SpeechKit (`ai.speechkit-stt.user`)
  и Qwen LLM (`ai.languageModels.user`, endpoint `https://llm.api.cloud.yandex.net/v1`,
  OpenAI-совместимый, model `gpt://<folder>/qwen3.6-35b-a3b/latest`).
- БД: SQLite для прототипа; переезд на Managed PostgreSQL = смена `DATABASE_URL`.

## Текущее состояние и что дальше

- ✅ Приём ПУД файлом → извлечение структуры → ведомость → ввод (кнопки/текст/голос) → расчёт → Excel.
- ✅ Яндекс.Диск: выгрузка .xlsx как редактируемой таблицы в Яндекс Документах (нужен `YANDEX_DISK_TOKEN`).
- ✅ Я.Таблицы = xlsx на Диске; демо-генерация 5 ведомостей из дампа ПУД — `scripts/generate_demo_statements.py`.
- ✅ Напоминания (контур 4): фоновый цикл в процессе бота шлёт алерт преподавателю, если по ведомости
  «Заполняется» нет оценок дольше `REMINDER_INACTIVITY_HOURS` (прод 240ч, тест 24ч); таймзона МСК;
  месяцы-исключения `REMINDER_SKIP_MONTHS`; ручная проверка доставки — команда `/remind_now`.
- ✅ Детектор аномалий, этап 1: жёсткая валидация «вне шкалы 0–10» при вводе (кнопки/текст/голос),
  ответ с максимумом за элемент (`grading_service.element_max`).
- ✅ Согласие на ПДн (152-ФЗ, паттерн из TutorAI): на `/start` без согласия — двухэкранный флоу
  (интро → краткое содержание + `.md` с SHA-256 → Согласен/Не согласен), аудит в `consent_records`,
  права субъекта `/my_data` и `/forget_me`. Версия `consent_vedomost_v1` (оператор ШК ВШЭ; e-mail —
  заглушка, вписать перед запуском).
- ✅ Обратная связь: кнопка «💬 Оставить обратную связь» в меню + контекстная плашка 👍/👎 после
  распознавания ПУД; необязательный комментарий; хранится в `feedback`.
- ✅ Стартовое сообщение: принадлежность к ШК НИУ ВШЭ + ценность в 1–2 предложениях.
- 🔜 Дальше: аномалии (пропуски/выбросы/«всем одно»), дашборд академрука, СЭВ, per-element `max_score`.

## Конвенции

- Правки движка/парсеров сопровождать тестами (движок — цель покрытия ≥95%).
- ПУД из конструктора dp.hse.ru стандартизирован: формула после метки «Формула оценивания:».
- Данные студентов в прототипе — только выдуманные (ФЗ-152).
