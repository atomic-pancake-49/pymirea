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
