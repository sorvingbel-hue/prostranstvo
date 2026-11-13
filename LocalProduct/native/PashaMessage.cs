using System;
using System.IO;
using System.Text;
using System.Drawing;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Windows.Forms;
using System.Collections.Generic;

namespace PashaMessage {
 sealed class TwoOkGate {
  internal int Count { get; private set; }
  readonly int delay;DateTime armedAt;
  internal TwoOkGate(int milliseconds){delay=milliseconds;}
  internal bool Click(DateTime now){
   if(Count==0){Count=1;armedAt=now.AddMilliseconds(delay);return false;}
   if(Count==1&&now>=armedAt){Count=2;return true;}
   return Count==2;
  }
 }
 static class FamilyReply {
  internal static string MessageId;
  internal static void Emit(string marker) {
   if(String.IsNullOrEmpty(MessageId))return;
   try { Console.WriteLine(marker+" "+MessageId); Console.Out.Flush(); } catch { }
  }
  internal static string AppRoot() {
   string here=Path.GetFullPath(AppDomain.CurrentDomain.BaseDirectory);
   if(File.Exists(Path.Combine(here,"ProstranstvoCore.exe")))return here;
   throw new FileNotFoundException("Модуль ответов пока не установлен.",Path.Combine(here,"ProstranstvoCore.exe"));
  }
  internal static Process Start() {
   string root=AppRoot();
   string arguments="reply --ready-stdout";
   if(!String.IsNullOrEmpty(MessageId))arguments+=" --reply-to "+MessageId;
   return Process.Start(new ProcessStartInfo(Path.Combine(root,"ProstranstvoCore.exe"),arguments){UseShellExecute=false,CreateNoWindow=true,WorkingDirectory=root,RedirectStandardOutput=true});
  }
 }
 static class Native {
  [DllImport("user32.dll")] internal static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] internal static extern uint GetWindowThreadProcessId(IntPtr h, out uint p);
  [DllImport("kernel32.dll")] internal static extern uint GetCurrentThreadId();
  [DllImport("user32.dll")] internal static extern bool AttachThreadInput(uint a, uint b, bool attach);
  [DllImport("user32.dll")] internal static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] internal static extern bool BringWindowToTop(IntPtr h);
  [DllImport("user32.dll")] internal static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int w, int ht, uint flags);
  [DllImport("user32.dll")] internal static extern bool ShowWindow(IntPtr h, int command);
  [DllImport("user32.dll")] internal static extern bool SetProcessDPIAware();
  [DllImport("kernel32.dll")] internal static extern uint WTSGetActiveConsoleSessionId();
 }
 static class Log {
  static string DataPath() {
   string local=Environment.GetEnvironmentVariable("LOCALAPPDATA");
   if(String.IsNullOrWhiteSpace(local)||!Path.IsPathRooted(local))local=Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
   if(String.IsNullOrWhiteSpace(local)||!Path.IsPathRooted(local))throw new InvalidOperationException("Не найдена локальная папка данных пользователя.");
   return Path.Combine(local,"ProstranstvoLocalBeta","data","logs","message.log");
  }
  internal static string PathName { get { return DataPath(); } }
  internal static void Write(string s) {
   try { string path=PathName;Directory.CreateDirectory(Path.GetDirectoryName(path));File.AppendAllText(path, DateTime.Now.ToString("s") + " " + s + Environment.NewLine, Encoding.UTF8); } catch { }
  }
 }
 sealed class Speaker : IDisposable {
  dynamic voice;
  System.Media.SoundPlayer player;
  MemoryStream waveMemory;
  volatile bool stopped;
  internal Speaker() {
   try {
    voice = Activator.CreateInstance(Type.GetTypeFromProgID("SAPI.SpVoice", true));
    string baseKey = @"SOFTWARE\Microsoft\Speech_OneCore\Voices\Tokens";
    string chosen = null;
    using (Microsoft.Win32.RegistryKey root = Microsoft.Win32.Registry.LocalMachine.OpenSubKey(baseKey)) {
     if (root != null) foreach (string name in root.GetSubKeyNames()) {
      using (Microsoft.Win32.RegistryKey attrs = root.OpenSubKey(name + @"\Attributes")) {
       string language = attrs == null ? "" : Convert.ToString(attrs.GetValue("Language"));
       if (Array.IndexOf(language.Split(';'), "419") >= 0) {
        chosen = name;
        if (name.IndexOf("Irina", StringComparison.OrdinalIgnoreCase) >= 0) break;
       }
      }
     }
    }
    if (chosen == null) throw new InvalidOperationException("Русский голос Windows не найден.");
    dynamic token = Activator.CreateInstance(Type.GetTypeFromProgID("SAPI.SpObjectToken", true));
    try { token.SetId(@"HKEY_LOCAL_MACHINE\" + baseKey + @"\" + chosen); voice.Voice = token; }
    finally { Marshal.FinalReleaseComObject((object)token); }
    voice.Rate = 0; voice.Volume = 100;
    Log.Write("VOICE=" + chosen);
   } catch { Dispose(); throw; }
  }
  internal void Speak(string text) {
   string path = Path.Combine(Path.GetTempPath(), "PashaMessage-" + Guid.NewGuid().ToString("N") + ".wav");
   try {
    RenderWave(text, path);
    waveMemory = new MemoryStream(File.ReadAllBytes(path));
    player = new System.Media.SoundPlayer(waveMemory);
    player.Load();
   } finally { try { File.Delete(path); } catch { } }
   System.Media.SoundPlayer currentPlayer = player;
   Log.Write("WAV_PLAYBACK_STARTED default_multimedia_output");
   System.Threading.ThreadPool.QueueUserWorkItem(delegate {
    try {
     if (!stopped) currentPlayer.PlaySync();
     if (!stopped) Log.Write("WAV_PLAYBACK_RETURNED");
    } catch (Exception e) { if (!stopped) Log.Write("PLAYBACK_ERROR type=" + e.GetType().Name); }
   });
  }
  internal void Poll() { }
  void RenderWave(string text, string path) {
   dynamic stream = Activator.CreateInstance(Type.GetTypeFromProgID("SAPI.SpFileStream", true));
   try {
    stream.Open(path, 3, false);
    voice.AudioOutputStream = stream;
    voice.Speak(text, 16);
    stream.Close();
   } finally { Marshal.FinalReleaseComObject((object)stream); }
   if (new FileInfo(path).Length <= 44) throw new InvalidOperationException("Пустая запись синтеза речи.");
   Log.Write("WAV_READY bytes=" + new FileInfo(path).Length);
  }
  internal void RenderTest(string path) {
   dynamic stream = Activator.CreateInstance(Type.GetTypeFromProgID("SAPI.SpFileStream", true));
   try {
    stream.Open(path, 3, false);
    voice.AudioOutputStream = stream;
    voice.Speak("Привет, Паша! Как прошёл твой день?", 16);
    stream.Close();
   } finally { Marshal.FinalReleaseComObject((object)stream); }
   if (new FileInfo(path).Length <= 44) throw new InvalidOperationException("Пустая запись синтеза речи.");
   Log.Write("SPEECH_TEST_OK bytes=" + new FileInfo(path).Length);
  }
  public void Dispose() {
   stopped = true;
   if (player != null) { try { player.Stop(); player.Dispose(); } catch { } player = null; }
   if (waveMemory != null) { waveMemory.Dispose(); waveMemory = null; }
   if (voice == null) return;
   try { voice.Speak("", 3); } catch { }
   try { Marshal.FinalReleaseComObject((object)voice); } catch { }
   voice = null;
  }
 }
 sealed class MessageWindow : Form {
  Label body;
  float preferred;
  readonly bool strictMode;bool allowClose;Button confirmButton;
  internal MessageWindow(Screen screen, string text, float fontSize, Action close, Action reply, bool twoOk=false) {
   strictMode=twoOk;
   Text = "Сообщение от папы";
   TopMost = true; StartPosition = FormStartPosition.Manual;
   FormBorderStyle = FormBorderStyle.FixedDialog; MaximizeBox = false; MinimizeBox = false;
   if(twoOk)ControlBox=false;
   ShowInTaskbar = true; KeyPreview = true; AutoScaleMode = AutoScaleMode.None;
   BackColor = Color.FromArgb(255, 249, 220);
   Rectangle area = screen.WorkingArea;
   int w = Math.Min(twoOk?1400:1000, area.Width - 40), h = Math.Min(twoOk?850:540, area.Height - 40);
   Size = new Size(Math.Max(300,w), Math.Max(240,h));
   Location = new Point(area.Left + Math.Max(10,(area.Width - Width)/2 - area.Width/12), area.Top + Math.Max(10,(area.Height - Height)/2));
   var heading = new Label { Text = "СООБЩЕНИЕ ОТ ПАПЫ", Dock = DockStyle.Top, Height = 56, TextAlign = ContentAlignment.MiddleCenter, BackColor = Color.FromArgb(30,65,110), ForeColor = Color.White, Font = new Font("Segoe UI", 18, FontStyle.Bold) };
   var buttons = new TableLayoutPanel { Dock = DockStyle.Bottom, Height = twoOk?90:82, ColumnCount = twoOk?1:2, RowCount = 1, Padding = new Padding(8) };
   buttons.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,twoOk?100:50));if(!twoOk)buttons.ColumnStyles.Add(new ColumnStyle(SizeType.Percent,50));
   var button = new Button { Text = twoOk?"OK — подтверждение 1 из 2":"Прочитал — закрыть", Dock = DockStyle.Fill, Font = new Font("Segoe UI", twoOk?22:18, FontStyle.Bold), BackColor = Color.White };
   confirmButton=button;
   button.Click += delegate { if(!twoOk)FamilyReply.Emit("FAMILY_READ_BUTTON");close(); };
   var replyButton = new Button { Text = "Ответить папе", Dock = DockStyle.Fill, Font = new Font("Segoe UI", 18, FontStyle.Bold), BackColor = Color.FromArgb(218,238,255) };
   replyButton.Click += delegate { reply(); };
   buttons.Controls.Add(button,0,0);if(!twoOk)buttons.Controls.Add(replyButton,1,0);
   body = new Label { Text = text, Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleLeft, Padding = new Padding(24), ForeColor = Color.FromArgb(20,30,45), UseMnemonic = false };
   preferred = fontSize;
   Controls.Add(body); Controls.Add(heading); Controls.Add(buttons);
   KeyDown += delegate(object sender, KeyEventArgs e) {
    if(twoOk&&(e.KeyCode==Keys.Escape||e.KeyCode==Keys.Enter||e.KeyCode==Keys.Space)){e.Handled=true;e.SuppressKeyPress=true;}
    else if(!twoOk&&e.KeyCode==Keys.Escape)close();
   };
   FormClosing += delegate(object sender,FormClosingEventArgs e) { if(twoOk&&!allowClose){e.Cancel=true;return;}close(); };
   Shown += delegate { FitText(); RaiseNow(true); Log.Write("VISIBLE monitor=" + screen.DeviceName + " bounds=" + Bounds.ToString());FamilyReply.Emit("FAMILY_SHOWN"); };
   body.SizeChanged += delegate { FitText(); };
  }
  internal void UpdateConfirmation(bool enabled){confirmButton.Text="OK — нажми ещё раз, чтобы закрыть";confirmButton.Enabled=enabled;}
  internal void PermitClose(){allowClose=true;}
  protected override bool ProcessCmdKey(ref Message message,Keys keyData){
   Keys code=keyData&Keys.KeyCode;
   if(strictMode&&(code==Keys.Enter||code==Keys.Escape||code==Keys.Space||keyData==(Keys.Alt|Keys.F4)))return true;
   return base.ProcessCmdKey(ref message,keyData);
  }
  protected override bool ProcessDialogKey(Keys keyData){
   Keys code=keyData&Keys.KeyCode;
   if(strictMode&&(code==Keys.Enter||code==Keys.Escape||code==Keys.Space))return true;
   return base.ProcessDialogKey(keyData);
  }
  internal bool TestCommandBlocked(Keys key){Message message=new Message();return ProcessCmdKey(ref message,key);}
  internal bool TestCloseCancelled(){var e=new FormClosingEventArgs(CloseReason.UserClosing,false);OnFormClosing(e);return e.Cancel;}
  void FitText() {
   if (body.ClientSize.Width < 60 || body.ClientSize.Height < 60) return;
   float size = preferred;
   Size available = new Size(body.ClientSize.Width-48, body.ClientSize.Height-48);
   while (size > 14) {
    using (Font f = new Font("Segoe UI", size, FontStyle.Bold)) {
     Size measured = TextRenderer.MeasureText(body.Text, f, new Size(available.Width, int.MaxValue), TextFormatFlags.WordBreak | TextFormatFlags.NoPrefix);
     if (measured.Height <= available.Height && measured.Width <= available.Width) break;
    }
    size -= 1;
   }
   Font previous = body.Font;
   body.Font = new Font("Segoe UI", size, FontStyle.Bold);
   if (previous != null && previous != Control.DefaultFont) previous.Dispose();
  }
  internal void RaiseNow(bool focus) {
   if (IsDisposed || !Visible) return;
   Native.SetWindowPos(Handle, new IntPtr(-1), 0,0,0,0, 0x0013);
   if (!focus) return;
   IntPtr foreground = Native.GetForegroundWindow();
   uint pid; uint foregroundThread = Native.GetWindowThreadProcessId(foreground, out pid);
   uint currentThread = Native.GetCurrentThreadId(); bool attached = false;
   try {
    if (foregroundThread != 0 && foregroundThread != currentThread) attached = Native.AttachThreadInput(currentThread, foregroundThread, true);
    Native.ShowWindow(Handle, 9); Native.BringWindowToTop(Handle); Native.SetForegroundWindow(Handle); Activate();
   } finally { if (attached) Native.AttachThreadInput(currentThread, foregroundThread, false); }
   bool front = Native.GetForegroundWindow() == Handle;
   Log.Write("FOREGROUND=" + front);
  }
 }
 sealed class MessageContext : ApplicationContext {
  List<MessageWindow> windows = new List<MessageWindow>();
  System.Windows.Forms.Timer timer;
  Speaker speaker;
  bool closing,replyOpening; DateTime start = DateTime.UtcNow; int duration;
  readonly bool strictMode;TwoOkGate confirmations;System.Windows.Forms.Timer confirmTimer;
  internal MessageContext(string text, float font, bool all, int seconds, bool silent, bool twoOk=false) {
   strictMode=twoOk;if(twoOk){confirmations=new TwoOkGate(SystemInformation.DoubleClickTime+150);Log.Write("STRICT_TWO_OK seconds=0");}
   duration = seconds;
   Screen[] screens = all ? Screen.AllScreens : new Screen[] { Screen.FromPoint(Cursor.Position) };
   foreach (Screen screen in screens) windows.Add(new MessageWindow(screen,text,font,twoOk?(Action)ConfirmOk:CloseAll,OpenReply,twoOk));
   foreach (MessageWindow window in windows) window.Show();
   timer = new System.Windows.Forms.Timer { Interval = 1000 };
   timer.Tick += delegate {
    if (duration > 0 && (DateTime.UtcNow-start).TotalSeconds >= duration) { CloseAll(); return; }
    foreach (MessageWindow window in windows) window.RaiseNow(false);
    if (speaker != null) { try { speaker.Poll(); } catch (Exception e) { Log.Write("SPEECH_ERROR type=" + e.GetType().Name); speaker.Dispose(); speaker = null; } }
   };
   timer.Start();
   if (!silent) {
    try { speaker = new Speaker(); speaker.Speak(text); }
    catch (Exception e) {
     Log.Write("SPEECH_ERROR type=" + e.GetType().Name);
     if (speaker != null) { speaker.Dispose(); speaker = null; }
     foreach (MessageWindow window in windows) window.Text = "Сообщение от папы — озвучка недоступна";
    }
   }
  }
  void ConfirmOk(){
   if(closing)return;
   int before=confirmations.Count;
   if(confirmations.Click(DateTime.UtcNow)){Log.Write("OK_CONFIRMATION_2");foreach(var window in windows)window.PermitClose();CloseAll();return;}
   if(before==0){
    Log.Write("OK_CONFIRMATION_1");
    foreach(var window in windows)window.UpdateConfirmation(false);
    confirmTimer=new System.Windows.Forms.Timer{Interval=SystemInformation.DoubleClickTime+160};
    confirmTimer.Tick+=delegate{confirmTimer.Stop();confirmTimer.Dispose();confirmTimer=null;foreach(var window in windows)if(!window.IsDisposed)window.UpdateConfirmation(true);};
    confirmTimer.Start();
   }
  }
  void OpenReply() {
   if(closing||replyOpening||strictMode)return;replyOpening=true;
   System.Threading.ThreadPool.QueueUserWorkItem(delegate {
    bool ready=false;
    try {
     using(Process child=FamilyReply.Start()) {
      try {
       var line=System.Threading.Tasks.Task.Factory.StartNew<string>(delegate { return child.StandardOutput.ReadLine(); });
       ready=line.Wait(12000)&&line.Result=="FAMILY_REPLY_READY";
      } finally {
       if(!ready){try { if(!child.HasExited)child.Kill(); } catch { }}
      }
     }
    } catch(Exception e){Log.Write("REPLY_START_ERROR type="+e.GetType().Name);}
    try {
     if(windows.Count>0&&!windows[0].IsDisposed)windows[0].BeginInvoke((Action)delegate {
      replyOpening=false;
      if(ready)CloseAll();
      else MessageBox.Show(windows[0],"Не удалось открыть ответ. Сообщение осталось на экране. Открой «Пространство» → «Общение».","Ответ папе");
     });
    } catch { }
   });
  }
  void CloseAll() {
   if(strictMode&&confirmations.Count<2)return;
   if (closing) return; closing = true;
   if(confirmTimer!=null){confirmTimer.Stop();confirmTimer.Dispose();confirmTimer=null;}
   if (timer != null) { timer.Stop(); timer.Dispose(); }
   if (speaker != null) { speaker.Dispose(); speaker = null; }
   foreach (MessageWindow window in windows) if (!window.IsDisposed) window.Close();
   Log.Write("CLOSED"); ExitThread();
  }
 }
 static class Program {
  static void TestTwoOk(){
   var gate=new TwoOkGate(700);DateTime now=DateTime.UtcNow;
   if(gate.Click(now)||gate.Count!=1)throw new Exception("First OK closed window");
   if(gate.Click(now.AddMilliseconds(50))||gate.Count!=1)throw new Exception("Double click closed window");
   if(!gate.Click(now.AddMilliseconds(800))||gate.Count!=2)throw new Exception("Second separate OK failed");
   int calls=0;
   using(var form=new MessageWindow(Screen.PrimaryScreen,"Synthetic two-OK test",32,delegate{calls++;},delegate{throw new Exception("Unexpected reply");},true)){
    if(form.ControlBox||!form.TestCloseCancelled()||calls!=0)throw new Exception("Close bypass");
    foreach(Keys key in new[]{Keys.Escape,Keys.Enter,Keys.Space,Keys.Alt|Keys.F4})if(!form.TestCommandBlocked(key))throw new Exception("Keyboard bypass");
    form.PermitClose();if(form.TestCloseCancelled()||calls!=1)throw new Exception("Authorized close failed");
   }
   using(var form=new MessageWindow(Screen.PrimaryScreen,"Synthetic ordinary test",32,delegate{calls++;},delegate{},false))if(form.TestCloseCancelled())throw new Exception("Ordinary close broken");
   Log.Write("TWO_OK_SELF_TEST_OK");
  }
  static int TextLength(string value) {
   int count=0;
   for(int i=0;i<value.Length;i++){
    if(Char.IsHighSurrogate(value[i])){if(i+1>=value.Length||!Char.IsLowSurrogate(value[++i]))return Int32.MaxValue;}
    else if(Char.IsLowSurrogate(value[i]))return Int32.MaxValue;
    count++;
   }
   return count;
  }
  [STAThread] static int Main(string[] args) {
   try {
    Native.SetProcessDPIAware();
    string text = "Привет, Паша, как у тебя дела?\nКак прошел твой день?\nПозвони мне и расскажи.\nОчень сильно жду.";
    bool all = true, selfTest = false, silent = false, replyTest=false, stdinText=false, argumentText=false,twoOk=false,twoOkTest=false; string speechTest = null; int seconds = 0; float font = 32;
    for (int i=0; i<args.Length; i++) {
     if (args[i] == "--text" && i+1<args.Length) {text=args[++i];argumentText=true;}
     else if (args[i] == "--file" && i+1<args.Length) {text=File.ReadAllText(args[++i],Encoding.UTF8);argumentText=true;}
     else if (args[i] == "--stdin") stdinText=true;
     else if (args[i] == "--seconds" && i+1<args.Length) seconds=int.Parse(args[++i]);
     else if (args[i] == "--font" && i+1<args.Length) font=float.Parse(args[++i],System.Globalization.CultureInfo.InvariantCulture);
     else if (args[i] == "--active-monitor") all=false;
     else if (args[i] == "--self-test") selfTest=true;
     else if (args[i] == "--two-ok") twoOk=true;
     else if (args[i] == "--two-ok-self-test") twoOkTest=true;
     else if (args[i] == "--reply-self-test") replyTest=true;
     else if (args[i] == "--message-id" && i+1<args.Length) {
      FamilyReply.MessageId=args[++i];
      if(!System.Text.RegularExpressions.Regex.IsMatch(FamilyReply.MessageId,@"\A[A-Za-z0-9_-]{1,100}\z"))throw new ArgumentException("Неверный ID сообщения.");
     }
     else if (args[i] == "--silent") silent=true;
     else if (args[i] == "--speech-test" && i+1<args.Length) speechTest=args[++i];
     else throw new ArgumentException("Неизвестный или неполный параметр: " + args[i]);
    }
    if(stdinText){
     if(argumentText)throw new ArgumentException("Выбери один источник текста.");
     using(var input=new StreamReader(Console.OpenStandardInput(),new UTF8Encoding(false,true))){
      char[] buffer=new char[8001];int used=0,read;
      while(used<buffer.Length&&(read=input.Read(buffer,used,buffer.Length-used))>0)used+=read;
      text=new string(buffer,0,used);
     }
    }
    if (String.IsNullOrWhiteSpace(text) || TextLength(text)>4000 || font<16 || font>72 || seconds<0) throw new ArgumentException("Нужен текст от 1 до 4000 символов, шрифт 16–72 и неотрицательная длительность.");
    if(twoOk&&seconds!=0)throw new ArgumentException("Для двух подтверждений автоматическое закрытие запрещено.");
    int session=Process.GetCurrentProcess().SessionId;
    uint console=Native.WTSGetActiveConsoleSessionId();
    Log.Write("START session="+session+" console="+console+" monitors="+Screen.AllScreens.Length);
    if (session==0) throw new InvalidOperationException("Запуск в служебной сессии 0: сообщение не будет видно. Запустите приложение в сессии пользователя.");
    if (speechTest != null) { using (Speaker testVoice = new Speaker()) { testVoice.RenderTest(speechTest); } return 0; }
    if (twoOkTest) {TestTwoOk();return 0;}
    if (replyTest) { FamilyReply.AppRoot();Log.Write("REPLY_TARGET_OK");return 0; }
    if (selfTest) {
     foreach(Screen screen in Screen.AllScreens) Log.Write("TEST monitor="+screen.DeviceName+" workarea="+screen.WorkingArea);
     Log.Write("SELF_TEST_OK"); return 0;
    }
    Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
    Application.Run(new MessageContext(text,font,all,seconds,silent,twoOk)); return 0;
   } catch(Exception e) { Log.Write("ERROR type=" + e.GetType().Name); return 1; }
  }
 }
}




