import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import datetime,tempfile,unittest
from pathlib import Path
from unittest import mock
from history_schedule import HistoryArchive,epoch_cutoff
def row(ident="1",at=None):
 at=at or datetime.datetime.now(datetime.timezone.utc)
 from browser_history import EPOCH
 return dict(profile="Default",visit_id=ident,raw_time=int((at-EPOCH).total_seconds()*1e6),time=at.isoformat(),url="https://example.com/page",title="Example",site="example.com",video_id="")
class ScheduleTests(unittest.TestCase):
 def setUp(self):self.temp=tempfile.TemporaryDirectory();self.a=HistoryArchive(Path(self.temp.name)/"history")
 def tearDown(self):self.temp.cleanup()
 def reader(self,rows):return lambda **kwargs:{"rows":rows,"errors":[],"truncated":False}
 def test_default_disabled_never_reads_browser(self):
  reader=mock.Mock();self.assertEqual(self.a.collect(reader=reader),{"skipped":"disabled"});reader.assert_not_called()
 def test_duplicate_visits_are_merged(self):
  r=row();self.assertEqual(self.a.ingest([r]),(1,1));self.assertEqual(self.a.ingest([r]),(0,1))
 def test_reused_id_with_new_timestamp_is_a_new_visit(self):
  a=row();b=row(at=datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(seconds=10));self.assertEqual(self.a.ingest([a,b]),(2,2))
 def test_prior_archive_survives_empty_browser_result(self):
  self.a.collect(force=True,reader=self.reader([row()]));self.a.collect(force=True,reader=self.reader([]));self.assertEqual(self.a.report()["returned"],1)
 def test_scheduled_runs_do_not_repeat_within_two_minutes(self):
  self.a.set_config(enabled=True);self.a.collect(reader=self.reader([row()]));r=mock.Mock();self.assertEqual(self.a.collect(reader=r),{"skipped":"recent"});r.assert_not_called()
 def test_manual_collection_when_auto_disabled(self):
  self.a.collect(force=True,reader=self.reader([row()]));self.assertFalse(self.a.config()["enabled"]);self.assertEqual(self.a.report()["returned"],1)
 def test_reader_failure_does_not_claim_success(self):
  with self.assertRaisesRegex(RuntimeError,"bad source"):self.a.collect(force=True,reader=mock.Mock(side_effect=RuntimeError("bad source")))
  self.assertEqual(self.a.state()["phase"],"failed");self.assertFalse(self.a.state().get("last_success"))
 def test_truncated_read_is_saved_as_partial(self):
  state=self.a.collect(force=True,reader=lambda **kwargs:{"rows":[row()],"truncated":True,"errors":[]})
  self.assertEqual(state["phase"],"partial")
  self.assertFalse(state.get("last_success"))
  self.assertEqual(self.a.report()["returned"],1)
 def test_retention_and_outside_source_untouched(self):
  old=row(at=datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(days=20));new=row(ident="2");self.assertEqual(self.a.ingest([old,new]),(1,1));self.assertEqual(old["title"],"Example")
 def test_invalid_config_is_not_written(self):
  for value in ({"enabled":"yes"},{"interval_hours":3},{"retention_days":0}):
   with self.assertRaises(ValueError):self.a.set_config(**value)
  self.assertEqual(self.a.config()["interval_hours"],4)
 def test_failed_task_registration_rolls_back_config(self):
  with mock.patch("history_schedule.helper",side_effect=RuntimeError("denied")):
   with self.assertRaises(RuntimeError):self.a.configure_task(True,4)
  self.assertFalse(self.a.config()["enabled"])
if __name__=="__main__":unittest.main()
