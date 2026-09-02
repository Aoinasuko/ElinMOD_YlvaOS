using System;
using System.Collections.Generic;
using System.Security.Cryptography;
using System.Text;

namespace YlvaOS
{
    internal sealed class YlvaMachine
    {
        private const int MaxScreenLines = 300;
        private const int MaxInputLength = 256;
        private const int MaxVmOutputChunksPerPump = 96;

        private readonly YlvaState state;
        private readonly YlvaVmBackend vm;
        private readonly YlvaTerminalBuffer terminalBuffer;

        public YlvaMachine(YlvaState state, YlvaVmBackend vm)
        {
            this.state = state;
            this.state.Normalize();
            this.vm = vm;
            terminalBuffer = new YlvaTerminalBuffer(
                this.state.ScreenLines,
                YlvaTerminalBuffer.DefaultRows,
                YlvaTerminalBuffer.DefaultColumns,
                HandleHostCommand,
                SendTerminalResponse);
        }

        public YlvaState State
        {
            get { return state; }
        }

        public IList<string> History
        {
            get { return state.History; }
        }

        public string WorkingDirectory
        {
            get { return state.WorkingDirectory; }
            set { state.WorkingDirectory = string.IsNullOrEmpty(value) ? "/home/player" : value; }
        }

        public string CurrentInput
        {
            get { return state.CurrentInput ?? string.Empty; }
            set
            {
                if (value == null)
                {
                    state.CurrentInput = string.Empty;
                    return;
                }

                state.CurrentInput = value.Length > MaxInputLength ? value.Substring(0, MaxInputLength) : value;
            }
        }

        public bool CloseRequested { get; private set; }

        public bool IsVmConsoleActive
        {
            get { return IsVmRunning && state.DisplayMode != YlvaDisplayMode.Desktop; }
        }

        public bool IsVmRunning
        {
            get { return state.Phase == YlvaBootPhase.Shell && vm != null && vm.IsRunning; }
        }

        public bool IsDesktopMode
        {
            get { return IsVmRunning && state.DisplayMode == YlvaDisplayMode.Desktop; }
        }

        public YlvaVmBackend Vm
        {
            get { return vm; }
        }

        public string CurrentUserName
        {
            get { return string.IsNullOrEmpty(state.UserName) ? "player" : state.UserName; }
        }

        public bool IsSecretInput
        {
            get { return state.Phase == YlvaBootPhase.SetupPassword || state.Phase == YlvaBootPhase.LoginPassword; }
        }

        public string Prompt
        {
            get
            {
                switch (state.Phase)
                {
                    case YlvaBootPhase.SetupUserName:
                        return "setup username: ";
                    case YlvaBootPhase.SetupPassword:
                        return "setup password: ";
                    case YlvaBootPhase.LoginUserName:
                        return state.HostName + " login: ";
                    case YlvaBootPhase.LoginPassword:
                        return "Password: ";
                    case YlvaBootPhase.Shell:
                        if (vm != null && vm.IsRunning)
                        {
                            return string.Empty;
                        }

                        return CurrentUserName + "@" + state.HostName + ":" + state.WorkingDirectory + "$ ";
                    default:
                        return "> ";
                }
            }
        }

        public string WindowTitle
        {
            get
            {
                if (IsVmConsoleActive)
                {
                    return "YlvaOS 0.1.0 - Real Linux console - " + CurrentUserName;
                }

                if (IsDesktopMode)
                {
                    return "YlvaOS 0.1.0 - Lightweight Desktop - " + CurrentUserName;
                }

                return "YlvaOS 0.1.0 - Login";
            }
        }

        public bool VisibleLinesUseRichText
        {
            get { return terminalBuffer.LastRenderUsedRichText; }
        }

        public void ColdBoot()
        {
            CloseRequested = false;
            state.Normalize();
            state.HasBooted = true;
            state.PoweredOff = false;
            state.Authenticated = false;
            state.PendingUserName = string.Empty;
            state.LastBootUtc = DateTime.UtcNow;
            state.WorkingDirectory = "/";
            state.CurrentInput = string.Empty;
            state.DisplayMode = YlvaDisplayMode.Kernel;
            terminalBuffer.Clear();

            if (!state.SetupComplete)
            {
                state.Phase = YlvaBootPhase.SetupUserName;
                Append("YlvaOS initial setup");
                Append("Create the Linux user used by the YlvaOS VM.");
                Append("Leave username empty to use `player`. Password may also be empty.");
            }
            else
            {
                state.Phase = YlvaBootPhase.LoginUserName;
                Append("YlvaOS login");
            }
        }

        public bool Submit(string command)
        {
            CloseRequested = false;
            command = command ?? string.Empty;

            if (state.Phase == YlvaBootPhase.Shell && vm != null && vm.IsRunning)
            {
                vm.SendLine(command);
                CurrentInput = string.Empty;
                return CloseRequested;
            }

            Append(Prompt + (IsSecretInput ? new string('*', command.Length) : command));

            string trimmed = command.Trim();
            if (state.Phase == YlvaBootPhase.Shell && trimmed.Length > 0)
            {
                state.History.Add(trimmed);
                while (state.History.Count > 80)
                {
                    state.History.RemoveAt(0);
                }
            }

            switch (state.Phase)
            {
                case YlvaBootPhase.SetupUserName:
                    SubmitSetupUserName(trimmed);
                    break;
                case YlvaBootPhase.SetupPassword:
                    SubmitSetupPassword(command);
                    break;
                case YlvaBootPhase.LoginUserName:
                    SubmitLoginUserName(trimmed);
                    break;
                case YlvaBootPhase.LoginPassword:
                    SubmitLoginPassword(command);
                    break;
                case YlvaBootPhase.Shell:
                    ExecuteOfflineCommand(trimmed);
                    break;
                default:
                    state.Phase = state.SetupComplete ? YlvaBootPhase.LoginUserName : YlvaBootPhase.SetupUserName;
                    break;
            }

            CurrentInput = string.Empty;
            return CloseRequested;
        }

        public void Append(string text)
        {
            terminalBuffer.ResetTransientState();
            if (text == null)
            {
                text = string.Empty;
            }

            string[] lines = text.Replace("\r", string.Empty).Split('\n');
            foreach (string line in lines)
            {
                terminalBuffer.AppendPlainLine(line);
            }
        }

        public void ClearScreen()
        {
            terminalBuffer.Clear();
        }

        public List<string> GetVisibleLines(int maxLines)
        {
            return GetVisibleLines(maxLines, false);
        }

        public List<string> GetVisibleLines(int maxLines, bool showCursor)
        {
            return terminalBuffer.GetVisibleLines(maxLines, showCursor);
        }

        public bool PumpExternalOutput()
        {
            if (vm == null)
            {
                return false;
            }

            bool updated = vm.DrainOutput(terminalBuffer.AppendChunk, MaxVmOutputChunksPerPump) > 0;
            updated = vm.DrainControlMessages(HandleHostCommand, 32) > 0 || updated;
            if (vm.ConsumeExitedSinceLastStart())
            {
                state.PoweredOff = true;
                state.Authenticated = false;
                state.DisplayMode = YlvaDisplayMode.Kernel;
                state.Phase = state.SetupComplete ? YlvaBootPhase.LoginUserName : YlvaBootPhase.SetupUserName;
                CloseRequested = true;
                updated = true;
            }

            return updated;
        }

        public void SendRawInput(string text)
        {
            if (vm == null || !vm.IsRunning)
            {
                return;
            }

            vm.SendRaw(text ?? string.Empty);
        }

        public bool TryCopyDesktopFrame(out int width, out int height, out byte[] rgba)
        {
            if (!IsDesktopMode || vm == null)
            {
                width = 0;
                height = 0;
                rgba = null;
                return false;
            }

            return vm.TryCopyDesktopFrame(out width, out height, out rgba);
        }

        public void SendDesktopKey(uint keySym, bool down)
        {
            if (IsVmRunning && vm != null)
            {
                vm.SendDesktopKey(keySym, down);
            }
        }

        public void SendDesktopPointer(int x, int y, int buttonMask)
        {
            if (IsVmRunning && vm != null)
            {
                vm.SendDesktopPointer(x, y, buttonMask);
            }
        }

        public bool TryPasteTextToDesktop(string text, out string message)
        {
            if (IsVmRunning && vm != null)
            {
                return vm.TryPasteTextToDesktop(text, out message);
            }

            message = "VM is not running.";
            return false;
        }

        public void RequestShutdown()
        {
            if (vm != null && vm.IsRunning)
            {
                vm.Stop();
            }

            state.PoweredOff = true;
            state.Authenticated = false;
            state.DisplayMode = YlvaDisplayMode.Kernel;
            state.Phase = state.SetupComplete ? YlvaBootPhase.LoginUserName : YlvaBootPhase.SetupUserName;
            Append(string.Empty);
            Append("[  OK  ] Stopped YlvaOS userspace.");
            Append("[  OK  ] Reached target Power-Off.");
            Append("Power down.");
            CloseRequested = true;
        }

        public void Reboot()
        {
            ColdBoot();
        }

        private void SubmitSetupUserName(string rawUserName)
        {
            string userName;
            string error;
            if (!TryNormalizeUserName(rawUserName, out userName, out error))
            {
                Append("setup: " + error);
                return;
            }

            state.PendingUserName = userName;
            state.Phase = YlvaBootPhase.SetupPassword;
            Append("Creating user `" + userName + "`.");
            Append("Enter login password. Empty password is allowed.");
        }

        private void SubmitSetupPassword(string password)
        {
            string userName = string.IsNullOrEmpty(state.PendingUserName) ? "player" : state.PendingUserName;
            string salt = CreateSalt();
            state.PasswordSalt = salt;
            state.PasswordHash = HashPassword(password ?? string.Empty, salt);
            state.UserName = userName;
            state.SetupComplete = true;
            state.Authenticated = false;
            state.PendingUserName = string.Empty;
            state.WorkingDirectory = "/home/" + userName;

            state.Phase = YlvaBootPhase.Shell;
            StartRealYlvaOs(password ?? string.Empty);
        }

        private void SubmitLoginUserName(string rawUserName)
        {
            string userName = string.IsNullOrEmpty(rawUserName) ? CurrentUserName : rawUserName;
            state.PendingUserName = userName;
            state.Phase = YlvaBootPhase.LoginPassword;
        }

        private void SubmitLoginPassword(string password)
        {
            string userName = string.IsNullOrEmpty(state.PendingUserName) ? CurrentUserName : state.PendingUserName;
            if (userName == CurrentUserName && VerifyPassword(password ?? string.Empty))
            {
                state.Authenticated = true;
                state.PendingUserName = string.Empty;
                state.WorkingDirectory = "/home/" + CurrentUserName;
                state.Phase = YlvaBootPhase.Shell;
                StartRealYlvaOs(password ?? string.Empty);
                return;
            }

            state.Authenticated = false;
            state.PendingUserName = string.Empty;
            state.Phase = YlvaBootPhase.LoginUserName;
            Append("Login incorrect");
        }

        private void ExecuteOfflineCommand(string command)
        {
            if (string.IsNullOrWhiteSpace(command))
            {
                return;
            }

            string[] args = SplitArgs(command);
            string name = args.Length > 0 ? args[0] : string.Empty;
            try
            {
                switch (name)
                {
                    case "help":
                        OfflineHelp();
                        return;
                    case "clear":
                        ClearScreen();
                        return;
                    case "vm":
                        OfflineVm(args);
                        return;
                    case "YlvaOS":
                        OfflineYlvaOs(args);
                        return;
                    case "reboot":
                        Reboot();
                        return;
                    case "shutdown":
                    case "poweroff":
                    case "exit":
                        RequestShutdown();
                        return;
                    default:
                        Append(name + ": real Linux VM is not running");
                        Append("Use `vm start` to retry booting, or `vm paths` to see required files.");
                        return;
                }
            }
            catch (YlvaUserException ex)
            {
                Append(name + ": " + ex.Message);
            }
            catch (Exception ex)
            {
                if (Plugin.Log != null)
                {
                    Plugin.Log.LogWarning("YlvaOS offline command failed: " + ex);
                }

                Append(name + ": " + ex.Message);
            }
        }

        private void OfflineHelp()
        {
            Append("YlvaOS host-side commands available before the real Linux VM starts:");
            Append("  vm status | vm paths | vm start | vm stop");
            Append("  YlvaOS set memory <MiB> | YlvaOS set disk <MiB>");
            Append("  clear | reboot | shutdown | poweroff | exit");
            Append("After the VM boots, Linux commands run inside the guest OS.");
        }

        private void OfflineVm(string[] args)
        {
            if (vm == null)
            {
                Append("VM backend is unavailable.");
                return;
            }

            string subcommand = args.Length > 1 ? args[1] : "status";
            switch (subcommand)
            {
                case "status":
                    AppendVmStatus();
                    return;
                case "paths":
                    Append(vm.DescribePaths());
                    return;
                case "start":
                    StartRealYlvaOs(string.Empty);
                    return;
                case "stop":
                    vm.Stop();
                    Append("VM stopped.");
                    return;
                default:
                    throw new YlvaUserException("usage: vm status | vm paths | vm start | vm stop");
            }
        }

        private void OfflineYlvaOs(string[] args)
        {
            if (vm == null)
            {
                Append("VM backend is unavailable.");
                return;
            }

            if (args.Length == 2 && args[1] == "status")
            {
                AppendVmStatus();
                return;
            }

            if (args.Length == 4 && args[1] == "set" && (args[2] == "memory" || args[2] == "mem"))
            {
                vm.SetMemoryMiB(ParsePositiveInt(args[3], "memory"));
                Append("YlvaOS memory target set to " + vm.Config.MemoryMiB + " MiB. Reboot YlvaOS to apply.");
                return;
            }

            if (args.Length == 4 && args[1] == "set" && args[2] == "disk")
            {
                vm.SetDiskMiB(ParsePositiveInt(args[3], "disk"));
                Append("YlvaOS disk target set to " + vm.Config.DiskMiB + " MiB. Reboot YlvaOS to apply.");
                return;
            }

            throw new YlvaUserException("usage: YlvaOS status | YlvaOS set memory <MiB> | YlvaOS set disk <MiB>");
        }

        private void AppendVmStatus()
        {
            if (vm == null)
            {
                Append("VM backend is unavailable.");
                return;
            }

            YlvaVmStatusSnapshot snapshot = vm.Snapshot();
            Append("VM status: " + snapshot.Status);
            Append("Memory: " + snapshot.MemoryMiB + " MiB");
            Append("Disk target: " + snapshot.DiskMiB + " MiB");
            Append("Desktop: " + snapshot.DesktopWidth + "x" + snapshot.DesktopHeight + " @ " + snapshot.DesktopRefreshFps + " fps");
            if (snapshot.VncPort > 0)
            {
                Append("VNC: 127.0.0.1:" + snapshot.VncPort);
            }

            if (!string.IsNullOrEmpty(snapshot.Message))
            {
                Append(snapshot.Message);
            }
        }

        private static string[] SplitArgs(string command)
        {
            List<string> args = new List<string>();
            StringBuilder current = new StringBuilder();
            bool quoted = false;
            for (int i = 0; i < command.Length; i++)
            {
                char ch = command[i];
                if (ch == '"')
                {
                    quoted = !quoted;
                    continue;
                }

                if (char.IsWhiteSpace(ch) && !quoted)
                {
                    if (current.Length > 0)
                    {
                        args.Add(current.ToString());
                        current.Length = 0;
                    }

                    continue;
                }

                current.Append(ch);
            }

            if (current.Length > 0)
            {
                args.Add(current.ToString());
            }

            return args.ToArray();
        }

        private static bool TryNormalizeUserName(string raw, out string userName, out string error)
        {
            userName = string.IsNullOrWhiteSpace(raw) ? "player" : raw.Trim().ToLowerInvariant();
            error = null;

            if (userName.Length > 32)
            {
                error = "username must be 32 characters or less";
                return false;
            }

            char first = userName[0];
            if (!((first >= 'a' && first <= 'z') || first == '_'))
            {
                error = "username must start with a lowercase letter or underscore";
                return false;
            }

            foreach (char ch in userName)
            {
                if (!((ch >= 'a' && ch <= 'z') || (ch >= '0' && ch <= '9') || ch == '_' || ch == '-'))
                {
                    error = "username may contain only lowercase letters, digits, underscore, and hyphen";
                    return false;
                }
            }

            return true;
        }

        private void StartRealYlvaOs(string password)
        {
            terminalBuffer.Clear();
            state.DisplayMode = YlvaDisplayMode.Kernel;

            if (vm == null)
            {
                Append("VM backend is unavailable.");
                return;
            }

            vm.SetGuestLogin(CurrentUserName, password ?? string.Empty);
            string message;
            if (vm.TryStart(out message))
            {
                Append(message);
                return;
            }

            Append(message);
            Append("Use `vm paths` to see where QEMU and Linux boot assets must be placed.");
            Append("The real Linux VM cannot start until those files exist.");
        }

        private void HandleHostCommand(string command)
        {
            if (string.IsNullOrWhiteSpace(command) || vm == null)
            {
                return;
            }

            string[] parts = command.Trim().Split(new[] { ' ' }, StringSplitOptions.RemoveEmptyEntries);
            try
            {
                if (parts.Length == 2 && parts[0] == "mode")
                {
                    if (parts[1] == "desktop" || parts[1] == "desktop-ready")
                    {
                        state.DisplayMode = YlvaDisplayMode.Desktop;
                        return;
                    }

                    if (parts[1] == "desktop-starting")
                    {
                        state.DisplayMode = YlvaDisplayMode.DesktopStarting;
                        Append("Starting YlvaOS Desktop...");
                        return;
                    }

                    if (parts[1] == "kernel")
                    {
                        state.DisplayMode = YlvaDisplayMode.Kernel;
                        vm.DisconnectDesktopClient();
                        Append("Returned to YlvaOS kernel console.");
                        return;
                    }
                }

                if (parts.Length == 3 && parts[0] == "set" && (parts[1] == "memory" || parts[1] == "mem"))
                {
                    vm.SetMemoryMiB(ParsePositiveInt(parts[2], "memory"));
                    return;
                }

                if (parts.Length == 3 && parts[0] == "set" && parts[1] == "disk")
                {
                    vm.SetDiskMiB(ParsePositiveInt(parts[2], "disk"));
                    return;
                }

                if (parts.Length == 2 && parts[0] == "network" && parts[1] == "connect")
                {
                    string message;
                    if (!vm.TryConnectNetwork(out message))
                    {
                        Append(message);
                        SendTerminalResponse("\r\n" + message + "\r\n");
                        if (Plugin.Log != null)
                        {
                            Plugin.Log.LogWarning(message);
                        }

                        return;
                    }

                    Append(message);
                    return;
                }

                if (Plugin.Log != null)
                {
                    Plugin.Log.LogWarning("Unknown YlvaOS host command: " + command);
                }
            }
            catch (Exception ex)
            {
                if (Plugin.Log != null)
                {
                    Plugin.Log.LogWarning("YlvaOS host command failed: " + command + " (" + ex.Message + ")");
                }
            }
        }

        private void SendTerminalResponse(string text)
        {
            try
            {
                if (vm != null && vm.IsRunning)
                {
                    vm.SendRaw(text);
                }
            }
            catch (Exception ex)
            {
                if (Plugin.Log != null)
                {
                    Plugin.Log.LogWarning("Failed to send terminal response: " + ex.Message);
                }
            }
        }

        private static int ParsePositiveInt(string value, string name)
        {
            int parsed;
            if (!int.TryParse(value, out parsed) || parsed <= 0)
            {
                throw new YlvaUserException(name + " must be a positive integer");
            }

            return parsed;
        }

        private bool VerifyPassword(string password)
        {
            if (string.IsNullOrEmpty(state.PasswordSalt) || string.IsNullOrEmpty(state.PasswordHash))
            {
                return string.IsNullOrEmpty(password);
            }

            string candidate = HashPassword(password, state.PasswordSalt);
            return ConstantTimeEquals(candidate, state.PasswordHash);
        }

        private static string CreateSalt()
        {
            byte[] bytes = new byte[16];
            using (RandomNumberGenerator generator = RandomNumberGenerator.Create())
            {
                generator.GetBytes(bytes);
            }

            return Convert.ToBase64String(bytes);
        }

        private static string HashPassword(string password, string salt)
        {
            byte[] bytes = Encoding.UTF8.GetBytes(salt + "\n" + (password ?? string.Empty));
            using (SHA256 sha = SHA256.Create())
            {
                return ToHex(sha.ComputeHash(bytes));
            }
        }

        private static bool ConstantTimeEquals(string left, string right)
        {
            if (left == null || right == null || left.Length != right.Length)
            {
                return false;
            }

            int diff = 0;
            for (int i = 0; i < left.Length; i++)
            {
                diff |= left[i] ^ right[i];
            }

            return diff == 0;
        }

        private static string ToHex(byte[] bytes)
        {
            StringBuilder builder = new StringBuilder(bytes.Length * 2);
            foreach (byte b in bytes)
            {
                builder.Append(b.ToString("x2"));
            }

            return builder.ToString();
        }

    }
}
