"""Acceptance of the frozen local learning service on synthetic temporary data.

No desktop windows, microphones, network services, or personal profiles are used.
Package inspection covers explicit prohibited files and credential patterns;
it is not a universal certification that an archive contains no private data.
"""
from __future__ import annotations

from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from learning_module.client import StdioClient


class VerificationFailure(Exception):
    pass


def require(value, code):
    if not value:
        raise VerificationFailure(code)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def contained(path, parent):
    try:
        Path(path).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


_CREDENTIAL_PATTERNS = (
    re.compile(rb"\b[0-9]{6,12}:[A-Za-z0-9_-]{30,}\b"),
    re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{30,}\b"),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)


def inspect_archive(path):
    count = 0
    scanned_text_files = 0
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        require(len(infos) <= 10000, "archive_entry_limit")
        names = [info.filename for info in infos]
        require(len(names) == len(set(names)), "archive_duplicate_path")
        require(sum(info.file_size for info in infos) <= 512 * 1024 * 1024, "archive_size_limit")
        for info in infos:
            relative = PurePosixPath(info.filename)
            parts = [part.casefold() for part in relative.parts]
            require(not relative.is_absolute() and ".." not in parts
                    and "\\" not in info.filename and ":" not in info.filename,
                    "unsafe_archive_path")
            require(not stat.S_ISLNK(info.external_attr >> 16), "archive_symlink")
            if info.is_dir():
                continue
            name = relative.name.casefold()
            require(not name.endswith((".sqlite", ".sqlite3", ".db", ".sqlite-wal", ".sqlite-shm")),
                    "packaged_database")
            require(name not in {"family-profile.json", "server-session.json", ".env", "credentials", "credentials.json"},
                    "packaged_sensitive_config")
            require(not ({"data", "chat-logs", ".aws", ".codex", "__pycache__"} & set(parts)),
                    "packaged_personal_or_cache_directory")
            require(info.file_size <= 64 * 1024 * 1024, "archive_member_limit")
            if relative.suffix.casefold() in {".py", ".cs", ".json", ".txt", ".toml", ".ini", ".yaml", ".yml"}:
                content = archive.read(info)
                require(not any(pattern.search(content) for pattern in _CREDENTIAL_PATTERNS),
                        "credential_pattern_in_archive")
                scanned_text_files += 1
            count += 1
        bad_crc = archive.testzip()
        require(bad_crc is None, "archive_crc_error")
    return {"file_count": count, "text_files_scanned_for_credential_patterns": scanned_text_files,
            "names": set(names)}


def verify_manifest(runtime):
    manifest_path = runtime / "files-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    require(manifest.get("version") == "0.1.0" and type(manifest.get("files")) is dict,
            "manifest_format")
    for relative, expected in manifest["files"].items():
        require(isinstance(relative, str) and "\\" not in relative, "manifest_path_format")
        path = runtime / relative
        require(contained(path, runtime) and path.is_file() and not path.is_symlink(),
                "manifest_file_missing_or_outside")
        require(path.stat().st_size == expected["size"], "manifest_size_mismatch")
        require(digest(path) == expected["sha256"], "manifest_sha256_mismatch")
    actual = {path.relative_to(runtime).as_posix() for path in runtime.rglob("*") if path.is_file()}
    require(actual == set(manifest["files"]) | {"files-manifest.json"}, "unmanifested_runtime_file")
    return manifest


def completed(client, operation, params=None, **kwargs):
    response = client.request(operation, params, **kwargs)
    require(response["status"] == "completed", "operation_not_completed_" + operation.replace(".", "_"))
    return response


def exercise_frozen(runtime):
    executable = runtime / "LearningModuleCore.exe"
    require(executable.is_file(), "frozen_executable_missing")
    with tempfile.TemporaryDirectory(prefix="learning-frozen-acceptance-") as temporary:
        data = Path(temporary) / "synthetic-data"
        argv = [str(executable), "--stdio", "--data-dir", str(data)]
        with StdioClient(argv, timeout=10) as client:
            described = completed(client, "module.describe")["result"]
            require(described.get("runtime_available") is True and described.get("local_only") is True,
                    "frozen_describe_unavailable")
            created = completed(client, "task.create", {"preset_id": "g4_same_2"})
            original_task = created["result"]["task"]
            started = completed(client, "session.start", {
                "task_id": original_task["task_id"], "learner_id": "synthetic-verification-learner"})
            session = started["result"]["session"]
            session_id = session["session_id"]

            wrong = completed(client, "turn.submit", {
                "session_id": session_id, "action": "answer", "text": "2/6"},
                expected_revision=started["session_revision"])
            require(wrong["result"]["assessment"]["correct_value"] is False, "wrong_answer_accepted")
            unreduced = completed(client, "turn.submit", {
                "session_id": session_id, "action": "answer", "text": "3/6"},
                expected_revision=wrong["session_revision"])
            assessment = unreduced["result"]["assessment"]
            require(assessment["correct_value"] is True and assessment["correct_form"] is False
                    and assessment["correct"] is False, "reduction_requirement_not_distinguished")
            hinted = completed(client, "turn.submit", {"session_id": session_id, "action": "hint"},
                               expected_revision=unreduced["session_revision"])
            require(hinted["result"]["session"]["hint_level"] > 0, "hint_not_recorded")
            answer_params = {"session_id": session_id, "action": "answer", "text": "1/2"}
            answer_revision = hinted["session_revision"]
            answer_id = "frozen-correct-answer"
            answered = completed(client, "turn.submit", answer_params,
                                 expected_revision=answer_revision, request_id=answer_id)
            require(answered["result"]["assessment"]["correct"] is True,
                    "reduced_correct_answer_rejected")
            require(answered["result"]["session"]["assisted_correct"] is True
                    and answered["result"]["session"]["independent_correct"] is False,
                    "assisted_answer_mislabeled")

            practice = completed(client, "turn.submit", {"session_id": session_id, "action": "practice"},
                                 expected_revision=answered["session_revision"])
            new_task = practice["result"]["task"]
            require(new_task["task_id"] != original_task["task_id"]
                    and (new_task["left"], new_task["right"]) != (original_task["left"], original_task["right"]),
                    "practice_not_a_new_task")
            independent_answer = str(Fraction(new_task["left"]) + Fraction(new_task["right"]))
            independent = completed(client, "turn.submit", {
                "session_id": session_id, "action": "answer", "text": independent_answer},
                expected_revision=practice["session_revision"])
            require(independent["result"]["assessment"]["correct"] is True
                    and independent["result"]["session"]["independent_correct"] is True
                    and independent["result"]["session"]["assisted_correct"] is False,
                    "independent_practice_mislabeled")
            attempts_before_restart = independent["result"]["session"]["attempt_count"]
            revision_before_restart = independent["session_revision"]

            exported = completed(client, "report.export", {"session_id": session_id})
            export_path = Path(exported["result"]["export"]["path"])
            require(exported["result"]["export"]["saved"] is True and contained(export_path, data / "exports"),
                    "export_not_local")
            payload = json.loads(export_path.read_text(encoding="utf-8"))
            require(payload["source"] == "local_learning_module", "export_source")
            tasks = payload["tasks"]
            require(original_task["task_id"] in tasks and new_task["task_id"] in tasks,
                    "export_missing_original_or_practice_context")
            require(all(type(task) is dict and task.get("prompt_ru") and task.get("prompt_en")
                        for task in tasks.values()), "export_incomplete_task_context")
            for event in payload["events"]:
                for key in ("task_id", "new_task_id"):
                    if key in event:
                        require(event[key] in tasks, "export_event_without_task_context")
            assisted_events = [event for event in payload["events"]
                               if event.get("kind") == "answer" and event.get("task_id") == original_task["task_id"]
                               and event.get("assessment", {}).get("correct") is True]
            require(assisted_events and assisted_events[-1]["assisted"] is True,
                    "export_lost_assisted_evidence")

        # A fresh frozen process must retain the original idempotency receipt.
        with StdioClient(argv, timeout=10) as client:
            replay = completed(client, "turn.submit", answer_params,
                               expected_revision=answer_revision, request_id=answer_id)
            require(replay == answered, "persisted_replay_changed")
            changed = client.request("turn.submit", dict(answer_params, text="1/3"),
                                     expected_revision=answer_revision, request_id=answer_id)
            require(changed["status"] == "conflict", "request_id_payload_conflict_accepted")
            snapshot = completed(client, "session.snapshot", {"session_id": session_id})
            require(snapshot["session_revision"] == revision_before_restart
                    and snapshot["result"]["session"]["attempt_count"] == attempts_before_restart
                    and snapshot["result"]["task"]["task_id"] == new_task["task_id"],
                    "replay_changed_attempt_count_revision_or_task")
            history = completed(client, "history.list")["result"]["history"]
            require(len(history) == 1 and history[0]["session_id"] == session_id, "restart_lost_history")
            stopped = completed(client, "session.stop", {"session_id": session_id},
                                expected_revision=snapshot["session_revision"])
            require(stopped["result"]["session"]["state"] == "stopped", "stop_not_persisted")

        return {"wrong_answer_rejected": True, "answer_value_and_form_distinguished": True,
                "assisted_and_independent_results_distinguished": True,
                "new_practice_task": True, "export_includes_all_task_contexts": True,
                "restart_preserves_history": True, "persisted_idempotency_replay": True,
                "conflicting_request_id_rejected": True, "no_extra_attempt_on_replay": True,
                "temporary_data_only": True, "desktop_ui_started": False}


def main():
    report_path = ROOT / "frozen-verification.json"
    report = {"version": "0.1.0", "checked_at_utc": datetime.now(timezone.utc).isoformat(),
              "status": "failed", "desktop_ui_started": False, "personal_data_used": False,
              "limits": ["Frozen stdio service on this PC; no clean second Windows device tested.",
                         "No desktop GUI, physical microphone, speaker or active kitten integration acceptance.",
                         "Arithmetic and persistence checks do not establish educational effectiveness.",
                         "Privacy inspection covers prohibited paths and credential patterns, not all possible sensitive content."]}
    try:
        receipt = json.loads((ROOT / "build-receipt.json").read_text(encoding="utf-8"))
        runtime = Path(receipt["runtime"])
        portable = Path(receipt["portable"])
        source = Path(receipt["source"])
        require(receipt["version"] == "0.1.0", "unexpected_build_version")
        require(contained(runtime, ROOT / "build") and contained(portable, ROOT / "build")
                and contained(source, ROOT / "build"), "build_path_outside_workspace")
        require(digest(portable) == receipt["portable_sha256"], "portable_receipt_hash_mismatch")
        require(digest(source) == receipt["source_sha256"], "source_receipt_hash_mismatch")
        manifest = verify_manifest(runtime)
        portable_check = inspect_archive(portable)
        require(portable_check.pop("names") == set(manifest["files"]) | {"files-manifest.json"},
                "portable_manifest_entries_mismatch")
        source_check = inspect_archive(source)
        source_check.pop("names")
        with zipfile.ZipFile(source) as archive:
            for code_path in (ROOT / "src" / "learning_module").glob("*.py"):
                relative = code_path.relative_to(ROOT).as_posix()
                require(hashlib.sha256(archive.read(relative)).hexdigest() == digest(code_path),
                        "source_runtime_code_changed_since_build")
        report.update(runtime=str(runtime), executable_sha256=digest(runtime / "LearningModuleCore.exe"),
                      portable_sha256=digest(portable), source_sha256=digest(source),
                      manifest_files_verified=len(manifest["files"]),
                      portable_privacy_inspection=portable_check, source_privacy_inspection=source_check,
                      frozen_acceptance=exercise_frozen(runtime), status="passed")
    except Exception as error:
        report["failure"] = str(error) if isinstance(error, VerificationFailure) else type(error).__name__
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"status":"failed", "failure":report["failure"]}))
        return 1
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status":report["status"], "manifest_files_verified":report["manifest_files_verified"],
                      "frozen_acceptance":report["frozen_acceptance"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
