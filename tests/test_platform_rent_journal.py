"""Historical rent journal immutability; local files only."""

import os
from pathlib import Path
import tempfile
import unittest

from studio_platform.rent_journal import RentJournal

TAG = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
POD = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
OTHER = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
EXECUTOR = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"


class RentJournalTests(unittest.TestCase):
    def test_terminal_marker_cannot_be_rewritten_to_no_charge(self):
        with tempfile.TemporaryDirectory() as root:
            journal = RentJournal(Path(root) / "rents")
            journal.save(TAG, "checking")
            journal.save(TAG, "post_started", executor_id=EXECUTOR)
            with self.assertRaisesRegex(ValueError, "transition_refused"):
                journal.save(TAG, "not_submitted")
            journal.save(TAG, "confirmed", executor_id=EXECUTOR, instance_id=POD)
            with self.assertRaisesRegex(ValueError, "transition_refused"):
                journal.save(TAG, "checking")

    def test_invalid_fields_and_executor_change_cannot_corrupt_marker(self):
        with tempfile.TemporaryDirectory() as root:
            journal = RentJournal(Path(root) / "rents")
            journal.save(TAG, "checking")
            with self.assertRaises(ValueError):
                journal.save(TAG, "not_submitted", instance_id=POD)
            journal.save(TAG, "post_started", executor_id=EXECUTOR)
            with self.assertRaisesRegex(ValueError, "executor_conflict"):
                journal.save(TAG, "confirmed", executor_id=OTHER, instance_id=POD)
            self.assertEqual(journal.read(TAG)["phase"], "post_started")

    @unittest.skipIf(os.name == "nt", "POSIX private-mode and link semantics")
    def test_insecure_or_linked_marker_refused(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "rents"
            journal = RentJournal(directory)
            journal.save(TAG, "checking")
            path = directory / (TAG + ".json")
            path.chmod(0o644)
            with self.assertRaisesRegex(ValueError, "not_private"):
                journal.read(TAG)
            path.chmod(0o600)
            link = Path(root) / "linked"
            link.symlink_to(directory, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "linked_directory"):
                RentJournal(link).read(TAG)
