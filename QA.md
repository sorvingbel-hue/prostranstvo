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
