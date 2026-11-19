# Prostranstvo - source QA snapshot 0.1.15

Current source-QA baseline: 19 November 2026. This update contains source only. It does not publish binaries, alter an installed family computer, or establish completion of the full product concept.

- Build baseline: final CPython 3.13.x, patch 18 or later, with Tk. Recorded packaging tool: PyInstaller 6.22.3 (pinned build requirements).
- Privately checked candidate 0.1.15: CPython 3.13.18, OpenSSL 3.5.9, SQLite 3.53.1, zlib 1.3.2; actual frozen hidden-Tk check: Tk 8.6.15.
- Recorded Windows LocalProduct run: 1080 tests, 1075 successful, 5 skipped, 98.093 seconds. Separate LearningModule run: 67 successful. These counts are not evidence of a live parent/child workflow.
- Private package no-GUI verification passed, including isolated install/upgrade and runtime checks. Restricted MCP and frozen worker checks remain distinct from real Windows acceptance. Installers, personal profiles, credentials and runtime logs are not part of this source update.
- Added source includes sanitized security reports and a restricted two-tool channel, metadata-only storage jobs/UI, file rollback regression, runtime preflight and isolated family workflow checks.
- Still open: real Study/PIN recovery and Windows sign-in/reboot; separate child-account setup/use; live microphone/audibility, API, Telegram and deliberate browser login; clean-machine install/upgrade/uninstall; complete native-component/license mapping.
- LearningModule remains a limited fraction-addition prototype. Percentage scoring, reward wallets, savings goals, phone products, multi-PC networking and a universal AI shield are not completed features.

Read [current QA](QA.md), [scope and release gates](LocalProduct/docs/PRODUCT-ROADMAP-RU.md), [runtime provenance and license-mapping limits](LocalProduct/docs/RELEASE-PYTHON-RUNTIME-RU.txt), [security reports](LocalProduct/docs/SECURITY-REPORT-RU.txt), [restricted channel](LocalProduct/docs/SECURITY-CHANNEL-RU.txt) and [storage jobs](LocalProduct/docs/STORAGE-TASKS-RU.txt).

The fixed runtime-only build probe checks loaded metadata before assets are copied; it does not replace package, Tk or end-to-end checks. Passing tests or an old version alone establishes neither safety nor exploitability. Public source availability does not grant a new redistribution license; retain existing notices and the explicit remaining review limits.

---

## Retained historical notes and dated QA

The material below belongs to earlier snapshots through 18 November. Earlier beta 0.1.12 and pending-build statements are historical, superseded by the 0.1.15 source-QA summary above. Historical results are not added to the current test totals. Historical commands using generic Python 3.13 must now satisfy the final 3.13.18+ build floor.

# Пространство / Prostranstvo

Снимок исходников от 18 ноября 2026 года для публичного QA. Локальное приложение Windows для работы с файлами, семейного общения и режима «Уроки». Учебная мастерская запускается отдельно.

**Для независимого рецензента / AI-агента: [задание и сценарии QA](QA.md).** Сначала зафиксируйте SHA проверяемого коммита.

Новые исходники: отдельный headless Edge для агента, подготовленный ручной вход Facebook через тот же профиль, выбор родительских приложений для сворачивания во время Study, запрос блокировки Windows и вкладка питания с PIN/отсчётом/отменой. Проверенные границы и зависимости описаны в [инструкции](LocalProduct/docs/AGENT-CONTROL-RU.txt) и QA. Текущий установщик 0.1.12 этих изменений не содержит; наличие команды не доказывает вход в Facebook или устойчивую блокировку общей учётной записи.

| Папка | Содержимое |
|---|---|
| `LocalProduct/src` | Приложение Python/Tk, Мурчик, режим «Уроки», «План на сегодня», файлы и локальный MCP |
| `LocalProduct/native` | Компоненты Windows на C# |
| `LocalProduct/tests` | Автоматические проверки на временных данных |
| `LearningModule` | Учебный модуль, тесты и контракты |

В этом снимке используется переносимое дерево. Последняя упакованная бета — 0.1.12; исходники содержат более новые изменения. Нового установщика эта публикация не выпускает.

## Что обновлено

- Добавлен ограниченный запуск известных браузеров и приложений через MCP: каталог, план и запуск с отдельным разрешением на экран.
- Подготовка stdio/headless-плана для отдельного профиля Edge/Chrome. Подключение управления и вход на сайты пока не проверены; текущие сессии не копируются.
- Восстановление действующего занятия при повреждённом settings.json: последние проверенные параметры или запасной режим, без изменения срока и перезаписи повреждённого файла.
- Постоянный статус родителя: активен, восстановлен, завершён или не восстановился; ошибки и запасные параметры оставляют панель видимой при входе.
- Восстановление действующего сеанса при повреждённом settings.json: проверенные параметры или явный запасной режим, с сохранением исходного срока.
- Отдельные статусы для родителя: активен, восстановлен, завершён или не восстановился; ошибку восстановления приложение не скрывает.
- Сохранение блокировки попыток PIN между перезапусками приложения.
- Открытие существующего «Плана на сегодня» при запуске/восстановлении «Уроков», с защитой от повторных окон.
- Повторная обработка Roblox, который игнорирует обычное закрытие, с проверкой процесса и текущего сеанса Windows.
- Родительская настройка сворачивания окон Codex во время «Уроков» с сохранением его фоновой работы.
- Устойчивость цикла проверки «Уроков» и файл состояния контроллера.
- Тихое поведение котёнка в нижнем правом углу.
- Самостоятельный запуск тестов из чистого клона без заранее созданного build и личного дерева разработки.

Результаты автоматических проверок приведены в [QA.md](QA.md). Звук, установка на чистую Windows, вход в систему и живые подключения Telegram/ИИ требуют отдельной приёмки.

## Сборка на Windows x64

Понадобятся Python 3.13 с Tk и компилятор C# из .NET Framework. Из папки `LocalProduct`:

```powershell
python -m venv build/venv
./build/venv/Scripts/python.exe -m pip install -r packaging/build-requirements.txt
./build/venv/Scripts/python.exe packaging/prepare_speech_assets.py
./build/venv/Scripts/python.exe -B -m unittest discover -s tests -v
./build/venv/Scripts/python.exe packaging/build_beta.py
```

Модель распознавания и DLL скачиваются отдельным скриптом подготовки с проверкой хешей. Blockly уже включён. Для мастерской см. `LearningModule/START-HERE-RU.txt`.

## Данные и доступ

Рабочие настройки, ключи, семейная переписка, история браузеров, скриншоты, базы данных, исполняемые сборки и локальные профили исключены. Пользовательские данные приложение хранит отдельно от исходников.

Репозиторий открыт для QA. Общая лицензия на свободное распространение проекта пока не выбрана; лицензии сторонних компонентов сохранены. После возврата репозитория в private уже скачанные копии останутся у получателей.
