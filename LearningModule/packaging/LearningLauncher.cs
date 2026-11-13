using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Windows.Forms;

internal static class LearningLauncher {
    private static string Quote(string value) {
        StringBuilder b = new StringBuilder("\"");
        int slashes = 0;
        foreach (char c in value) {
            if (c == '\\') { slashes++; continue; }
            if (c == '"') { b.Append('\\', slashes * 2 + 1); b.Append(c); slashes = 0; continue; }
            b.Append('\\', slashes); slashes = 0; b.Append(c);
        }
        b.Append('\\', slashes * 2); b.Append('"'); return b.ToString();
    }
    [STAThread]
    private static void Main(string[] args) {
        try {
            StringBuilder forwarded = new StringBuilder();
            for (int i = 0; i < args.Length; i++) {
                if ((args[i] != "--data-dir" && args[i] != "--language") || i + 1 >= args.Length)
                    throw new ArgumentException("Unsupported launcher argument");
                forwarded.Append(Quote(args[i])).Append(' ').Append(Quote(args[++i])).Append(' ');
            }
            string root = AppDomain.CurrentDomain.BaseDirectory;
            ProcessStartInfo start = new ProcessStartInfo(Path.Combine(root, "LearningModuleCore.exe"), forwarded.ToString());
            start.WorkingDirectory = root;
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            start.WindowStyle = ProcessWindowStyle.Hidden;
            Process.Start(start);
        } catch {
            MessageBox.Show("Не удалось запустить учебный модуль. Распакуйте всю папку программы и попробуйте снова.", "Учебный модуль", MessageBoxButtons.OK, MessageBoxIcon.Information);
        }
    }
}
