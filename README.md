# Prostranstvo - source QA snapshot 0.1.20

Current source-QA baseline: candidate 0.1.20, 19 November 2026 (Pacific time). This source-only update adds the daily-board changes to the accepted QA tree. It includes no installers, private profiles or personal daily-plan data, and does not establish completion of the whole product concept.

- Windows LocalProduct source suite: 1228 run, 1222 successful, 6 skipped, 141.620 seconds. The child verified its own non-input Windows QA desktop before GUI imports; the input desktop remained Default, without desktop switching or injected input. This was a real Windows desktop, not a VM or OS/account/service isolation, and is not household end-to-end acceptance.
- A parent can opt into a repeating daily template. Explicitly opening the board, or starting an already-requested Study session, materializes today's list once; skipped days are not invented. Reading status/history has no write effect. Creating a list does not itself start Study.
- Checkboxes show provisional progress. Only parent approval credits confirmed Steve progress. Revision/date checks prevent duplicate awards; late review and rework refer to the selected day. Reviewing an old day cannot stop today's Study, and a newer Study lease remains protected from an old approval.
- Rebirth resets the confirmed path and preserves history. An old approval cannot restore points already spent on rebirth; a previously unapproved old day can add its award once to the current confirmed path. These are local progress records and animation, not delivery of Minecraft items or money.
- History is bounded to 366 past lists and a 4 MiB record. At the bound, new-day creation fails visibly without deleting history or resetting points. Version-1 data is read without an immediate rewrite and becomes version 2 on an explicit saved operation. Close old version-1 code before that write; rollback needs a compatible data backup and must not overwrite newer history automatically.
- Application-removal and scoped shader-cache cleanup from 0.1.19 retain their separate confirmation, fresh checks and receipts. Opening native uninstall UI is not proof of removal. Private policy holds and personal inventory are excluded; no universal SYSTEM shell/delete capability or new real-uninstall acceptance is claimed. Parent PIN is a UI boundary within a shared Windows account, not isolation from that account's code.
- Game minutes remain an informational agreement. Native game-time enforcement, automatic Telegram/phone approval and scheduled Study activation are not included. Offline-family scenarios and remote official installation from a phone remain documented future work. Existing until-parent-cancel Study support is not a new 0.1.20 feature.
- Build baseline remains final CPython 3.13.x patch 18 or later with Tk; recorded PyInstaller 6.22.3. Runtime provenance and remaining license-mapping limits are documented separately. Tests do not establish absence of vulnerabilities or full redistribution rights.
- Candidate binaries remain private. Package checks and personal deployment acceptance are separate receipts and are not inferred from the source suite. Clean-machine install/update/uninstall, real application removal, sign-in/reboot, separate child-account and family acceptance, and remaining security/license gates stay open. LearningModule and unrelated base-tree files are preserved.

Read the [daily-board contract](LocalProduct/docs/DAILY-BOARD-RU.md), [application-removal scope](LocalProduct/docs/APPLICATION-REMOVAL-RU.txt), [cache-cleanup scope](LocalProduct/docs/CACHE-CLEANUP-RU.txt), [offline scenario concept](LocalProduct/docs/OFFLINE-FAMILY-SCENARIOS-RU.md), [future phone/official-install concept](LocalProduct/docs/REMOTE-APPLICATION-INSTALL-RU.md), [release gates](LocalProduct/docs/PRODUCT-ROADMAP-RU.md), and [runtime provenance/license limits](LocalProduct/docs/RELEASE-PYTHON-RUNTIME-RU.txt).

---

## Retained historical notes and dated QA

The material below belongs to earlier snapshots through 18 November. Earlier beta 0.1.12 and pending-build statements are historical, superseded by the 0.1.20 source-QA summary above. Historical results are not added to the current test totals. Historical commands using generic Python 3.13 must now satisfy the final 3.13.18+ build floor.

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
