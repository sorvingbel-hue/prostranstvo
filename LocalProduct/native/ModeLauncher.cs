using System;
using System.IO;
using System.Diagnostics;
using System.Windows.Forms;

#if (UI && (MESSAGES || FAMILY || ACTIVITY || HISTORY || KITTEN)) || (MESSAGES && (FAMILY || ACTIVITY || HISTORY || KITTEN)) || (FAMILY && (ACTIVITY || HISTORY || KITTEN)) || (ACTIVITY && (HISTORY || KITTEN)) || (HISTORY && KITTEN)
#error Select exactly one launcher mode.
#endif

// Build separate, fixed-purpose executables with /define:UI|MESSAGES|FAMILY|ACTIVITY|HISTORY|KITTEN.
// No arguments can select another executable or an arbitrary Core command.
static class ModeLauncher {
 static string Arguments(string[] args) {
#if UI
  if(args.Length==1&&args[0]=="--study-resume")return "study-resume";
  if(args.Length>1||(args.Length==1&&args[0]!="--activity"&&args[0]!="--messages"))throw new ArgumentException("Ожидался только --activity или --messages.");
  return "ui"+(args.Length==1?" "+args[0]:"");
#elif MESSAGES
  if(args.Length>1||(args.Length==1&&args[0]!="--setup"))throw new ArgumentException("Ожидался только --setup.");
  return "messages"+(args.Length==1?" --setup":"");
#elif FAMILY
  if(args.Length!=0)throw new ArgumentException("Агент общения не принимает параметры.");
  return "family-agent";
#elif ACTIVITY
  if(args.Length!=0)throw new ArgumentException("Агент записи не принимает параметры.");
  return "activity-agent --background";
#elif HISTORY
  if(args.Length!=0)throw new ArgumentException("Сборщик истории не принимает параметры.");
  return "history-collect";
#elif KITTEN
  if(args.Length>1||(args.Length==1&&args[0]!="--chat"))throw new ArgumentException("Котёнок принимает только --chat.");
  return args.Length==1?"kitten":"pet";
#else
#error Select exactly one launcher mode: UI, MESSAGES, FAMILY, ACTIVITY, HISTORY or KITTEN.
#endif
 }
 [STAThread] static int Main(string[] args) {
  try {
   string arguments=Arguments(args);
   string root=Path.GetFullPath(AppDomain.CurrentDomain.BaseDirectory);
   string core=Path.Combine(root,"ProstranstvoCore.exe");
   if(!File.Exists(core))throw new FileNotFoundException("Рядом с программой отсутствует ProstranstvoCore.exe. Распакуйте весь комплект приложения.",core);
   using(Process child=Process.Start(new ProcessStartInfo(core,arguments){UseShellExecute=false,CreateNoWindow=true,WorkingDirectory=root})) {
    if(child==null)throw new InvalidOperationException("Не удалось запустить приложение.");
    child.WaitForExit();
    int result=child.ExitCode;
#if UI || MESSAGES || KITTEN
    if(result!=0)MessageBox.Show("Приложение завершилось с ошибкой (код "+result+"). Журналы находятся в локальной папке данных «Пространства».","Пространство",MessageBoxButtons.OK,MessageBoxIcon.Error);
#endif
    return result;
   }
  } catch(Exception error) {
#if UI || MESSAGES || KITTEN
   MessageBox.Show(error.Message,"Пространство",MessageBoxButtons.OK,MessageBoxIcon.Error);
#endif
   return 1;
  }
 }
}
