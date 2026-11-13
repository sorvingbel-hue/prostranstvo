using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;

// Per-user, offline installer. No service, autorun, account or network setup.
internal static class SetupLocalBeta {
    internal const string Product = "prostranstvo-local-beta";
    const string MarkerName = ".prostranstvo-install.json";
    const long MaxTotal = 1024L * 1024 * 1024;
    const long MaxFile = 512L * 1024 * 1024;
    const int MaxFiles = 20000;
    const int MaxManifest = 4 * 1024 * 1024;
    const string ShortcutName = "Пространство — Local Beta.lnk";
    static readonly JavaScriptSerializer Json = new JavaScriptSerializer { MaxJsonLength = MaxManifest, RecursionLimit = 32 };

    internal sealed class FileSpec {
        internal string Name, Sha;
        internal long Size;
    }
    internal sealed class Plan {
        internal string Version;
        internal byte[] ManifestBytes;
        internal Dictionary<string, FileSpec> Files = new Dictionary<string, FileSpec>(StringComparer.OrdinalIgnoreCase);
        internal long Total;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct FileInfoNative {
        public uint Attributes, CreationLow, CreationHigh, AccessLow, AccessHigh,
                    WriteLow, WriteHigh, Volume, SizeHigh, SizeLow, Links, IndexHigh, IndexLow;
    }
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern IntPtr CreateFile(string name, uint access, uint share, IntPtr security,
                                   uint disposition, uint flags, IntPtr template);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool GetFileInformationByHandle(IntPtr handle, out FileInfoNative info);
    [DllImport("kernel32.dll")]
    static extern bool CloseHandle(IntPtr handle);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern IntPtr OpenProcess(uint access, bool inherit, int processId);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool QueryFullProcessImageName(IntPtr process, uint flags, StringBuilder name, ref int length);

    sealed class InstallationLock : IDisposable {
        readonly Mutex mutex;
        bool held;
        internal InstallationLock() {
            string identity = Path.GetFullPath(InstallRoot()).TrimEnd('\\').ToUpperInvariant();
            // Global serializes this per-user destination even across Windows sessions.
            mutex = new Mutex(false, @"Global\ProstranstvoLocalBetaInstaller-" + HashBytes(Encoding.UTF8.GetBytes(identity)));
            try { held = mutex.WaitOne(0); } catch (AbandonedMutexException) { held = true; }
            if (!held) { mutex.Dispose(); throw new IOException("Установка или удаление этой beta уже выполняется. Дождитесь завершения другого окна."); }
        }
        public void Dispose() { if (held) { held = false; mutex.ReleaseMutex(); } mutex.Dispose(); }
    }

    static string Hex(byte[] bytes) { return BitConverter.ToString(bytes).Replace("-", "").ToLowerInvariant(); }
    static string Hash(Stream stream) { using (var sha = SHA256.Create()) return Hex(sha.ComputeHash(stream)); }
    static string HashFile(string file) { using (var input = File.OpenRead(file)) return Hash(input); }
    static string HashBytes(byte[] data) { using (var stream = new MemoryStream(data, false)) return Hash(stream); }
    static string HashBounded(Stream stream, long expected) {
        using (var sha = SHA256.Create()) {
            var buffer = new byte[65536]; long total = 0; int count;
            while ((count = stream.Read(buffer, 0, buffer.Length)) > 0) {
                total += count;
                if (total > expected) throw new InvalidDataException("Распакованный файл превышает заявленный размер.");
                sha.TransformBlock(buffer, 0, count, buffer, 0);
            }
            if (total != expected) throw new InvalidDataException("Распакованный файл неполон.");
            sha.TransformFinalBlock(new byte[0], 0, 0);
            return Hex(sha.Hash);
        }
    }

    internal static string RelativeName(string raw) {
        if (String.IsNullOrEmpty(raw) || raw.Length > 500 || raw[0] == '/' || raw[0] == '\\')
            throw new InvalidDataException("Недопустимое имя файла в пакете.");
        string name = raw.Replace('\\', '/');
        if (name.EndsWith("/", StringComparison.Ordinal)) name = name.Substring(0, name.Length - 1);
        foreach (string part in name.Split('/')) {
            if (part.Length == 0 || part == "." || part == ".." || part.EndsWith(".") || part.EndsWith(" "))
                throw new InvalidDataException("Неоднозначный путь в пакете.");
            foreach (char c in part) if (c < 32 || ":<>\"|?*".IndexOf(c) >= 0)
                throw new InvalidDataException("Недопустимый символ пути.");
            string stem = part.Split('.')[0].ToUpperInvariant();
            if (stem == "CON" || stem == "PRN" || stem == "AUX" || stem == "NUL" ||
                Regex.IsMatch(stem, "^(COM|LPT)[1-9¹²³]$"))
                throw new InvalidDataException("Служебное имя Windows в пакете.");
        }
        return name;
    }

    static bool IsInside(string path, string root) {
        string clean = Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
        return Path.GetFullPath(path).StartsWith(clean, StringComparison.OrdinalIgnoreCase);
    }

    static void CheckPath(string path) {
        string current = Path.GetFullPath(path);
        while (!String.IsNullOrEmpty(current)) {
            if (Directory.Exists(current) || File.Exists(current)) {
                FileAttributes attributes = File.GetAttributes(current);
                if ((attributes & FileAttributes.ReparsePoint) != 0)
                    throw new IOException("Установка через ссылки и junction запрещена.");
                if ((attributes & FileAttributes.Directory) == 0) {
                    IntPtr handle = CreateFile(current, 0x80, 7, IntPtr.Zero, 3, 0x00200000, IntPtr.Zero);
                    if (handle == new IntPtr(-1)) throw new IOException("Не удалось проверить файл установки.");
                    try {
                        FileInfoNative info;
                        if (!GetFileInformationByHandle(handle, out info) || info.Links != 1)
                            throw new IOException("Жёсткие ссылки в папке установки запрещены.");
                    } finally { CloseHandle(handle); }
                }
            }
            DirectoryInfo parent = Directory.GetParent(current);
            current = parent == null ? null : parent.FullName;
        }
    }

    static void CheckTree(string root) {
        var pending = new Stack<string>();
        pending.Push(root);
        int count = 0;
        while (pending.Count > 0) {
            string directory = pending.Pop();
            CheckPath(directory);
            foreach (string item in Directory.GetFileSystemEntries(directory)) {
                if (++count > MaxFiles * 2) throw new IOException("Неожиданно большая папка предыдущей установки.");
                CheckPath(item);
                if (Directory.Exists(item)) pending.Push(item);
            }
        }
    }

    static string LocalBase() {
        string value = Environment.GetEnvironmentVariable("LOCALAPPDATA");
        if (String.IsNullOrWhiteSpace(value)) value = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        if (!Path.IsPathRooted(value) || value.StartsWith(@"\\", StringComparison.Ordinal) || value.IndexOf(':') != 1)
            throw new IOException("Нужен локальный каталог учётной записи Windows.");
        string result = Path.GetFullPath(value);
        if (String.Equals(result.TrimEnd('\\'), Path.GetPathRoot(result).TrimEnd('\\'), StringComparison.OrdinalIgnoreCase))
            throw new IOException("Корень диска не является каталогом пользователя.");
        CheckPath(result);
        return result;
    }
    internal static string InstallRoot() { return Path.Combine(LocalBase(), "Programs", "ProstranstvoLocalBeta"); }
    internal static string AppRoot() { return Path.Combine(InstallRoot(), "app"); }
    internal static string DataRoot() { return Path.Combine(LocalBase(), "ProstranstvoLocalBeta", "data"); }

    static string ChildPath(string root, string name) {
        string result = Path.GetFullPath(Path.Combine(root, RelativeName(name).Replace('/', Path.DirectorySeparatorChar)));
        if (!IsInside(result, root)) throw new IOException("Выход за пределы каталога установки.");
        CheckPath(result);
        return result;
    }

    static Dictionary<string, object> ObjectValue(object value) {
        var result = value as Dictionary<string, object>;
        if (result == null) throw new InvalidDataException("Неверный формат manifest.json.");
        return result;
    }

    static Plan ParseManifest(byte[] bytes) {
        if (bytes.Length == 0 || bytes.Length > MaxManifest) throw new InvalidDataException("Неверный размер manifest.json.");
        var document = ObjectValue(Json.DeserializeObject(new UTF8Encoding(false, true).GetString(bytes)));
        if (!document.ContainsKey("product") || !String.Equals(document["product"] as string, Product, StringComparison.Ordinal))
            throw new InvalidDataException("Пакет относится к другому продукту.");
        string version = document.ContainsKey("version") ? document["version"] as string : null;
        if (version == null || !Regex.IsMatch(version, "^[0-9A-Za-z._-]{1,64}$"))
            throw new InvalidDataException("Неверная версия пакета.");
        var files = ObjectValue(document.ContainsKey("files") ? document["files"] : null);
        if (files.Count < 2 || files.Count > MaxFiles) throw new InvalidDataException("Неверное число файлов.");
        var plan = new Plan { Version = version, ManifestBytes = bytes };
        foreach (var pair in files) {
            string name = RelativeName(pair.Key);
            if (String.Equals(name, "manifest.json", StringComparison.OrdinalIgnoreCase) ||
                String.Equals(name, MarkerName, StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("Служебный файл не может входить в список полезной нагрузки.");
            var details = ObjectValue(pair.Value);
            object sizeValue;
            if (!details.TryGetValue("size", out sizeValue) || !(sizeValue is int || sizeValue is long))
                throw new InvalidDataException("Неверный размер файла в manifest.json.");
            long size = Convert.ToInt64(sizeValue);
            string sha = details.ContainsKey("sha256") ? details["sha256"] as string : null;
            if (size < 0 || size > MaxFile || sha == null || !Regex.IsMatch(sha, "^[0-9a-fA-F]{64}$"))
                throw new InvalidDataException("Неверная контрольная сумма или размер файла.");
            if (plan.Files.ContainsKey(name)) throw new InvalidDataException("Повтор имени файла без учёта регистра.");
            plan.Files.Add(name, new FileSpec { Name = name, Size = size, Sha = sha.ToLowerInvariant() });
            plan.Total = checked(plan.Total + size);
            if (plan.Total > MaxTotal) throw new InvalidDataException("Пакет превышает допустимый размер.");
        }
        foreach (string name in plan.Files.Keys) {
            string parent = name;
            while (parent.LastIndexOf('/') >= 0) {
                parent = parent.Substring(0, parent.LastIndexOf('/'));
                if (plan.Files.ContainsKey(parent)) throw new InvalidDataException("Файл одновременно используется как каталог.");
            }
        }
        if (!plan.Files.ContainsKey("ProstranstvoCore.exe") || !plan.Files.ContainsKey("Prostranstvo.exe"))
            throw new InvalidDataException("В пакете отсутствует основная программа.");
        return plan;
    }

    static byte[] ReadBounded(Stream input, int limit) {
        using (var output = new MemoryStream()) {
            byte[] buffer = new byte[8192];
            int n;
            while ((n = input.Read(buffer, 0, buffer.Length)) > 0) {
                if (output.Length + n > limit) throw new InvalidDataException("Превышен допустимый размер данных.");
                output.Write(buffer, 0, n);
            }
            return output.ToArray();
        }
    }

    static Plan ValidateArchive(ZipArchive archive) {
        if (archive.Entries.Count < 3 || archive.Entries.Count > MaxFiles * 2 + 1)
            throw new InvalidDataException("Неверное число элементов ZIP.");
        ZipArchiveEntry manifest = null;
        var names = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var entry in archive.Entries) {
            string name = RelativeName(entry.FullName);
            if (!names.Add(name)) throw new InvalidDataException("Повтор пути ZIP.");
            int unixType = (entry.ExternalAttributes >> 16) & 0xF000;
            if (unixType == 0xA000 || (entry.ExternalAttributes & 0x400) != 0)
                throw new InvalidDataException("Ссылки в ZIP запрещены.");
            if (String.Equals(name, "manifest.json", StringComparison.Ordinal)) manifest = entry;
        }
        if (manifest == null || manifest.Length > MaxManifest) throw new InvalidDataException("Нет manifest.json.");
        byte[] body;
        using (var stream = manifest.Open()) body = ReadBounded(stream, MaxManifest);
        Plan plan = ParseManifest(body);
        int files = 0;
        foreach (var entry in archive.Entries) {
            string name = RelativeName(entry.FullName);
            if (name == "manifest.json") continue;
            bool directory = entry.FullName.EndsWith("/", StringComparison.Ordinal) || entry.FullName.EndsWith("\\", StringComparison.Ordinal);
            if (directory) {
                if (entry.Length != 0 || plan.Files.ContainsKey(name)) throw new InvalidDataException("Неверный каталог ZIP.");
                continue;
            }
            FileSpec expected;
            if (!plan.Files.TryGetValue(name, out expected) || entry.Length != expected.Size)
                throw new InvalidDataException("Файл ZIP отсутствует в manifest.json или имеет неверный размер.");
            using (var stream = entry.Open()) if (HashBounded(stream, expected.Size) != expected.Sha)
                throw new InvalidDataException("Контрольная сумма файла ZIP не совпала.");
            files++;
        }
        if (files != plan.Files.Count) throw new InvalidDataException("Пакет неполон.");
        return plan;
    }

    static Stream Payload() {
        Stream stream = Assembly.GetExecutingAssembly().GetManifestResourceStream("payload.zip");
        if (stream == null) throw new InvalidDataException("В этой сборке нет установочного пакета.");
        return stream;
    }

    static Plan InspectPayload() {
        using (Stream input = Payload()) using (var archive = new ZipArchive(input, ZipArchiveMode.Read, false))
            return ValidateArchive(archive);
    }

    static void CheckOwnedInstallation(string app) {
        CheckPath(app);
        string marker = ChildPath(app, MarkerName);
        string manifest = ChildPath(app, "manifest.json");
        if (!File.Exists(marker) || !File.Exists(manifest) || new FileInfo(marker).Length > 65536 || new FileInfo(manifest).Length > MaxManifest)
            throw new IOException("Папка app занята неизвестными файлами. Они сохранены без изменений.");
        var identity = ObjectValue(Json.DeserializeObject(File.ReadAllText(marker, Encoding.UTF8)));
        if (!identity.ContainsKey("product") || !String.Equals(identity["product"] as string, Product, StringComparison.Ordinal) ||
            !identity.ContainsKey("manifest_sha256") || !String.Equals(identity["manifest_sha256"] as string, HashFile(manifest), StringComparison.Ordinal))
            throw new IOException("Не удалось подтвердить принадлежность предыдущей установки.");
        ParseManifest(File.ReadAllBytes(manifest));
        CheckTree(app);
    }

    static void RefuseRunning(string app) {
        foreach (Process process in Process.GetProcesses()) {
            string path = null; int id = 0;
            try {
                id = process.Id;
                path = process.MainModule.FileName;
            } catch (InvalidOperationException) { }
              catch (Win32Exception) { }
              catch (NotSupportedException) { }
            finally { process.Dispose(); }
            if (path == null && id != 0) {
                IntPtr handle = OpenProcess(0x1000, false, id); // QUERY_LIMITED_INFORMATION
                if (handle != IntPtr.Zero) {
                    try {
                        var name = new StringBuilder(32768); int length = name.Capacity;
                        if (QueryFullProcessImageName(handle, 0, name, ref length)) path = name.ToString();
                    } finally { CloseHandle(handle); }
                }
            }
            if (path != null && IsInside(path, app))
                throw new InvalidOperationException("Сначала закройте Пространство Local Beta и его видимые агенты. Процесс ещё работает: " + id + ".");
        }
    }

    static void PrepareUninstall(string app) {
        // The marker is ownership/integrity metadata, not a signature. Anchor the
        // helper and its runtime to this installer's embedded payload before running.
        CheckOwnedInstallation(app);
        byte[] installedManifest = File.ReadAllBytes(ChildPath(app, "manifest.json"));
        Plan installed = ParseManifest(installedManifest), plan = InspectPayload();
        if (installed.Version != plan.Version || HashBytes(installedManifest) != HashBytes(plan.ManifestBytes))
            throw new IOException("Для удаления используйте установщик именно этой сборки beta. Текущий пакет отличается от установленного; его helper не запускался.");
        foreach (FileSpec expected in plan.Files.Values) {
            string file = ChildPath(app, expected.Name);
            if (!File.Exists(file) || new FileInfo(file).Length != expected.Size || HashFile(file) != expected.Sha)
                throw new IOException("Файлы beta изменены. Отключите её автозапуск и расписания вручную; автоматическое удаление остановлено.");
        }
        var pending = new Stack<string>(); pending.Push(app);
        while (pending.Count > 0) {
            foreach (string item in Directory.GetFileSystemEntries(pending.Pop())) {
                CheckPath(item);
                if (Directory.Exists(item)) { pending.Push(item); continue; }
                string relative = item.Substring(app.TrimEnd('\\').Length + 1).Replace('\\', '/');
                if (relative != "manifest.json" && relative != MarkerName && !plan.Files.ContainsKey(relative))
                    throw new IOException("В папке beta есть неизвестные файлы. Они сохранены; подготовка удаления не запускалась.");
            }
        }
        var start = new ProcessStartInfo(ChildPath(app, "ProstranstvoCore.exe"), "prepare-uninstall") {
            UseShellExecute = false, CreateNoWindow = true, WorkingDirectory = app,
            RedirectStandardOutput = true, RedirectStandardError = true
        };
        using (var process = new Process { StartInfo = start }) {
            process.OutputDataReceived += delegate { }; process.ErrorDataReceived += delegate { };
            if (!process.Start()) throw new IOException("Не удалось отключить фоновые функции beta.");
            process.BeginOutputReadLine(); process.BeginErrorReadLine();
            if (!process.WaitForExit(60000))
                throw new IOException("Подготовка beta ещё выполняется. Удаление остановлено; процесс не завершался принудительно. Подождите и повторите.");
            process.WaitForExit();
            if (process.ExitCode != 0)
                throw new IOException("Не удалось безопасно отключить автозапуск и расписания beta. Откройте приложение, отключите их вручную и повторите.");
        }
    }

    static void MoveChecked(string from, string to, string root) {
        // Every directory move is limited to this explicitly verified product root.
        if (!IsInside(from, root) || !IsInside(to, root)) throw new IOException("Перенос вне папки продукта запрещён.");
        CheckPath(from); CheckPath(to);
        if (!Directory.Exists(from) || Directory.Exists(to) || File.Exists(to)) throw new IOException("Неожиданное состояние папок установки.");
        Directory.Move(from, to);
    }

    static string BackupPath(string root, string prefix) {
        string backups = ChildPath(root, "backups");
        Directory.CreateDirectory(backups); CheckPath(backups);
        return ChildPath(root, "backups/" + prefix + "-" + DateTime.UtcNow.ToString("yyyyMMddTHHmmssZ") + "-" + Guid.NewGuid().ToString("N").Substring(0, 8));
    }

    static Dictionary<string, object> Install(bool shortcut) {
        using (var guard = new InstallationLock()) return InstallLocked(shortcut);
    }
    static Dictionary<string, object> InstallLocked(bool shortcut) {
        Plan plan = InspectPayload();
        string root = InstallRoot(), app = AppRoot();
        CheckPath(root); CheckPath(app);
        if (File.Exists(root) || File.Exists(app)) throw new IOException("Путь установки занят файлом.");
        if (Directory.Exists(app)) { CheckOwnedInstallation(app); RefuseRunning(app); }
        var drive = new DriveInfo(Path.GetPathRoot(root));
        if (drive.AvailableFreeSpace < plan.Total + 64L * 1024 * 1024) throw new IOException("Недостаточно свободного места.");
        Directory.CreateDirectory(root); CheckPath(root);
        string stage = ChildPath(root, "stage-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(stage); CheckPath(stage);
        using (Stream input = Payload()) using (var archive = new ZipArchive(input, ZipArchiveMode.Read, false)) {
            ValidateArchive(archive);
            foreach (var entry in archive.Entries) {
                string name = RelativeName(entry.FullName);
                bool directory = entry.FullName.EndsWith("/") || entry.FullName.EndsWith("\\");
                if (directory) { Directory.CreateDirectory(ChildPath(stage, name)); continue; }
                string target = ChildPath(stage, name);
                Directory.CreateDirectory(Path.GetDirectoryName(target));
                CheckPath(target);
                using (var source = entry.Open()) using (var output = new FileStream(target, FileMode.CreateNew, FileAccess.Write, FileShare.None)) {
                    byte[] buffer = new byte[65536]; long total = 0; int count;
                    long expected = name == "manifest.json" ? plan.ManifestBytes.Length : plan.Files[name].Size;
                    while ((count = source.Read(buffer, 0, buffer.Length)) > 0) {
                        total += count;
                        if (total > expected) throw new InvalidDataException("ZIP изменился во время извлечения.");
                        output.Write(buffer, 0, count);
                    }
                    if (total != expected) throw new InvalidDataException("Не удалось полностью извлечь файл.");
                    output.Flush(true);
                }
                if (name != "manifest.json" && HashFile(target) != plan.Files[name].Sha)
                    throw new InvalidDataException("Контрольная сумма установленного файла не совпала.");
            }
        }
        var marker = new Dictionary<string, object> { {"product", Product}, {"version", plan.Version},
            {"manifest_sha256", HashBytes(plan.ManifestBytes)}, {"installed_utc", DateTime.UtcNow.ToString("o")} };
        File.WriteAllText(ChildPath(stage, MarkerName), Json.Serialize(marker), new UTF8Encoding(false));
        CheckTree(stage);
        string backup = null;
        if (Directory.Exists(app)) {
            CheckOwnedInstallation(app); RefuseRunning(app);
            backup = BackupPath(root, "app");
            MoveChecked(app, backup, root);
        }
        try { MoveChecked(stage, app, root); }
        catch {
            if (backup != null && !Directory.Exists(app)) MoveChecked(backup, app, root);
            throw;
        }
        bool shortcutMade = false;
        if (shortcut) { try { shortcutMade = UpdateShortcut(app, false); } catch { } }
        return new Dictionary<string, object> { {"status", "installed"}, {"product", Product}, {"version", plan.Version},
            {"app", app}, {"data", DataRoot()}, {"backup", backup}, {"files", plan.Files.Count},
            {"shortcut", shortcutMade}, {"data_preserved", true}, {"network_used", false} };
    }

    static object ComGet(object target, string name) { return target.GetType().InvokeMember(name, BindingFlags.GetProperty, null, target, null); }
    static void ComSet(object target, string name, object value) { target.GetType().InvokeMember(name, BindingFlags.SetProperty, null, target, new object[] { value }); }
    static bool UpdateShortcut(string app, bool remove) {
        string directory = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.StartMenu), "Programs");
        string path = Path.Combine(directory, ShortcutName);
        CheckPath(directory); CheckPath(path);
        if (remove && !File.Exists(path)) return false;
        Directory.CreateDirectory(directory);
        Type type = Type.GetTypeFromProgID("WScript.Shell", true);
        object shell = Activator.CreateInstance(type), link = null;
        try {
            link = type.InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, shell, new object[] { path });
            string target = Path.Combine(app, "Prostranstvo.exe");
            if (File.Exists(path) && !String.Equals(Path.GetFullPath(Convert.ToString(ComGet(link, "TargetPath"))), target, StringComparison.OrdinalIgnoreCase)) return false;
            if (remove) { File.Delete(path); return true; }
            ComSet(link, "TargetPath", target); ComSet(link, "WorkingDirectory", app);
            ComSet(link, "Description", "Пространство Local Beta — локальный помощник");
            link.GetType().InvokeMember("Save", BindingFlags.InvokeMethod, null, link, null);
            return File.Exists(path);
        } finally {
            if (link != null && Marshal.IsComObject(link)) Marshal.FinalReleaseComObject(link);
            if (Marshal.IsComObject(shell)) Marshal.FinalReleaseComObject(shell);
        }
    }

    static Dictionary<string, object> Uninstall() {
        using (var guard = new InstallationLock()) return UninstallLocked();
    }
    static Dictionary<string, object> UninstallLocked() {
        string root = InstallRoot(), app = AppRoot();
        if (!Directory.Exists(app)) return new Dictionary<string, object> { {"status", "not_installed"}, {"data_preserved", true} };
        CheckOwnedInstallation(app); RefuseRunning(app); PrepareUninstall(app); RefuseRunning(app);
        string backup = BackupPath(root, "removed-app");
        RefuseRunning(app);
        MoveChecked(app, backup, root);
        try { UpdateShortcut(app, true); } catch { }
        return new Dictionary<string, object> { {"status", "removed_to_backup"}, {"app_backup", backup},
            {"data", DataRoot()}, {"data_preserved", true}, {"background_settings_changed", true} };
    }

    static void ExpectReject(Action action) {
        bool refused = false;
        try { action(); } catch (InvalidDataException) { refused = true; }
        if (!refused) throw new Exception("Unsafe test input was accepted");
    }
    static MemoryStream TestArchive(byte[] manifest, byte[] core, byte[] gui, string extra, byte[] extraBody) {
        var memory = new MemoryStream();
        using (var archive = new ZipArchive(memory, ZipArchiveMode.Create, true)) {
            foreach (var pair in new Dictionary<string, byte[]> { {"manifest.json", manifest}, {"ProstranstvoCore.exe", core}, {"Prostranstvo.exe", gui} })
                using (var stream = archive.CreateEntry(pair.Key).Open()) stream.Write(pair.Value, 0, pair.Value.Length);
            if (extra != null) using (var stream = archive.CreateEntry(extra).Open()) stream.Write(extraBody, 0, extraBody.Length);
        }
        memory.Position = 0; return memory;
    }
    static void SelfTest() {
        int checks = 0;
        foreach (string value in new[] { "../escape", "/absolute", "a/../b", "a//b", "C:/x", "NUL.txt", "a/CON", "COM¹", "a./b", "a /b", "x:y", "a?b", "a\u0000b" }) {
            string copy = value; ExpectReject(delegate { RelativeName(copy); }); checks++;
        }
        if (RelativeName("_internal/lib/file.dll") != "_internal/lib/file.dll") throw new Exception("Safe path refused");
        checks++;
        byte[] core = Encoding.UTF8.GetBytes("synthetic-core"), gui = Encoding.UTF8.GetBytes("synthetic-gui");
        var files = new Dictionary<string, object> {
            {"ProstranstvoCore.exe", new Dictionary<string, object>{{"size",core.Length},{"sha256",HashBytes(core)}}},
            {"Prostranstvo.exe", new Dictionary<string, object>{{"size",gui.Length},{"sha256",HashBytes(gui)}}}
        };
        byte[] body = Encoding.UTF8.GetBytes(Json.Serialize(new Dictionary<string, object>{{"product",Product},{"version","0.1-test"},{"files",files}}));
        using (var memory = TestArchive(body, core, gui, null, null)) {
            using (var archive = new ZipArchive(memory, ZipArchiveMode.Read, true)) if (ValidateArchive(archive).Files.Count != 2) throw new Exception("Valid archive refused");
            checks++;
        }
        ExpectReject(delegate { ParseManifest(Encoding.UTF8.GetBytes("{\"product\":\"other\",\"version\":\"0.1\",\"files\":{}}")); }); checks++;
        byte[] changed = (byte[])core.Clone(); changed[0] ^= 1;
        using (var memory = TestArchive(body, changed, gui, null, null)) {
            using (var archive = new ZipArchive(memory, ZipArchiveMode.Read, true)) ExpectReject(delegate { ValidateArchive(archive); });
            checks++;
        }
        foreach (string extra in new[] { "../escape.exe", "PROSTRANSTVO.EXE", "unlisted.exe", "manifest.json" }) {
            using (var memory = TestArchive(body, core, gui, extra, gui))
            using (var archive = new ZipArchive(memory, ZipArchiveMode.Read, true)) ExpectReject(delegate { ValidateArchive(archive); });
            checks++;
        }
        using (var memory = TestArchive(body, core, gui, "link", new byte[0])) {
            using (var archive = new ZipArchive(memory, ZipArchiveMode.Update, true)) archive.GetEntry("link").ExternalAttributes = unchecked((int)0xA1FF0000);
            memory.Position=0;
            using (var archive = new ZipArchive(memory, ZipArchiveMode.Read, true)) ExpectReject(delegate { ValidateArchive(archive); });
            checks++;
        }
        using (var memory = new MemoryStream(core, false)) ExpectReject(delegate { HashBounded(memory, core.Length - 1); }); checks++;
        Console.WriteLine(Json.Serialize(new Dictionary<string, object>{{"status","self_test_ok"},{"checks",checks},{"writes",false},{"network",false}}));
    }

    sealed class InstallerWindow : Form {
        readonly Label status;
        readonly Button install, remove;
        readonly CheckBox shortcut, launch;
        bool busy;
        internal InstallerWindow() {
            Text="Пространство Local Beta — установка"; Size=new Size(820,640); MinimumSize=Size;
            StartPosition=FormStartPosition.CenterScreen; Font=new Font("Segoe UI",11);
            var body=new TableLayoutPanel { Dock=DockStyle.Fill,Padding=new Padding(24),ColumnCount=1,RowCount=8 };
            body.Controls.Add(new Label {Text="ПРОСТРАНСТВО · LOCAL BETA",Font=new Font("Segoe UI",21,FontStyle.Bold),AutoSize=true},0,0);
            body.Controls.Add(new Label {Text="Установка только в твою учётную запись. Онлайн-база, сервер и SYSTEM-служба не создаются.",AutoSize=true,MaximumSize=new Size(740,0),Padding=new Padding(0,12,0,12)},0,1);
            body.Controls.Add(new Label {Text="Программа:\n"+AppRoot()+"\n\nЛокальные данные:\n"+DataRoot(),AutoSize=true,MaximumSize=new Size(740,0)},0,2);
            body.Controls.Add(new Label {Text="Обновление сохранит прежнюю программу в backups. Удаление уберёт программу в резервную папку; личные данные останутся. Перед операцией приостанови функции, отключи их автозапуск/расписания, закрой окна, агенты и MCP beta.\n\nЭто неподписанная тестовая сборка. Проверяй источник и SHA-256; защита Windows не отключается.",AutoSize=true,MaximumSize=new Size(740,0),Padding=new Padding(0,14,0,10)},0,3);
            shortcut=new CheckBox {Text="Создать ярлык в меню «Пуск»",Checked=true,AutoSize=true};body.Controls.Add(shortcut,0,4);
            launch=new CheckBox {Text="Открыть программу после установки",Checked=true,AutoSize=true};body.Controls.Add(launch,0,5);
            var bar=new FlowLayoutPanel {AutoSize=true,Dock=DockStyle.Fill,Padding=new Padding(0,12,0,8)};
            install=new Button {Text="Установить / обновить",AutoSize=true,Padding=new Padding(10)};
            remove=new Button {Text="Удалить beta",AutoSize=true,Padding=new Padding(10)};
            install.Click+=delegate { StartWork(false); };remove.Click+=delegate {StartWork(true);};
            bar.Controls.Add(install);bar.Controls.Add(remove);body.Controls.Add(bar,0,6);
            status=new Label {Text="Готово к проверке пакета.",AutoSize=true,MaximumSize=new Size(740,0)};body.Controls.Add(status,0,7);
            Controls.Add(body);FormClosing+=delegate(object sender,FormClosingEventArgs e){if(busy)e.Cancel=true;};
        }
        void StartWork(bool uninstall) {
            if(busy)return;
            if(uninstall&&!MessageBox.Show(this,"Убрать установленную beta в резервную папку? Данные сохранятся. Автозапуск и расписания должны быть заранее выключены в приложении.","Удалить beta?",MessageBoxButtons.YesNo,MessageBoxIcon.Question).Equals(DialogResult.Yes))return;
            busy=true;install.Enabled=false;remove.Enabled=false;status.Text=uninstall?"Проверяю предыдущую установку…":"Проверяю и устанавливаю пакет…";
            bool createShortcut=shortcut.Checked,openAfter=launch.Checked;
            var worker=new BackgroundWorker();
            worker.DoWork+=delegate(object sender,DoWorkEventArgs e){e.Result=uninstall?Uninstall():Install(createShortcut);};
            worker.RunWorkerCompleted+=delegate(object sender,RunWorkerCompletedEventArgs e){
                busy=false;install.Enabled=true;remove.Enabled=true;
                if(e.Error!=null){status.Text="Операция остановлена. Личные данные не удалялись.";MessageBox.Show(this,e.Error.Message,"Пространство",MessageBoxButtons.OK,MessageBoxIcon.Warning);return;}
                var result=(Dictionary<string,object>)e.Result;
                status.Text=uninstall?"Beta убрана в резервную папку. Локальные данные сохранены.":"Установка завершена. Сбор и подключения включаются отдельно в приложении.";
                if(!uninstall&&openAfter){try{Process.Start(new ProcessStartInfo(Path.Combine(AppRoot(),"Prostranstvo.exe")){UseShellExecute=false,WorkingDirectory=AppRoot()});}catch{MessageBox.Show(this,"Открой Prostranstvo.exe из папки app.","Установка завершена");}}
            };
            worker.RunWorkerAsync();
        }
    }

    [STAThread]
    static int Main(string[] args) {
        try {
            if (args.Length > 0)
                Console.SetOut(new StreamWriter(Console.OpenStandardOutput(), new UTF8Encoding(false)) { AutoFlush = true });
            if(args.Length==1&&args[0]=="--self-test"){SelfTest();return 0;}
            if(args.Length==1&&args[0]=="--inspect-payload") {
                Plan plan=InspectPayload();Console.WriteLine(Json.Serialize(new Dictionary<string,object>{{"status","payload_ok"},{"product",Product},{"version",plan.Version},{"files",plan.Files.Count},{"uncompressed_bytes",plan.Total},{"writes",false}}));return 0;
            }
            if(args.Length==1&&args[0]=="--install"){Console.WriteLine(Json.Serialize(Install(false)));return 0;}
            if(args.Length>=1&&args[0]=="--uninstall") {
                bool yes=args.Length==2&&args[1]=="--yes";
                if(args.Length>2||(args.Length==2&&!yes))return 2;
                if(!yes&&MessageBox.Show("Убрать beta в резервную папку, сохранив данные? Перед этим выключи beta-автозапуск и расписания и закрой приложение.","Удалить Пространство Local Beta",MessageBoxButtons.YesNo)!=DialogResult.Yes)return 3;
                Console.WriteLine(Json.Serialize(Uninstall()));return 0;
            }
            if(args.Length!=0)return 2;
            Application.EnableVisualStyles();Application.SetCompatibleTextRenderingDefault(false);Application.Run(new InstallerWindow());return 0;
        } catch(Exception error) {
            if(args.Length>0)Console.WriteLine(Json.Serialize(new Dictionary<string,object>{{"status","error"},{"message",error.Message}}));
            else MessageBox.Show(error.Message,"Пространство Local Beta — установка не выполнена",MessageBoxButtons.OK,MessageBoxIcon.Warning);
            return 1;
        }
    }
}
