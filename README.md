# Prostranstvo - source QA snapshot 0.1.17

Current source-QA baseline: 19 November 2026. This source-only update is relative to the accepted 0.1.16 QA tree. It does not publish installers, replace an installed family computer or establish completion of the entire concept.

- Build baseline remains final CPython 3.13.x patch 18 or later with Tk; recorded packaging tool PyInstaller 6.22.3. The privately checked 0.1.17 runtime reports CPython 3.13.18, OpenSSL 3.5.9, SQLite 3.53.1 and zlib 1.3.2.
- Current Windows LocalProduct suite: 1101 run, 1096 successful, 5 skipped, 113.214 seconds. It executed on a newly created non-input QA desktop, checked before importing Tk/tests. The input desktop remained Default before/after; no desktop switch or input injection occurred. This scopes GUI execution, not OS/account/registry/service isolation or a real family workflow.
- Separate LearningModule result remains 67 successful on the same runtime; that module did not change.
- Private 0.1.17 package no-GUI install/upgrade and MCP checks passed (23 general + 3 family + 2 restricted tools). Actual frozen hidden Tk 8.6.15 and source hidden App checks passed separately.
- The actual isolated frozen storage worker passed on two owned fixture files, 37 bytes total, both preserved, without Python on the child PATH. Polling observed queued/completed without EACCES harness retries; running was not observed. Packaged TTS generated a 228374-byte WAV in memory without playback or microphone use; this is not audibility.
- New source requires the configured parent PIN before enabling the external pet API, opening AI settings and each settings save. Offline remains selectable without PIN. The PIN stays in the UI handler, not the worker/API/log. A shared administrator account can still bypass this UI boundary outside the application.
- General MCP reads at most 1 MiB characters plus one sentinel character per line. Oversized input returns one protocol error and closes without parsing a following suffix. Portable source exposes no SYSTEM maintenance tool.
- The separate personal MCP route rejects repair/cleanup before its broker and advertises only diagnostics/health/scan. That private route is not published here; the SYSTEM service/pipe and already cached MCP processes are not changed by this source update.
- The 0.1.16 mandatory-byte-lock fix and real-process regressions remain. Earlier 0.1.15 passing checks did not cover the subsequently discovered lock race.
- Still open: actual Study/PIN recovery and Windows sign-in/reboot, child-account setup/use, live voice/API/Telegram/browser login, clean-machine install/upgrade/uninstall and native dependency/license mapping. Other independent review findings remain review work.
- Percentage scoring, reward wallets, savings goals, full curriculum, phone/multi-PC products and a universal AI shield are not completed features.

Read [current QA](QA.md), [scope/release gates](LocalProduct/docs/PRODUCT-ROADMAP-RU.md), [runtime provenance and remaining license limits](LocalProduct/docs/RELEASE-PYTHON-RUNTIME-RU.txt), [AI/PIN scope](LocalProduct/docs/CAT-AI-RU.txt), [maintenance boundaries](LocalProduct/docs/WINDOWS-HEALTH-RU.txt), and [restricted security channel](LocalProduct/docs/SECURITY-CHANNEL-RU.txt).

The runtime-only build probe checks loaded metadata before resources are copied, separately from package/Tk/end-to-end checks. Passing tests alone proves neither universal safety nor exploitability. Public source availability does not grant a new redistribution license.

---

## Retained historical notes and dated QA

The material below belongs to earlier snapshots through 18 November. Earlier beta 0.1.12 and pending-build statements are historical, superseded by the 0.1.17 source-QA summary above. Historical results are not added to the current test totals. Historical commands using generic Python 3.13 must now satisfy the final 3.13.18+ build floor.

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
