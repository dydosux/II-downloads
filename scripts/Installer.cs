using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Net;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

public sealed class Part {
    public string name { get; set; }
    public long size { get; set; }
    public string sha256 { get; set; }
}
public sealed class Payload {
    public string version { get; set; }
    public string baseUrl { get; set; }
    public long installedBytes { get; set; }
    public int fileCount { get; set; }
    public Part[] parts { get; set; }
}

// Seek directly across downloaded parts: no second, multi-gigabyte ZIP copy.
public sealed class PartStream : Stream {
    readonly string[] paths;
    readonly long[] starts;
    readonly long length;
    long position;
    FileStream current;
    int openIndex = -1;
    public PartStream(string folder, Part[] parts) {
        paths = parts.Select(p => Path.Combine(folder, p.name)).ToArray();
        starts = new long[parts.Length];
        for (int i=0; i<parts.Length; i++) { starts[i] = length; length += parts[i].size; }
    }
    public override bool CanRead { get { return true; } }
    public override bool CanSeek { get { return true; } }
    public override bool CanWrite { get { return false; } }
    public override long Length { get { return length; } }
    public override long Position { get { return position; } set { Seek(value, SeekOrigin.Begin); } }
    public override long Seek(long offset, SeekOrigin origin) {
        long next = origin == SeekOrigin.Begin ? offset : origin == SeekOrigin.Current ? position + offset : length + offset;
        if (next < 0 || next > length) throw new IOException("Invalid archive offset");
        return position = next;
    }
    public override int Read(byte[] buffer, int offset, int count) {
        if (position == length || count == 0) return 0;
        int i = Array.BinarySearch(starts, position);
        if (i < 0) i = ~i - 1;
        if (i != openIndex) {
            if (current != null) current.Dispose();
            current = File.OpenRead(paths[i]); openIndex = i;
        }
        current.Position = position - starts[i];
        int read = current.Read(buffer, offset, (int)Math.Min(count, current.Length - current.Position));
        if (read == 0) throw new EndOfStreamException("Incomplete archive part");
        position += read;
        return read;
    }
    protected override void Dispose(bool disposing) { if (disposing && current != null) current.Dispose(); base.Dispose(disposing); }
    public override void Flush() { }
    public override void SetLength(long value) { throw new NotSupportedException(); }
    public override void Write(byte[] buffer, int offset, int count) { throw new NotSupportedException(); }
}

public sealed class Engine {
    public readonly Payload Manifest;
    public readonly string Identity;
    readonly Action<string, double> report;
    readonly CancellationToken token;
    readonly long[] received;
    readonly object progressLock = new object();
    long lastTick;
    readonly Stopwatch watch = new Stopwatch();
    long networkBytes;
    public Engine(string json, Action<string, double> progress, CancellationToken cancellation) {
        Manifest = new JavaScriptSerializer().Deserialize<Payload>(json);
        using (var hash = SHA256.Create()) Identity = Hex(hash.ComputeHash(Encoding.UTF8.GetBytes(json))).Substring(0, 16);
        if (Manifest == null || Manifest.parts == null || Manifest.parts.Length == 0 || Manifest.installedBytes <= 0 || Manifest.fileCount <= 0)
            throw new InvalidDataException("Invalid installer manifest");
        var names = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var p in Manifest.parts) {
            if (p.name != Path.GetFileName(p.name) || p.name.Contains(":") || p.name.Contains("\\") || p.name.Contains("/") || !names.Add(p.name)
                || p.size <= 0 || p.size >= 2L*1024*1024*1024 || p.sha256 == null || !System.Text.RegularExpressions.Regex.IsMatch(p.sha256, "^[0-9a-f]{64}$"))
                throw new InvalidDataException("Invalid archive part");
        }
        report = progress; token = cancellation; received = new long[Manifest.parts.Length];
    }
    public static string Hex(byte[] value) { return BitConverter.ToString(value).Replace("-", "").ToLowerInvariant(); }
    string HashFile(string path) {
        using (var stream = File.OpenRead(path))
        using (var sha = SHA256.Create()) {
            byte[] buffer = new byte[1024*1024]; int n;
            while ((n = stream.Read(buffer, 0, buffer.Length)) > 0) { token.ThrowIfCancellationRequested(); sha.TransformBlock(buffer, 0, n, buffer, 0); }
            sha.TransformFinalBlock(new byte[0], 0, 0); return Hex(sha.Hash);
        }
    }
    void Progress(int i, long value, long added) {
        lock (progressLock) {
            received[i] = value; networkBytes += added;
            if (watch.ElapsedMilliseconds - lastTick < 200) return;
            lastTick = watch.ElapsedMilliseconds;
            long total = Manifest.parts.Sum(p => p.size), done = received.Sum();
            double speed = networkBytes / Math.Max(0.1, watch.Elapsed.TotalSeconds) / 1048576;
            report(String.Format("Загрузка: {0:F2} / {1:F2} ГБ • {2:F2} МБ/с", done/1073741824.0, total/1073741824.0, speed), 75.0*done/total);
        }
    }
    void Download(int i, string cache, string baseUrl) {
        Part p = Manifest.parts[i]; string final = Path.Combine(cache, p.name), temp = final + ".partial";
        if (File.Exists(final)) {
            report("Проверка сохранённой части " + (i+1) + "…", -1);
            if (new FileInfo(final).Length == p.size && HashFile(final) == p.sha256) { Progress(i, p.size, 0); return; }
            File.Delete(final);
        }
        for (int attempt=0; attempt<4; attempt++) {
            token.ThrowIfCancellationRequested();
            try {
                long offset = File.Exists(temp) ? new FileInfo(temp).Length : 0;
                if (offset > p.size) { File.Delete(temp); offset = 0; }
                Progress(i, offset, 0);
                if (offset < p.size) {
                    var request = (HttpWebRequest)WebRequest.Create(baseUrl.TrimEnd('/') + "/" + Uri.EscapeDataString(p.name));
                    request.UserAgent = "II-Installer/1.0"; request.Timeout = 30000; request.ReadWriteTimeout = 30000;
                    request.AutomaticDecompression = DecompressionMethods.None;
                    if (offset > 0) request.AddRange(offset);
                    using (token.Register(() => request.Abort()))
                    using (var response = (HttpWebResponse)request.GetResponse()) {
                        if (response.StatusCode == HttpStatusCode.PartialContent) {
                            string expected = String.Format("bytes {0}-{1}/{2}", offset, p.size-1, p.size);
                            if (response.Headers["Content-Range"] != expected) throw new InvalidDataException("Incorrect resume response");
                        } else if (response.StatusCode == HttpStatusCode.OK) { offset = 0; }
                        else throw new IOException("Unexpected HTTP status " + response.StatusCode);
                        if (response.ContentLength >= 0 && response.ContentLength != p.size-offset) throw new InvalidDataException("Incorrect download size");
                        using (var input = response.GetResponseStream())
                        using (var output = new FileStream(temp, offset == 0 ? FileMode.Create : FileMode.Append, FileAccess.Write, FileShare.None)) {
                            byte[] buffer = new byte[1024*1024]; int n;
                            while ((n = input.Read(buffer, 0, buffer.Length)) > 0) {
                                token.ThrowIfCancellationRequested();
                                if (offset + n > p.size) throw new InvalidDataException("Download exceeds expected size");
                                output.Write(buffer, 0, n); offset += n; Progress(i, offset, n);
                            }
                        }
                    }
                }
                if (new FileInfo(temp).Length != p.size) throw new IOException("Загрузка части прервалась");
                report("Проверка SHA-256: часть " + (i+1) + "…", -1);
                if (HashFile(temp) != p.sha256) { File.Delete(temp); Progress(i, 0, 0); throw new InvalidDataException("Контрольная сумма не совпала; часть будет загружена заново"); }
                File.Move(temp, final); Progress(i, p.size, 0); return;
            } catch (Exception ex) {
                token.ThrowIfCancellationRequested();
                if (!(ex is IOException || ex is WebException || ex is InvalidDataException) || attempt == 3) throw;
                report("Повтор загрузки части " + (i+1) + ": " + ex.Message, -1);
                if (token.WaitHandle.WaitOne(1000*(attempt+1))) token.ThrowIfCancellationRequested();
            }
        }
    }
    public static string SafePath(string root, string entry) {
        string normalized = entry.Replace('/', '\\');
        if (Path.IsPathRooted(normalized) || normalized.Contains(":") || normalized.Split('\\').Any(p => p == ".." || p.EndsWith(".") || p.EndsWith(" ")))
            throw new InvalidDataException("Unsafe archive path");
        string prefix = Path.GetFullPath(root).TrimEnd('\\') + "\\";
        string full = Path.GetFullPath(Path.Combine(prefix, normalized));
        if (!full.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException("Archive path escapes installation folder");
        return full;
    }
    public void Install(string destination, string baseUrl, int parallel, bool cleanup, bool allowLocal) {
        Uri uri;
        if (!Uri.TryCreate(baseUrl, UriKind.Absolute, out uri) || !(uri.Scheme == "https" || (allowLocal && uri.Scheme == "http" && uri.IsLoopback)))
            throw new InvalidDataException("Адрес загрузки должен использовать HTTPS");
        destination = Path.GetFullPath(destination).TrimEnd(Path.DirectorySeparatorChar);
        if (Directory.Exists(destination) || File.Exists(destination)) throw new IOException("Папка назначения уже существует. Выберите новую папку, чтобы сохранить ваши файлы.");
        string parent = Path.GetDirectoryName(destination);
        if (String.IsNullOrEmpty(parent)) throw new IOException("Выберите отдельную папку для приложения");
        Directory.CreateDirectory(parent);
        string cache = Path.Combine(parent, ".II-download-" + Identity);
        Directory.CreateDirectory(cache);
        if ((File.GetAttributes(cache) & FileAttributes.ReparsePoint) != 0) throw new IOException("Download cache cannot be a link");
        long cached = 0;
        foreach (var part in Manifest.parts) {
            long partCached = 0;
            foreach (string suffix in new [] { "", ".partial" }) {
                string file = Path.Combine(cache, part.name + suffix);
                if (File.Exists(file)) {
                    if ((File.GetAttributes(file) & FileAttributes.ReparsePoint) != 0) throw new IOException("Download part cannot be a link");
                    partCached = Math.Max(partCached, Math.Min(new FileInfo(file).Length, part.size));
                }
            }
            cached += partCached;
        }
        long needed = Manifest.installedBytes + Math.Max(0, Manifest.parts.Sum(p => p.size)-cached) + 512L*1024*1024;
        var disk = new DriveInfo(Path.GetPathRoot(parent));
        if (disk.AvailableFreeSpace < needed) throw new IOException(String.Format("Недостаточно места на {0}. Нужно ещё примерно {1:F1} ГБ. Выберите другой диск.", disk.Name, needed/1073741824.0));
        watch.Start();
        Parallel.For(0, Manifest.parts.Length, new ParallelOptions { MaxDegreeOfParallelism = Math.Max(1, Math.Min(4, parallel)), CancellationToken = token }, i => Download(i, cache, baseUrl));
        token.ThrowIfCancellationRequested();
        string stage = Path.Combine(parent, ".II-install-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(stage);
        try {
            long written = 0; int files = 0;
            using (var stream = new PartStream(cache, Manifest.parts))
            using (var zip = new ZipArchive(stream, ZipArchiveMode.Read)) {
                byte[] buffer = new byte[1024*1024];
                foreach (var entry in zip.Entries) {
                    token.ThrowIfCancellationRequested();
                    if (((entry.ExternalAttributes >> 16) & 0xF000) == 0xA000) throw new InvalidDataException("Archive links are not supported");
                    string path = SafePath(stage, entry.FullName);
                    if (entry.FullName.EndsWith("/")) { Directory.CreateDirectory(path); continue; }
                    Directory.CreateDirectory(Path.GetDirectoryName(path));
                    using (var input = entry.Open())
                    using (var output = new FileStream(path, FileMode.CreateNew, FileAccess.Write)) {
                        int n; long entryBytes = 0;
                        while ((n = input.Read(buffer, 0, buffer.Length)) > 0) {
                            token.ThrowIfCancellationRequested();
                            written += n; entryBytes += n;
                            if (written > Manifest.installedBytes || entryBytes > entry.Length) throw new InvalidDataException("Archive exceeds declared size");
                            output.Write(buffer, 0, n);
                        }
                        if (entryBytes != entry.Length) throw new InvalidDataException("Incomplete archive entry");
                    }
                    files++;
                    if (files % 50 == 0) report(String.Format("Распаковка: {0} / {1} файлов", files, Manifest.fileCount), 75 + 24.0*written/Manifest.installedBytes);
                }
            }
            if (written != Manifest.installedBytes || files != Manifest.fileCount || !File.Exists(Path.Combine(stage, "II.exe")))
                throw new InvalidDataException("Сборка неполная; установка отменена");
            using (var license = Assembly.GetExecutingAssembly().GetManifestResourceStream("SD15-LICENSE.txt")) {
                if (license != null) {
                    Directory.CreateDirectory(Path.Combine(stage, "LICENSES"));
                    using (var output = File.Create(Path.Combine(stage, "LICENSES", "Stable-Diffusion-1.5.txt"))) license.CopyTo(output);
                }
            }
            token.ThrowIfCancellationRequested();
            Directory.Move(stage, destination);
        } finally {
            if (Directory.Exists(stage)) { try { Directory.Delete(stage, true); } catch { } }
        }
        if (cleanup) {
            foreach (var p in Manifest.parts) { try { File.Delete(Path.Combine(cache, p.name)); } catch { } }
            try { Directory.Delete(cache, false); } catch { }
        }
        report("Установка завершена", 100);
    }
}

public sealed class InstallerForm : Form {
    readonly TextBox folder = new TextBox();
    readonly Button browse = new Button(), start = new Button(), cancel = new Button();
    readonly Label status = new Label();
    readonly ProgressBar progress = new ProgressBar();
    readonly CheckBox shortcut = new CheckBox(), launch = new CheckBox(), cleanup = new CheckBox();
    readonly NumericUpDown parallel = new NumericUpDown();
    readonly string manifest;
    CancellationTokenSource cancellation;
    bool busy;
    public InstallerForm(string json) {
        manifest = json;
        var payload = new JavaScriptSerializer().Deserialize<Payload>(json);
        Text = "Установка II+"; ClientSize = new Size(620, 405); Font = new Font("Segoe UI", 10);
        FormBorderStyle = FormBorderStyle.FixedDialog; MaximizeBox = false; StartPosition = FormStartPosition.CenterScreen;
        Controls.Add(new Label { Text = "II+ — локальная нейросеть", Font = new Font("Segoe UI", 17, FontStyle.Bold), Location = new Point(24,20), AutoSize = true });
        Controls.Add(new Label { Text = String.Format("Загрузка: {0:F2} ГБ • Приложение: {1:F2} ГБ\nPython и файлы моделей входят в сборку.", payload.parts.Sum(p=>p.size)/1073741824.0, payload.installedBytes/1073741824.0), Location = new Point(24,65), Size = new Size(570,50) });
        var licenseLink = new LinkLabel { Text = "Лицензия модели", Location = new Point(450,120), AutoSize = true };
        licenseLink.LinkClicked += delegate {
            using (var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream("SD15-LICENSE.txt"))
            using (var reader = new StreamReader(stream))
            using (var dialog = new Form { Text = "Stable Diffusion 1.5 — CreativeML Open RAIL-M", Size = new Size(720,540), StartPosition = FormStartPosition.CenterParent }) {
                dialog.Controls.Add(new TextBox { Text = reader.ReadToEnd().Replace("\n", "\r\n"), Multiline = true, ReadOnly = true, ScrollBars = ScrollBars.Vertical, Dock = DockStyle.Fill });
                dialog.ShowDialog(this);
            }
        };
        Controls.Add(licenseLink);
        Controls.Add(new Label { Text = "Папка установки (новая):", Location = new Point(24,122), AutoSize = true });
        folder.SetBounds(24,148,468,28); folder.Text = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", "II-Plus"); Controls.Add(folder);
        browse.Text = "Обзор…"; browse.SetBounds(502,147,92,29); browse.Click += delegate { using (var dialog = new FolderBrowserDialog { Description = "Выберите родительскую папку. В ней будет создана папка II-Plus." }) if (dialog.ShowDialog() == DialogResult.OK) folder.Text = Path.Combine(dialog.SelectedPath, "II-Plus"); }; Controls.Add(browse);
        shortcut.Text = "Создать ярлык на рабочем столе"; shortcut.Checked = true; shortcut.SetBounds(24,188,390,25); Controls.Add(shortcut);
        launch.Text = "Запустить после установки"; launch.Checked = true; launch.SetBounds(24,214,390,25); Controls.Add(launch);
        cleanup.Text = "Удалить загруженные части после установки"; cleanup.Checked = true; cleanup.SetBounds(24,240,400,25); Controls.Add(cleanup);
        Controls.Add(new Label { Text = "Потоков:", Location = new Point(457,190), AutoSize = true });
        parallel.Minimum = 1; parallel.Maximum = 4; parallel.Value = 2; parallel.SetBounds(532,187,62,28); Controls.Add(parallel);
        status.Text = "Готово к установке. При обрыве загрузку можно продолжить."; status.SetBounds(24,276,570,42); Controls.Add(status);
        progress.SetBounds(24,320,570,18); Controls.Add(progress);
        start.Text = "Установить"; start.SetBounds(370,354,115,32); start.Click += async delegate { await Run(); }; Controls.Add(start);
        cancel.Text = "Закрыть"; cancel.SetBounds(495,354,100,32); cancel.Click += delegate { if (busy) { cancellation.Cancel(); status.Text = "Остановка… Скачанные части сохранятся."; cancel.Enabled = false; } else Close(); }; Controls.Add(cancel);
        FormClosing += delegate(object sender, FormClosingEventArgs e) { if (busy) { e.Cancel = true; cancellation.Cancel(); status.Text = "Остановка… Скачанные части сохранятся."; } };
    }
    void Report(string text, double percent) {
        if (!IsDisposed && IsHandleCreated) BeginInvoke((Action)delegate { status.Text = text; if (percent >= 0) progress.Value = Math.Max(0, Math.Min(100, (int)percent)); });
    }
    async Task Run() {
        string target = folder.Text.Trim(); int threads = (int)parallel.Value; bool clean = cleanup.Checked;
        cancellation = new CancellationTokenSource(); busy = true;
        start.Enabled = browse.Enabled = folder.Enabled = parallel.Enabled = shortcut.Enabled = launch.Enabled = cleanup.Enabled = false;
        cancel.Text = "Остановить";
        try {
            var engine = new Engine(manifest, Report, cancellation.Token);
            await Task.Run(() => engine.Install(target, engine.Manifest.baseUrl, threads, clean, false));
            target = Path.GetFullPath(target);
            string note = "";
            if (shortcut.Checked) {
                try { CreateShortcut(target); } catch { note = " Ярлык не создан; запустите II.exe из папки установки."; }
            }
            if (launch.Checked) {
                try { Process.Start(new ProcessStartInfo(Path.Combine(target, "II.exe")) { WorkingDirectory = target }); }
                catch { note += " Приложение можно запустить вручную из папки установки."; }
            }
            status.Text = "Установлено: " + target + note; progress.Value = 100;
            start.Text = "Готово";
        } catch (Exception ex) {
            var aggregate = ex as AggregateException;
            if (aggregate != null) ex = aggregate.Flatten().InnerExceptions[0];
            status.Text = cancellation.IsCancellationRequested ? "Остановлено. Нажмите «Установить», чтобы продолжить." : "Ошибка: " + ex.Message;
            if (!cancellation.IsCancellationRequested) MessageBox.Show(this, ex.Message + "\n\nПроверенные части сохранены. Можно повторить установку в ту же папку.", "Установка II+", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            start.Enabled = browse.Enabled = folder.Enabled = parallel.Enabled = shortcut.Enabled = launch.Enabled = cleanup.Enabled = true;
        } finally { busy = false; cancel.Text = "Закрыть"; cancel.Enabled = true; cancellation.Dispose(); }
    }
    static void CreateShortcut(string target) {
        Type type = Type.GetTypeFromProgID("WScript.Shell"); object shell = Activator.CreateInstance(type);
        string path = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory), "II+.lnk");
        object link = type.InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, shell, new object[] { path });
        try {
            var t = link.GetType();
            t.InvokeMember("TargetPath", BindingFlags.SetProperty, null, link, new object[] { Path.Combine(target, "II.exe") });
            t.InvokeMember("WorkingDirectory", BindingFlags.SetProperty, null, link, new object[] { target });
            t.InvokeMember("Save", BindingFlags.InvokeMethod, null, link, new object[0]);
        } finally { System.Runtime.InteropServices.Marshal.ReleaseComObject(link); System.Runtime.InteropServices.Marshal.ReleaseComObject(shell); }
    }
}

public static class Program {
    [STAThread]
    public static int Main(string[] args) {
        AppContext.SetSwitch("Switch.System.IO.UseLegacyPathHandling", false);
        AppContext.SetSwitch("Switch.System.IO.BlockLongPaths", false);
        ServicePointManager.SecurityProtocol = SecurityProtocolType.Tls12;
        ServicePointManager.DefaultConnectionLimit = 8;
        // Diagnostic mode uses a caller-provided manifest, never launches installed files.
        if (args.Length == 5 && args[0] == "--test-install") {
            try {
                var engine = new Engine(File.ReadAllText(args[1]), (s,p) => Console.WriteLine(s), CancellationToken.None);
                engine.Install(args[2], args[3], 2, false, true);
                File.WriteAllText(args[4], "OK"); return 0;
            } catch (Exception ex) { File.WriteAllText(args[4], ex.ToString()); return 1; }
        }
        bool created;
        using (var mutex = new Mutex(true, "Local\\IIPlusInstaller", out created)) {
            if (!created) { MessageBox.Show("Установщик II+ уже открыт."); return 1; }
            try {
                string json;
                using (var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream("payload.json"))
                using (var reader = new StreamReader(stream)) json = reader.ReadToEnd();
                Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
                Application.Run(new InstallerForm(json)); return 0;
            } catch (Exception ex) { MessageBox.Show(ex.Message, "Установка II+"); return 1; }
        }
    }
}
