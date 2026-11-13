"""Read existing Windows Security 4663 records for selected local folders."""
from contextlib import closing
import ctypes
from datetime import datetime, timedelta, timezone
import ntpath
import os
import re
import time
import xml.etree.ElementTree as ET


SOURCE = 'Windows Security 4663'
_NS = {'e': 'http://schemas.microsoft.com/win/2004/08/events/event'}
_PROVIDER = 'Microsoft-Windows-Security-Auditing'
_DRIVE_PATH = re.compile(r'^[A-Za-z]:\\[^<>:"|?*\x00-\x1f]+$')
_SID = re.compile(r'S-\d+(?:-\d+){1,15}')
_RIGHTS = {
    0x1: 'read_data_or_list_directory',
    0x2: 'write_data_or_add_file',
    0x4: 'append_data_or_add_subdirectory',
    0x8: 'read_extended_attributes',
    0x10: 'write_extended_attributes',
    0x20: 'execute_or_traverse',
    0x40: 'delete_child',
    0x80: 'read_attributes',
    0x100: 'write_attributes',
    0x10000: 'delete_right_used',
    0x20000: 'read_permissions',
    0x40000: 'change_permissions',
    0x80000: 'change_owner',
}


def _local_path(value):
    if not isinstance(value, str) or len(value) > 4096:
        return None
    value = value.replace('/', '\\').rstrip('\\')
    if not _DRIVE_PATH.fullmatch(value):
        return None
    parts = value[3:].split('\\')
    if any(not part or part.endswith((' ', '.')) for part in parts):
        return None
    return value


def _parse_record(xml, roots, earliest, latest):
    if '<!' in xml:
        raise ValueError('unexpected_xml_declaration')
    event = ET.fromstring(xml)
    system = event.find('e:System', _NS)
    if system is None or system.findtext('e:EventID', namespaces=_NS) != '4663':
        return None
    provider = system.find('e:Provider', _NS)
    if (provider is None or provider.get('Name') != _PROVIDER
            or system.findtext('e:Channel', namespaces=_NS) != 'Security'):
        return None
    fields = {item.get('Name'): item.text or '' for item in event.findall('e:EventData/e:Data', _NS)}
    if fields.get('ObjectType') != 'File' or fields.get('ObjectServer') != 'Security':
        return None
    path = _local_path(fields.get('ObjectName'))
    if path is None:
        return None
    key = ntpath.normcase(path)
    if not any(key == root or key.startswith(root + '\\') for root in roots):
        return None
    created = system.find('e:TimeCreated', _NS)
    if created is None:
        raise ValueError('event_time_missing')
    stamp = datetime.fromisoformat(created.get('SystemTime', '').replace('Z', '+00:00'))
    if stamp.tzinfo is None or not earliest <= stamp <= latest:
        return None
    record_id = int(system.findtext('e:EventRecordID', '0', _NS))
    mask = int(fields.get('AccessMask', '0'), 16)
    if record_id <= 0 or not 0 < mask <= 0xffffffff:
        raise ValueError('invalid_record_or_mask')
    sid = fields.get('SubjectUserSid', '')
    return {
        'record_id': record_id, 'time_utc': stamp.astimezone(timezone.utc).isoformat(), 'path': path,
        'subject_sid': sid if len(sid) <= 184 and _SID.fullmatch(sid) else None,
        'account_name': fields.get('SubjectUserName', '')[:256],
        'account_domain': fields.get('SubjectDomainName', '')[:256],
        'process': ntpath.basename(fields.get('ProcessName', ''))[:256],
        'access_mask': mask, 'rights': [name for bit, name in _RIGHTS.items() if mask & bit],
    }


def _security_records(hours):
    from ctypes import wintypes

    api = ctypes.WinDLL('wevtapi.dll', use_last_error=True)
    handle, dword, pointer = wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER
    api.EvtQuery.argtypes = [handle, wintypes.LPCWSTR, wintypes.LPCWSTR, dword]
    api.EvtQuery.restype = handle
    api.EvtNext.argtypes = [handle, dword, pointer(handle), dword, dword, pointer(dword)]
    api.EvtNext.restype = wintypes.BOOL
    api.EvtRender.argtypes = [handle, handle, dword, dword, wintypes.LPVOID, pointer(dword), pointer(dword)]
    api.EvtRender.restype = wintypes.BOOL
    api.EvtClose.argtypes = [handle]
    api.EvtClose.restype = wintypes.BOOL

    query = f'*[System[EventID=4663 and TimeCreated[timediff(@SystemTime)<={hours * 3600000}]]]'
    # Local Security channel only, newest entries first. No tolerance of query errors.
    results = api.EvtQuery(None, 'Security', query, 0x201)
    if not results:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        events = (handle * 1)()
        returned, used, properties = dword(), dword(), dword()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if not api.EvtNext(results, 1, events, 1000, 0, ctypes.byref(returned)):
                error = ctypes.get_last_error()
                if error == 259:  # ERROR_NO_MORE_ITEMS
                    return
                raise ctypes.WinError(error)
            if returned.value != 1:
                return
            event = events[0]
            try:
                if api.EvtRender(None, event, 1, ctypes.sizeof(buffer), buffer,
                                 ctypes.byref(used), ctypes.byref(properties)):
                    xml = buffer.value
                else:
                    error = ctypes.get_last_error()
                    if error != 122:  # ERROR_INSUFFICIENT_BUFFER
                        raise ctypes.WinError(error)
                    xml = None
            finally:
                api.EvtClose(event)
            yield xml
        raise TimeoutError('query_time_limit')
    finally:
        api.EvtClose(results)


def read_file_audit(roots, *, hours=24, max_events=1000):
    """Return bounded observations, including reason when the source is unavailable.

    4663 records account and access rights used on audited objects. A read/list
    right does not show what a person viewed. Delete rights do not show that
    removal completed or prove a move. No entries do not establish coverage.
    """
    if not isinstance(roots, (list, tuple)) or not 1 <= len(roots) <= 32:
        raise ValueError('explicit_folders_required')
    allowed = []
    for root in roots:
        path = _local_path(os.fspath(root))
        if path is None:
            raise ValueError('invalid_local_folder')
        allowed.append(ntpath.normcase(path))
    if type(hours) is not int or not 1 <= hours <= 168:
        raise ValueError('invalid_audit_period')
    if type(max_events) is not int or not 1 <= max_events <= 2000:
        raise ValueError('invalid_event_limit')
    latest = datetime.now(timezone.utc)
    earliest = latest - timedelta(hours=hours)
    report = {
        'source': SOURCE, 'status': 'no_events', 'reason': 'no_scoped_records',
        'start_utc': earliest.isoformat(), 'end_utc': latest.isoformat(), 'events': [],
        'scanned': 0, 'unreadable': 0, 'truncated': False, 'coverage_verified': False,
    }
    if os.name != 'nt':
        return {**report, 'status': 'unavailable', 'reason': 'windows_required'}
    seen = set()
    try:
        with closing(_security_records(hours)) as records:
            for xml in records:
                report['scanned'] += 1
                try:
                    record = _parse_record(xml, allowed, earliest, latest) if xml is not None else None
                except (ET.ParseError, ValueError):
                    record, xml = None, None
                if xml is None:
                    report['unreadable'] += 1
                if record and record['record_id'] not in seen:
                    seen.add(record['record_id'])
                    report['events'].append(record)
                if report['scanned'] >= max_events:
                    report['truncated'] = True
                    break
    except OSError as error:
        code = getattr(error, 'winerror', None)
        if code in (5, 1314):
            return {**report, 'status': 'access_denied', 'reason': 'security_log_permission_required'}
        if code == 1460 or isinstance(error, TimeoutError) or report['scanned']:
            return {**report, 'status': 'partial', 'reason': 'security_log_read_interrupted', 'truncated': True}
        return {**report, 'status': 'unavailable', 'reason': 'security_log_unavailable'}
    if report['truncated'] or report['unreadable']:
        report.update(status='partial', reason='event_limit_or_unreadable_records')
    elif report['events']:
        report.update(status='ok', reason='scoped_records_found')
    return report
