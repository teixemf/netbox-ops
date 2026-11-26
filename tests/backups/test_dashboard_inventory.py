"""The dashboard inventory is read-only and never reads archive contents."""
import datetime as dt
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from netbox_ops.backups import MAX_MANIFEST_BYTES, BackupError, BackupManager
from netbox_ops.config import Configuration, DEFAULT_UNITS


class DashboardInventoryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.store = self.root / 'backups'
        self.store.mkdir()
        self.manager = BackupManager(Configuration(DEFAULT_UNITS.copy(), self.store))
        self.usage = SimpleNamespace(used=4096, free=4096, total=8192)
        self.disk = self.enterContext(patch('netbox_ops.backups.shutil.disk_usage',
                                           return_value=self.usage))
        self.archive = self.enterContext(patch('netbox_ops.backups._read_archive_metadata',
                                              side_effect=AssertionError('no archive reads')))
        self.sidecar = self.enterContext(patch('netbox_ops.backups._read_sidecar_metadata',
                                              side_effect=AssertionError('no unbounded sidecar reads')))
        self.enterContext(patch.object(Path, 'read_text', side_effect=AssertionError('no unbounded manifest reads')))

    def add(self, name, mtime=0, size=4):
        path = self.store / name
        path.write_bytes(b'x' * size)
        os.utime(path, (mtime, mtime))
        return path

    def test_latest_uses_chronology_not_kind_prefix_or_filesystem_mtime(self):
        first = self.add('netbox-media-pre-restore-20261125T223000Z-abcdef.media.tar.gz')
        self.add('netbox-db-20261124T223000Z-abcdef.dump', mtime=1800000000)
        self.add('netbox-combined-20261125T213000Z-abcdef.combined.tar.gz')
        self.add('netbox-full-20261124T120000Z-abcdef.full.tar.gz')
        self.add('netbox-files-20261123T120000Z-abcdef.files.tar.gz')
        self.add(first.name + '.json', size=9)  # deliberately invalid metadata
        self.add('ignored.txt')
        (self.store / 'netbox-db-20261126T230000Z-abcdef.dump').symlink_to(first)
        with patch.object(self.manager, 'list_backups', wraps=self.manager.list_backups) as listing:
            disk, latest = self.manager.dashboard_storage()
        listing.assert_called_once_with()
        self.disk.assert_called_once_with(self.store)
        self.assertEqual(disk, (29, 4096 - 29, 4096, 8192, 5))
        self.assertEqual(latest.path, first)
        self.assertEqual(latest.timestamp, dt.datetime(2026, 11, 25, 22, 30, tzinfo=dt.timezone.utc))
        self.assertEqual((latest.kind, latest.source, latest.purpose), ('media', 'filename', 'pre_restore'))
        self.archive.assert_not_called()
        self.sidecar.assert_not_called()

    def test_invalid_filename_date_uses_modification_time_and_unknown_tag(self):
        old = self.add('netbox-db-20261124T120000Z-abcdef.dump')
        stamp = dt.datetime(2026, 11, 26, 1, 20, tzinfo=dt.timezone.utc)
        latest_path = self.add('import-20261340T999999Z.dump', mtime=stamp.timestamp())
        _, latest = self.manager.dashboard_storage()
        self.assertEqual(latest.path, latest_path)
        self.assertEqual(latest.timestamp, stamp)
        self.assertEqual((latest.kind, latest.source, latest.purpose), ('db', 'mtime', 'unknown'))
        latest_path.unlink()
        _, latest = self.manager.dashboard_storage()
        self.assertEqual(latest.path, old)
        self.assertEqual(latest.purpose, 'manual')

    def test_historical_full_and_legacy_types_are_preserved(self):
        full = self.add('netbox-full-pre-restore-20261126T000000Z-abcdef.full.tar.gz')
        legacy = self.add('netbox-files-20261125T000000Z-abcdef.files.tar.gz')
        _, latest = self.manager.dashboard_storage()
        self.assertEqual((latest.path, latest.kind, latest.purpose), (full, 'full', 'pre_restore'))
        full.unlink()
        _, latest = self.manager.dashboard_storage()
        self.assertEqual((latest.path, latest.kind, latest.purpose), (legacy, 'legacy', 'manual'))

    def test_empty_missing_and_non_directory(self):
        self.assertEqual(self.manager.dashboard_storage(), ((0, 4096, 4096, 8192, 0), None))
        self.store.rmdir()
        self.assertEqual(self.manager.dashboard_storage(), ((0, 4096, 4096, 8192, 0), None))
        self.disk.assert_called_with(self.root)
        self.store.write_text('not a directory')
        with self.assertRaises(BackupError):
            self.manager.dashboard_storage()

    def test_listing_failure_or_disappearing_file_is_not_a_stale_snapshot(self):
        path = self.add('netbox-db-20261126T000000Z-abcdef.dump')
        with patch.object(self.manager, 'list_backups', side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError):
                self.manager.dashboard_storage()
        path.unlink()
        with patch.object(self.manager, 'list_backups', return_value=[path]):
            with self.assertRaises(FileNotFoundError):
                self.manager.dashboard_storage()
        self.assertIsNone(self.manager.dashboard_storage()[1])

    def test_only_latest_sidecar_is_read_and_reliable_metadata_preferred(self):
        first = self.add('netbox-db-20261125T120000Z-abcdef.dump')
        older = self.add('netbox-media-20261124T120000Z-abcdef.media.tar.gz')
        for path, kind, date in ((first, 'db', '2026-11-25T14:22:00+01:00'),
                                 (older, 'media', '2026-11-26T15:00:00Z')):
            Path(str(path) + '.json').write_bytes(json.dumps({
                'tool': 'netbox-ops', 'format': 1, 'kind': kind,
                'created_utc': date, 'purpose': 'pre_restore',
            }).encode())
        with patch.object(self.manager, '_dashboard_metadata',
                          wraps=self.manager._dashboard_metadata) as metadata:
            _, latest = self.manager.dashboard_storage()
        metadata.assert_called_once_with(first, 'db')
        self.assertEqual(latest.path, first)
        self.assertEqual(latest.timestamp, dt.datetime(2026, 11, 25, 13, 22, tzinfo=dt.timezone.utc))
        self.assertEqual((latest.source, latest.purpose), ('metadata', 'pre_restore'))

    def test_malformed_or_unsafe_metadata_falls_back_without_archive_read(self):
        path = self.add('netbox-db-20261125T120000Z-abcdef.dump')
        sidecar = Path(str(path) + '.json')
        for invalid in (
            b'not json', b'[' * 5000, b'x' * (MAX_MANIFEST_BYTES + 1),
            json.dumps({'tool': 'other', 'format': 1, 'kind': 'db',
                        'created_utc': '2026-11-26T00:00:00Z'}).encode(),
            json.dumps({'tool': 'netbox-ops', 'format': 1, 'kind': 'db',
                        'created_utc': '2026-11-26T00:00:00', 'purpose': 'other'}).encode(),
        ):
            with self.subTest(length=len(invalid)):
                sidecar.write_bytes(invalid)
                _, latest = self.manager.dashboard_storage()
                self.assertEqual((latest.source, latest.purpose), ('filename', 'manual'))
                self.assertEqual(latest.timestamp, dt.datetime(2026, 11, 25, 12, tzinfo=dt.timezone.utc))
        sidecar.unlink()
        sidecar.symlink_to(self.add('external.json'))
        self.assertEqual(self.manager.dashboard_storage()[1].source, 'filename')
        sidecar.unlink()
        os.mkfifo(sidecar)
        self.assertEqual(self.manager.dashboard_storage()[1].source, 'filename')
        self.archive.assert_not_called()

    def test_archive_replaced_while_reading_metadata_is_rejected(self):
        path = self.add('netbox-db-20261125T120000Z-abcdef.dump')
        def change(*args):
            path.write_bytes(b'changed')
            return {}
        with patch.object(self.manager, '_dashboard_metadata', side_effect=change):
            with self.assertRaisesRegex(BackupError, 'changed during refresh'):
                self.manager.dashboard_storage()

    def test_import_with_similar_prefix_but_no_managed_date_has_unknown_tag(self):
        path = self.add('netbox-db-imported.dump')
        _, latest = self.manager.dashboard_storage()
        self.assertEqual((latest.path, latest.purpose, latest.source), (path, 'unknown', 'mtime'))
