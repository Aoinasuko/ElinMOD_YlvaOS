using System;
using System.Collections.Concurrent;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using BepInEx.Logging;
using Newtonsoft.Json;

namespace YlvaOS
{
    internal sealed class YlvaVmBackend
    {
        private readonly ManualLogSource log;
        private readonly string rootDirectory;
        private readonly string pluginDirectory;
        private readonly ConcurrentQueue<string> outputQueue = new ConcurrentQueue<string>();
        private readonly object processLock = new object();
        private Process process;
        private YlvaVmConfig config;
        private string lastMessage;
        private string guestUserName = "ylva";
        private string guestPassword = string.Empty;
        private string controlToken = string.Empty;
        private YlvaControlServer controlServer;
        private YlvaAudioServer audioServer;
        private YlvaHostInputServer hostInputServer;
        private YlvaQmpClient qmpClient;
        private YlvaVncClient vncClient;
        private int vncDisplay = -1;
        private int vncPort;
        private int qmpPort;
        private bool exitedSinceLastStart;
        private bool networkConnected;

        public YlvaVmBackend(string rootDirectory, string pluginDirectory, ManualLogSource log)
        {
            this.rootDirectory = rootDirectory;
            this.pluginDirectory = pluginDirectory;
            this.log = log;
            ConfigPath = Path.Combine(rootDirectory, "vmconfig.json");
            LoadConfig();
            EnsureLayout();
        }

        public string ConfigPath { get; private set; }

        public YlvaVmConfig Config
        {
            get { return config; }
        }

        public int VncPort
        {
            get { return vncPort; }
        }

        public int QmpPort
        {
            get { return qmpPort; }
        }

        public bool IsRunning
        {
            get
            {
                lock (processLock)
                {
                    return process != null && !process.HasExited;
                }
            }
        }

        public YlvaVmStatusSnapshot Snapshot()
        {
            YlvaVmPaths paths = ResolvePaths();
            return new YlvaVmStatusSnapshot
            {
                Status = GetStatus(paths),
                Message = lastMessage ?? string.Empty,
                MemoryMiB = config.MemoryMiB,
                DiskMiB = config.DiskMiB,
                DesktopWidth = config.DesktopWidth,
                DesktopHeight = config.DesktopHeight,
                DesktopRefreshFps = config.DesktopRefreshFps,
                VncPort = vncPort,
                DiskPath = paths.DiskPath,
                KernelPath = paths.KernelPath,
                InitrdPath = paths.InitrdPath,
                QemuSystemPath = paths.QemuSystemPath
            };
        }

        public string DescribePaths()
        {
            YlvaVmPaths paths = ResolvePaths();
            StringBuilder builder = new StringBuilder();
            builder.AppendLine("YlvaOS VM paths:");
            builder.AppendLine("  config: " + ConfigPath);
            builder.AppendLine("  assets: " + paths.AssetsDirectory);
            builder.AppendLine("  tools:  " + paths.ToolsDirectory);
            builder.AppendLine("  disk:   " + paths.DiskPath + "  " + (File.Exists(paths.DiskPath) ? "[OK]" : "[missing]"));
            builder.AppendLine("  import: " + paths.ImportDirectory + "  [read-only guest drive]");
            builder.AppendLine("  update: " + paths.UpdateDirectory + "  " + (Directory.Exists(paths.UpdateDirectory) ? "[OK]" : "[missing]"));
            builder.AppendLine("Required files:");
            builder.AppendLine("  " + paths.QemuSystemPath + "  " + (File.Exists(paths.QemuSystemPath) ? "[OK]" : "[missing]"));
            builder.AppendLine("  " + paths.QemuImgPath + "  " + (File.Exists(paths.QemuImgPath) ? "[OK]" : "[missing]"));
            builder.AppendLine("  " + paths.KernelPath + "  " + (File.Exists(paths.KernelPath) ? "[OK]" : "[missing]"));
            builder.AppendLine("  " + paths.InitrdPath + "  " + (File.Exists(paths.InitrdPath) ? "[OK]" : "[missing]"));
            return builder.ToString().TrimEnd();
        }

        public YlvaProvisioningResult PrepareStartupAssets()
        {
            YlvaProvisioningResult result = new YlvaProvisioningResult
            {
                RootDirectory = rootDirectory
            };

            try
            {
                EnsureLayout();
                YlvaVmPaths paths = ResolvePaths();
                WriteSetupReadme(paths);
                result.Actions.Add("Ensured LocalLow/YlvaOS VM directories.");

                int copiedTools = CopyDirectoryIfPresent(
                    Path.Combine(pluginDirectory ?? string.Empty, "Tools", "qemu"),
                    paths.ToolsDirectory);
                if (copiedTools > 0)
                {
                    result.Actions.Add("Copied " + copiedTools + " QEMU file(s) from the MOD package.");
                }

                int copiedAssets = CopyDirectoryIfPresent(
                    Path.Combine(pluginDirectory ?? string.Empty, "vm", "assets"),
                    paths.AssetsDirectory,
                    overwriteChanged: true);
                copiedAssets += CopyDirectoryIfPresent(
                    Path.Combine(pluginDirectory ?? string.Empty, "Assets", "vm"),
                    paths.AssetsDirectory,
                    overwriteChanged: true);
                if (copiedAssets > 0)
                {
                    result.Actions.Add("Copied " + copiedAssets + " Linux boot asset file(s) from the MOD package.");
                }

                paths = ResolvePaths();
                string diskAction = CopyBundledRootDiskIfMissing(paths);
                if (!string.IsNullOrEmpty(diskAction))
                {
                    result.Actions.Add(diskAction);
                }

                if (File.Exists(paths.DiskPath) && File.Exists(paths.QemuImgPath))
                {
                    EnsureDiskSize(paths);
                }

                CollectMissing(paths, result);
                result.Ready = result.Missing.Count == 0 && result.Errors.Count == 0;
                result.PathsText = DescribePaths();
                lastMessage = result.Ready ? "YlvaOS VM assets are ready." : "YlvaOS VM assets are incomplete.";
            }
            catch (Exception ex)
            {
                result.Errors.Add(ex.Message);
                result.PathsText = DescribePaths();
                result.Ready = false;
                lastMessage = "YlvaOS VM provisioning failed: " + ex.Message;
                if (log != null)
                {
                    log.LogError("YlvaOS VM provisioning failed: " + ex);
                }
            }

            return result;
        }

        public void SaveConfig()
        {
            config.Normalize();
            Directory.CreateDirectory(rootDirectory);
            File.WriteAllText(ConfigPath, JsonConvert.SerializeObject(config, Formatting.Indented));
        }

        public void SetMemoryMiB(int memoryMiB)
        {
            config.MemoryMiB = memoryMiB;
            config.Normalize();
            SaveConfig();
        }

        public void SetDiskMiB(int diskMiB)
        {
            if (diskMiB < config.DiskMiB)
            {
                throw new YlvaUserException("disk size can only be expanded");
            }

            config.DiskMiB = diskMiB;
            config.Normalize();
            SaveConfig();
        }

        public void SetDesktopSize(int width, int height)
        {
            config.DesktopWidth = width;
            config.DesktopHeight = height;
            config.Normalize();
            SaveConfig();
        }

        public void SetDesktopRefreshFps(int fps)
        {
            config.DesktopRefreshFps = fps;
            config.Normalize();
            SaveConfig();
        }

        public void SetAutoStart(bool enabled)
        {
            config.AutoStartAfterLogin = enabled;
            SaveConfig();
        }

        public void AcceptStartupWarning()
        {
            config.StartupWarningAccepted = true;
            SaveConfig();
        }

        public void SetGuestLogin(string userName, string password)
        {
            guestUserName = NormalizeGuestUserName(userName);
            guestPassword = password ?? string.Empty;
        }

        public bool EnsureDesktopClient()
        {
            if (!IsRunning || vncPort <= 0)
            {
                return false;
            }

            if (vncClient == null)
            {
                vncClient = new YlvaVncClient(log);
            }

            return vncClient.TryConnect("127.0.0.1", vncPort, 80);
        }

        public void DisconnectDesktopClient()
        {
            if (vncClient == null)
            {
                return;
            }

            vncClient.Dispose();
            vncClient = null;
        }

        public bool TryCopyDesktopFrame(out int width, out int height, out byte[] rgba)
        {
            if (!EnsureDesktopClient())
            {
                width = 0;
                height = 0;
                rgba = null;
                return false;
            }

            return vncClient.TryCopyFrame(out width, out height, out rgba);
        }

        public void SendDesktopKey(uint keySym, bool down)
        {
            if (EnsureDesktopClient())
            {
                vncClient.SendKey(keySym, down);
            }
        }

        public void SendDesktopPointer(int x, int y, int buttonMask)
        {
            if (EnsureDesktopClient())
            {
                vncClient.SendPointer(x, y, buttonMask);
            }
        }

        public bool TryPasteTextToDesktop(string text, out string message)
        {
            if (!IsRunning)
            {
                message = "VM is not running.";
                return false;
            }

            if (hostInputServer == null)
            {
                message = "YlvaOS host input channel is unavailable.";
                return false;
            }

            string normalized = (text ?? string.Empty).Replace("\r\n", "\n").Replace('\r', '\n');
            if (normalized.Length == 0)
            {
                message = "paste text is empty.";
                return false;
            }

            string payload = Convert.ToBase64String(Encoding.UTF8.GetBytes(normalized));
            return hostInputServer.TrySendCommand("paste-b64 " + payload, out message);
        }

        public void FillAudio(float[] output)
        {
            if (audioServer != null)
            {
                audioServer.Fill(output);
                return;
            }

            if (output != null)
            {
                Array.Clear(output, 0, output.Length);
            }
        }

        public int DrainControlMessages(Action<string> handle, int maxMessages)
        {
            if (controlServer == null)
            {
                return 0;
            }

            return controlServer.DrainMessages(handle, maxMessages);
        }

        public bool ConsumeExitedSinceLastStart()
        {
            lock (processLock)
            {
                if (!exitedSinceLastStart)
                {
                    return false;
                }

                exitedSinceLastStart = false;
                return true;
            }
        }

        public bool TryStart(out string message)
        {
            lock (processLock)
            {
                if (process != null && !process.HasExited)
                {
                    message = "VM is already running.";
                    return true;
                }
            }

            YlvaVmPaths paths = ResolvePaths();
            string validationError = ValidateStart(paths);
            if (validationError != null)
            {
                lastMessage = validationError;
                message = validationError;
                return false;
            }

            try
            {
                ClearOutputQueue();
                exitedSinceLastStart = false;
                DisposeSideChannels();
                controlToken = CreateToken();
                controlServer = new YlvaControlServer(controlToken, log);
                controlServer.Start();
                audioServer = new YlvaAudioServer(log);
                audioServer.Start();
                hostInputServer = new YlvaHostInputServer(controlToken, log);
                hostInputServer.Start();
                vncDisplay = FindAvailableVncDisplay();
                vncPort = 5900 + vncDisplay;
                qmpPort = FindAvailableTcpPort();
                networkConnected = false;
                EnsureDiskSize(paths);
                ProcessStartInfo info = CreateQemuStartInfo(paths);
                Process next = new Process();
                next.StartInfo = info;
                next.EnableRaisingEvents = true;
                next.Exited += OnExited;

                if (!next.Start())
                {
                    message = "QEMU did not start.";
                    lastMessage = message;
                    return false;
                }

                System.Threading.Tasks.Task.Run(() => ReadStream(next.StandardOutput.BaseStream, false));
                System.Threading.Tasks.Task.Run(() => ReadStream(next.StandardError.BaseStream, true));

                lock (processLock)
                {
                    process = next;
                }

                WarmQmpConnection();

                message = "Starting YlvaOS VM with " + config.MemoryMiB + " MiB RAM, " + config.DiskMiB + " MiB disk, and VNC display :" + vncDisplay + ".";
                lastMessage = message;
                return true;
            }
            catch (Exception ex)
            {
                DisposeSideChannels();
                lastMessage = "Failed to start VM: " + ex.Message;
                if (log != null)
                {
                    log.LogError("Failed to start YlvaOS VM: " + ex);
                }

                message = lastMessage;
                return false;
            }
        }

        public void Stop()
        {
            lock (processLock)
            {
                if (process == null)
                {
                    return;
                }

                try
                {
                    if (!process.HasExited)
                    {
                        process.StandardInput.WriteLine("poweroff");
                        if (!process.WaitForExit(1500))
                        {
                            process.Kill();
                        }
                    }
                }
                catch (Exception ex)
                {
                    if (log != null)
                    {
                        log.LogWarning("Failed to stop YlvaOS VM cleanly: " + ex);
                    }
                }
                finally
                {
                    DisposeSideChannels();
                    process.Dispose();
                    process = null;
                }
            }
        }

        public void SendLine(string line)
        {
            SendRaw((line ?? string.Empty) + "\r\n");
        }

        public void SendRaw(string text)
        {
            lock (processLock)
            {
                if (process == null || process.HasExited)
                {
                    throw new YlvaUserException("VM is not running");
                }

                process.StandardInput.Write(text ?? string.Empty);
                process.StandardInput.Flush();
            }
        }

        public bool TryConnectNetwork(out string message)
        {
            int port;
            lock (processLock)
            {
                if (process == null || process.HasExited)
                {
                    message = "VM is not running.";
                    lastMessage = message;
                    return false;
                }

                if (networkConnected)
                {
                    message = "YlvaOS network is already connected.";
                    lastMessage = message;
                    return true;
                }

                port = qmpPort;
            }

            if (port <= 0)
            {
                message = "QMP control port is unavailable.";
                lastMessage = message;
                return false;
            }

            if (qmpClient == null)
            {
                qmpClient = new YlvaQmpClient();
            }

            string qmpMessage;
            if (!qmpClient.TryConnect("127.0.0.1", port, 5000, out qmpMessage))
            {
                message = "Failed to connect to QMP: " + qmpMessage;
                lastMessage = message;
                return false;
            }

            if (!qmpClient.TryEnableUserNetwork(out qmpMessage))
            {
                message = "Failed to enable YlvaOS network: " + qmpMessage;
                lastMessage = message;
                return false;
            }

            networkConnected = true;
            message = qmpMessage;
            lastMessage = message;
            return true;
        }

        private void WarmQmpConnection()
        {
            if (qmpPort <= 0)
            {
                return;
            }

            try
            {
                if (qmpClient == null)
                {
                    qmpClient = new YlvaQmpClient();
                }

                string message;
                if (!qmpClient.TryConnect("127.0.0.1", qmpPort, 1000, out message) && log != null)
                {
                    log.LogWarning("YlvaOS QMP preconnect failed: " + message);
                }
            }
            catch (Exception ex)
            {
                if (log != null)
                {
                    log.LogWarning("YlvaOS QMP preconnect failed: " + ex.Message);
                }
            }
        }

        public int DrainOutput(Action<string> append, int maxLines)
        {
            int count = 0;
            string line;
            while (count < maxLines && outputQueue.TryDequeue(out line))
            {
                append(line);
                count++;
            }

            return count;
        }

        private void LoadConfig()
        {
            try
            {
                Directory.CreateDirectory(rootDirectory);
                if (File.Exists(ConfigPath))
                {
                    config = JsonConvert.DeserializeObject<YlvaVmConfig>(File.ReadAllText(ConfigPath)) ?? new YlvaVmConfig();
                }
                else
                {
                    config = new YlvaVmConfig();
                }
            }
            catch (Exception ex)
            {
                if (log != null)
                {
                    log.LogWarning("Failed to load VM config: " + ex);
                }

                config = new YlvaVmConfig();
            }

            config.Normalize();
            SaveConfig();
        }

        private void EnsureLayout()
        {
            YlvaVmPaths paths = ResolvePaths();
            Directory.CreateDirectory(rootDirectory);
            Directory.CreateDirectory(paths.VmDirectory);
            Directory.CreateDirectory(paths.AssetsDirectory);
            Directory.CreateDirectory(paths.ToolsDirectory);
            Directory.CreateDirectory(paths.ImportDirectory);
        }

        private void WriteSetupReadme(YlvaVmPaths paths)
        {
            string text =
                "YlvaOS VM setup\n" +
                "\n" +
                "Place QEMU for Windows files in:\n" +
                paths.ToolsDirectory + "\n" +
                "\n" +
                "Required QEMU files:\n" +
                "- qemu-system-x86_64.exe\n" +
                "- qemu-img.exe\n" +
                "- any DLL files shipped with that QEMU build\n" +
                "\n" +
                "Place Linux boot assets in:\n" +
                paths.AssetsDirectory + "\n" +
                "\n" +
                "Required boot assets:\n" +
                "- vmlinuz\n" +
                "- initrd.img\n" +
                "\n" +
                "Place the preinstalled Linux root disk in the MOD package before first launch:\n" +
                "- Mod_YlvaOS/vm/disk.qcow2\n" +
                "- or Mod_YlvaOS/vm/disk.qcow2.gz\n" +
                "\n" +
                "On first launch it is copied or decompressed to:\n" +
                paths.DiskPath + "\n" +
                "\n" +
                "Files placed here are exposed to the guest as a read-only import drive:\n" +
                paths.ImportDirectory + "\n" +
                "\n" +
                "Bundled YlvaOS update payload exposed read-only to the guest:\n" +
                paths.UpdateDirectory + "\n";

            File.WriteAllText(Path.Combine(rootDirectory, "SETUP.txt"), text);
        }

        private YlvaVmPaths ResolvePaths()
        {
            string vmDirectory = Path.Combine(rootDirectory, "vm");
            string assetsDirectory = Path.Combine(vmDirectory, "assets");
            string toolsDirectory = Path.Combine(rootDirectory, "Tools", "qemu");
            string importDirectory = Path.Combine(rootDirectory, "Import");
            string updateDirectory = PreferExistingDirectory(
                Path.Combine(pluginDirectory ?? string.Empty, "vm", "update"),
                Path.Combine(pluginDirectory ?? string.Empty, "Assets", "vm", "update"));
            string pluginToolsDirectory = Path.Combine(pluginDirectory ?? string.Empty, "Tools", "qemu");
            string qemuSystem = PreferExisting(
                Path.Combine(toolsDirectory, "qemu-system-x86_64.exe"),
                Path.Combine(pluginToolsDirectory, "qemu-system-x86_64.exe"));
            string qemuImg = PreferExisting(
                Path.Combine(toolsDirectory, "qemu-img.exe"),
                Path.Combine(pluginToolsDirectory, "qemu-img.exe"));

            return new YlvaVmPaths
            {
                RootDirectory = rootDirectory,
                ConfigPath = ConfigPath,
                VmDirectory = vmDirectory,
                AssetsDirectory = assetsDirectory,
                ToolsDirectory = toolsDirectory,
                ImportDirectory = importDirectory,
                UpdateDirectory = updateDirectory,
                DiskPath = ConfinedPath(vmDirectory, config.DiskFileName),
                KernelPath = ConfinedPath(assetsDirectory, config.KernelFileName),
                InitrdPath = ConfinedPath(assetsDirectory, config.InitrdFileName),
                QemuSystemPath = qemuSystem,
                QemuImgPath = qemuImg
            };
        }

        private YlvaVmStatus GetStatus(YlvaVmPaths paths)
        {
            if (IsRunning)
            {
                return YlvaVmStatus.Running;
            }

            return ValidateStart(paths) == null ? YlvaVmStatus.Stopped : YlvaVmStatus.MissingAssets;
        }

        private string ValidateStart(YlvaVmPaths paths)
        {
            if (!File.Exists(paths.QemuSystemPath))
            {
                return "QEMU is missing. Place qemu-system-x86_64.exe under " + paths.ToolsDirectory + ".";
            }

            if (!File.Exists(paths.QemuImgPath))
            {
                return "qemu-img is missing. Place qemu-img.exe under " + paths.ToolsDirectory + ".";
            }

            if (!File.Exists(paths.KernelPath))
            {
                return "Linux kernel is missing. Place a YlvaOS-branded vmlinuz at " + paths.KernelPath + ".";
            }

            if (!File.Exists(paths.InitrdPath))
            {
                return "Linux initrd is missing. Place a YlvaOS initrd at " + paths.InitrdPath + ".";
            }

            if (!File.Exists(paths.DiskPath))
            {
                return "Preinstalled YlvaOS root disk is missing. Place disk.qcow2 or disk.qcow2.gz under the MOD package vm folder.";
            }

            return null;
        }

        private void CollectMissing(YlvaVmPaths paths, YlvaProvisioningResult result)
        {
            if (!File.Exists(paths.QemuSystemPath))
            {
                result.Missing.Add("qemu-system-x86_64.exe");
            }

            if (!File.Exists(paths.QemuImgPath))
            {
                result.Missing.Add("qemu-img.exe");
            }

            if (!File.Exists(paths.KernelPath))
            {
                result.Missing.Add("vm/assets/" + config.KernelFileName);
            }

            if (!File.Exists(paths.InitrdPath))
            {
                result.Missing.Add("vm/assets/" + config.InitrdFileName);
            }

            if (!File.Exists(paths.DiskPath))
            {
                result.Missing.Add("vm/" + config.DiskFileName + " or vm/" + config.DiskFileName + ".gz");
            }
        }

        private void EnsureDiskSize(YlvaVmPaths paths)
        {
            if (!File.Exists(paths.DiskPath))
            {
                throw new YlvaUserException("preinstalled YlvaOS root disk is missing");
            }

            RunQemuImg(paths, "resize \"" + paths.DiskPath + "\" " + config.DiskMiB + "M");
        }

        private void RunQemuImg(YlvaVmPaths paths, string arguments)
        {
            ProcessStartInfo info = new ProcessStartInfo
            {
                FileName = paths.QemuImgPath,
                Arguments = arguments,
                CreateNoWindow = true,
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                WorkingDirectory = paths.VmDirectory
            };

            using (Process qemuImg = Process.Start(info))
            {
                string output = qemuImg.StandardOutput.ReadToEnd();
                string error = qemuImg.StandardError.ReadToEnd();
                qemuImg.WaitForExit();
                if (qemuImg.ExitCode != 0)
                {
                    throw new YlvaUserException("qemu-img failed: " + (error.Length > 0 ? error : output));
                }
            }
        }

        private string CopyBundledRootDiskIfMissing(YlvaVmPaths paths)
        {
            if (File.Exists(paths.DiskPath))
            {
                return null;
            }

            string[] sourceDirectories =
            {
                Path.Combine(pluginDirectory ?? string.Empty, "vm"),
                Path.Combine(pluginDirectory ?? string.Empty, "Assets", "vm")
            };

            foreach (string sourceDirectory in sourceDirectories)
            {
                if (string.IsNullOrEmpty(sourceDirectory) || !Directory.Exists(sourceDirectory))
                {
                    continue;
                }

                string sourceDisk = Path.Combine(sourceDirectory, config.DiskFileName);
                if (File.Exists(sourceDisk))
                {
                    Directory.CreateDirectory(Path.GetDirectoryName(paths.DiskPath));
                    File.Copy(sourceDisk, paths.DiskPath, overwrite: false);
                    return "Copied preinstalled YlvaOS root disk from the MOD package.";
                }

                string sourceArchive = sourceDisk + ".gz";
                if (File.Exists(sourceArchive))
                {
                    DecompressGzipToFile(sourceArchive, paths.DiskPath);
                    return "Decompressed preinstalled YlvaOS root disk from the MOD package.";
                }
            }

            return null;
        }

        private static void DecompressGzipToFile(string sourceArchive, string destinationPath)
        {
            Directory.CreateDirectory(Path.GetDirectoryName(destinationPath));
            string temporaryPath = destinationPath + ".tmp";
            if (File.Exists(temporaryPath))
            {
                File.Delete(temporaryPath);
            }

            try
            {
                using (FileStream source = File.OpenRead(sourceArchive))
                using (GZipStream gzip = new GZipStream(source, CompressionMode.Decompress))
                using (FileStream destination = File.Create(temporaryPath))
                {
                    gzip.CopyTo(destination);
                }

                if (File.Exists(destinationPath))
                {
                    File.Delete(temporaryPath);
                    return;
                }

                File.Move(temporaryPath, destinationPath);
            }
            catch
            {
                if (File.Exists(temporaryPath))
                {
                    File.Delete(temporaryPath);
                }

                throw;
            }
        }

        private ProcessStartInfo CreateQemuStartInfo(YlvaVmPaths paths)
        {
            string append = EscapeAppend(config.KernelAppend) + BuildGuestKernelArgs();
            string updateDriveArgument = Directory.Exists(paths.UpdateDirectory)
                ? " -drive file=\"fat:ro:" + ToQemuPath(paths.UpdateDirectory) + "\",if=virtio,format=raw,media=disk,readonly=on"
                : string.Empty;
            string args =
                "-m " + config.MemoryMiB +
                " -machine accel=tcg" +
                " -cpu max" +
                " -smp 2" +
                " -display none" +
                " -vga std" +
                " -vnc 127.0.0.1:" + vncDisplay +
                " -k en-us" +
                " -usb" +
                " -device usb-kbd" +
                " -device usb-tablet" +
                " -serial stdio" +
                " -monitor none" +
                " -qmp tcp:127.0.0.1:" + qmpPort + ",server=on,wait=off" +
                " -net none" +
                " -drive file=\"" + paths.DiskPath + "\",if=virtio,format=qcow2" +
                " -drive file=\"fat:ro:" + ToQemuPath(paths.ImportDirectory) + "\",if=virtio,format=raw,media=disk,readonly=on" +
                updateDriveArgument +
                " -device virtio-serial-pci" +
                " -chardev socket,id=ylva_ctl,host=127.0.0.1,port=" + (controlServer != null ? controlServer.Port : 0) + ",server=off,reconnect-ms=1000" +
                " -device virtserialport,chardev=ylva_ctl,name=org.ylvaos.control" +
                " -chardev socket,id=ylva_audio,host=127.0.0.1,port=" + (audioServer != null ? audioServer.Port : 0) + ",server=off,reconnect-ms=1000" +
                " -device virtserialport,chardev=ylva_audio,name=org.ylvaos.audio" +
                " -chardev socket,id=ylva_hostinput,host=127.0.0.1,port=" + (hostInputServer != null ? hostInputServer.Port : 0) + ",server=off,reconnect-ms=1000" +
                " -device virtserialport,chardev=ylva_hostinput,name=org.ylvaos.hostinput" +
                " -kernel \"" + paths.KernelPath + "\"" +
                " -initrd \"" + paths.InitrdPath + "\"" +
                " -append \"" + append + "\"";

            return new ProcessStartInfo
            {
                FileName = paths.QemuSystemPath,
                Arguments = args,
                CreateNoWindow = true,
                UseShellExecute = false,
                RedirectStandardInput = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                WorkingDirectory = paths.VmDirectory,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8
            };
        }

        private static string ToQemuPath(string path)
        {
            return (path ?? string.Empty).Replace('\\', '/');
        }

        private void ReadStream(Stream stream, bool filterQemuStderr)
        {
            byte[] buffer = new byte[1024];
            char[] chars = new char[2048];
            Decoder decoder = Encoding.UTF8.GetDecoder();
            string pendingLine = string.Empty;
            try
            {
                while (true)
                {
                    int read = stream.Read(buffer, 0, buffer.Length);
                    if (read <= 0)
                    {
                        return;
                    }

                    int charCount = decoder.GetChars(buffer, 0, read, chars, 0, false);
                    if (charCount > 0)
                    {
                        string chunk = new string(chars, 0, charCount);
                        if (!filterQemuStderr)
                        {
                            outputQueue.Enqueue(chunk);
                            continue;
                        }

                        pendingLine += chunk;
                        int newlineIndex;
                        while ((newlineIndex = pendingLine.IndexOf('\n')) >= 0)
                        {
                            string line = pendingLine.Substring(0, newlineIndex + 1);
                            pendingLine = pendingLine.Substring(newlineIndex + 1);
                            if (!ShouldSuppressQemuStderrLine(line))
                            {
                                outputQueue.Enqueue(line);
                            }
                        }
                    }
                }
            }
            catch (Exception ex)
            {
                if (log != null)
                {
                    log.LogWarning("YlvaOS VM stream reader stopped: " + ex.Message);
                }
            }
            finally
            {
                if (filterQemuStderr && pendingLine.Length > 0 && !ShouldSuppressQemuStderrLine(pendingLine))
                {
                    outputQueue.Enqueue(pendingLine);
                }
            }
        }

        private static bool ShouldSuppressQemuStderrLine(string line)
        {
            return line != null &&
                line.IndexOf("GLib: WaitForMultipleObjectsEx failed", StringComparison.OrdinalIgnoreCase) >= 0;
        }

        private void OnExited(object sender, EventArgs e)
        {
            lastMessage = "YlvaOS VM stopped.";
            outputQueue.Enqueue("\n" + lastMessage + "\n");
            DisposeSideChannels();
            lock (processLock)
            {
                exitedSinceLastStart = true;
                if (process != null && object.ReferenceEquals(sender, process))
                {
                    process.Dispose();
                    process = null;
                }
            }
        }

        private void ClearOutputQueue()
        {
            string ignored;
            while (outputQueue.TryDequeue(out ignored))
            {
            }
        }

        private void DisposeSideChannels()
        {
            try
            {
                if (vncClient != null)
                {
                    vncClient.Dispose();
                }
            }
            catch
            {
            }

            try
            {
                if (qmpClient != null)
                {
                    qmpClient.Dispose();
                }
            }
            catch
            {
            }

            try
            {
                if (hostInputServer != null)
                {
                    hostInputServer.Dispose();
                }
            }
            catch
            {
            }

            try
            {
                if (controlServer != null)
                {
                    controlServer.Dispose();
                }
            }
            catch
            {
            }

            qmpClient = null;
            vncClient = null;
            hostInputServer = null;
            controlServer = null;
            try
            {
                if (audioServer != null)
                {
                    audioServer.Dispose();
                }
            }
            catch
            {
            }

            audioServer = null;
            vncDisplay = -1;
            vncPort = 0;
            qmpPort = 0;
            networkConnected = false;
            controlToken = string.Empty;
        }

        private static int FindAvailableTcpPort()
        {
            TcpListener probe = new TcpListener(IPAddress.Loopback, 0);
            try
            {
                probe.Start();
                return ((IPEndPoint)probe.LocalEndpoint).Port;
            }
            finally
            {
                probe.Stop();
            }
        }

        private static int FindAvailableVncDisplay()
        {
            for (int display = 20; display <= 99; display++)
            {
                int port = 5900 + display;
                try
                {
                    TcpListener probe = new TcpListener(IPAddress.Loopback, port);
                    probe.Start();
                    probe.Stop();
                    return display;
                }
                catch (SocketException)
                {
                }
            }

            throw new YlvaUserException("no free localhost VNC port was found");
        }

        private static string CreateToken()
        {
            byte[] bytes = new byte[16];
            using (RandomNumberGenerator generator = RandomNumberGenerator.Create())
            {
                generator.GetBytes(bytes);
            }

            StringBuilder builder = new StringBuilder(bytes.Length * 2);
            foreach (byte value in bytes)
            {
                builder.Append(value.ToString("x2"));
            }

            return builder.ToString();
        }

        private static string PreferExisting(string primary, string secondary)
        {
            if (File.Exists(primary))
            {
                return primary;
            }

            return secondary;
        }

        private static string PreferExistingDirectory(string primary, string secondary)
        {
            if (Directory.Exists(primary))
            {
                return primary;
            }

            return secondary;
        }

        private static int CopyDirectoryIfPresent(string sourceDirectory, string destinationDirectory, bool overwriteChanged = false)
        {
            if (string.IsNullOrEmpty(sourceDirectory) || !Directory.Exists(sourceDirectory))
            {
                return 0;
            }

            Directory.CreateDirectory(destinationDirectory);
            int copied = 0;
            foreach (string sourcePath in Directory.GetFiles(sourceDirectory, "*", SearchOption.AllDirectories))
            {
                string relative = sourcePath.Substring(sourceDirectory.Length).TrimStart(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
                string destinationPath = Path.Combine(destinationDirectory, relative);
                Directory.CreateDirectory(Path.GetDirectoryName(destinationPath));

                if (File.Exists(destinationPath))
                {
                    if (!overwriteChanged || FilesAreEqual(sourcePath, destinationPath))
                    {
                        continue;
                    }
                }

                File.Copy(sourcePath, destinationPath, overwriteChanged);
                copied++;
            }

            return copied;
        }

        private static bool FilesAreEqual(string left, string right)
        {
            FileInfo leftInfo = new FileInfo(left);
            FileInfo rightInfo = new FileInfo(right);
            if (leftInfo.Length != rightInfo.Length)
            {
                return false;
            }

            using (SHA256 sha = SHA256.Create())
            using (FileStream leftStream = File.OpenRead(left))
            using (FileStream rightStream = File.OpenRead(right))
            {
                byte[] leftHash = sha.ComputeHash(leftStream);
                byte[] rightHash = sha.ComputeHash(rightStream);
                if (leftHash.Length != rightHash.Length)
                {
                    return false;
                }

                int diff = 0;
                for (int i = 0; i < leftHash.Length; i++)
                {
                    diff |= leftHash[i] ^ rightHash[i];
                }

                return diff == 0;
            }
        }

        private static string ConfinedPath(string directory, string fileName)
        {
            string safeName = Path.GetFileName(fileName ?? string.Empty);
            if (string.IsNullOrEmpty(safeName))
            {
                safeName = "unnamed";
            }

            return Path.Combine(directory, safeName);
        }

        private static string EscapeAppend(string append)
        {
            return (append ?? string.Empty).Replace("\"", string.Empty);
        }

        private string BuildGuestKernelArgs()
        {
            string passwordBase64 = Convert.ToBase64String(Encoding.UTF8.GetBytes(guestPassword ?? string.Empty));
            return
                " ylva_user=" + NormalizeGuestUserName(guestUserName) +
                " ylva_password_b64=" + passwordBase64 +
                " ylva_rows=" + YlvaTerminalBuffer.DefaultRows +
                " ylva_cols=" + YlvaTerminalBuffer.DefaultColumns +
                " ylva_control_token=" + controlToken +
                " ylva_desktop_width=" + config.DesktopWidth +
                " ylva_desktop_height=" + config.DesktopHeight;
        }

        private static string NormalizeGuestUserName(string userName)
        {
            if (string.IsNullOrEmpty(userName))
            {
                return "ylva";
            }

            StringBuilder builder = new StringBuilder();
            foreach (char ch in userName.ToLowerInvariant())
            {
                if ((ch >= 'a' && ch <= 'z') || (ch >= '0' && ch <= '9') || ch == '_' || ch == '-')
                {
                    builder.Append(ch);
                }
            }

            if (builder.Length == 0 || !((builder[0] >= 'a' && builder[0] <= 'z') || builder[0] == '_'))
            {
                return "ylva";
            }

            return builder.Length > 32 ? builder.ToString(0, 32) : builder.ToString();
        }
    }
}
