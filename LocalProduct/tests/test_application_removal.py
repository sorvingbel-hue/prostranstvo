"""Owned fake installers/providers/openers only; no real uninstaller or UI."""
import copy
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import application_removal as ar


class ApplicationRemovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="application-removal-owned-")
        self.owned = Path(self.temp.name).resolve()
        self.ledger = self.owned / "ledger"
        self.client = self.owned / "steam.exe"
        self.client.write_bytes(b"synthetic executable; never executed")
        self.system = self.owned / "System32"
        self.system.mkdir()
        self.msi = self.system / "msiexec.exe"
        self.msi.write_bytes(b"synthetic native system executable")
        self.install = self.owned / "installed" / "Garden"
        self.install.mkdir(parents=True)
        self.now = 1_800_000_000.0
        self.rows = [self.steam()]
        self.complete = True
        self.calls = []
        self.manager = self.manager_for()
        self.forbid_process = mock.patch.object(ar.subprocess, "Popen",
                                               side_effect=AssertionError("real process forbidden")).start()
        self.forbid_settings = mock.patch.object(ar.os, "startfile", create=True,
                                                side_effect=AssertionError("real UI forbidden")).start()
        self.free = mock.patch.object(ar, "_free_snapshot", return_value={"owned-volume": 1000}).start()
        self.addCleanup(mock.patch.stopall)

    def tearDown(self):
        self.assertEqual(self.owned.parent, Path(tempfile.gettempdir()).resolve())
        self.assertTrue(self.owned.name.startswith("application-removal-owned-"))
        self.temp.cleanup()  # Only this test's newly created directory.

    def steam(self, identity="123456"):
        return dict(id="steam:" + identity, name="Fixture Garden", publisher="Fixture Vendor",
                    install_path=str(self.install), source="registry", registered=True,
                    registry_context="HKLM32", registry_key="Steam App " + identity,
                    uninstall_string=f'"{self.client}" steam://uninstall/{identity}',
                    reported_bytes=42, windows_installer=False, system_component=False,
                    no_remove=False, release_type="")

    def snapshot(self):
        return dict(items=copy.deepcopy(self.rows), complete=self.complete, warnings=[],
                    steam_client=str(self.client), system32=str(self.system))

    def open_fake(self, argv):
        self.calls.append(argv)
        return None

    def manager_for(self, opener=None):
        return ar.ApplicationRemovalManager(root=self.ledger, provider=self.snapshot,
                                            opener=opener or self.open_fake, clock=lambda: self.now)

    def prepare(self):
        return self.manager.prepare(self.rows[0]["id"])

    def add_hold(self, **changes):
        descriptor = dict(id=self.rows[0]["id"], name=self.rows[0]["name"],
                          install_path=str(self.install))
        descriptor.update(changes)
        return self.manager.add_policy_hold(descriptor, "Prior review rejected this target",
                                            "Owned fixture policy")

    def msi_row(self):
        product = "{12345678-1234-1234-1234-123456789ABC}"
        return dict(self.steam(), id="msi:" + product, registry_key=product,
                    windows_installer=True,
                    uninstall_string=f'"{self.msi}" /I{product}')

    def generic(self):
        return dict(self.steam(), id="registry:" + "a" * 64,
                    registry_key="FixtureGarden",
                    uninstall_string='malicious.exe /arbitrary command')

    def test_constructor_close_and_default_path_resolution_are_inert(self):
        with mock.patch.object(ar, "_local_appdata", side_effect=AssertionError("must be lazy")):
            ar.ApplicationRemovalManager(provider=self.snapshot).close()
        self.assertFalse(self.ledger.exists())
        self.assertFalse(self.calls)

    def test_catalog_is_read_only_and_does_not_expose_raw_commands(self):
        catalog = self.manager.catalog()
        self.assertFalse(self.ledger.exists())
        self.assertFalse(self.manager.anchor_path.exists())
        row = catalog["items"][0]
        self.assertEqual(row["status"], "available")
        self.assertEqual(row["action"], "steam_uninstall")
        self.assertNotIn("uninstall_string", row)
        self.assertNotIn("argv", row)
        row["name"] = "changed"
        self.assertEqual(self.rows[0]["name"], "Fixture Garden")

    def test_explicit_local_ledger_path_required(self):
        for path in ("relative", Path(self.owned.anchor), r"\\server\share\ledger"):
            with self.subTest(path=str(path)), self.assertRaises(ar.RemovalError):
                ar.ApplicationRemovalManager(root=path)

    def test_prepare_saves_authenticated_bounded_route_without_dispatch(self):
        plan = self.prepare()
        self.assertEqual(plan["target"], "steam://uninstall/123456")
        self.assertEqual(plan["argv"], [str(self.client), plan["target"]])
        self.assertEqual(ar._epoch(plan["expires_utc"]) - ar._epoch(plan["created_utc"]), ar.TTL)
        self.assertTrue(self.manager.anchor_path.exists())
        self.assertTrue(self.manager._plan_path(plan["plan_id"]).is_file())
        self.assertFalse(self.calls)

    def test_true_boolean_approval_required_and_valid_request_runs_only_once(self):
        plan = self.prepare()
        for value in (False, None, 1, "yes"):
            self.assertEqual(self.manager.request(plan["plan_id"], approved=value)["state"], "not_approved")
        self.assertEqual(self.calls, [])
        result = self.manager.request(plan["plan_id"], approved=True)
        self.assertEqual(result["state"], "requested")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.manager_for().request(plan["plan_id"], approved=True)["state"], "requested")
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.install.exists())

    def test_plan_expiration_does_not_open_anything(self):
        plan = self.prepare()
        self.now += ar.TTL + 1
        self.assertEqual(self.manager.request(plan["plan_id"], approved=True)["state"], "expired")
        self.assertFalse(self.calls)

    def test_plan_tamper_and_unknown_fields_fail_authentication(self):
        plan = self.prepare()
        for field, value in (("target", "steam://uninstall/999"), ("argv", ["evil.exe"]),
                             ("expires_utc", ar._utc(self.now + 10_000)), ("unexpected", True)):
            damaged = copy.deepcopy(plan)
            damaged[field] = value
            self.manager._plan_path(plan["plan_id"]).write_text(json.dumps(damaged), encoding="utf-8")
            with self.subTest(field=field), self.assertRaises(ar.RemovalError):
                self.manager.request(plan["plan_id"], approved=True)
        self.assertFalse(self.calls)

    def test_duplicate_keys_nonfinite_and_oversized_plan_fail_closed(self):
        plan = self.prepare()
        for raw in ('{"x":1,"x":2}', '{"x":NaN}', " " * (ar.MAX_JSON + 1)):
            self.manager._plan_path(plan["plan_id"]).write_text(raw, encoding="utf-8")
            with self.assertRaises(ValueError):
                self.manager.request(plan["plan_id"], approved=True)
        self.assertFalse(self.calls)

    def test_registration_change_after_prepare_prevents_dispatch(self):
        plan = self.prepare()
        self.rows[0]["publisher"] = "Different registration"
        self.assertEqual(self.manager.request(plan["plan_id"], approved=True)["state"], "stale")
        self.assertFalse(self.calls)

    def test_registration_disappearing_before_request_is_stale_not_removed(self):
        plan = self.prepare()
        self.rows.clear()
        self.assertEqual(self.manager.request(plan["plan_id"], approved=True)["state"], "stale")
        self.assertFalse(self.calls)

    def test_changed_executable_is_rejected_even_with_same_size_and_mtime(self):
        plan = self.prepare()
        old = self.client.stat()
        self.client.write_bytes(b"X" * old.st_size)
        os.utime(self.client, ns=(old.st_atime_ns, old.st_mtime_ns))
        self.assertEqual(self.manager.request(plan["plan_id"], approved=True)["state"], "stale")
        self.assertFalse(self.calls)

    def test_fresh_protection_added_after_prepare_blocks_dispatch(self):
        plan = self.prepare()
        self.rows[0]["name"] = "Minecraft Legends"
        self.assertEqual(self.manager.request(plan["plan_id"], approved=True)["state"], "protected")
        self.assertFalse(self.calls)

    def test_all_minecraft_aliases_java_and_dependencies_are_protected(self):
        names = ("Minecraft Legends", "Minecraft Dungeons", "Mojang", "TLauncher",
                 "Prism Launcher", "MultiMC", "PolyMC", "CurseForge", "Modrinth",
                 "Fabric", "Forge", "NeoForge", "OptiFine", "Modpacks",
                 "Java 8", "JRE", "JDK 21", "OpenJDK", "Temurin", "Zulu",
                 "Eclipse Adoptium", "NVIDIA Driver", "Example Runtime",
                 "Security Tool", "Prostranstvo", "Steam")
        for name in names:
            self.rows[0]["name"] = name
            with self.subTest(name=name):
                self.assertEqual(self.manager.catalog()["items"][0]["status"], "protected")
        self.rows[0]["name"] = "Ｍｉｎｅｃｒａｆｔ"
        self.assertEqual(self.manager.catalog()["items"][0]["status"], "protected")

    def test_minecraft_publisher_and_world_path_are_protected(self):
        self.rows[0]["publisher"] = "Mojang Studios"
        self.assertEqual(self.manager.catalog()["items"][0]["status"], "protected")
        self.rows[0]["publisher"] = "Fixture Vendor"
        self.rows[0]["install_path"] = str(self.owned / "worlds")
        self.assertEqual(self.manager.catalog()["items"][0]["status"], "protected")

    def test_roblox_player_studio_and_publisher_are_protected(self):
        for name in ("Roblox", "Roblox Player for fixture", "Roblox Studio for fixture",
                     "RobloxPlayerBeta", "RobloxStudioLauncher", "Ｒｏｂｌｏｘ Studio"):
            self.rows[0] = dict(self.generic(), name=name)
            with self.subTest(name=name):
                self.assertEqual(self.manager.catalog()["items"][0]["status"], "protected")
                with self.assertRaises(ar.RemovalError):
                    self.prepare()
        self.rows[0] = dict(self.generic(), publisher="Roblox Corporation")
        self.assertEqual(self.manager.catalog()["items"][0]["status"], "protected")
        self.assertFalse(self.calls)

    def test_roblox_install_roots_protect_unusual_registry_identity(self):
        for relative in ("Roblox", "Roblox/Versions/version-fixture", "RobloxPlayer",
                         "Roblox Studio/Versions/version-fixture"):
            self.rows[0] = dict(self.generic(), install_path=str(self.owned / relative))
            with self.subTest(relative=relative):
                self.assertEqual(self.manager.catalog()["items"][0]["status"], "protected")
                with self.assertRaises(ar.RemovalError):
                    self.prepare()
        self.assertFalse(self.calls)

    def test_roblox_protection_does_not_match_unrelated_names_or_path_substrings(self):
        for name in ("Rob's Garden", "Robot Arena", "NotRoblox", "Robloxish Garden"):
            self.rows[0] = dict(self.steam(), name=name,
                                install_path=str(self.owned / "Robloxish" / "Garden"))
            with self.subTest(name=name):
                self.assertEqual(self.manager.catalog()["items"][0]["status"], "available")
        self.assertFalse(self.calls)

    def test_generic_codex_ancestor_is_not_an_agent_installation(self):
        install = self.owned / "Codex" / "Applications" / "FixtureGarden"
        install.mkdir(parents=True)
        self.rows[0]["install_path"] = str(install)
        self.assertEqual(self.manager.catalog()["items"][0]["status"], "available")
        plan = self.prepare()
        self.assertEqual(self.manager.request(plan["plan_id"], approved=True)["state"], "requested")
        self.assertEqual(len(self.calls), 1)

    def test_genuine_agent_identity_and_actual_self_directory_stay_protected(self):
        install = self.owned / "Codex" / "Applications" / "FixtureGarden"
        install.mkdir(parents=True)
        self.rows[0]["install_path"] = str(install)
        for field, value in (("name", "Codex"), ("publisher", "OpenAI Codex Tools"),
                             ("name", "Prostranstvo")):
            self.rows[0] = dict(self.steam(), install_path=str(install), **{field: value})
            with self.subTest(field=field, value=value):
                self.assertEqual(self.manager.catalog()["items"][0]["status"], "protected")
        self.rows[0] = dict(self.steam(), install_path=str(Path(ar.__file__).absolute().parent))
        self.assertEqual(self.manager.catalog()["items"][0]["status"], "protected")
        self.assertFalse(self.calls)

    def test_minecraft_path_below_codex_parent_remains_protected(self):
        self.rows[0]["install_path"] = str(self.owned / "Codex" / ".minecraft")
        self.assertEqual(self.manager.catalog()["items"][0]["status"], "protected")
        self.assertFalse(self.calls)

    def test_system_no_remove_and_update_are_protected(self):
        for field, value in (("system_component", True), ("no_remove", True), ("release_type", "Update")):
            original = self.steam()
            self.rows[0] = dict(original, **{field: value})
            with self.subTest(field=field):
                self.assertEqual(self.manager.catalog()["items"][0]["status"], "protected")

    def test_hold_survives_manager_restart_and_portable_hold_is_visible(self):
        self.add_hold(id="portable:fixture-garden")
        catalog = self.manager_for().catalog()
        self.assertTrue(all(x["status"] == "blocked_by_policy" for x in catalog["items"]))
        self.assertIn("portable:fixture-garden", [x["id"] for x in catalog["items"]])
        with self.assertRaises(ar.RemovalError):
            self.prepare()

    def test_hold_added_after_prepare_and_alias_action_change_stay_blocked(self):
        plan = self.prepare()
        self.add_hold(id="portable:fixture", name="Different title", action="windows_settings")
        self.assertEqual(self.manager_for().request(plan["plan_id"], approved=True)["state"], "blocked_by_policy")
        self.assertFalse(self.calls)

    def test_hold_alias_name_ignores_brand_punctuation_and_does_not_replace_old_alias(self):
        self.add_hold(id="portable:fixture", name="Fixture Garden", install_path="")
        self.manager.add_policy_hold(dict(id="portable:fixture", name="Second alias"),
                                     "Still held", "Owned policy")
        self.rows[0]["name"] = "Fixture® Garden"
        self.assertEqual(self.manager_for().catalog()["items"][0]["status"], "blocked_by_policy")

    def test_partial_hold_loss_and_corruption_fail_closed(self):
        self.add_hold()
        holds = self.ledger / "holds.json"
        original = holds.read_bytes()
        for raw in (b"[]", b'{"x":NaN}', b"broken"):
            holds.write_bytes(raw)
            self.assertEqual(self.manager_for().catalog()["items"][0]["status"], "blocked_by_policy")
        holds.write_bytes(original)
        holds.unlink()
        with self.assertRaises((ValueError, OSError)):
            self.manager_for().prepare(self.rows[0]["id"])

    def test_complete_ledger_folder_loss_is_blocked_by_adjacent_anchor(self):
        self.add_hold()
        anchor = self.manager.anchor_path
        self.assertTrue(anchor.is_file())
        shutil.rmtree(self.ledger)  # Own synthetic ledger only; keep own anchor.
        other = self.manager_for()
        self.assertEqual(other.catalog()["items"][0]["status"], "blocked_by_policy")
        with self.assertRaises(ar.RemovalError):
            other.prepare(self.rows[0]["id"])
        self.assertFalse(self.ledger.exists())

    def test_missing_or_mismatched_anchor_blocks_new_instances(self):
        self.add_hold()
        anchor = self.manager.anchor_path
        saved = anchor.read_bytes()
        anchor.unlink()
        self.assertEqual(self.manager_for().catalog()["items"][0]["status"], "blocked_by_policy")
        anchor.write_text('{"version":1,"ledger_id":"' + "0" * 32 + '"}', encoding="utf-8")
        with self.assertRaises(ar.RemovalError):
            self.manager_for().prepare(self.rows[0]["id"])
        anchor.write_bytes(saved)

    def test_duplicate_and_incomplete_provider_cannot_confirm_absence(self):
        plan = self.prepare()
        self.rows.append(copy.deepcopy(self.rows[0]))
        self.assertFalse(self.manager.catalog()["items"])
        self.assertEqual(self.manager.verify(plan["plan_id"])["state"], "verification_incomplete")
        self.rows.clear()
        self.complete = False
        self.assertEqual(self.manager.verify(plan["plan_id"])["state"], "verification_incomplete")
        self.assertFalse(self.calls)

    def test_extra_steam_args_mismatched_ids_and_arbitrary_executable_rejected(self):
        good = self.steam()
        variants = [
            good["uninstall_string"] + " extra", good["uninstall_string"] + " & calc.exe",
            f'"{self.client}" steam://uninstall/999',
            f'"{self.owned / "evil.exe"}" steam://uninstall/123456',
            good["uninstall_string"] + "\n", f'"{self.client}" "steam://uninstall/123456"',
        ]
        for command in variants:
            self.rows[0] = dict(good, uninstall_string=command)
            with self.subTest(command=command):
                catalog = self.manager.catalog()
                self.assertTrue(not catalog["items"] or catalog["items"][0]["status"] == "unsupported")
        self.assertFalse(self.calls)

    def test_msi_uses_fixed_system_binary_guid_and_no_restart_only(self):
        self.rows[:] = [self.msi_row()]
        plan = self.prepare()
        self.assertEqual(plan["action"], "msi_uninstall")
        self.assertEqual(plan["argv"], [str(self.msi), "/x", self.rows[0]["registry_key"], "/norestart"])
        self.assertEqual(self.manager.request(plan["plan_id"], approved=True)["state"], "requested")
        self.assertNotIn("/quiet", self.calls[0])

    def test_legitimate_native_system_hardlink_allowed_but_steam_link_forbidden(self):
        alias = self.owned / "native-system-alias"
        os.link(self.msi, alias)
        self.rows[:] = [self.msi_row()]
        self.assertEqual(self.manager.catalog()["items"][0]["status"], "available")
        self.assertEqual(self.prepare()["action"], "msi_uninstall")
        os.link(self.client, self.owned / "steam-alias")
        self.rows[:] = [self.steam()]
        self.assertEqual(self.manager.catalog()["items"][0]["status"], "unsupported")

    def test_guid_shaped_non_msi_and_untrusted_msi_parameters_never_dispatch(self):
        row = self.msi_row()
        for changed in (dict(row, windows_installer=False),
                        dict(row, uninstall_string=row["uninstall_string"] + " /quiet"),
                        dict(row, uninstall_string=row["uninstall_string"] + " REBOOT=Force"),
                        dict(row, uninstall_string='evil.exe /x' + row["registry_key"])):
            self.rows[:] = [changed]
            self.assertEqual(self.manager.catalog()["items"][0]["status"], "unsupported")
        self.assertFalse(self.calls)

    def test_other_registered_app_opens_only_fixed_manual_settings_uri(self):
        self.rows[:] = [self.generic()]
        plan = self.prepare()
        self.assertEqual(plan["action"], "windows_settings")
        self.assertEqual(plan["target"], ar.SETTINGS)
        self.assertEqual(self.manager.request(plan["plan_id"], approved=True)["state"], "requested")
        self.assertEqual(self.calls, [[ar.SETTINGS]])

    def test_write_ahead_receipt_exists_before_fake_external_effect(self):
        def opening(argv):
            saved = ar._json_read(self.manager._receipt_path(plan["plan_id"]))
            self.assertEqual(saved["state"], "dispatching")
            self.assertTrue(saved["attempted"])
            self.calls.append(argv)
        self.manager.opener = opening
        plan = self.prepare()
        self.manager.request(plan["plan_id"], approved=True)
        self.assertEqual(len(self.calls), 1)

    def test_failed_and_uncertain_dispatch_are_not_retried(self):
        for error, expected in ((OSError("owned failure"), "failed"), (RuntimeError("unknown outcome"), "uncertain")):
            def failing(argv, error=error):
                self.calls.append(argv)
                raise error
            manager = self.manager_for(opener=failing)
            plan = manager.prepare(self.rows[0]["id"])
            self.assertEqual(manager.request(plan["plan_id"], approved=True)["state"], expected)
            before = len(self.calls)
            self.assertEqual(manager.request(plan["plan_id"], approved=True)["state"], expected)
            self.assertEqual(len(self.calls), before)

    def test_crash_after_write_ahead_becomes_uncertain_without_retry(self):
        def crash(argv):
            self.calls.append(argv)
            raise KeyboardInterrupt()
        manager = self.manager_for(opener=crash)
        plan = manager.prepare(self.rows[0]["id"])
        with self.assertRaises(KeyboardInterrupt):
            manager.request(plan["plan_id"], approved=True)
        self.assertEqual(self.manager_for().request(plan["plan_id"], approved=True)["state"], "uncertain")
        self.assertEqual(len(self.calls), 1)

    def test_missing_receipt_after_dispatch_does_not_reopen_uninstaller(self):
        plan = self.prepare()
        self.manager.request(plan["plan_id"], approved=True)
        self.manager._receipt_path(plan["plan_id"]).unlink()
        with self.assertRaises(ar.RemovalError):
            self.manager_for().request(plan["plan_id"], approved=True)
        self.assertEqual(len(self.calls), 1)

    def test_replayed_authentic_initial_receipt_does_not_reopen_uninstaller(self):
        plan = self.prepare()
        path = self.manager._receipt_path(plan["plan_id"])
        initial = path.read_bytes()
        self.manager.request(plan["plan_id"], approved=True)
        path.write_bytes(initial)
        self.assertEqual(self.manager_for().request(plan["plan_id"], approved=True)["state"], "uncertain")
        self.assertEqual(len(self.calls), 1)

    def test_verify_is_observation_not_removed_and_never_dispatches(self):
        plan = self.prepare()
        self.manager.request(plan["plan_id"], approved=True)
        self.assertEqual(self.manager.verify(plan["plan_id"])["state"], "requested")
        self.rows.clear()
        observed = self.manager.verify(plan["plan_id"])
        self.assertEqual(observed["state"], "registration_absent_remnants")
        self.assertTrue(self.install.exists())
        self.assertNotEqual(observed["state"], "removed")
        self.assertEqual(len(self.calls), 1)

    def test_verify_absent_then_present_does_not_keep_false_absence_state(self):
        plan = self.prepare()
        self.manager.request(plan["plan_id"], approved=True)
        saved = self.rows[:]
        self.rows.clear()
        self.assertTrue(self.manager.verify(plan["plan_id"])["state"].startswith("registration_absent"))
        self.rows[:] = saved
        result = self.manager.verify(plan["plan_id"])
        self.assertEqual(result["state"], "uncertain")
        self.assertTrue(result["registration_present"])
        self.assertEqual(len(self.calls), 1)

    def test_verify_before_request_does_not_consume_external_attempt(self):
        plan = self.prepare()
        self.assertEqual(self.manager.verify(plan["plan_id"])["state"], "not_requested")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.manager.request(plan["plan_id"], approved=True)["state"], "requested")
        self.assertEqual(len(self.calls), 1)

    def test_opener_removed_claim_does_not_become_removal_proof(self):
        self.manager.opener = lambda argv: {"state": "removed", "returncode": 0}
        plan = self.prepare()
        result = self.manager.request(plan["plan_id"], approved=True)
        self.assertEqual(result["state"], "requested")
        self.assertEqual(result["before_free"], result["after_free"])

    def test_ledger_hardlink_is_forbidden_even_for_native_system_exception(self):
        plan = self.prepare()
        key = self.ledger / "key.bin"
        os.link(key, self.owned / "copied-key-link")
        with self.assertRaises((ValueError, OSError)):
            self.manager.request(plan["plan_id"], approved=True)
        self.assertFalse(self.calls)

    def test_malformed_marker_and_anchor_json_block_without_dispatch(self):
        self.add_hold()
        marker = self.ledger / "marker.json"
        original = marker.read_bytes()
        for value in ([], 1, None):
            marker.write_text(json.dumps(value), encoding="utf-8")
            self.assertEqual(self.manager_for().catalog()["items"][0]["status"], "blocked_by_policy")
        marker.write_bytes(original)


if __name__ == "__main__":
    unittest.main()
