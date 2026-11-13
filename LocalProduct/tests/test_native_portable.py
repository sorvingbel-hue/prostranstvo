"""Compile native beta helpers and exercise only synthetic Core/task fixtures."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


PRODUCT = Path(__file__).resolve().parents[1]
NATIVE = PRODUCT / 'native'
CSC = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'Microsoft.NET' / 'Framework64' / 'v4.0.30319' / 'csc.exe'

CORE_SOURCE = r'''
using System;using System.IO;
static class FixtureCore {
 static int Main(string[] args){
  string record=Environment.GetEnvironmentVariable("PROSTRANSTVO_TEST_RECORD");
  if(!String.IsNullOrEmpty(record))File.WriteAllLines(record,args);
  Console.WriteLine("FAMILY_REPLY_READY");
  return Int32.Parse(Environment.GetEnvironmentVariable("PROSTRANSTVO_TEST_EXIT")??"0");
 }
}
'''

KITTEN_ARGUMENT_HARNESS = r'''
using System;using System.Reflection;
static class KittenArgumentHarness {
 static int Main(string[] args) {
  try {
   var method=typeof(ModeLauncher).GetMethod("Arguments",BindingFlags.Static|BindingFlags.NonPublic);
   Console.WriteLine((string)method.Invoke(null,new object[]{args}));return 0;
  } catch(TargetInvocationException error) {
   if(error.InnerException is ArgumentException)return 23;
   throw;
  }
 }
}
'''

PASHA_HARNESS = r'''
using System;using System.IO;
static class NativePortableHarness {
 static int Main(string[] args){
  if(args[0]=="root"){
   try{Console.WriteLine(PashaMessage.FamilyReply.AppRoot());return 0;}
   catch(FileNotFoundException){return 17;}
  }
  if(args[0]=="reply"){
   PashaMessage.FamilyReply.MessageId="fixture_123";
   using(var child=PashaMessage.FamilyReply.Start()){
    if(child.StandardOutput.ReadLine()!="FAMILY_REPLY_READY")return 21;
    child.WaitForExit();return child.ExitCode;
   }
  }
  if(args[0]=="gate"){
   var gate=new PashaMessage.TwoOkGate(700);var at=DateTime.UtcNow;
   if(gate.Click(at)||gate.Count!=1)return 22;
   if(gate.Click(at.AddMilliseconds(50))||gate.Count!=1)return 23;
   if(!gate.Click(at.AddMilliseconds(800))||gate.Count!=2)return 24;
   return 0;
  }
  if(args[0]=="log"){
   PashaMessage.Log.Write("NATIVE_FIXTURE_EVENT");
   Console.WriteLine(PashaMessage.Log.PathName);return 0;
  }
  return 25;
 }
}
'''

HISTORY_HARNESS = r'''
using System;using System.IO;using System.Reflection;using System.Security.Principal;
public sealed class FixtureAction {public string Path{get;set;} public string Arguments{get;set;}}
public sealed class FixtureActions {
 public int Count{get;set;} public FixtureAction Action{get;set;}
 public FixtureAction Item(int index){if(index!=1)throw new Exception("Index");return Action;}
}
public sealed class FixturePrincipal {public string UserId{get;set;}public int RunLevel{get;set;}public int LogonType{get;set;}}
public sealed class FixtureDefinition {public FixtureActions Actions{get;set;}public FixturePrincipal Principal{get;set;}}
public sealed class FixtureTask {public FixtureDefinition Definition{get;set;}}
public sealed class FixtureFolder {
 public int Removed;public string Name;
 public void DeleteTask(string name,int flags){if(flags!=0)throw new Exception("Flags");Removed++;Name=name;}
}
static class NativePortableHarness {
 static object Call(string name,params object[] args){return typeof(HistorySchedule).GetMethod(name,BindingFlags.Static|BindingFlags.NonPublic).Invoke(null,args);}
 static FixtureTask Task(){return new FixtureTask{Definition=new FixtureDefinition{
  Actions=new FixtureActions{Count=1,Action=new FixtureAction{Path=System.IO.Path.Combine(AppDomain.CurrentDomain.BaseDirectory,"HistoryCollector.exe"),Arguments=""}},
  Principal=new FixturePrincipal{UserId=WindowsIdentity.GetCurrent().User.Value,RunLevel=0,LogonType=3}}};}
 static void Rejected(FixtureTask task){try{Call("Check",task);}catch(TargetInvocationException e){if(e.InnerException is InvalidOperationException)return;throw;}throw new Exception("Unsafe task accepted");}
 static int Main(){
  Call("Check",Task());
  var task=Task();task.Definition.Actions.Action.Path+=".other";Rejected(task);
  task=Task();task.Definition.Principal.UserId+="-999";Rejected(task);
  task=Task();task.Definition.Principal.RunLevel=1;Rejected(task);
  task=Task();task.Definition.Principal.LogonType=5;Rejected(task);
  task=Task();task.Definition.Actions.Count=2;Rejected(task);
  task=Task();task.Definition.Actions.Action.Arguments="--other";Rejected(task);
  var folder=new FixtureFolder();Call("Remove",folder,Task());
  if(folder.Removed!=1||folder.Name!="ProstranstvoLocalBetaHistory")throw new Exception("Wrong removed task");
  task=Task();task.Definition.Principal.UserId+="-999";
  try{Call("Remove",folder,task);throw new Exception("Wrong owner removed");}
  catch(TargetInvocationException e){if(!(e.InnerException is InvalidOperationException))throw;}
  if(folder.Removed!=1)throw new Exception("Deletion happened before ownership check");
  Call("Remove",folder,null);if(folder.Removed!=1)throw new Exception("Missing task deletion");
  Console.WriteLine("SYNTHETIC_TASK_CHECKS_OK");return 0;
 }
}
'''


@unittest.skipUnless(os.name == 'nt' and CSC.is_file(), 'Windows .NET Framework compiler required')
class NativePortableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = PRODUCT / 'build' / 'test-native-temporary'
        temporary.mkdir(parents=True, exist_ok=True)
        cls.temp = tempfile.TemporaryDirectory(prefix='native-', dir=temporary)
        cls.root = Path(cls.temp.name)
        cls.addClassCleanup(cls.temp.cleanup)
        cls.core_source = cls.root / 'FixtureCore.cs'
        cls.core_source.write_text(CORE_SOURCE, encoding='utf-8')
        cls.fixture_core = cls.root / 'FixtureCore.exe'
        cls.compile(cls.fixture_core, [cls.core_source])
        cls.launchers = {}
        for mode in ('UI', 'MESSAGES', 'FAMILY', 'ACTIVITY', 'HISTORY', 'KITTEN'):
            folder = cls.root / mode
            folder.mkdir()
            output = folder / 'Launcher.exe'
            cls.compile(output, [NATIVE / 'ModeLauncher.cs'], define=mode, winexe=True)
            shutil.copyfile(cls.fixture_core, folder / 'ProstranstvoCore.exe')
            cls.launchers[mode] = output
        cls.kitten_arguments = cls.root / 'KittenArguments.exe'
        kitten_harness = cls.root / 'KittenArgumentHarness.cs'
        kitten_harness.write_text(KITTEN_ARGUMENT_HARNESS, encoding='utf-8')
        cls.compile(cls.kitten_arguments, [NATIVE / 'ModeLauncher.cs', kitten_harness], define='KITTEN', main='KittenArgumentHarness')
        cls.pasha = cls.root / 'PashaHarness.exe'
        pasha_source = cls.root / 'PashaHarness.cs'
        pasha_source.write_text(PASHA_HARNESS, encoding='utf-8')
        cls.compile(cls.pasha, [NATIVE / 'PashaMessage.cs', pasha_source], main='NativePortableHarness')
        cls.history = cls.root / 'HistoryHarness.exe'
        history_source = cls.root / 'HistoryHarness.cs'
        history_source.write_text(HISTORY_HARNESS, encoding='utf-8')
        cls.compile(cls.history, [NATIVE / 'HistorySchedule.cs', history_source], main='NativePortableHarness')
        # These native entry points are compiled but never run: no real UI or scheduler API.
        cls.compile(cls.root / 'PashaMessage.exe', [NATIVE / 'PashaMessage.cs'], winexe=True)
        cls.compile(cls.root / 'HistorySchedule.exe', [NATIVE / 'HistorySchedule.cs'])

    @classmethod
    def compile(cls, output, sources, define=None, main=None, winexe=False):
        command = [str(CSC), '/nologo', '/target:' + ('winexe' if winexe else 'exe'), '/out:' + str(output), '/reference:System.Windows.Forms.dll', '/reference:System.Drawing.dll', '/reference:Microsoft.CSharp.dll', '/reference:System.Web.Extensions.dll']
        if define:
            command.append('/define:' + define)
        if main:
            command.append('/main:' + main)
        command.extend(map(str, sources))
        result = subprocess.run(command, capture_output=True, text=True, timeout=40, creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)

    def invoke(self, path, *args, exit_code=0):
        record = self.root / 'fixture-args.txt'
        if record.exists():
            record.unlink()
        environment = {**os.environ, 'PROSTRANSTVO_TEST_RECORD': str(record), 'PROSTRANSTVO_TEST_EXIT': str(exit_code), 'LOCALAPPDATA': str(self.root / 'local-profile')}
        result = subprocess.run([str(path), *args], capture_output=True, text=True, timeout=15, env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
        recorded = record.read_text(encoding='utf-8-sig').splitlines() if record.exists() else None
        return result, recorded

    def test_mode_dispatch_allowlists(self):
        cases = [
            ('UI', (), ['ui']),
            ('UI', ('--activity',), ['ui', '--activity']),
            ('UI', ('--messages',), ['ui', '--messages']),
            ('UI', ('--study-resume',), ['study-resume']),
            ('MESSAGES', (), ['messages']),
            ('MESSAGES', ('--setup',), ['messages', '--setup']),
            ('FAMILY', (), ['family-agent']),
            ('ACTIVITY', (), ['activity-agent', '--background']),
            ('HISTORY', (), ['history-collect']),
            ('KITTEN', (), ['pet']),
            ('KITTEN', ('--chat',), ['kitten']),
        ]
        for mode, arguments, expected in cases:
            with self.subTest(mode=mode, arguments=arguments):
                result, recorded = self.invoke(self.launchers[mode], *arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(recorded, expected)

    def test_background_worker_rejects_arbitrary_command_without_dispatch(self):
        # UI/MESSAGES reject too, but their error path intentionally displays a dialog.
        for mode in ('FAMILY', 'ACTIVITY', 'HISTORY'):
            result, recorded = self.invoke(self.launchers[mode], '--arbitrary-shell')
            self.assertEqual(result.returncode, 1)
            self.assertIsNone(recorded)

    def test_kitten_launcher_rejects_arbitrary_arguments_without_starting_ui(self):
        for arguments in (('--arbitrary-shell',), ('--mini',), ('--chat', 'extra'), ('--setup',), ('kitten',)):
            with self.subTest(arguments=arguments):
                result, recorded = self.invoke(self.kitten_arguments, *arguments)
                self.assertEqual(result.returncode, 23)
                self.assertIsNone(recorded)

    def test_background_worker_handles_missing_core_without_modal(self):
        folder = self.root / 'missing-core'
        folder.mkdir(exist_ok=True)
        launcher = folder / 'Worker.exe'
        shutil.copyfile(self.launchers['FAMILY'], launcher)
        result, recorded = self.invoke(launcher)
        self.assertEqual(result.returncode, 1)
        self.assertIsNone(recorded)

    def test_history_waits_and_propagates_core_failure(self):
        result, recorded = self.invoke(self.launchers['HISTORY'], exit_code=7)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(recorded, ['history-collect'])

    def test_reply_uses_sibling_core_and_fixed_reply_arguments(self):
        target = self.root / 'ProstranstvoCore.exe'
        shutil.copyfile(self.fixture_core, target)
        try:
            result, recorded = self.invoke(self.pasha, 'reply')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(recorded, ['reply', '--ready-stdout', '--reply-to', 'fixture_123'])
        finally:
            target.unlink()
        result, recorded = self.invoke(self.pasha, 'root')
        self.assertEqual(result.returncode, 17)
        self.assertIsNone(recorded)

    def test_two_ok_gate_still_requires_separate_second_click(self):
        result, _ = self.invoke(self.pasha, 'gate')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_message_log_is_private_local_data_and_contains_only_metadata(self):
        result, _ = self.invoke(self.pasha, 'log')
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = self.root / 'local-profile' / 'ProstranstvoLocalBeta' / 'data' / 'logs' / 'message.log'
        self.assertEqual(Path(result.stdout.strip()), expected)
        self.assertIn('NATIVE_FIXTURE_EVENT', expected.read_text(encoding='utf-8-sig'))
        self.assertFalse((self.root / 'message.log').exists())
        source = (NATIVE / 'PashaMessage.cs').read_text(encoding='utf-8-sig')
        self.assertNotIn('Log.Write("ERROR " + e.ToString())', source)
        self.assertNotIn('Log.Write("SPEECH_ERROR " + e.ToString())', source)

    def test_scheduler_ownership_path_and_removal_checks_with_fake_tasks(self):
        result, _ = self.invoke(self.history)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('SYNTHETIC_TASK_CHECKS_OK', result.stdout)

    def test_native_sources_have_no_legacy_machine_dependencies(self):
        for filename in ('PashaMessage.cs', 'HistorySchedule.cs', 'ModeLauncher.cs'):
            source = (NATIVE / filename).read_text(encoding='utf-8-sig')
            for legacy in ('python-path.txt', str(Path.home()), 'D:\\Prostranstvo-PC', 'ProstranstvoBrowserHistory'):
                with self.subTest(filename=filename, legacy=legacy):
                    self.assertNotIn(legacy, source)


if __name__ == '__main__':
    unittest.main()
