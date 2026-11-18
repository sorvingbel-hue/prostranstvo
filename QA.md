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
