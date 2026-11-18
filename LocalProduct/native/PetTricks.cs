using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Text;
using System.Text.RegularExpressions;
using System.Web.Script.Serialization;

namespace ProstranstvoPet {
 sealed class TrickStep {public string Action;public int Duration;}
 sealed class TrickRule {public string Id,Name,Event;public bool Enabled;public int Cooldown,Duration;public TrickStep[] Steps;}
 sealed class TrickSet {public bool Enabled;public TrickRule[] Rules;}
 sealed class TrickPose {
  public bool Active,Heart,Purr,Flip,Finished;public int Frame=-1;public string Action="wait";
 }
 // Blockly is only an editor for this bounded declarative format. Neither
 // generated JavaScript nor Python, paths, commands or expressions are run.
 static class TrickCodec {
  public const int MaxBytes=32768;
  public static readonly Regex Id=new Regex("\\A[0-9a-f]{32}\\z",RegexOptions.CultureInvariant);
  static readonly HashSet<string> Actions=new HashSet<string>(new[]{"turn","heart","blink","purr","wait","stretch"},StringComparer.Ordinal);
  static JavaScriptSerializer Serializer(){return new JavaScriptSerializer{MaxJsonLength=MaxBytes,RecursionLimit=16};}
  public static void Keys(Dictionary<string,object> value,params string[] keys) {
   if(value==null||value.Count!=keys.Length)throw new InvalidDataException();foreach(string key in keys)if(!value.ContainsKey(key))throw new InvalidDataException();
  }
  sealed class Scope {public bool Object,Key;public HashSet<string> Keys=new HashSet<string>(StringComparer.Ordinal);}
  static void UniqueKeys(string text) {
   Stack<Scope> scopes=new Stack<Scope>();
   for(int i=0;i<text.Length;i++) {
    char c=text[i];
    if(c=='"') {
     int start=i;bool end=false;
     while(++i<text.Length){if(text[i]=='\\'){i++;continue;}if(text[i]=='"'){end=true;break;}}
     if(!end)throw new InvalidDataException();
     if(scopes.Count>0&&scopes.Peek().Object&&scopes.Peek().Key) {
      string key=Serializer().Deserialize<string>(text.Substring(start,i-start+1));
      if(!scopes.Peek().Keys.Add(key))throw new InvalidDataException();scopes.Peek().Key=false;
     }
    } else if(c=='{'||c=='[') {if(scopes.Count>=12)throw new InvalidDataException();scopes.Push(new Scope{Object=c=='{',Key=c=='{'});}
    else if(c=='}'||c==']'){if(scopes.Count==0||scopes.Peek().Object!=(c=='}'))throw new InvalidDataException();scopes.Pop();}
    else if(c==','&&scopes.Count>0&&scopes.Peek().Object)scopes.Peek().Key=true;
   }
   if(scopes.Count!=0)throw new InvalidDataException();
  }
  public static Dictionary<string,object> Object(string text) {
   if(text==null||new UTF8Encoding(false,true).GetByteCount(text)>MaxBytes)throw new InvalidDataException();
   UniqueKeys(text);Dictionary<string,object> value=Serializer().DeserializeObject(text) as Dictionary<string,object>;
   if(value==null)throw new InvalidDataException();return value;
  }
  public static int Integer(object value,int minimum,int maximum) {if(!(value is int)||(int)value<minimum||(int)value>maximum)throw new InvalidDataException();return (int)value;}
  public static bool Boolean(object value){if(!(value is bool))throw new InvalidDataException();return (bool)value;}
  public static long Epoch(object value) {if(!(value is int)&&!(value is long))throw new InvalidDataException();long number=Convert.ToInt64(value,CultureInfo.InvariantCulture);if(number<=0)throw new InvalidDataException();return number;}
  public static string Identifier(object value){string id=value as string;if(id==null||!Id.IsMatch(id))throw new InvalidDataException();return id;}
  static string Name(object value) {
   string text=value as string;if(String.IsNullOrWhiteSpace(text)||text!=text.Trim())throw new InvalidDataException();
   new UTF8Encoding(false,true).GetByteCount(text);int count=0;
   for(int i=0;i<text.Length;i++){if(Char.IsControl(text[i]))throw new InvalidDataException();if(Char.IsHighSurrogate(text[i]))i++;count++;}
   if(count>60)throw new InvalidDataException();return text;
  }
  public static TrickSet Parse(string text) {
   Dictionary<string,object> value=Object(text);Keys(value,"version","enabled","rules");Integer(value["version"],1,1);
   object[] rules=value["rules"] as object[];if(rules==null||rules.Length>8)throw new InvalidDataException();
   TrickSet result=new TrickSet{Enabled=Boolean(value["enabled"]),Rules=new TrickRule[rules.Length]};HashSet<string> ids=new HashSet<string>(StringComparer.Ordinal);
   for(int i=0;i<rules.Length;i++) {
    Dictionary<string,object> row=rules[i] as Dictionary<string,object>;Keys(row,"id","name","enabled","event","cooldown_seconds","steps");
    TrickRule rule=new TrickRule{Id=Identifier(row["id"]),Name=Name(row["name"]),Enabled=Boolean(row["enabled"]),Event=row["event"] as string,Cooldown=Integer(row["cooldown_seconds"],10,3600)};
    if(!ids.Add(rule.Id)||(rule.Event!="greet"&&rule.Event!="petted"&&rule.Event!="manual"))throw new InvalidDataException();
    object[] steps=row["steps"] as object[];if(steps==null||steps.Length<1||steps.Length>8)throw new InvalidDataException();rule.Steps=new TrickStep[steps.Length];int purr=0;
    for(int n=0;n<steps.Length;n++) {
     Dictionary<string,object> step=steps[n] as Dictionary<string,object>;Keys(step,"action","duration_ms");string action=step["action"] as string;
     if(action==null||!Actions.Contains(action))throw new InvalidDataException();int duration=Integer(step["duration_ms"],100,5000);
     if(action=="purr"){if(duration>2000)throw new InvalidDataException();purr+=duration;}
     rule.Duration+=duration;rule.Steps[n]=new TrickStep{Action=action,Duration=duration};
    }
    if(rule.Duration>30000||purr>3000)throw new InvalidDataException();result.Rules[i]=rule;
   }
   return result;
  }
  public static string Json(object value){return Serializer().Serialize(value);}
 }

 // A monotonic pure state machine. It cannot access audio, windows or files.
 sealed class TrickRunner {
  TrickSet config=new TrickSet{Enabled=false,Rules=new TrickRule[0]};TrickRule active;
  readonly Dictionary<string,double> cooldowns=new Dictionary<string,double>(StringComparer.Ordinal);
  double start,nextGlobal,last=-1;
  public bool Active {get{return active!=null;}} public string ActiveId {get{return active==null?"":active.Id;}}
  public void Replace(TrickSet value) {
   Cancel();config=value;HashSet<string> keep=new HashSet<string>(StringComparer.Ordinal);foreach(TrickRule rule in config.Rules)keep.Add(rule.Id);
   foreach(string id in new List<string>(cooldowns.Keys))if(!keep.Contains(id))cooldowns.Remove(id);
  }
  public void Cancel(){active=null;}
  static bool GoodTime(double now){return !Double.IsNaN(now)&&!Double.IsInfinity(now)&&now>=0;}
  public bool Start(string id,double now,bool preview,out string reason) {
   reason="rule_missing";TrickRule selected=null;foreach(TrickRule rule in config.Rules)if(rule.Id==id){selected=rule;break;}
   if(selected==null)return false;if(!GoodTime(now)||(last>=0&&now<last)){Cancel();reason="clock_invalid";return false;}last=now;
   if(Active){reason="busy";return false;}if(!preview&&(!config.Enabled||!selected.Enabled)){reason="disabled";return false;}
   double next; if(now<nextGlobal||(!preview&&cooldowns.TryGetValue(id,out next)&&now<next)){reason="cooldown";return false;}
   active=selected;start=now;nextGlobal=now+10;if(!preview)cooldowns[id]=now+selected.Cooldown;reason="started";return true;
  }
  public bool Trigger(string name,double now) {
   if(!config.Enabled||Active)return false;
   foreach(TrickRule rule in config.Rules)if(rule.Enabled&&rule.Event==name){string reason;if(Start(rule.Id,now,false,out reason))return true;}
   return false;
  }
  public TrickPose Advance(double now,bool blocked) {
   if(!GoodTime(now)||(last>=0&&now<last)||blocked){Cancel();return new TrickPose();}last=now;
   if(active==null)return new TrickPose();double elapsed=(now-start)*1000;
   if(elapsed>=active.Duration){active=null;return new TrickPose{Finished=true};}
   int cursor=0;bool flip=false;
   foreach(TrickStep step in active.Steps) {
    if(step.Action=="turn")flip=!flip;
    if(elapsed<cursor+step.Duration) {
     double progress=Math.Max(0,Math.Min(1,(elapsed-cursor)/step.Duration));
     return new TrickPose{Active=true,Action=step.Action,Flip=flip,Heart=step.Action=="heart",Purr=step.Action=="purr",
      Frame=step.Action=="blink"?(progress<.25||progress>=.75?5:6):step.Action=="stretch"?11:step.Action=="purr"?6:step.Action=="wait"||step.Action=="turn"?4:-1};
    }
    cursor+=step.Duration;
   }
   Cancel();return new TrickPose();
  }
 }

 static class TrickFiles {
  public static long UtcNow(){return (long)(DateTime.UtcNow-new DateTime(1970,1,1,0,0,0,DateTimeKind.Utc)).TotalSeconds;}
  public static string DataRoot() {
#if PERSONAL
   return Path.GetFullPath(@"D:\Prostranstvo-PC\data\cat-companion\tricks");
#else
   return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"ProstranstvoLocalBeta","data","cat-companion","tricks");
#endif
  }
  public static string StudyPath(string root){return Path.GetFullPath(Path.Combine(root,"..","..","study","active-session.json"));}
  public static void NoLinks(string path) {
   for(string item=Path.GetFullPath(path);!String.IsNullOrEmpty(item);) {
    if(File.Exists(item)||Directory.Exists(item))if((File.GetAttributes(item)&FileAttributes.ReparsePoint)!=0)throw new IOException();
    DirectoryInfo parent=Directory.GetParent(item);item=parent==null?null:parent.FullName;
   }
  }
  public static string Read(string path,int maximum) {
   NoLinks(path);
   try {
    using(FileStream input=new FileStream(path,FileMode.Open,FileAccess.Read,FileShare.ReadWrite|FileShare.Delete)) {
     if(input.Length>maximum)throw new InvalidDataException();byte[] bytes=new byte[maximum+1];int count=0,read;
     while(count<bytes.Length&&(read=input.Read(bytes,count,bytes.Length-count))>0)count+=read;
     if(count>maximum)throw new InvalidDataException();NoLinks(path);string result=new UTF8Encoding(false,true).GetString(bytes,0,count);return result.TrimStart('\ufeff');
    }
   }catch(FileNotFoundException){return null;}catch(DirectoryNotFoundException){return null;}
  }
  public static void Write(string path,object value) {
   string root=Path.GetDirectoryName(path);NoLinks(root);Directory.CreateDirectory(root);NoLinks(path);
   string temporary=Path.Combine(root,".trick-"+Guid.NewGuid().ToString("N")+".tmp");
   try {
    byte[] bytes=new UTF8Encoding(false,true).GetBytes(TrickCodec.Json(value));
    using(FileStream output=new FileStream(temporary,FileMode.CreateNew,FileAccess.Write,FileShare.None)){output.Write(bytes,0,bytes.Length);output.Flush(true);}
    NoLinks(path);if(File.Exists(path))File.Replace(temporary,path,null);else File.Move(temporary,path);
   }finally{try{if(File.Exists(temporary))File.Delete(temporary);}catch{}}
  }
 }

 sealed class TrickStudyGate {
  string id,source;double deadline,last=-1;
  public bool Blocked(string path,double mono,long utc) {
   try {
    if(Double.IsNaN(mono)||Double.IsInfinity(mono)||mono<0||utc<=0||(last>=0&&mono<last))throw new InvalidDataException();last=mono;
    string body=TrickFiles.Read(path,2048);if(body==null){id=source=null;return false;}
    Dictionary<string,object> value=TrickCodec.Object(body);TrickCodec.Keys(value,"version","session_id","expires_utc","duration_seconds");TrickCodec.Integer(value["version"],1,1);
    string nextId=TrickCodec.Identifier(value["session_id"]);int duration=TrickCodec.Integer(value["duration_seconds"],0,14400);object raw=value["expires_utc"];
    double remaining;
    if(duration==0&&raw==null) {
     remaining=Double.PositiveInfinity;
    } else {
     if(duration<60||(!(raw is int)&&!(raw is long)&&!(raw is double)&&!(raw is decimal)))throw new InvalidDataException();
     double expiry=Convert.ToDouble(raw,CultureInfo.InvariantCulture);
     if(Double.IsNaN(expiry)||Double.IsInfinity(expiry)||expiry<=0)throw new InvalidDataException();
     remaining=Math.Min(duration,Math.Max(0,expiry-utc));
    }
    string canonical=TrickCodec.Json(value);
    if(id!=nextId){id=nextId;source=canonical;deadline=mono+remaining;}else if(source!=canonical)throw new InvalidDataException();
    deadline=Math.Min(deadline,mono+remaining);return deadline>mono;
   }catch{return true;}
  }
 }

 // Own fixed local files only. Construction is inert; Poll is called at most
 // every two seconds by Advance. Errors disable tricks, never the desktop pet.
 sealed class TricksHost {
  readonly string root,studyPath;readonly TrickRunner runner=new TrickRunner();readonly TrickStudyGate study=new TrickStudyGate();
  string configText,lastRequest,activeRequest,pendingRequest,pendingState,pendingReason;long pendingUtc;double nextPoll;bool initialized,studyBlocked;
  public bool ConfigOk {get;private set;} public TrickPose Pose {get;private set;}
  public TricksHost(string directory=null,string lease=null){root=Path.GetFullPath(directory??TrickFiles.DataRoot());studyPath=lease??TrickFiles.StudyPath(root);Pose=new TrickPose();}
  void RetryAck() {
   if(pendingRequest==null)return;if(pendingRequest!=lastRequest){pendingRequest=null;return;}
   try{TrickFiles.Write(Path.Combine(root,"preview-status.json"),new{version=1,request_id=pendingRequest,state=pendingState,reason=pendingReason,updated_utc=pendingUtc});pendingRequest=null;}catch{}
  }
  void Ack(string request,string state,string reason,long utc) {if(request!=lastRequest)return;pendingRequest=request;pendingState=state;pendingReason=reason;pendingUtc=utc;RetryAck();}
  void Recover(long utc) {
   try {
    string text=TrickFiles.Read(Path.Combine(root,"preview-status.json"),4096);if(text==null)return;
    Dictionary<string,object> value=TrickCodec.Object(text);TrickCodec.Keys(value,"version","request_id","state","reason","updated_utc");TrickCodec.Integer(value["version"],1,1);TrickCodec.Epoch(value["updated_utc"]);
    string state=value["state"] as string;if(state!="started"&&state!="completed"&&state!="blocked"&&state!="stopped"&&state!="failed")return;
    lastRequest=TrickCodec.Identifier(value["request_id"]);if(state=="started")Ack(lastRequest,"failed","interrupted",utc);
   }catch{}
  }
  void CancelPreview(string state,string reason,long utc) {
   runner.Cancel();Pose=new TrickPose();string request=activeRequest;activeRequest=null;if(request!=null)Ack(request,state,reason,utc);
  }
  public void Stop(long utc){CancelPreview("stopped","requested",utc);}
  void Poll(double now,long utc,bool blocked) {
   if(!initialized){Recover(utc);initialized=true;}
   try {
    string text=TrickFiles.Read(Path.Combine(root,"rules.json"),TrickCodec.MaxBytes);
    if(text!=configText||!ConfigOk){TrickSet next=text==null?new TrickSet{Enabled=false,Rules=new TrickRule[0]}:TrickCodec.Parse(text);CancelPreview("stopped","config_changed",utc);runner.Replace(next);configText=text;}ConfigOk=true;
   }catch{ConfigOk=false;configText=null;CancelPreview("failed","config_invalid",utc);}
   studyBlocked=study.Blocked(studyPath,now,utc);
   try {
    string text=TrickFiles.Read(Path.Combine(root,"command.json"),4096);
    if(text!=null) {
     Dictionary<string,object> value=TrickCodec.Object(text);TrickCodec.Keys(value,"version","request_id","action","rule_id","expires_utc");TrickCodec.Integer(value["version"],1,1);
     string request=TrickCodec.Identifier(value["request_id"]),action=value["action"] as string;long expires=TrickCodec.Epoch(value["expires_utc"]);
     string rule=value["rule_id"] as string;if(action=="run")TrickCodec.Identifier(rule);else if(action!="stop"||rule!="")throw new InvalidDataException();
     if(request!=lastRequest) {
      lastRequest=request;
      if(expires>utc&&expires<=utc+30) {
       if(action=="stop"){runner.Cancel();Pose=new TrickPose();activeRequest=null;Ack(request,"stopped","requested",utc);}
       else if(!ConfigOk)Ack(request,"failed","config_invalid",utc);
       else if(blocked||studyBlocked)Ack(request,"blocked","context_blocked",utc);
       else {string reason;if(runner.Start(rule,now,true,out reason)){activeRequest=request;Ack(request,"started","started",utc);}else Ack(request,"blocked",reason,utc);}
      }
     }
    }
   }catch{}
   RetryAck();
   try{TrickFiles.Write(Path.Combine(root,"status.json"),new{version=1,pid=Process.GetCurrentProcess().Id,updated_utc=utc,config_ok=ConfigOk,active_rule_id=runner.ActiveId});}catch{}
  }
  public TrickPose Advance(double now,long utc,bool blocked) {
   if(Double.IsNaN(now)||Double.IsInfinity(now)||now<0){CancelPreview("failed","clock_invalid",utc);return Pose;}
   if(now>=nextPoll){nextPoll=now+2;Poll(now,utc,blocked);}
   bool gated=blocked||studyBlocked||!ConfigOk;
   if(gated&&runner.Active){CancelPreview("blocked","context_blocked",utc);return Pose;}
   Pose=runner.Advance(now,gated);
   if(Pose.Finished&&activeRequest!=null){string request=activeRequest;activeRequest=null;Ack(request,"completed","completed",utc);}
   return Pose;
  }
  public bool Trigger(string name,double now,bool blocked) {return ConfigOk&&!blocked&&!studyBlocked&&runner.Trigger(name,now);}
 }

 static class PetTrickTests {
  static int checks;static void Check(bool value){checks++;if(!value)throw new Exception("pet_trick_self_test_failed");}
  static string ID(char c){return new String(c,32);}
  static string Rules(bool enabled,bool ruleEnabled,int cooldown,string action,int duration) {
   return TrickCodec.Json(new{version=1,enabled=enabled,rules=new[]{new{id=ID('a'),name="Тестовое умение",enabled=ruleEnabled,@event="petted",cooldown_seconds=cooldown,steps=new[]{new{action=action,duration_ms=duration}}}}});
  }
  static void Refuse(string text){bool refused=false;try{TrickCodec.Parse(text);}catch{refused=true;}Check(refused);}
  static void Command(string root,char request,string action,long expiry){TrickFiles.Write(Path.Combine(root,"command.json"),new{version=1,request_id=ID(request),action=action,rule_id=action=="stop"?"":ID('a'),expires_utc=expiry});}
  static Dictionary<string,object> Ack(string root){return TrickCodec.Object(TrickFiles.Read(Path.Combine(root,"preview-status.json"),4096));}
  public static int Run() {
   checks=0;string single=Rules(true,true,3600,"heart",100);TrickSet parsed=TrickCodec.Parse(single);Check(parsed.Rules.Length==1&&parsed.Rules[0].Duration==100);
   foreach(string text in new[]{single.Replace("\"version\":1","\"version\":true"),single.Replace("\"version\":1","\"version\":1,\"version\":1"),single.Replace("\"version\":1","\"version\":1,\"\\u0076ersion\":1"),single.Replace("\"heart\"","\"run_program\""),single.Replace("\"duration_ms\":100","\"duration_ms\":100.0"),single.Replace("\"cooldown_seconds\":3600","\"cooldown_seconds\":9"),single.Replace("\"rules\":","\"unknown\":1,\"rules\":"),new String(' ',TrickCodec.MaxBytes+1)+"{}"})Refuse(text);
   Refuse(Rules(true,true,10,"purr",2001));Refuse(Rules(true,true,10,"wait",99));Refuse(Rules(true,true,10,"wait",5001));
   Refuse(single.Replace("Тестовое умение"," Тестовое умение"));Refuse(single.Replace("Тестовое умение","Тестовое умение "));
   StringBuilder scalarName=new StringBuilder();for(int i=0;i<60;i++)scalarName.Append(Char.ConvertFromUtf32(0x1f43e));
   Check(TrickCodec.Parse(single.Replace("Тестовое умение",scalarName.ToString())).Rules.Length==1);
   Dictionary<string,object> changed=TrickCodec.Object(single);object[] rows=(object[])changed["rules"];
   changed["rules"]=new[]{rows[0],rows[0]};Refuse(TrickCodec.Json(changed));changed["rules"]=rows;
   Dictionary<string,object> row=(Dictionary<string,object>)rows[0];object[] steps=(object[])row["steps"];
   row["steps"]=new[]{steps[0],steps[0],steps[0],steps[0],steps[0],steps[0],steps[0],steps[0],steps[0]};Refuse(TrickCodec.Json(changed));
   row["steps"]=new[]{new{action="wait",duration_ms=5000},new{action="wait",duration_ms=5000},new{action="wait",duration_ms=5000},new{action="wait",duration_ms=5000},new{action="wait",duration_ms=5000},new{action="wait",duration_ms=5000},new{action="wait",duration_ms=100}};Refuse(TrickCodec.Json(changed));
   row["steps"]=new[]{new{action="purr",duration_ms=2000},new{action="purr",duration_ms=1100}};Refuse(TrickCodec.Json(changed));
   TrickRunner runner=new TrickRunner();runner.Replace(parsed);string reason;
   Check(runner.Start(ID('a'),0,false,out reason));Check(runner.Advance(.2,false).Finished&&!runner.Active);
   Check(!runner.Start(ID('a'),5,true,out reason)&&reason=="cooldown");
   Check(runner.Start(ID('a'),11,true,out reason));runner.Advance(12,false);
   Check(!runner.Start(ID('a'),22,false,out reason)&&reason=="cooldown");Check(runner.Start(ID('a'),22,true,out reason));runner.Cancel();
   runner.Replace(TrickCodec.Parse(Rules(false,false,10,"purr",100)));Check(!runner.Trigger("petted",33));Check(runner.Start(ID('a'),33,true,out reason));
   Check(runner.Advance(33.01,false).Purr);Check(!runner.Advance(33.02,true).Active&&!runner.Active);Check(!runner.Advance(100,false).Purr);
   Check(runner.Start(ID('a'),101,true,out reason));Check(!runner.Advance(100,false).Active);Check(!runner.Advance(Double.NaN,false).Active);
   string sequence=TrickCodec.Json(new{version=1,enabled=true,rules=new[]{new{id=ID('a'),name="Поворот и моргание",enabled=true,@event="greet",cooldown_seconds=10,steps=new[]{new{action="turn",duration_ms=100},new{action="heart",duration_ms=200},new{action="blink",duration_ms=200},new{action="purr",duration_ms=100}}}}});
   TrickRunner scripted=new TrickRunner();scripted.Replace(TrickCodec.Parse(sequence));Check(scripted.Trigger("greet",0));
   Check(scripted.Advance(.05,false).Flip);Check(scripted.Advance(.15,false).Heart);Check(scripted.Advance(.35,false).Frame==6);Check(scripted.Advance(.55,false).Purr);
   Check(!scripted.Trigger("greet",.56));Check(scripted.Advance(.7,false).Finished&&!scripted.Advance(20,false).Active);
   string directory=Path.Combine(Path.GetTempPath(),"ProstranstvoPetTricksTest-"+Guid.NewGuid().ToString("N"));Directory.CreateDirectory(directory);
   try {
    string lease=Path.Combine(directory,"study.json");long utc=1000000000;
    TrickStudyGate indefinite=new TrickStudyGate();
    TrickFiles.Write(lease,new{version=1,session_id=ID('a'),expires_utc=(object)null,duration_seconds=0});
    Check(indefinite.Blocked(lease,0,utc));Check(indefinite.Blocked(lease,86400,utc+86400));
    File.Delete(lease);Check(!indefinite.Blocked(lease,86401,utc+86401));
    // The first editor save may immediately queue a preview between two polls.
    // Missing rules are valid and inert; each poll reloads rules before command.
    TricksHost firstStart=new TricksHost(directory,lease);firstStart.Advance(0,utc,false);
    Check(firstStart.ConfigOk&&!firstStart.Pose.Active&&!File.Exists(Path.Combine(directory,"rules.json")));
    Check((bool)TrickCodec.Object(TrickFiles.Read(Path.Combine(directory,"status.json"),4096))["config_ok"]);
    File.WriteAllText(Path.Combine(directory,"rules.json"),Rules(false,false,3600,"wait",600),new UTF8Encoding(false));Command(directory,'0',"run",utc+20);
    Check(firstStart.Advance(2,utc+2,false).Active);Check((string)Ack(directory)["request_id"]==ID('0')&&(string)Ack(directory)["state"]=="started");
    firstStart.Advance(3,utc+3,false);Check((string)Ack(directory)["state"]=="completed");
    File.WriteAllText(Path.Combine(directory,"keep.txt"),"unrelated fixture");TricksHost host=new TricksHost(directory,lease);
    host.Advance(0,utc,false);Check(host.ConfigOk&&!host.Pose.Active);Dictionary<string,object> status=TrickCodec.Object(TrickFiles.Read(Path.Combine(directory,"status.json"),4096));Check((bool)status["config_ok"]&&(string)status["active_rule_id"]=="");
    Command(directory,'b',"run",utc+20);Check(host.Advance(2,utc+2,false).Active);Check((string)Ack(directory)["state"]=="started");
    using(FileStream locked=new FileStream(Path.Combine(directory,"preview-status.json"),FileMode.Open,FileAccess.Read,FileShare.Read)) {
     host.Advance(2.7,utc+3,false);Check((string)Ack(directory)["state"]=="started"&&!host.Pose.Active);
    }
    host.Advance(4,utc+4,false);Check((string)Ack(directory)["state"]=="completed");host.Advance(12,utc+12,false);Check(!host.Pose.Active&&(string)Ack(directory)["state"]=="completed");
    TricksHost restarted=new TricksHost(directory,lease);Check(!restarted.Advance(0,utc+13,false).Active);Check((string)Ack(directory)["request_id"]==ID('b'));
    Command(directory,'c',"run",utc+35);host.Advance(14,utc+14,false);Check((string)Ack(directory)["state"]=="started");Command(directory,'d',"stop",utc+36);host.Advance(16,utc+16,false);host.Advance(30,utc+30,false);
    Check((string)Ack(directory)["request_id"]==ID('d')&&(string)Ack(directory)["state"]=="stopped"&&!host.Pose.Active);
    File.WriteAllText(Path.Combine(directory,"rules.json"),Rules(false,false,3600,"wait",5000),new UTF8Encoding(false));host.Advance(32,utc+32,false);
    Command(directory,'e',"run",utc+54);host.Advance(34,utc+34,false);Check(host.Pose.Active);
    Command(directory,'f',"run",utc+56);host.Advance(36,utc+36,false);Check((string)Ack(directory)["request_id"]==ID('f')&&(string)Ack(directory)["reason"]=="busy");host.Advance(40,utc+40,false);Check((string)Ack(directory)["request_id"]==ID('f'));
    TrickFiles.Write(lease,new{version=1,session_id=ID('e'),expires_utc=utc+600,duration_seconds=600});Command(directory,'1',"run",utc+70);host.Advance(50,utc+50,false);Check((string)Ack(directory)["state"]=="blocked"&&!host.Pose.Active);
    File.WriteAllText(lease,"invalid fixture");Command(directory,'2',"run",utc+72);host.Advance(52,utc+52,false);Check((string)Ack(directory)["reason"]=="context_blocked");File.Delete(lease);
    Command(directory,'3',"run",utc+90);host.Advance(70,utc+70,false);Check(host.Pose.Active&&(string)Ack(directory)["state"]=="started");
    TricksHost interrupted=new TricksHost(directory,lease);Check(!interrupted.Advance(0,utc+71,false).Active);Check((string)Ack(directory)["reason"]=="interrupted");
    Command(directory,'4',"run",utc+70);interrupted.Advance(2,utc+72,false);Check(!interrupted.Pose.Active&&(string)Ack(directory)["request_id"]==ID('3'));
    Command(directory,'5',"run",utc+200);interrupted.Advance(4,utc+74,false);Check(!interrupted.Pose.Active);
    Command(directory,'6',"run",utc+76);interrupted.Advance(6,utc+76,false);Check(!interrupted.Pose.Active&&(string)Ack(directory)["request_id"]==ID('3'));
    File.WriteAllText(Path.Combine(directory,"rules.json"),new String(' ',TrickCodec.MaxBytes+1));interrupted.Advance(8,utc+78,false);Check(!interrupted.ConfigOk&&!interrupted.Pose.Active);Check(File.ReadAllText(Path.Combine(directory,"keep.txt"))=="unrelated fixture");
   }finally {
    foreach(string name in new[]{"rules.json","command.json","status.json","preview-status.json","study.json","keep.txt"}){string path=Path.Combine(directory,name);if(File.Exists(path))File.Delete(path);}Directory.Delete(directory,false);
   }
   return checks;
  }
 }
}
