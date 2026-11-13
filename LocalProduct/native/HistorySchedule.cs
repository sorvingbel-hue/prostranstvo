using System;using System.IO;using System.Security.Principal;using System.Runtime.InteropServices;using System.Web.Script.Serialization;
static class HistorySchedule {
 const string Name="ProstranstvoLocalBetaHistory";static readonly string Target=Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory,"HistoryCollector.exe"));
 static dynamic Service(){dynamic s=Activator.CreateInstance(Type.GetTypeFromProgID("Schedule.Service"));s.Connect();return s;}
 static dynamic Existing(dynamic folder){try{return folder.GetTask(Name);}catch(FileNotFoundException){return null;}catch(COMException e){if(e.ErrorCode==unchecked((int)0x80070002))return null;throw;}}
 static void Check(dynamic task){if(task==null)return;string p=(string)task.Definition.Actions.Item(1).Path;string user=(string)task.Definition.Principal.UserId;var me=WindowsIdentity.GetCurrent();string actualSid=user;try{if(!user.StartsWith("S-1-"))actualSid=((SecurityIdentifier)new NTAccount(user).Translate(typeof(SecurityIdentifier))).Value;}catch{}if(!String.Equals(p.Trim('"'),Target,StringComparison.OrdinalIgnoreCase)||actualSid!=me.User.Value||(int)task.Definition.Principal.RunLevel!=0||(int)task.Definition.Principal.LogonType!=3||(int)task.Definition.Actions.Count!=1||!String.IsNullOrEmpty((string)task.Definition.Actions.Item(1).Arguments))throw new InvalidOperationException("Unexpected task configuration: path="+p+" user="+user+" sid="+actualSid+" level="+(int)task.Definition.Principal.RunLevel);}
 static void Remove(dynamic folder,dynamic task){Check(task);if(task!=null)folder.DeleteTask(Name,0);}
 static string Date(object value){DateTime t=(DateTime)value;return t.Year<2000?null:t.ToUniversalTime().ToString("o");}
 static object Status(dynamic task){if(task==null)return new {installed=false,enabled=false};return new {installed=true,name=Name,enabled=(bool)task.Enabled,state=(int)task.State,last_result=(int)task.LastTaskResult,last_run=Date(task.LastRunTime),next_run=Date(task.NextRunTime),interval=(string)task.Definition.Triggers.Item(1).Repetition.Interval,user=(string)task.Definition.Principal.UserId,run_level=(int)task.Definition.Principal.RunLevel,priority=(int)task.Definition.Settings.Priority};}
 static int Main(string[] args){
  try{
   if(args.Length<1||args.Length>2)throw new ArgumentException("status|enable hours|disable|run|remove");
   string cmd=args[0];if(cmd!="status"&&cmd!="enable"&&cmd!="disable"&&cmd!="run"&&cmd!="remove")throw new ArgumentException("Unknown command");
   if((cmd=="enable")!=(args.Length==2))throw new ArgumentException("Unexpected arguments");
   dynamic service=Service(),folder=service.GetFolder(@"\");dynamic task=Existing(folder);Check(task);
   if(cmd=="enable"){
    int hours;if(!Int32.TryParse(args[1],out hours)||(hours!=1&&hours!=4&&hours!=6&&hours!=12&&hours!=24))throw new ArgumentException("Invalid interval");
    if(!File.Exists(Target))throw new FileNotFoundException("Collector is not installed",Target);
    dynamic definition=service.NewTask(0);string sid=WindowsIdentity.GetCurrent().User.Value;
    definition.RegistrationInfo.Author=WindowsIdentity.GetCurrent().Name;definition.RegistrationInfo.Description="Visible setting in Prostranstvo. Collect local Yandex history into private archive. Normal current-user permissions, no passwords.";
    definition.Principal.UserId=sid;definition.Principal.LogonType=3;definition.Principal.RunLevel=0;
    definition.Settings.Enabled=true;definition.Settings.Hidden=false;definition.Settings.StartWhenAvailable=true;definition.Settings.WakeToRun=false;definition.Settings.MultipleInstances=2;definition.Settings.ExecutionTimeLimit="PT5M";definition.Settings.Priority=6;definition.Settings.DisallowStartIfOnBatteries=false;definition.Settings.StopIfGoingOnBatteries=false;
    dynamic time=definition.Triggers.Create(1);time.StartBoundary=DateTime.UtcNow.AddHours(hours).ToString("yyyy-MM-ddTHH:mm:ss'Z'",System.Globalization.CultureInfo.InvariantCulture);time.Repetition.Interval="PT"+hours+"H";time.Enabled=true;
    dynamic logon=definition.Triggers.Create(9);logon.UserId=sid;logon.Delay="PT30S";logon.Enabled=true;
    dynamic action=definition.Actions.Create(0);action.Path=Target;action.WorkingDirectory=Path.GetDirectoryName(Target);
    task=folder.RegisterTaskDefinition(Name,definition,6,sid,null,3,null);
   }else if(cmd=="disable"){if(task!=null)task.Enabled=false;}
   else if(cmd=="run"){if(task==null)throw new InvalidOperationException("Task is not installed");task.Run(null);}
   else if(cmd=="remove"){Remove(folder,task);task=null;}
   Console.OutputEncoding=new System.Text.UTF8Encoding(false);Console.WriteLine(new JavaScriptSerializer().Serialize(Status(task)));return 0;
  }catch(Exception e){Console.SetError(new StreamWriter(Console.OpenStandardError(),new System.Text.UTF8Encoding(false)){AutoFlush=true});Console.Error.WriteLine(e.GetType().FullName+": "+e.Message);return 1;}
 }
}
