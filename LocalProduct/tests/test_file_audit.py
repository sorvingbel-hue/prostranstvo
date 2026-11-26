import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
"""Audit observations use synthetic XML. No Security log access is performed."""
from datetime import datetime, timezone
import unittest
from unittest import mock
from xml.sax.saxutils import escape

import file_audit


def record(path, record_id=10, mask='0x10001', object_type='File'):
    stamp = datetime.now(timezone.utc).isoformat()
    return f'''<Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event">
    <System><Provider Name="Microsoft-Windows-Security-Auditing"/><EventID>4663</EventID>
    <EventRecordID>{record_id}</EventRecordID><Channel>Security</Channel>
    <TimeCreated SystemTime="{stamp}"/></System><EventData>
    <Data Name="ObjectServer">Security</Data><Data Name="ObjectType">{object_type}</Data>
    <Data Name="ObjectName">{escape(path)}</Data><Data Name="AccessMask">{mask}</Data>
    <Data Name="SubjectUserSid">S-1-5-21-11-22-33-1001</Data>
    <Data Name="SubjectUserName">fixture</Data><Data Name="ProcessName">C:/Tools/view.exe</Data>
    </EventData></Event>'''


class FileAuditTests(unittest.TestCase):
    def read(self, records, **kwargs):
        with (mock.patch.object(file_audit.os, 'name', 'nt'),
              mock.patch.object(file_audit, '_security_records', return_value=(item for item in records))):
            return file_audit.read_file_audit([r'D:\School'], **kwargs)

    def test_literal_folder_scope_and_event_deduplication(self):
        allowed = record(r'd:\SCHOOL\notes.txt')
        result = self.read([allowed, allowed, record(r'D:\SchoolOther\private.txt', 11),
                            record(r'D:\School\..\Other\private.txt', 12),
                            record(r'D:\School\state', 13, object_type='Key')])
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['scanned'], 5)
        self.assertEqual(len(result['events']), 1)
        event = result['events'][0]
        self.assertEqual(event['rights'], ['read_data_or_list_directory', 'delete_right_used'])
        self.assertEqual(event['process'], 'view.exe')
        self.assertEqual(event['subject_sid'], 'S-1-5-21-11-22-33-1001')
        self.assertFalse(result['coverage_verified'])

    def test_scope_is_required_and_query_limits_are_reported(self):
        for roots in ([], ['D:\\'], [r'\\server\share'], [r'D:\School\..\Other']):
            with self.subTest(roots=roots), self.assertRaises(ValueError):
                file_audit.read_file_audit(roots)
        result = self.read([record(r'D:\School\a'), record(r'D:\School\b', 12)], max_events=1)
        self.assertEqual((result['status'], result['scanned'], result['truncated']), ('partial', 1, True))

    def test_empty_corrupt_and_permission_cases_stay_distinct(self):
        self.assertEqual(self.read([])['status'], 'no_events')
        self.assertEqual(self.read(['<broken', None])['unreadable'], 2)
        error = OSError('private underlying error')
        error.winerror = 5
        with (mock.patch.object(file_audit.os, 'name', 'nt'),
              mock.patch.object(file_audit, '_security_records', side_effect=error)):
            result = file_audit.read_file_audit([r'D:\School'])
        self.assertEqual(result['status'], 'access_denied')
        self.assertNotIn('private', str(result))


if __name__ == '__main__':
    unittest.main()
