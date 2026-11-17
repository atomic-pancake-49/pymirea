# История изменений

Заметные изменения **pymirea** перечислены здесь. Формат —
[Keep a Changelog](https://keepachangelog.com/ru/1.1.0/), версии — по
[SemVer](https://semver.org/lang/ru/).

## [Unreleased]

### Изменено
- `MireaSchedule` больше не ходит в `app-api.mirea.ninja` (API устарел, авторы просили его не использовать). Поиск и расписание идут через публичные RPC Supabase mirea.ninja (`search_schedule_targets`, `get_schedule_for_entity`) с publishable-ключом. Запасной источник — официальный iCal `schedule-of.mirea.ru`. Сигнатуры методов не изменились
- `search_teachers` возвращает полное ФИО, `search_classrooms` — название с корпусом («А-9 (В-78)») и `campus.short_name`
- `SCHEDULE_API_BASE` удалён; новые константы: `SCHEDULE_SUPABASE_URL`, `SCHEDULE_SUPABASE_KEY`, `SCHEDULE_ORGANIZATION_ID`, `SCHEDULE_OF_BASE`

### Добавлено
- `MireaSDO` (`pymirea.sdo`) — клиент СДО МИРЭА (online-edu.mirea.ru, Moodle): курсы, ближайшие дедлайны, оценки. Сессия `MoodleSession` берётся молча через тот же Keycloak SSO (OIDC-плагин Moodle), повторный логин не нужен; данные читаются через штатный Moodle AJAX (`/lib/ajax/service.php`), без токена веб-сервисов. Команда CLI `pymirea sdo`
- `MireaSchedule.get_free_rooms(start, end)` — свободные аудитории в интервале (`get_free_rooms_window`)

### Добавлено
- CLI `pymirea`: `login`/`logout`/`status`, `schedule`, `grades`, `attendance`, `acs`, `attend`, `search`, `timetable` (с выгрузкой в `.ics`), у всех команд есть `--json`
- MCP-сервер (`pymirea mcp` / `pymirea-mcp`, extra `[mcp]`) с read-only инструментами: расписание, БРС, посещаемость, турникеты, публичное расписание. Работает с `mcp` 1.x и 2.x
- `pymirea.store.SessionStore` — локальное зашифрованное хранилище сессии (`~/.config/pymirea`, права `0600`)
- `pymirea.app` — общий слой для CLI и MCP, возвращает JSON-сериализуемые структуры
- Тесты `tests/test_cli_mcp.py`

## [0.5.1] — 2026-11-04

### Исправлено
- `pymirea.schedule`: время пар в календарях МИРЭА записано без часового пояса и
  оставалось «наивным», поэтому на машине не в Москве пары сдвигались. Теперь такое
  время считается московским. Даты целого дня остаются без зоны. Экспорт в iCal
  пишет такие события в UTC с `Z`: тот же момент, но однозначно.
- `MireaACS` восстанавливает сессию Пульса так же, как БРС и отметки: при HTTP 401,
  gRPC 7/16 или странице входа он один раз обновляет cookie через Keycloak-сессию
  (запасной путь — обновление токена) и повторяет запрос. Раньше ACS на устаревшей
  cookie возвращал `HTTP 401`, пока сессию не обновлял какой-нибудь другой вызов.

### Добавлено
- `MireaGrades.get_schedule(days, *, start_date=None)`: N дней начиная с указанной
  даты (по умолчанию сегодня по Москве), как в мобильном backend Pairx.
- Типизированные ошибки по желанию: у `AttendanceResult`, `GradesResult`,
  `ScheduleResult`, `AttendanceDetailResult` и `AcsResult` есть
  `raise_for_error(cookies)`. При неудаче он бросает `MireaSessionExpired`
  (надёжно — по метке мёртвой сессии в cookies), `MireaRateLimited` с `retry_after`,
  `MireaServerError` со `status_code`, `MireaParseFailed` или `MireaError` для
  отказов вроде «Отметка пока недоступна». Сами методы по-прежнему возвращают
  результат, а не бросают исключения.
- `pymirea.error_from_message(message, session_cookies=...)` — та же классификация
  для собственного кода.
- `pymirea.grades.renew_pulse_session(cookies)` — общее восстановление сессии Пульса
  для клиентов, которые держат один и тот же dict cookies.

## [0.5.0] — 2026-11-04

Перенос исправлений из продакшена Pairx после переразвёртывания Пульса
6 октября 2026. С 0.4.0 любой вызов с сохранённой сессией получает HTTP 401,
новый вход не даёт рабочей сессии, а БРС возвращает пустой ответ. Первый
опубликованный релиз после 0.3.3: в него входят и неопубликованные 0.3.5 и 0.4.0.

### Исправлено
- Сессия Пульса: cookie теперь `Pulse.Auth.Cookie`. Прежняя `.AspNetCore.Cookies`
  принимается и заменяется, как только Пульс выдаст новую. Bootstrap идёт через
  `/api/auth/login?redirectUri=https://pulse.mirea.ru/&rememberMe=True` вместо
  удалённого `/api/baseinfo`.
- HTTP 401, gRPC 7/16 или HTML-страница входа обновляют cookie через сохранённую
  Keycloak-сессию один раз и повторяют вызов; обновление токена осталось запасным путём.
- Cookies потока входа Keycloak (`AUTH_SESSION_ID`, `KC_*`) больше не отправляются:
  с ними SSO зацикливался на форме входа. Цикл редиректов считается истёкшей
  сессией, а не сбоем Пульса.
- Сессия, которую SSO отверг, помечается (`__pulse_bootstrap_failed_at`) и 30 минут
  не трогается, чтобы фоновые вызовы не повторяли заход на SSO.
- БРС: `MireaGrades.get_grades` берёт данные из
  `StudentService/GetStudentTermPlansWithDisciplines` и
  `LearnRatingScoreService/GetAttendancesAndLRSByDisciplinesIds` за текущий семестр.
  Старый метод отчёта удалён Пульсом. Сигнатура не изменилась.
- Отметка: убран запасной путь через HTML-страницу `/selfapprove`, которая с конца
  сентября отдаёт только оболочку веб-приложения. Ошибка всегда несёт настоящую причину.
- Вход: `login()` начинается на `pulse.mirea.ru/api/auth/login` (PAR), код OAuth
  обменивает сам Пульс. Почтовый код отправляется с `login=true`, тип кода берётся
  из `pageId` страницы SSO. Необязательная настройка MAX пропускается только при
  явном предложении SSO; настоящий код MAX по-прежнему нужно ввести.
- Логин и пароль отправляются с `rememberMe=on`: Keycloak выдаёт долгую сессию
  «запомнить меня» вместо 16 часов.

### Добавлено
- `pymirea.pulse_session`: имена cookie и параметры входа; в публичном API
  `has_pulse_auth_cookie()` и `pulse_session_needs_login()` — признак того, что
  пользователю нужно войти заново.
- `Subject`: максимумы по типам баллов (`current_control_max`, `semester_control_max`,
  `attendance_max`, `achievements_max`, `additional_max`, `total_max`). В 2026/27
  семестровый контроль стал 40, посещения 20.
- `AuthChallenge.pulse_cookies`: области cookies сохраняются между шагами 2FA
  (по умолчанию `None`).
- Тесты: поток отметки, БРС, помощники сессии, 19 сценариев PAR-входа,
  разбор страниц с кодом. Всего 212 тестов.

### Изменено
- `_ensure_aspnet_cookie` стал алиасом `_ensure_pulse_cookie`.
- Логи входа и bootstrap не содержат адресов SSO, тел ответов и redirect URI.

### Удалено
- Четыре теста прежнего потока входа через прямой OIDC с PKCE.

## [0.4.0] — 2026-04-30 (не опубликован)

### Добавлено
- Модуль расписания `MireaSchedule` с экспортом в iCal.

## [0.3.5] — 2026-04-30 (не опубликован)

### Добавлено
- Хуки наблюдаемости и единый асинхронный `Client`.
- Типизированные исключения (`MireaSessionExpired`, `MireaRefreshFailed` и др.),
  однопоточное обновление токена под блокировкой, проактивное обновление JWT.

## [0.3.0]–[0.3.3] — 2026-04-29

### Добавлено
- Необязательная TLS-имитация через `curl_cffi` (`pip install pymirea[tls]`),
  фабрика HTTP-клиентов, перевод исключений `curl_cffi` в `httpx`, наблюдаемость.

## [0.2.1]–[0.2.3] — 2026-04-16…29

### Исправлено
- Проверки границ в парсерах protobuf и gRPC-Web, голые `except`, уровни логов.

### Изменено
- Пул соединений httpx увеличен до 100/50.


## [0.2.0] — 2026-04-14

### Исправлено
- `AuthResult.tokens` добавлен как property-алиас для `cookies` — README/examples ссылались на `result.tokens`, которого не существовало
- `MireaAuth.complete_2fa()` добавлен как алиас для `submit_otp()` — README/examples звали `complete_2fa`, которого не существовало
- `_handle_required_actions`: после `max_account_config` и подобных required-actions, если token exchange упал но KC-cookies есть, теперь выполняется bootstrap `.AspNetCore.Cookies` через `pulse.mirea.ru/api/auth/login`. Без этого downstream Pulse API возвращает 401 для части юзеров (порт фикса из qrscaner production)

### Добавлено
- 2 smoke-теста, фиксирующих контракт обоих алиасов на будущее
- 10 интеграционных тестов в `tests/test_auth_integration.py` для `MireaAuth.refresh_tokens` и `verify_session` через `respx` (мок httpx). Покрывают: success-флоу, 4xx ответ, пустой вход, отсутствующий access_token, фильтрацию internal cookies, поведение при редиректе на `/login`
- `respx>=0.20` в `dev`-зависимостях
- Тест `test_session_crypto_logs_warning_on_corrupt_payload` — повреждённая cookie-row в БД теперь логируется WARNING

### Изменено
- `crypto.py`: голые `except Exception` заменены на конкретные (`InvalidToken`, `JSONDecodeError`, `binascii.Error`). Повреждённая cookie-row теперь логирует WARNING вместо тихого `None`.
- `auth.py`: голые `except Exception` в `refresh_tokens`, `_exchange_code_for_token`, `verify_session`, `_extract_code_from_url` заменены на конкретные (`httpx.NetworkError`, `httpx.TimeoutException`, `ValueError`, `KeyError`). Сетевые сбои логируются с типом исключения для диагностики.

## [0.1.2] — 2026-04-14

### Добавлено
- README расширен тремя готовыми примерами (CLI-скрипт, Telegram-бот, FastAPI-сервис)
- Папка `examples/` с runnable-скриптами
- GitHub Actions CI: ruff lint + pytest на каждый push и PR
- GitHub Actions release workflow: автопубликация на PyPI на push тега `v*` через trusted publishing (OIDC, без API-токенов)
- Smoke-тесты в `tests/test_smoke.py` (6 offline-проверок API без живого аккаунта)

### Изменено
- Документация переведена на русский (целевая аудитория — студенты МИРЭА)
- `LKS` → `Пульс` в README/CHANGELOG/docstring'ах: lks.mirea.ru как название портала устарело, актуальное — pulse.mirea.ru

### Исправлено
- `SessionCrypto.get_crypto()` обращался к отсутствующему `settings.jwt_secret` (переименован в `legacy_bot_token` при extraction). Заменено на `getattr` с fallback'ом, чтобы приложения без legacy-секрета не падали.

## [0.1.1] — 2026-04-14

### Изменено
- Зависимость `httpx` ослаблена до `>=0.25` (было `>=0.27`) — для совместимости с downstream-приложениями, ещё запиненными на старые версии.

## [0.1.0] — 2026-04-14

Первый релиз. Async-клиент для Пульса МИРЭА извлечён из двух внутренних
проектов (`silverhans/versiti-project` MireaScanner Web и `Oplexx`
backend мессенджера) в единый канонический пакет.

### Добавлено
- `MireaAuth` — login + 2FA (Keycloak SSO + OTP) + refresh токенов
- `MireaAPI` — расписание, оценки, посещаемость, детали посещаемости
- `MireaACS` — события турникетов через pulse.mirea.ru
- `MireaEsports` — регистрация в e-sports
- `SessionCrypto` — Fernet+HKDF шифрование cookies
- `Config` + `configure()` — конфигурация runtime через DI
- Public-хелперы: `get_authorization_header`, `try_refresh_tokens`, `get_token_age_seconds`
- Лицензия MIT

[Unreleased]: https://github.com/silverhans/pymirea/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/silverhans/pymirea/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/silverhans/pymirea/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/silverhans/pymirea/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/silverhans/pymirea/releases/tag/v0.1.0
