# Independent QA - current source snapshot 0.1.16

Current source-QA baseline: 19 November 2026. This update contains source only. It does not publish binaries, alter an installed family computer, or establish completion of the full product concept.

- Build baseline: final CPython 3.13.x, patch 18 or later, with Tk. Recorded packaging tool: PyInstaller 6.22.3 (pinned build requirements).
- Privately checked candidate 0.1.16: CPython 3.13.18, OpenSSL 3.5.9, SQLite 3.53.1, zlib 1.3.2; actual frozen hidden-Tk check: Tk 8.6.15.
- Recorded Windows LocalProduct run: 1083 tests, 1078 successful, 5 skipped, 96.878 seconds. Separate LearningModule run: 67 successful. These counts are not evidence of a live parent/child workflow.
- Private package no-GUI verification passed, including isolated install/upgrade and runtime checks. Restricted MCP and frozen worker checks remain distinct from real Windows acceptance. Installers, personal profiles, credentials and runtime logs are not part of this source update.
- A further real frozen-worker check after 0.1.15 found a Windows mandatory-byte-lock initialization race. Version 0.1.16 removes the pre-lock byte read/write and adds real cross-process locking regressions; prior passing suites did not cover that scenario.
- Actual 0.1.16 frozen worker passed on two owned fixture files (37 bytes total), both preserved. Parent status observed queued/completed without EACCES retries; running was not an observed state. Source-widget and in-memory TTS checks also passed; in-memory TTS is not proof of audible playback.
- Added source includes sanitized security reports and a restricted two-tool channel, metadata-only storage jobs/UI, file rollback regression, runtime preflight and isolated family workflow checks.
- Still open: real Study/PIN recovery and Windows sign-in/reboot; separate child-account setup/use; live microphone/audibility, API, Telegram and deliberate browser login; clean-machine install/upgrade/uninstall; complete native-component/license mapping.
- LearningModule remains a limited fraction-addition prototype. Percentage scoring, reward wallets, savings goals, phone products, multi-PC networking and a universal AI shield are not completed features.

Read [current QA](QA.md), [scope and release gates](LocalProduct/docs/PRODUCT-ROADMAP-RU.md), [runtime provenance and license-mapping limits](LocalProduct/docs/RELEASE-PYTHON-RUNTIME-RU.txt), [security reports](LocalProduct/docs/SECURITY-REPORT-RU.txt), [restricted channel](LocalProduct/docs/SECURITY-CHANNEL-RU.txt) and [storage jobs](LocalProduct/docs/STORAGE-TASKS-RU.txt).

The fixed runtime-only build probe checks loaded metadata before assets are copied; it does not replace package, Tk or end-to-end checks. Passing tests or an old version alone establishes neither safety nor exploitability. Public source availability does not grant a new redistribution license; retain existing notices and the explicit remaining review limits.

The new file-locking regression always exercises the public portable backend and adds the private legacy backend only when present. Windows contention cases use a real hidden holder and native refusal barrier, not simulated PermissionError or outer harness retries. Empty and preexisting one-byte lock files must remain unchanged.

Priority review: allowed roots/changed files/collisions/receipts/recovery; durable PIN and approval bound to the correct lease; sanitized report unknown/freshness states; job ownership/cancellation/committed metadata; and runtime/version/license provenance. Use disposable fixtures. New family-flow cases use withdrawn Tk, real local stores and a fake desktop, not real sign-in/reboot/window enforcement.

Record the reviewed Git commit SHA. Do not connect real accounts, alter real startup/services, activate Study on a family computer, or run destructive file actions during unattended source QA. No general BIOS/root capability, stealth behavior or absolute protection is claimed. A PIN in a shared administrator account is not an OS security boundary.

---

## Retained historical notes and dated QA

The material below belongs to earlier snapshots through 18 November. Earlier beta 0.1.12 and pending-build statements are historical, superseded by the 0.1.16 source-QA summary above. Historical results are not added to the current test totals. Historical commands using generic Python 3.13 must now satisfy the final 3.13.18+ build floor.

# Independent QA / review guide

This is a source snapshot of Prostranstvo, a local Windows family application. Review this exact Git commit and report its SHA. Runtime profiles, credentials, messages, browser history, screenshots, databases and installed binaries are excluded.

## Review brief for an AI agent

> Act as an independent software reviewer. Read README.md and this guide, inspect the implementation, and run the available tests in an isolated working copy. Do not assume a feature works because a previous review or test count says so. Do not connect real Telegram/OpenAI accounts, alter startup settings or enable Study on a real family computer during unattended QA. For each reproducible defect, give severity, file and line, trigger, observed versus expected behavior, and a minimal fix. Distinguish source inspection, automated tests and actual Windows end-to-end checks. Return a prioritized list of findings, followed by remaining acceptance checks. Do not modify the repository unless explicitly assigned a fix.

## Running automated checks

Use Windows x64 with Python 3.13 and Tk. Run each suite from its own directory:

```powershell
cd LocalProduct
python -B -m unittest discover -s tests -v
cd ../LearningModule
python -B -m unittest discover -s tests -v
```

Some tests intentionally reject invalid command-line inputs and print argparse errors. Judge the final unittest summary. Skipped desktop, microphone or speech-asset checks are not successful end-to-end tests. Linux inspection is useful, but cannot certify the Windows APIs or installer.

## Recorded automated run: 17 November 2026

Run from this standalone source snapshot on Windows with Python 3.13 and `PROSTRANSTVO_STUDY_DESKTOP_TEST=1` for owned-window integration checks:

| Suite | Tests run | Successful | Skipped | Failures / errors |
|---|---:|---:|---:|---:|
| LocalProduct | 749 | 747 | 2 | 0 / 0 |
| LearningModule | 67 | 66 | 1 | 0 / 0 |

The LocalProduct skips include the intentionally excluded personal legacy source tree. Portable sources are still tested. Without the desktop opt-in, three additional native-window fixtures are skipped. The installer lifecycle tests use isolated profiles and synthetic payloads; they do not establish a clean-machine release result.

The Study runtime suite has 45 passing recovery/state tests, including corrupt JSON/schema/UTF-8 and unreadable preferences, an unchanged absolute lease deadline, actual close requests on a synthetic desktop, last-valid allowlist preservation, failure visibility, parent PIN and expired preview status. The finite home-deployment candidate separately passed 44 applicable tests. Runtime data and deployment receipts are excluded from this repository.

## Priority scenarios

0. **Recovery with corrupt preferences:** a valid active lease must survive malformed JSON, invalid schema, invalid UTF-8 and an unreadable settings.json. Recovery uses the last validated options in that process or explicit defaults (10-second grace, no additional exceptions). Do not overwrite the damaged preferences or extend the lease. Test actual close requests on disposable windows and expiry at the original deadline. With no valid lease, bad settings must not start restrictions. The separate parent status must keep the fallback warning visible through refreshes; a failed recovery must say restrictions are not working and must not be auto-hidden. Inspect the new regression cases in test_study_runtime.py.
1. **Study and parent approval:** preserve the absolute deadline on restart; approval must stop only the plan's associated session, never a newer session. Check preview mode separately. The source also supports an untimed session, which requires parent approval/PIN to finish.
2. **PIN throttling:** failed-attempt state must persist across application restarts and be shared by independent processes. Review atomic writes, strict validation, file locking and corruption handling. A PIN in a shared administrator account is not an operating-system security boundary.
3. **Daily plan:** an existing plan should open once when Study starts/resumes. Closing the plan popup must leave Study active. Confirm a second process cannot spawn duplicate plan windows.
4. **Roblox enforcement:** repeated window closure and the narrowly scoped escalation path need real Windows checks. Validate executable path, game version, session, process/window identity and creation time before termination. Other applications must retain normal WM_CLOSE behavior and save dialogs.
5. **Codex minimization:** when enabled by a parent, minimize recognized Codex UI windows during active Study, including after a manual restore. Codex background work must continue. Never terminate Codex. Preview/inactive Study must not apply this policy. Inspect codex_pause.py and the Study runtime/UI integration.
6. **Runtime resilience:** an unavailable popup, malformed optional preference or failed status-file write must not silently stop Study polling. Check runtime-status.json against actual running behavior.
7. **File operations:** use disposable fixtures only. Verify allowed-root checks, junction/symlink rejection, receipts, SHA-256, interrupted jobs and rollback integrity. A successful temporary-fixture test does not demonstrate safe sorting of an arbitrary real Downloads folder.
8. **Family messaging:** offline behavior, durable queue transitions, deduplication and uncertain delivery. A manual retry can duplicate a Telegram message. Live delivery remains a separate acceptance test with explicitly configured test credentials.
9. **Kitten and voice:** quiet bottom-right placement, no unsolicited roaming, responsive chat, correct default audio output, and a separately verified audible utterance. A successful synthesis call is not proof the speaker played it.
10. **Packaging:** source inclusion, third-party notices, speech assets/hashes, fresh installation, upgrade and removal with user data preserved. Check startup registration against recovery requirements before changing cleanup behavior.

## Boundaries and open acceptance work

- The last packaged beta is 0.1.12. This repository contains newer source changes; publishing it does not create a newly tested installer.
- Telegram/API readiness depends on the user's local configuration. No live tokens are supplied here; tests generally use fakes.
- LearningModule is a separate fraction-addition prototype, not a complete school curriculum or an integrated general AI tutor.
- The displayed play-time reward is not an enforced post-approval game timer.
- Percentage-based parent scoring, a reward wallet, long-term savings goals, phone apps and a family network are product concepts, not completed features in this snapshot.
- Full clean-machine installation, real Windows sign-in/recovery and independent live audio/API/Telegram acceptance remain necessary.
- runtime-status.json is a time-limited snapshot: use state, settings_source, settings_fallback and enforcement_active together with heartbeat_utc. A heartbeat older than valid_for_seconds (5 seconds) is not current confirmation of enforcement. A lease or startup registration alone is not proof the worker is running.
- Existing review suggestions are hypotheses to verify. In particular, Python WindowsPath equality already handles case-insensitive comparison; reproduce any alleged path-case defect before proposing a fix.

## Sharing and rights

The repository is public for QA. Public availability is not a grant of a new open-source license. Retain the bundled third-party notices and licenses. Making the repository private later cannot revoke copies that were already downloaded.


## Earlier launch-tool checks: 18 November 2026 (163bd57)

- 9 application-launch contract tests, 16 browser-plan/launch contract tests, 6 agent-tool routing tests, and 16 revised executor/protocol regressions passed. This is a focused 47-test run, not a new full-product Windows acceptance run.
- Executable launches in unit tests use mocks only. Real inventory reads found installed browsers/apps without starting them.
- The installed home stdio server was separately exercised with initialize, tools/list, inventory, a default-denied app launch and a headless plan. All four new tool names were present; no browser or desktop app was started. This verifies protocol wiring and the screen guard, not a successful foreground launch or browser control.
- New tools: agent_apps_status, prepare_app_launch, launch_agent_app, prepare_browser_background. Launches are restricted to the known catalog, with no arbitrary shell or executable argument. Foreground permission must come from the user's explicit screen-use approval for that request; the boolean is not an OS security boundary.
- Headless control is a configuration plan only. The official Playwright MCP dependency/client connection is required; the plan never installs it automatically, never starts a network listener and never imports personal cookies.
- An existing AI-client MCP connection may cache its old tool list/process. Installed source does not prove Dot has reconnected or can use new tools. Test that host separately.
- A real foreground launch test is pending the user's screen-use window. Existing Facebook login, an authenticated browser action, minimized-window DOM access, and true background operation remain unverified.
- No fresh installer was built. Previous 17 November full-suite results above remain dated evidence for that source revision.

## Agent controls: later checks, 18 November 2026

This section supersedes the earlier plan-only headless status above.

- **192 focused automated checks passed:** 181 launcher, agent routing, session-lock contracts, parent-app policy, Study runtime/UI, power control/UI and executor/protocol checks; plus 11 headless transport/Node fake-engine checks. No real power command, native input or real GUI fixture was used by these unit suites. The Node fake engine covers redirect aborts before navigation, popup isolation, blank-page adoption, closed/crashed context recovery, setup failures and EOF cleanup. This is not a new full-product acceptance run.
- The finite home-deployment candidates separately passed 90 applicable policy/runtime/UI checks using the installed finite core/resume modules. These overlap the suites above; do not add them to the count as unique tests.
- **Real installed stdio/Edge check:** a fresh home server advertised 21 unique tools, returned a power proposal without scheduling anything, opened `https://example.com/` in its own headless Edge profile, returned title/body, read the page again, reported the running controller and closed only that context. Native mouse/keyboard input was not used. Both recorded desktop-state checks were interactive; this does not prove operation while Windows is locked. The page was public; Facebook login and an authenticated action remain unverified.
- The selected parent apps are filtered out of Study close targets and normally minimized repeatedly. Missing policy is inert; invalid reloads retain the last validated in-process snapshot and report an error. Installed runtime reload retained the exact session ID and absolute deadline, produced a fresh active heartbeat and reported the selected-app count/minimization. This does not make a shared Windows account an access-control boundary.
- A real foreground Edge launch was also requested during the user's approved screen-use window and its native window was observed. The host then stopped native Computer Use because it could not verify the browser URL. No page control or authenticated action is claimed for that test.
- `lock_computer` only requests normal Windows locking. A noninteractive desktop was briefly observed after one request, but later checks did not confirm a sustained lock. Command success alone must not be presented as proof the child is locked out.
- The Power tab requires warning/confirmation and parent PIN, provides a cancellable local countdown and then sends only `/r` or `/s` with `/t 0`, never `/f` or global `/a`. MCP exposes proposal/status only. No real restart/shutdown was executed. Source and frozen dispatch wiring have mock tests; no fresh EXE was built or tested.
- Headless control requires optional Node.js, installed Edge and a compatible Playwright Core. The development machine has these dependencies; the current installer does not bundle them. The gateway opens a fixed separate profile and fixed HTTPS hosts; it neither imports existing browser cookies nor fills password fields. No network listener is started.
- Existing connected AI hosts may retain the old MCP tool list. A newly started server passing the check does not establish that Dot's host has reconnected. Keep Facebook ownership with Dot, require an explicit ACK and test its actual tools after reconnecting.
- See `LocalProduct/docs/AGENT-CONTROL-RU.txt`. Fresh installer, Windows sign-in/reboot, live parent UI, Facebook, Telegram, API and audible voice acceptance remain separate open work.

## Prepared manual Facebook login: 18 November 2026

- The real connected Windows MCP tools opened Facebook in the separate headless profile and observed its login page. This establishes browser/page control, not authentication or the state of the normal desktop browser.
- `browser_session.py` centralizes the owned profile path for manual login and headless use. A shared interprocess lease blocks concurrent updated controllers. A durable manual-login marker blocks background commands until an explicit finish, bound to the exact session ID. The normal generic launcher uses a different profile and must not be used for this login handoff.
- Manual launch requires explicit screen consent and inactive Study. The final Study check, marker write and fixed Edge spawn share Study's existing `session.lock`; this serializes launch against activation/clear without changing the lesson deadline. No Chromium lock files, cookies or existing browser processes are removed or copied.
- A positive owned-browser close acknowledgement is required before dropping a running transport. Failed worker termination retains its process reference and lease. Node EOF releases the Python lease without requiring another host call.
- The Connections tab has parent-PIN-protected launch/finish controls. Preparation and polling do not open windows or contact Facebook. Finish reports only the user's completion; a new headless check must verify the account. GUI behavior is covered by mocks, not a real user login.
- **225 focused tests passed** in the source QA copy, including 18 session-helper tests, 18 gateway/fake-engine tests and 8 new login-UI tests. These include concurrency, active/corrupt Study, marker corruption, stale finish, error-close, failed worker termination and idle EOF cases. No real visible login window or authentication was performed by the test suite.
- A separate fresh installed-server check actually read Facebook's public login page, rejected another profile lease while its context was open, and allowed the lease after acknowledged close. The readonly login plan correctly remained blocked by the active home Study session. This is independent of the mock tests.
- The owner chose to sign in later. No visible login window, credentials, 2FA, account changes or publication were performed. Live user login, correct-account verification, Dot's actual host use and a new installer remain open acceptance work.

## Windows health and connected-client AI advice: 18 November 2026

- **253 focused automated tests passed in 11.496 seconds** in the current LocalProduct working tree. The scope was application_launcher, browser_access, agent_apps, agent_control, headless_browser, browser_session, browser_login_ui, parent_apps, session_control, power_control, power_ui, engine_regressions, study_runtime, study_ui, maintenance_advisor and maintenance_ui. These are focused regressions, not a new full-product, installer or clean-machine acceptance run.
- The new coverage comprises **20 maintenance-advisor tests, 7 maintenance-UI tests and 1 agent-tool routing test**. These 28 tests are included in the 253 total; earlier 225-test results overlap and must not be added as unique tests. Test adapters, synthetic data and isolated fixtures were used; this focused run did not exercise visible desktop interaction, native input or reboot.
- After the allowlisted copy, the **27 maintenance-advisor/UI tests also passed inside this standalone clone** in 0.326 seconds. They overlap the 253-test baseline; this is a copy/path smoke check, not another set of unique tests or a full-product rerun.
- For this pre-sequence health/advisory snapshot, the packaging `SOURCES` inventory contains **72 unique Python modules**, matching `LocalProduct/src` exactly; duplicate maintenance entries were removed. This verifies source-list consistency, not a newly built installer or executable.
- A **fresh installed personal stdio process advertised 24 unique tools**. The real collect/get/submit round trip created a sanitized technical report and stored advice for its exact report ID. Advice remained explicitly unverified. This verifies protocol wiring and local persistence; it does not establish an autonomous AI API, execute a recommendation, or prove every existing connected client has refreshed its tools.
- Five updated files in the installed personal application were verified against the reviewed files by hash. Deployment receipts, actual reports, private paths, configurations and raw logs are not published in this snapshot. The maintenance UI was checked with mocks; its new tab was not verified by restarting or interacting with the live GUI.
- System maintenance operations belong to the separate personal service. Portable Local Beta retains the inert `system_broker` implementation and `not_included`; AI advice cannot start a scan, repair, cleanup, shell command or Windows action. The existing personal service uses its fixed command whitelist and Windows SID/ACL; that route does not itself supply a parent-PIN boundary.
- The advisor uses the connected Codex/ChatGPT MCP client, not a new autonomous API or the child's dialogue prompt. Its collected technical facts omit raw logs, credentials, browser history, account/SID, hostnames and personal paths; model-supplied observations remain unverified advisory text. Dated/stale job evidence is kept distinct from current execution. A stored recommendation is not evidence of a completed action or a clean antivirus result.
- Existing running MCP clients may cache an older process/tool list. A fresh installed-server check does not prove that Dot or another already connected host has reconnected.
- The last installer remains **0.1.12**. The new health UI and the [security-shield plan](LocalProduct/docs/SECURITY-SHIELD-PLAN-RU.md) are source additions, not a newly delivered installer or an installed security shield. The plan documents future controls, compatibility checks and rollback; no new OS/network enforcement is claimed here. Personal-branch broker references in that plan are comparisons to excluded code, not additional source shipped in this public tree.

## Bounded maintenance continuation: later checks, 18 November 2026

- `maintenance_sequence.py` observes one explicitly selected existing repair, archives its full local terminal receipt, and can request only the fixed cleanup and health stages after successful exact-step gates. It never starts a repair, shell, deletion, reboot, network listener or autonomous model. Portable beta remains `not_included`; running it does not discover a separate personal installation.
- **21 new fake-broker/tempfile tests passed** (0.875 seconds). They cover exact IDs, required step names and integer exit codes, failure/restart stops, post-health review, receipt ordering, process restart, request uncertainty, corrupt state, mutual exclusion and deadlines. A peer review found a delayed-intent-write deadline race; the dispatch boundary now rechecks time after persistence, with a regression test.
- A real explicit personal-runner check observed an already failed repair, saved its terminal receipt, stopped with `job_failed`, and issued **zero** cleanup/health requests. It verifies the failure-stop path, not successful forward execution of the whole sequence. No GUI, credentials, Windows restart or service replacement was involved. Raw receipts remain private.
- Completed means the specified jobs finished, not that Windows is free of damage, vulnerabilities or malware. Restart-required, unknown delivery and failure require review; they are not auto-replayed. The existing broker has no compare-and-swap previous-job parameter, so a separate caller can race between observation and dispatch; busy/ID checks do not make that gap atomic.
- The updated source inventory now contains **73 unique Python modules**, matching `LocalProduct/src`. The earlier 72-module result predates this module. There is no new maintenance-sequence UI button, packaged executable or tested installer. This explicit runner has no Windows autostart; a reboot does not automatically resume it.

- Final focused run from this exact standalone QA working copy: **274 tests passed in 12.637 seconds**, zero failures/errors/skips, across the earlier 16 modules plus maintenance_sequence. The 21 sequence cases are included in 274; the earlier 253 and the separate 21-test check overlap. Desktop opt-in was disabled. This is still not a fresh full-product or installer acceptance run.


## Metadata-only storage audit: 18 November 2026

- **14 new storage-audit unit tests passed** in the working source tree (0.349 seconds). Temporary files, real fixture hardlinks and mocked Windows attributes/priority cover bounded top-file output, continued traversal, fresh file identities, errors, progress, output protection, extended paths and configured-cloud exclusions. These are focused tests, not a new full-product or installer acceptance run; earlier totals overlap other suites and must not be added as a fresh product result.
- After the allowlisted copy, the **same 14 tests also passed in this standalone clone** (0.355 seconds), with no failures/errors/skips. This is a copy/path smoke check; the 14 cases overlap the source-tree run and are not 28 unique tests.
- An initial actual readonly C:/D: metadata traversal observed more than one million file paths and reported **partial** coverage. It exposed Windows long-path failures. Follow-up native checks also found masked cloud-ancestor flags for explicitly selected descendant roots; this does not mean the initial full-root traversal entered the registered OneDrive tree. Extended-path stat/scandir support and conservative configured-cloud exclusions were added. Native metadata stat then succeeded on an actual 265-character D: path, and a separate local guard check confirmed exclusion of the registered OneDrive root and a child. These checks did not read file contents.
- The corrected actual C:/D: traversal finished in **273.483 seconds at BelowNormal priority**: **1,153,136 observed file paths and 332,634 directories**. It remained **partial**, with **198 access/busy PermissionError failures** (Windows errors 5/32), **176 reparse entries skipped** and **2 configured cloud-root exclusions**. No long-path failure remained in that run. Traversal finished is not full visibility or a consistent filesystem snapshot.
- For that traversal, **54,757 duplicate hardlink paths** were identified and there were **zero unknown file identities** among observed files. The internal folder-aggregate limit omitted zero buckets; the bounded output omitted **28,605 aggregate rows** beyond its top 1,000. Report-byte-budget truncation was false. These are metadata/accounting checks, not content verification, physical-space estimates or cleanup results.
- The audit never reads file contents, computes content hashes, deletes/moves files or opens a GUI. It accepts explicit C:/D: roots, writes only local report/progress files on C:, excludes its output directory, and lowers only its own CLI process priority. Raw reports, personal file/media names and configurations are excluded from this public copy. No new MCP tool or GUI button was added.
- Report sizes are **logical**, not physical allocation or safely reclaimable bytes. Raw path totals are separate from unique file-identity totals; hardlink aliases count once in the known unique total, while unreliable identities and bounded-index omissions remain explicit uncertainty. Folder totals overlap between ancestor levels and must not be summed as disjoint categories. Extension and timestamps are hints, not evidence that a file is unused or safe to delete.
- Known configured OneDrive roots and conventional iCloud Photos locations are excluded before metadata traversal, alongside visible reparse/offline/Recall flags. This is **not a universal cloud-provider guard**: unrecognized or relocated sync roots whose attributes are masked can remain undetected. Finished traversal does not establish full visibility; denied/skipped entries, unknown descendants, errors, output caps and omitted names are reported separately.
- The packaging source inventory now has **74 unique Python modules**, matching LocalProduct/src exactly. The build script stages STORAGE-AUDIT-RU.txt next to WINDOWS-HEALTH-RU.txt. This is source-list/document-copy verification only: no build, new packaged executable or fresh installer was produced; the last installer remains **0.1.12**. See [the storage-audit guide](LocalProduct/docs/STORAGE-AUDIT-RU.txt).
