import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import datetime,sqlite3,tempfile,unittest
from pathlib import Path
from browser_history import safe_url,stamp,read_history,export_html,EPOCH,UNIX_OFFSET_US,snapshot
class BrowserHistoryTests(unittest.TestCase):
 def test_youtube_urls_and_secrets_removed(self):
  for url in ('https://www.youtube.com/watch?v=abcdefghijk&si=secret','https://youtu.be/abcdefghijk?t=40','https://youtube.com/shorts/abcdefghijk'):
   row=safe_url(url);self.assertEqual(row['video_id'],'abcdefghijk');self.assertEqual(row['url'],'https://www.youtube.com/watch?v=abcdefghijk')
 def test_fake_host_not_a_video(self):self.assertFalse(safe_url('https://youtube.com.attacker.test/watch?v=abcdefghijk')['video_id'])
 def test_non_web_urls_rejected(self):
  for url in ('javascript:alert(1)','file:///C:/secret','chrome://history'):self.assertIsNone(safe_url(url))
 def test_credentials_query_and_fragment_removed(self):
  row=safe_url('https://user:password@example.com/path?access_token=SECRET#private');self.assertEqual(row['url'],'https://example.com/path');self.assertNotIn('SECRET',str(row))
  self.assertEqual(safe_url('https://example.com/oauth/callback/code?x=1')['url'],'https://example.com/')
 def test_chromium_epoch(self):self.assertEqual(datetime.datetime.fromisoformat(stamp(11644473600000000)).timestamp(),0)
 def make_history(self,profile):
  profile.mkdir(parents=True);c=sqlite3.connect(profile/'History');c.executescript('CREATE TABLE urls(id INTEGER PRIMARY KEY,url TEXT,title TEXT);CREATE TABLE visits(id INTEGER PRIMARY KEY,url INTEGER,visit_time INTEGER);')
  at=int((datetime.datetime.now(datetime.timezone.utc)-EPOCH).total_seconds()*1e6)
  c.execute('INSERT INTO urls VALUES(1,?,?)',('https://www.youtube.com/watch?v=abcdefghijk&token=secret','<script>alert(1)</script>'));c.execute('INSERT INTO visits VALUES(1,1,?)',(at,));c.commit();c.close()
 def test_snapshot_only_reads_source_and_removes_temporary_files(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);profile=root/'profiles'/'Default';self.make_history(profile);original=(profile/'History').read_bytes();result=read_history(7,profile_root=root/'profiles',workspace=root/'work');self.assertEqual(len(result['youtube']),1);self.assertEqual((profile/'History').read_bytes(),original);self.assertEqual(list((root/'work'/'_tmp').iterdir()),[])
 def test_html_escapes_titles_and_never_includes_secret(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);self.make_history(root/'profiles'/'Default');result=read_history(7,profile_root=root/'profiles',workspace=root/'work');p=export_html(result,root/'work');text=p.read_text(encoding='utf-8');self.assertNotIn('<script>',text);self.assertIn('&lt;script&gt;',text);self.assertNotIn('token=secret',text)
 def test_snapshot_exclusive_source_connection(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);profile=root/'profiles'/'Default';self.make_history(profile);c=sqlite3.connect(profile/'History');c.execute('BEGIN EXCLUSIVE');c.execute('UPDATE urls SET title=? WHERE id=1',('uncommitted',))
   try:
    result=read_history(7,profile_root=root/'profiles',workspace=root/'work');self.assertNotEqual(result['rows'][0]['title'],'uncommitted');self.assertEqual(c.execute('SELECT title FROM urls').fetchone()[0],'uncommitted')
   finally:c.rollback();c.close()
 def test_consumer_error_not_retried_as_another_snapshot(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);profile=root/'Default';self.make_history(profile)
   with self.assertRaisesRegex(ValueError,'caller error'):
    with snapshot(profile,root/'work'):raise ValueError('caller error')
 def test_external_master_journal_is_refused(self):
  import struct
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);profile=root/'Default';self.make_history(profile);name=b'C:/outside/master-journal';magic=bytes.fromhex('d9d505f920a163d7');(profile/'History-journal').write_bytes(b'not-a-real-journal'+name+struct.pack('>II',len(name),sum(name))+magic)
   with self.assertRaisesRegex(RuntimeError,'multi-database'):read_history(7,profile_root=root,workspace=root/'work')
 def test_standard_browsers_keep_distinct_archive_keys_and_skip_guest(self):
  with tempfile.TemporaryDirectory() as tmp:
   root = Path(tmp)
   local, roaming, work = root/'Local', root/'Roaming', root/'work'
   yandex = local/'Yandex/YandexBrowser/User Data'
   for profile in (yandex/'Default', local/'Google/Chrome/User Data/Default',
                   local/'Microsoft/Edge/User Data/Profile 1',
                   local/'BraveSoftware/Brave-Browser/User Data/Default',
                   roaming/'Opera Software/Opera Stable',
                   local/'Google/Chrome/User Data/Guest Profile'):
    self.make_history(profile)
   result = read_history(workspace=work, local_root=local, roaming_root=roaming)
   self.assertEqual({row['profile'] for row in result['rows']},
       {'Default', 'Chrome / Default', 'Edge / Profile 1', 'Brave / Default', 'Opera / Основной'})
   self.assertEqual(result['total_visits_in_period'], 5)
   self.assertEqual(result['errors'], [])
   explicit = read_history(profile_root=yandex, workspace=work, local_root=local, roaming_root=roaming)
   self.assertEqual([row['profile'] for row in explicit['rows']], ['Default'])
   html_report = export_html(result, work).read_text(encoding='utf-8')
   self.assertIn('Яндекс / Default', html_report)
   self.assertIn('Chrome / Default', html_report)
 def test_firefox_places_epoch_and_url_scrubbing(self):
  with tempfile.TemporaryDirectory() as tmp:
   root = Path(tmp)
   profile = root/'Roaming/Mozilla/Firefox/Profiles/fixture.default-release'
   profile.mkdir(parents=True)
   database = profile/'places.sqlite'
   at = int(datetime.datetime.now(datetime.timezone.utc).timestamp()*1e6)
   with sqlite3.connect(database) as connection:
    connection.executescript('CREATE TABLE moz_places(id INTEGER PRIMARY KEY,url TEXT,title TEXT);'
                            'CREATE TABLE moz_historyvisits(id INTEGER PRIMARY KEY,place_id INTEGER,visit_date INTEGER);')
    connection.execute('INSERT INTO moz_places VALUES(1,?,?)',
                       ('https://example.test/oauth/callback/secret?token=secret','private title'))
    connection.execute('INSERT INTO moz_historyvisits VALUES(1,1,?)',(at,))
    connection.execute('INSERT INTO moz_historyvisits VALUES(2,1,?)',(at-2*86400*1000000,))
   connection.close()
   original = database.read_bytes()
   result = read_history(1, workspace=root/'work', local_root=root/'Local', roaming_root=root/'Roaming')
   self.assertEqual(result['total_visits_in_period'], 1)
   row = result['rows'][0]
   self.assertEqual(row['profile'], 'Firefox / fixture.default-release')
   self.assertEqual(row['raw_time'], at+UNIX_OFFSET_US)
   self.assertEqual(row['url'], 'https://example.test/')
   self.assertNotIn('private title', row['title'])
   self.assertEqual(database.read_bytes(), original)
   self.assertEqual(list((root/'work/_tmp').iterdir()), [])
 def test_unreadable_browser_does_not_erase_other_results(self):
  with tempfile.TemporaryDirectory() as tmp:
   root = Path(tmp)
   local = root/'Local'
   self.make_history(local/'Google/Chrome/User Data/Default')
   edge = local/'Microsoft/Edge/User Data/Default'
   edge.mkdir(parents=True)
   (edge/'History').write_bytes(b'invalid database')
   result = read_history(workspace=root/'work', local_root=local, roaming_root=root/'Roaming')
   self.assertEqual([row['profile'] for row in result['rows']], ['Chrome / Default'])
   self.assertTrue(any(error.startswith('Edge / Default:') for error in result['errors']))
if __name__=='__main__':unittest.main()
