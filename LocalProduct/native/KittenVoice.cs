using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Media;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using System.Threading;

namespace ProstranstvoKittenVoice {
 sealed class VoiceFailure : Exception {public readonly string Code;public VoiceFailure(string code){Code=code;}}
 static class Json {
  public static string Quote(string text) {
   StringBuilder result=new StringBuilder("\"");
   foreach(char c in text) {
    if(c=='\\'||c=='\"'){result.Append('\\');result.Append(c);}
    else if(c<32)result.Append("\\u"+((int)c).ToString("x4",CultureInfo.InvariantCulture));
    else result.Append(c);
   }
   return result.Append('"').ToString();
  }
 }
 static class Rules {
  public const int Rate=22050,Block=2,MaxSeconds=15,MaxText=4000;
  public static bool GoodText(string value) {
   if(String.IsNullOrWhiteSpace(value)||value.Length>MaxText)return false;
   try {if(new UTF8Encoding(false,true).GetByteCount(value)>16000)return false;}catch{return false;}
   foreach(char c in value)if((c<32&&c!='\n'&&c!='\r'&&c!='\t')||c=='\ufffe'||c=='\uffff')return false;
   return true;
  }
  public static int Seconds(string value) {int result;if(!Int32.TryParse(value,NumberStyles.None,CultureInfo.InvariantCulture,out result)||result<1||result>MaxSeconds)throw new VoiceFailure("invalid_duration");return result;}
  public static byte[] Wave(byte[] pcm,int rate,int channels,int bits) {
   if(pcm==null||pcm.Length<2||pcm.Length>10*1024*1024||rate<8000||rate>48000||channels<1||channels>2||bits!=16)throw new VoiceFailure("audio_format_invalid");
   int alignment=channels*bits/8;if(pcm.Length%alignment!=0)throw new VoiceFailure("audio_format_invalid");
   using(MemoryStream bytes=new MemoryStream(44+pcm.Length)) {
    using(BinaryWriter writer=new BinaryWriter(bytes,Encoding.ASCII,true)) {
     writer.Write(Encoding.ASCII.GetBytes("RIFF"));writer.Write(36+pcm.Length);writer.Write(Encoding.ASCII.GetBytes("WAVEfmt "));writer.Write(16);
     writer.Write((short)1);writer.Write((short)channels);writer.Write(rate);writer.Write(rate*alignment);writer.Write((short)alignment);writer.Write((short)bits);
     writer.Write(Encoding.ASCII.GetBytes("data"));writer.Write(pcm.Length);writer.Write(pcm);
    }
    return bytes.ToArray();
   }
  }
 }
 static class RussianVoice {
  // A gentle kitten style using installed voices, not a natural child model.
  // SAPI XML pitch syntax and range: Microsoft XML TTS Tutorial, SAPI 5.4.
  // https://learn.microsoft.com/en-us/previous-versions/windows/desktop/ee431815(v=vs.85)
  public const int KittenPitch=4,KittenRate=-1,KittenVolume=85,XmlSpeakFlags=9;
  public static string Markup(string text) {
   if(!Rules.GoodText(text))throw new VoiceFailure("invalid_text");
   // Every user character is text: only this fixed pitch element is executable.
   string escaped=text.Replace("&","&amp;").Replace("<","&lt;").Replace(">","&gt;").Replace("\"","&quot;").Replace("'","&apos;");
   return "<pitch absmiddle=\""+KittenPitch.ToString(CultureInfo.InvariantCulture)+"\">"+escaped+"</pitch>";
  }
  const string Tokens=@"SOFTWARE\Microsoft\Speech_OneCore\Voices\Tokens";
  public static string Token() {
   string chosen=null;int chosenScore=-1;
   try {
    using(Microsoft.Win32.RegistryKey root=Microsoft.Win32.Registry.LocalMachine.OpenSubKey(Tokens)) {
     if(root!=null)foreach(string name in root.GetSubKeyNames())using(Microsoft.Win32.RegistryKey attrs=root.OpenSubKey(name+@"\Attributes")) {
      string languages=attrs==null?"":Convert.ToString(attrs.GetValue("Language"));bool russian=false;
      foreach(string part in languages.Split(';')){int id;if(Int32.TryParse(part,NumberStyles.HexNumber,CultureInfo.InvariantCulture,out id)&&id==0x419)russian=true;}
      if(russian){string gender=attrs==null?"":Convert.ToString(attrs.GetValue("Gender"));int score=(String.Equals(gender,"Female",StringComparison.OrdinalIgnoreCase)?10:0)+(name.IndexOf("Irina",StringComparison.OrdinalIgnoreCase)>=0?20:0);if(score>chosenScore){chosen=name;chosenScore=score;}}
     }
    }
   }catch{return null;}
   return chosen==null?null:@"HKEY_LOCAL_MACHINE\"+Tokens+@"\"+chosen;
  }
  static void Release(object value){if(value!=null&&Marshal.IsComObject(value))try{Marshal.FinalReleaseComObject(value);}catch{}}
  static byte[] Bytes(object value) {
   byte[] direct=value as byte[];if(direct!=null)return direct;
   Array source=value as Array;if(source==null||source.Length>10*1024*1024)throw new VoiceFailure("speech_render_failed");
   byte[] bytes=new byte[source.Length];for(int i=0;i<bytes.Length;i++)bytes[i]=Convert.ToByte(source.GetValue(i));return bytes;
  }
  public static byte[] RenderWave(string text) {
   if(!Rules.GoodText(text))throw new VoiceFailure("invalid_text");
   string tokenId=Token();if(tokenId==null)throw new VoiceFailure("russian_voice_unavailable");
   dynamic voice=null,token=null,stream=null,format=null;
   try {
    voice=Activator.CreateInstance(Type.GetTypeFromProgID("SAPI.SpVoice",true));
    token=Activator.CreateInstance(Type.GetTypeFromProgID("SAPI.SpObjectToken",true));token.SetId(tokenId);voice.Voice=token;
    stream=Activator.CreateInstance(Type.GetTypeFromProgID("SAPI.SpMemoryStream",true));
    format=stream.Format;format.Type=22;stream.Format=format; // SAPI 22 kHz, 16-bit mono.
    voice.AudioOutputStream=stream;voice.Rate=KittenRate;voice.Volume=KittenVolume;
    voice.Speak(Markup(text),XmlSpeakFlags); // SVSFlagsAsync | SVSFIsXML: escaped text in fixed SAPI XML.
    Stopwatch deadline=Stopwatch.StartNew();
    while(!Convert.ToBoolean(voice.WaitUntilDone(100)))if(deadline.ElapsedMilliseconds>90000){try{voice.Speak("",3);}catch{}throw new VoiceFailure("speech_timeout");}
    byte[] pcm=Bytes(stream.GetData());
    if(pcm.Length<2||pcm.Length>Rules.Rate*Rules.Block*95)throw new VoiceFailure("speech_render_failed");
    if(pcm.Length>=44&&Encoding.ASCII.GetString(pcm,0,4)=="RIFF"&&Encoding.ASCII.GetString(pcm,8,4)=="WAVE")return pcm;
    return Rules.Wave(pcm,Rules.Rate,1,16);
   }catch(VoiceFailure){throw;}catch{throw new VoiceFailure("speech_unavailable");}
   finally {Release((object)format);Release((object)stream);Release((object)token);Release((object)voice);}
  }
  public static void Speak(string text) {
   byte[] wave=RenderWave(text);
   try {using(MemoryStream memory=new MemoryStream(wave,false))using(SoundPlayer player=new SoundPlayer(memory)){player.Load();player.PlaySync();}}
   catch {throw new VoiceFailure("speech_unavailable");}
  }
 }
 static class MicNative {
  [StructLayout(LayoutKind.Sequential,Pack=2)]public struct Format {public ushort tag,channels;public uint rate,bytes;public ushort alignment,bits,extra;}
  [StructLayout(LayoutKind.Sequential)]public struct Header {public IntPtr data;public uint length,recorded;public UIntPtr user;public uint flags,loops;public IntPtr next;public UIntPtr reserved;}
  [DllImport("winmm.dll")]public static extern uint waveInGetNumDevs();
  [DllImport("winmm.dll")]public static extern uint waveInOpen(out IntPtr input,uint device,ref Format format,IntPtr callback,IntPtr instance,uint flags);
  [DllImport("winmm.dll")]public static extern uint waveInPrepareHeader(IntPtr input,IntPtr header,uint size);
  [DllImport("winmm.dll")]public static extern uint waveInUnprepareHeader(IntPtr input,IntPtr header,uint size);
  [DllImport("winmm.dll")]public static extern uint waveInAddBuffer(IntPtr input,IntPtr header,uint size);
  [DllImport("winmm.dll")]public static extern uint waveInStart(IntPtr input);
  [DllImport("winmm.dll")]public static extern uint waveInReset(IntPtr input);
  [DllImport("winmm.dll")]public static extern uint waveInClose(IntPtr input);
 }
 static class PrivateFiles {
  public static string Root() {
   string root;
#if PERSONAL
   DirectoryInfo parent=Directory.GetParent(AppDomain.CurrentDomain.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar));
   if(parent==null)throw new VoiceFailure("private_folder_unavailable");
   root=Path.Combine(parent.FullName,"data","cat-companion","voice");
#else
   root=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"ProstranstvoLocalBeta","data","cat-companion","voice");
#endif
   return Path.GetFullPath(root);
  }
  public static void NoLinks(string path) {
   string full=Path.GetFullPath(path);if(full.IndexOf('"')>=0)throw new VoiceFailure("private_folder_unavailable");
   for(string item=full;!String.IsNullOrEmpty(item);) {
    if(File.Exists(item)||Directory.Exists(item))if((File.GetAttributes(item)&FileAttributes.ReparsePoint)!=0)throw new VoiceFailure("private_folder_unavailable");
    DirectoryInfo parent=Directory.GetParent(item);item=parent==null?null:parent.FullName;
   }
  }
  public static string PrepareRoot() {
   string root=Root();NoLinks(root);Directory.CreateDirectory(root);NoLinks(root);
   SecurityIdentifier user=WindowsIdentity.GetCurrent().User;
   DirectorySecurity policy=new DirectorySecurity();policy.SetAccessRuleProtection(true,false);policy.SetOwner(user);
   InheritanceFlags inherit=InheritanceFlags.ContainerInherit|InheritanceFlags.ObjectInherit;
   foreach(SecurityIdentifier sid in new[]{user,new SecurityIdentifier(WellKnownSidType.LocalSystemSid,null),new SecurityIdentifier(WellKnownSidType.BuiltinAdministratorsSid,null)})
    policy.AddAccessRule(new FileSystemAccessRule(sid,FileSystemRights.FullControl,inherit,PropagationFlags.None,AccessControlType.Allow));
   Directory.SetAccessControl(root,policy);return root;
  }
 }
 sealed class MicBuffer {
  public IntPtr Data,Header;public bool Prepared;public readonly int Length;
  public MicBuffer(int length){Length=length;Data=Marshal.AllocHGlobal(length);Header=Marshal.AllocHGlobal(Marshal.SizeOf(typeof(MicNative.Header)));}
  public void Queue(IntPtr input,uint size) {
   MicNative.Header header=new MicNative.Header();header.data=Data;header.length=(uint)Length;Marshal.StructureToPtr(header,Header,false);
   if(MicNative.waveInPrepareHeader(input,Header,size)!=0)throw new VoiceFailure("microphone_unavailable");Prepared=true;
   if(MicNative.waveInAddBuffer(input,Header,size)!=0)throw new VoiceFailure("microphone_unavailable");
  }
  public void Unprepare(IntPtr input,uint size) {
   if(!Prepared)return;
   uint result=MicNative.waveInUnprepareHeader(input,Header,size);
   if(result!=0)throw new VoiceFailure("microphone_capture_failed");Prepared=false;
  }
  public void Cleanup(IntPtr input,uint size) {
   if(Prepared) {
    for(int attempt=0;attempt<6;attempt++){if(MicNative.waveInUnprepareHeader(input,Header,size)==0){Prepared=false;break;}Thread.Sleep(20);}
   }
   // Never free a buffer still owned by the driver. This finite helper exits
   // immediately afterwards; the OS reclaims it if a driver refused cleanup.
   if(!Prepared){if(Header!=IntPtr.Zero)Marshal.FreeHGlobal(Header);if(Data!=IntPtr.Zero)Marshal.FreeHGlobal(Data);Header=Data=IntPtr.Zero;}
  }
 }
 static class Recorder {
  static void Copy(MicBuffer buffer,MemoryStream target,int limit,bool requireDone) {
   MicNative.Header header=(MicNative.Header)Marshal.PtrToStructure(buffer.Header,typeof(MicNative.Header));
   if(requireDone&&(header.flags&1)==0)return;
   if(header.recorded>buffer.Length)throw new VoiceFailure("microphone_capture_failed");
   int count=Math.Min((int)header.recorded,limit-(int)target.Length);count-=count%Rules.Block;
   if(count>0){byte[] bytes=new byte[count];Marshal.Copy(buffer.Data,bytes,0,count);target.Write(bytes,0,count);}
  }
  public static string Record(int seconds,ManualResetEvent stop) {
   if(seconds<1||seconds>Rules.MaxSeconds)throw new VoiceFailure("invalid_duration");
   string root;
   try{root=PrivateFiles.PrepareRoot();}catch(VoiceFailure){throw;}catch{throw new VoiceFailure("private_folder_unavailable");}
   if(MicNative.waveInGetNumDevs()==0)throw new VoiceFailure("microphone_unavailable");
   MicNative.Format format=new MicNative.Format();format.tag=1;format.channels=1;format.rate=Rules.Rate;format.bytes=Rules.Rate*Rules.Block;format.alignment=Rules.Block;format.bits=16;
   IntPtr input=IntPtr.Zero;uint size=(uint)Marshal.SizeOf(typeof(MicNative.Header));List<MicBuffer> buffers=new List<MicBuffer>();Queue<MicBuffer> pending=new Queue<MicBuffer>();
   int limit=seconds*Rules.Rate*Rules.Block;bool early=false;
   using(MemoryStream pcm=new MemoryStream(limit)) {
    try {
     if(MicNative.waveInOpen(out input,UInt32.MaxValue,ref format,IntPtr.Zero,IntPtr.Zero,0)!=0)throw new VoiceFailure("microphone_unavailable");
     for(int i=0;i<3;i++){MicBuffer buffer=new MicBuffer(Rules.Rate*Rules.Block/2);buffers.Add(buffer);buffer.Queue(input,size);pending.Enqueue(buffer);}
     if(MicNative.waveInStart(input)!=0)throw new VoiceFailure("microphone_unavailable");
     Stopwatch deadline=Stopwatch.StartNew();
     while(deadline.ElapsedMilliseconds<seconds*1000&&pcm.Length<limit) {
      if(stop.WaitOne(0)){early=true;break;}
      for(int returned=0;returned<buffers.Count;returned++) {
       MicBuffer buffer=pending.Peek();
       MicNative.Header header=(MicNative.Header)Marshal.PtrToStructure(buffer.Header,typeof(MicNative.Header));
       if((header.flags&1)==0)break;
       pending.Dequeue();Copy(buffer,pcm,limit,true);buffer.Unprepare(input,size);buffer.Queue(input,size);pending.Enqueue(buffer);
      }
      Thread.Sleep(15);
     }
     if(MicNative.waveInReset(input)!=0)throw new VoiceFailure("microphone_capture_failed");
     while(pending.Count>0)Copy(pending.Dequeue(),pcm,limit,true);
    }catch(VoiceFailure){throw;}catch{throw new VoiceFailure("microphone_capture_failed");}
    finally {
     if(input!=IntPtr.Zero){try{MicNative.waveInReset(input);}catch{}foreach(MicBuffer buffer in buffers)buffer.Cleanup(input,size);try{MicNative.waveInClose(input);}catch{}}
    }
    if(pcm.Length<Rules.Rate*Rules.Block/10)throw new VoiceFailure("recording_too_short");
    string name="recording-"+DateTime.UtcNow.ToString("yyyyMMddTHHmmssZ",CultureInfo.InvariantCulture)+"-"+Guid.NewGuid().ToString("N")+".wav";
    string target=Path.Combine(root,name),partial=target+".partial";
    byte[] wave=Rules.Wave(pcm.ToArray(),Rules.Rate,1,16);
    try {
     PrivateFiles.NoLinks(root);PrivateFiles.NoLinks(target);
     using(FileStream output=new FileStream(partial,FileMode.CreateNew,FileAccess.Write,FileShare.None)){output.Write(wave,0,wave.Length);output.Flush(true);}
     PrivateFiles.NoLinks(root);File.Move(partial,target);
    }catch {try{if(File.Exists(partial))File.Delete(partial);}catch{}throw new VoiceFailure("recording_save_failed");}
    double duration=pcm.Length/(double)(Rules.Rate*Rules.Block);
    return "{\"status\":\"recorded\",\"path\":"+Json.Quote(target)+",\"duration_seconds\":"+duration.ToString("0.000",CultureInfo.InvariantCulture)+",\"bytes\":"+wave.Length+",\"stopped\":"+(early?"true":"false")+",\"asr_available\":false,\"transcribed\":false}";
   }
  }
 }
 static class Tests {
  static int checks;static void Check(bool value){checks++;if(!value)throw new VoiceFailure("self_test_failed");}
  public static int Run() {
   Check(Rules.GoodText("Привет, Паша!"));Check(!Rules.GoodText(" "));Check(!Rules.GoodText(new String('а',4001)));Check(!Rules.GoodText("test\0"));Check(!Rules.GoodText("\ud800"));
   Check(Rules.Seconds("8")==8&&Rules.Seconds("15")==15);
   foreach(string value in new[]{"0","16","-1","8.5","8 extra","9999999999"}){bool refused=false;try{Rules.Seconds(value);}catch(VoiceFailure){refused=true;}Check(refused);}
   byte[] wave=Rules.Wave(new byte[44100],22050,1,16);Check(wave.Length==44144);Check(Encoding.ASCII.GetString(wave,0,4)=="RIFF"&&Encoding.ASCII.GetString(wave,8,8)=="WAVEfmt ");Check(BitConverter.ToInt32(wave,24)==22050&&BitConverter.ToInt32(wave,40)==44100);
   Check(Json.Quote("a\\b\"c\n")=="\"a\\\\b\\\"c\\u000a\"");
   Check(RussianVoice.KittenPitch>=0&&RussianVoice.KittenPitch<=5&&RussianVoice.KittenRate>=-2&&RussianVoice.KittenRate<=0&&RussianVoice.KittenVolume>=60&&RussianVoice.KittenVolume<=90);
   Check(RussianVoice.XmlSpeakFlags==9);
   Check(RussianVoice.Markup("Кот <audio> & \"голос\" 'тест'")=="<pitch absmiddle=\"4\">Кот &lt;audio&gt; &amp; &quot;голос&quot; &apos;тест&apos;</pitch>");
   Check(RussianVoice.Markup("</pitch><volume level='100'>")=="<pitch absmiddle=\"4\">&lt;/pitch&gt;&lt;volume level=&apos;100&apos;&gt;</pitch>");
   Check(!Rules.GoodText("\ufffe")&&!Rules.GoodText("\uffff"));
   Console.WriteLine("{\"status\":\"self_test_ok\",\"checks\":"+checks+",\"microphone_opened\":false,\"audio_played\":false,\"network\":false}");return 0;
  }
 }
 static class Program {
  static string ReadText() {
   using(StreamReader source=new StreamReader(Console.OpenStandardInput(),new UTF8Encoding(false,true))) {
    char[] text=new char[Rules.MaxText+1];int used=0,read;
    while(used<text.Length&&(read=source.Read(text,used,text.Length-used))>0)used+=read;
    string value=new String(text,0,used);if(!Rules.GoodText(value))throw new VoiceFailure("invalid_text");return value;
   }
  }
  [STAThread]static int Main(string[] args) {
   try {
    // OutputEncoding also queries console input handles on .NET Framework.
    // A Windows-subsystem helper can inherit a redirected stdout with no stdin.
    // Write UTF-8 directly to stdout instead, without configuring a console.
    Console.SetOut(new StreamWriter(Console.OpenStandardOutput(),new UTF8Encoding(false)){AutoFlush=true});
    if(args.Length==1&&args[0]=="--self-test")return Tests.Run();
    if(args.Length==1&&args[0]=="--synthesis-test") {
     byte[] wave=RussianVoice.RenderWave("Привет! Это проверка голоса котёнка.");
     Console.WriteLine("{\"status\":\"synthesis_ok\",\"wave_bytes\":"+wave.Length+",\"audio_played\":false,\"microphone_opened\":false}");return 0;
    }
    if(args.Length==1&&args[0]=="--status") {
     bool voice=RussianVoice.Token()!=null&&Type.GetTypeFromProgID("SAPI.SpVoice",false)!=null;
     uint microphones=MicNative.waveInGetNumDevs();if(microphones>128)microphones=128;
     Console.WriteLine("{\"status\":\"ok\",\"russian_tts_available\":"+(voice?"true":"false")+",\"microphone_devices\":"+microphones+",\"asr_available\":false,\"microphone_opened\":false,\"audio_played\":false}");return 0;
    }
    if(args.Length==1&&args[0]=="--speak") {
     using(Timer hardDeadline=new Timer(delegate{try{Console.WriteLine("{\"status\":\"error\",\"code\":\"speech_timeout\"}");}catch{}Environment.Exit(5);},null,180000,Timeout.Infinite))RussianVoice.Speak(ReadText());
     Console.WriteLine("{\"status\":\"spoken\",\"playback_requested\":true,\"audible_confirmed\":false}");return 0;
    }
    if(args.Length==2&&args[0]=="--record") {
     int seconds=Rules.Seconds(args[1]);using(ManualResetEvent stop=new ManualResetEvent(false)) {
      Thread listener=new Thread(new ThreadStart(delegate{try{using(StreamReader input=new StreamReader(Console.OpenStandardInput(),Encoding.UTF8)){StringBuilder token=new StringBuilder();for(int i=0;i<16;i++){int c=input.Read();if(c<0)break;if(c=='\n'){if(token.ToString().Trim()=="stop")stop.Set();break;}token.Append((char)c);}}}catch{}}));listener.IsBackground=true;listener.Start();
      Console.WriteLine(Recorder.Record(seconds,stop));return 0;
     }
    }
    throw new VoiceFailure("invalid_arguments");
   }catch(VoiceFailure error){Console.WriteLine("{\"status\":\"error\",\"code\":"+Json.Quote(error.Code)+"}");return 2;}
   catch {Console.WriteLine("{\"status\":\"error\",\"code\":\"voice_unavailable\"}");return 3;}
  }
 }
}
