"""Fixed, explicit family-message bridge. No chat scraping or arbitrary recipient.

Construction/import is inert. Calls use the current user's existing transport
configuration. Receipts never mean that a notification was displayed or read.
"""
from family_telegram import TelegramError


class FamilyBridgeError(TelegramError):
    """Only application-selected static codes cross the tool boundary."""


class FamilyBridge:
    def __init__(self, store=None, settings=None, ensure_worker=None):
        self._store = store
        self._settings = settings
        self._ensure_worker = ensure_worker

    def _get_settings(self):
        if self._settings is None:
            from family_telegram import TelegramSettings
            self._settings = TelegramSettings(self._store.root if self._store is not None else None)
        return self._settings

    def _get_store(self):
        if self._store is None:
            from family_messages import MessageStore
            self._store = MessageStore(self._get_settings().root)
        return self._store

    @staticmethod
    def _channel(settings):
        try:
            state = settings.status()
            configured = state.get('configured') is True
            paired = state.get('paired') is True and state.get('has_binding') is True
            enabled = state.get('enabled') is True
        except Exception:
            raise FamilyBridgeError('channel_unavailable') from None
        label = 'not_configured' if not configured else 'not_paired' if not paired else 'paused' if not enabled else 'ready'
        return {'transport': 'telegram', 'configured': configured, 'paired': paired,
                'enabled': enabled, 'channel_ready': label == 'ready', 'state': label}

    def channel_status(self):
        try:
            return self._channel(self._get_settings())
        except FamilyBridgeError:
            raise
        except Exception:
            raise FamilyBridgeError('channel_unavailable') from None

    @staticmethod
    def _receipt(row):
        state = row.get('status')
        if state not in ('queued', 'sending', 'sent', 'uncertain', 'failed') or row.get('direction') != 'outgoing':
            raise FamilyBridgeError('receipt_unavailable')
        return {'id': row['id'], 'status': state, 'transport': 'telegram',
                'accepted_by_telegram': state == 'sent', 'delivery_confirmed': False,
                'read_confirmed': False}

    def message_status(self, message_id):
        from family_messages import MessageStore, _check_path, _opaque
        from pathlib import Path
        import sqlite3
        try:
            _opaque(message_id, 'сообщение')
        except (ValueError, TypeError):
            raise FamilyBridgeError('invalid_message_id') from None
        try:
            root = Path(self._store.root if self._store is not None else self._get_settings().root).absolute()
            database = root / 'messages.sqlite'
            for name in ('messages.sqlite', 'messages.sqlite-journal', 'messages.sqlite-wal', 'messages.sqlite-shm'):
                _check_path(root / name)
            if not database.is_file():
                raise FamilyBridgeError('receipt_unavailable')
            # A status query must never initialize storage or select private text.
            conn = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=5)
            try:
                conn.row_factory = sqlite3.Row
                conn.execute('PRAGMA trusted_schema=OFF')
                MessageStore._validate(conn)
                value = conn.execute('SELECT id,direction,status FROM messages WHERE id=?', (message_id,)).fetchone()
                if value is None:
                    raise FamilyBridgeError('receipt_unavailable')
                row = dict(value)
            finally:
                conn.close()
            if row.get('direction') != 'outgoing':
                raise FamilyBridgeError('receipt_not_outgoing')
            return self._receipt(row)
        except FamilyBridgeError:
            raise
        except Exception:
            raise FamilyBridgeError('receipt_unavailable') from None

    def send_message(self, text, request_id):
        from family_messages import _text, _opaque
        try:
            _text(text)
        except (ValueError, TypeError):
            raise FamilyBridgeError('invalid_message') from None
        try:
            _opaque(request_id, 'повтор отправки')
        except (ValueError, TypeError):
            raise FamilyBridgeError('invalid_request_id') from None
        try:
            settings = self._get_settings()
            # Avoid creating an empty message DB for an unconfigured channel.
            channel = self._channel(settings)
            if not channel['channel_ready']:
                raise FamilyBridgeError({'not_configured': 'not_configured', 'not_paired': 'not_paired',
                                         'paused': 'channel_paused'}[channel['state']])
            store = self._get_store()
            # Rechecks state under the settings mutex; a concurrent pause cannot
            # slip a new message into the queue after it has taken effect.
            row = settings.queue_reply(store, text, request_id)
        except FamilyBridgeError:
            raise
        except TelegramError as error:
            code = error.code if error.code in ('not_configured', 'not_paired', 'channel_paused', 'binding_changed') else 'channel_unavailable'
            raise FamilyBridgeError(code) from None
        except (ValueError, TypeError):
            raise FamilyBridgeError('queue_rejected') from None
        except Exception:
            raise FamilyBridgeError('queue_unavailable') from None
        worker = 'not_needed'
        if row['status'] in ('queued', 'sending'):
            try:
                if self._ensure_worker is None:
                    from family_agent import ensure_running
                    ensure_running(store)
                else:
                    self._ensure_worker(store)
                worker = 'ready'
            except Exception:
                worker = 'unavailable'
        try:
            result = self.message_status(row['id'])
            result['status_refresh_unavailable'] = False
        except FamilyBridgeError:
            # The insertion already committed. Preserve its acknowledgement and
            # stable ID even if the optional fresh status read is unavailable.
            result = self._receipt(row)
            result['status_refresh_unavailable'] = True
        result['worker_state'] = worker
        return result
