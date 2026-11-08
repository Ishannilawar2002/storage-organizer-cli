"""Behavior tests for the preview-first storage organizer."""

import copy
import json
import tempfile
import unittest
from pathlib import Path

import storage_organizer as organizer


class StorageOrganizerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.cloud = self.base / "cloud"
        self.local = self.base / "local"
        self.cloud.mkdir()
        self.local.mkdir()
        self.config = copy.deepcopy(organizer.DEFAULT_CONFIG)
        self.config["roots"] = [str(self.base)]

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_reconcile_detects_content_matches_and_unique_files(self):
        (self.cloud / "already-local.txt").write_bytes(b"same bytes")
        (self.local / "renamed-copy.txt").write_bytes(b"same bytes")
        (self.cloud / "new").mkdir()
        (self.cloud / "new" / "only-here.txt").write_bytes(b"cloud only")
        (self.local / "local-only.txt").write_bytes(b"local only")

        report = organizer.reconcile_cloud_local(self.cloud, self.local, self.config)

        self.assertEqual(len(report["cloud_matches_local"]), 1)
        self.assertEqual(len(report["cloud_only_content"]), 1)
        self.assertEqual(len(report["local_only_content"]), 1)
        self.assertEqual(report["potential_localize_bytes"], len(b"cloud only"))

    def test_duplicate_quarantine_is_previewed_and_reversible(self):
        first = self.base / "a.bin"
        second = self.base / "b.bin"
        first.write_bytes(b"duplicate data")
        second.write_bytes(b"duplicate data")

        report = organizer.duplicate_report([self.base], self.config)
        plan = organizer.duplicate_quarantine_plan(report, self.config)
        plan_path = self.base / "quarantine-plan.json"
        journal_path = self.base / "quarantine-journal.json"
        organizer.json_write(plan_path, plan)

        self.assertEqual(report["duplicate_paths"], 1)
        self.assertEqual(plan["actions"][0]["status"], "ready")
        self.assertEqual(organizer.apply_plan(plan_path, journal_path, yes=False), 0)
        self.assertTrue(second.exists())

        self.assertEqual(organizer.apply_plan(plan_path, journal_path, yes=True), 0)
        self.assertTrue(first.exists())
        self.assertFalse(second.exists())
        self.assertEqual(organizer.rollback(journal_path, yes=True), 0)
        self.assertEqual(first.read_bytes(), b"duplicate data")
        self.assertEqual(second.read_bytes(), b"duplicate data")

    def test_localize_plan_copies_verified_content_and_rollback_restores_state(self):
        (self.cloud / "folder").mkdir()
        source = self.cloud / "folder" / "unique.txt"
        source.write_bytes(b"unique cloud data")
        report = organizer.reconcile_cloud_local(self.cloud, self.local, self.config)
        plan = organizer.localize_plan_from_reconcile(report, self.config)
        plan_path = self.base / "plan.json"
        journal_path = self.base / "journal.json"
        organizer.json_write(plan_path, plan)

        self.assertEqual(plan["actions"][0]["status"], "ready")
        self.assertEqual(organizer.apply_plan(plan_path, journal_path, yes=False), 0)
        destination = self.local / "folder" / "unique.txt"
        self.assertFalse(destination.exists())

        self.assertEqual(organizer.apply_plan(plan_path, journal_path, yes=True), 0)
        self.assertEqual(destination.read_bytes(), b"unique cloud data")
        self.assertTrue(source.exists(), "localizing must leave the cloud source in place")
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        self.assertEqual(len(journal["copies"]), 1)

        self.assertEqual(organizer.rollback(journal_path, yes=False), 0)
        self.assertTrue(destination.exists())
        self.assertEqual(organizer.rollback(journal_path, yes=True), 0)
        self.assertFalse(destination.exists())
        self.assertTrue(source.exists())

    def test_localize_plan_does_not_overwrite_different_destination(self):
        (self.cloud / "item.txt").write_bytes(b"cloud version")
        (self.local / "item.txt").write_bytes(b"local version")

        report = organizer.reconcile_cloud_local(self.cloud, self.local, self.config)
        plan = organizer.localize_plan_from_reconcile(report, self.config)

        self.assertEqual(plan["actions"][0]["status"], "conflict")
        self.assertEqual((self.local / "item.txt").read_bytes(), b"local version")

    def test_cloud_storage_is_skipped_by_general_scans_but_allowed_for_reconcile(self):
        cloud = self.base / "Library" / "CloudStorage" / "GoogleDrive" / "My Drive"
        cloud.mkdir(parents=True)
        local = self.base / "local-copy"
        local.mkdir()
        (cloud / "file.txt").write_bytes(b"drive data")
        self.config["roots"] = [str(self.base)]

        regular_scan = list(organizer.walk_files([cloud], self.config))
        self.assertEqual(regular_scan, [])

        report = organizer.reconcile_cloud_local(cloud, local, self.config)
        self.assertEqual(report["cloud_files_hashed"], 1)
        self.assertEqual(len(report["cloud_only_content"]), 1)


if __name__ == "__main__":
    unittest.main()
