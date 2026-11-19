# Prostranstvo - source QA snapshot 0.1.19

Current source-QA baseline: 19 November 2026. This source-only candidate 0.1.19 updates the accepted 0.1.17 QA tree. It publishes no installers or private machine configuration and does not establish completion of the entire product concept.

- Windows LocalProduct source suite: 1196 run, 1190 successful, 6 skipped, 128.529 seconds. The child verified its own non-input QA desktop before GUI imports; the input desktop remained Default, with no desktop switch or input injection. This scopes test GUI execution, not OS/account/service isolation or household end-to-end acceptance.
- The bounded application manager discovers registered applications only on explicit Refresh. It prepares exact plans for a registered Steam uninstall URI, a strict Windows Installer product GUID or manual Installed Apps settings. Confirming the plan and parent PIN precedes a native-window request. Opening a window is not a removal result; verification distinguishes registration absence, remaining files and incomplete/uncertain observations. No real application was uninstalled in this acceptance run.
- Protected, policy-held and unsupported entries cannot dispatch. The private acceptance configuration retained six local policy holds; their identities, paths and records are excluded from this source update. No universal SYSTEM delete/shell permission, forced process termination or recursive portable-folder fallback was added. The PIN is a UI boundary in a shared Windows account, not OS isolation from that account's own code.
- Scoped current-user shader-cache cleanup retains preview, exact approval, fresh HANDLE identity checks, busy/read-only/reparse/hardlink skips and write-ahead receipts. It is not a general file or application deletion endpoint. Reported/logical sizes and observed free space remain separate.
- Existing Study duration 0 persists an until-parent-cancel lease without a fabricated future expiry. That source capability already existed in earlier packages; a separate personal deployment refresh is outside this public source change. Reboot/sign-in and separate child-account acceptance remain open. After-school/offline scenarios are a concept document, not an enabled scheduler.
- Build baseline remains final CPython 3.13.x patch 18 or later with Tk; recorded PyInstaller 6.22.3. Runtime/dependency provenance and remaining licensing limits are documented separately. Tests do not establish absence of vulnerabilities or permission to redistribute every dependency.
- Candidate binaries stay private. The previous 0.1.18 installer/portable archives remain available locally as fallback. Clean-machine installation/update/uninstall, actual native application removal, remaining security findings, licensing and family-account acceptance are open gates.
- LearningModule and unrelated repository files are preserved. Earlier independent module results are historical and are not added to the current LocalProduct test total. Reward wallets, full curriculum, multi-PC/phone products and universal BIOS/root protection are not completed features.

Read [application-removal scope](LocalProduct/docs/APPLICATION-REMOVAL-RU.txt), [cache-cleanup scope](LocalProduct/docs/CACHE-CLEANUP-RU.txt), [offline scenario concept](LocalProduct/docs/OFFLINE-FAMILY-SCENARIOS-RU.md), [release gates](LocalProduct/docs/PRODUCT-ROADMAP-RU.md), and [runtime provenance/license limits](LocalProduct/docs/RELEASE-PYTHON-RUNTIME-RU.txt).

---

## Retained historical notes and dated QA

The material below belongs to earlier snapshots through 18 November. Earlier beta 0.1.12 and pending-build statements are historical, superseded by the 0.1.19 source-QA summary above. Historical results are not added to the current test totals. Historical commands using generic Python 3.13 must now satisfy the final 3.13.18+ build floor.

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
