using System;
using System.Collections.Generic;
using System.Linq;

namespace YlvaOS
{
    internal sealed class YlvaShell
    {
        private readonly YlvaMachine machine;
        private readonly YlvaVfs vfs;
        private readonly YlvaElfRunner elfRunner;

        public YlvaShell(YlvaMachine machine, YlvaVfs vfs)
        {
            this.machine = machine;
            this.vfs = vfs;
            elfRunner = new YlvaElfRunner(machine, vfs);
        }

        public void Execute(string command)
        {
            if (string.IsNullOrWhiteSpace(command))
            {
                return;
            }

            string error;
            List<string> args = Tokenize(command, out error);
            if (error != null)
            {
                machine.Append("sh: " + error);
                return;
            }

            if (args.Count == 0)
            {
                return;
            }

            string name = args[0];
            try
            {
                ExecuteCommand(name, args);
            }
            catch (YlvaUserException ex)
            {
                machine.Append(name + ": " + ex.Message);
            }
            catch (Exception ex)
            {
                if (Plugin.Log != null)
                {
                    Plugin.Log.LogWarning("YlvaOS sandbox command failed: " + ex);
                }

                machine.Append(name + ": sandbox fault: " + ex.Message);
            }
        }

        private void ExecuteCommand(string name, List<string> args)
        {
            if (!IsShellBuiltin(name) && elfRunner.TryExecuteCommand(name, args))
            {
                return;
            }

            switch (name)
            {
                case "help":
                    Help();
                    return;
                case "uname":
                    Uname(args);
                    return;
                case "whoami":
                    machine.Append(machine.CurrentUserName);
                    return;
                case "pwd":
                    machine.Append(machine.WorkingDirectory);
                    return;
                case "cd":
                    Cd(args);
                    return;
                case "ls":
                    Ls(args);
                    return;
                case "cat":
                    Cat(args);
                    return;
                case "echo":
                    Echo(args);
                    return;
                case "touch":
                    Touch(args);
                    return;
                case "mkdir":
                    Mkdir(args);
                    return;
                case "rm":
                    Rm(args);
                    return;
                case "clear":
                    machine.ClearScreen();
                    return;
                case "date":
                    machine.Append(DateTime.Now.ToString("ddd MMM dd HH:mm:ss yyyy"));
                    return;
                case "history":
                    History();
                    return;
                case "vm":
                    Vm(args);
                    return;
                case "YlvaOS":
                    YlvaOs(args);
                    return;
                case "Desktop":
                case "desktop":
                    machine.Append("Desktop mode is available after the real YlvaOS VM is running.");
                    machine.Append("Log in to YlvaOS and run `Desktop` inside the Linux console.");
                    return;
                case "Kernel":
                case "kernel":
                    machine.Append("Already in YlvaOS kernel console mode.");
                    return;
                case "file":
                    File(args);
                    return;
                case "elfrun":
                    Elfrun(args);
                    return;
                case "mount":
                    machine.Append("ylvafs on / type ylvafs (rw,nosuid,nodev,noexec,relatime)");
                    return;
                case "df":
                    machine.Append("Filesystem     1K-blocks  Used Available Use% Mounted on");
                    machine.Append("ylvafs              1024     1      1023   1% /");
                    return;
                case "ps":
                    machine.Append("  PID TTY          TIME CMD");
                    machine.Append("    1 tty0     00:00:00 init");
                    machine.Append("    2 tty0     00:00:00 ylvash");
                    return;
                case "reboot":
                    machine.Reboot();
                    return;
                case "shutdown":
                case "poweroff":
                    machine.RequestShutdown();
                    return;
                case "exit":
                    machine.Append("logout");
                    machine.RequestShutdown();
                    return;
                case "man":
                    Man(args);
                    return;
                default:
                    machine.Append(name + ": command not found");
                    return;
            }
        }

        private void Help()
        {
            machine.Append("YlvaOS shell commands:");
            machine.Append("  help uname whoami pwd cd ls cat echo touch mkdir rm clear");
            machine.Append("  date history vm mount df ps file elfrun man reboot shutdown poweroff exit");
            machine.Append("  Desktop | Kernel | YlvaOS set memory <MiB> | YlvaOS set disk <MiB>");
            machine.Append("VM management: vm status | vm paths | vm start | vm stop | vm memory <MiB> | vm disk <MiB> | vm autostart on|off");
            machine.Append("Bundled ELF64 commands: hello uname id true false syscall-demo");
            machine.Append("Filesystem is ylvafs: a sandboxed Linux-like VFS stored in LocalLow/Elin/YlvaOS.");
        }

        private void Uname(List<string> args)
        {
            if (args.Count > 1 && args[1] == "-a")
            {
                machine.Append("Linux ylva 0.1.0-ylva #1 SMP YlvaOS x86_64 GNU/Ylva");
                return;
            }

            machine.Append("Linux");
        }

        private void Cd(List<string> args)
        {
            string target = args.Count > 1 ? args[1] : "/home/player";
            string path = vfs.NormalizePath(machine.WorkingDirectory, target);
            if (!vfs.DirectoryExists(path))
            {
                throw new YlvaUserException(path + ": no such directory");
            }

            machine.WorkingDirectory = path;
        }

        private void Ls(List<string> args)
        {
            bool longFormat = false;
            string target = ".";
            for (int i = 1; i < args.Count; i++)
            {
                if (args[i].StartsWith("-", StringComparison.Ordinal))
                {
                    longFormat = args[i].IndexOf('l') >= 0;
                    continue;
                }

                target = args[i];
            }

            List<YlvaVfsListItem> items = vfs.ListDirectory(machine.WorkingDirectory, target);
            if (!longFormat)
            {
                machine.Append(string.Join("  ", items.Select(item => item.IsDirectory ? item.Name + "/" : item.Name).ToArray()));
                return;
            }

            foreach (YlvaVfsListItem item in items)
            {
                string mode = item.IsDirectory ? "drwxr-xr-x" : "-rw-r--r--";
                machine.Append(mode + " player player " + item.Size.ToString().PadLeft(6) + " " + item.ModifiedUtc.ToLocalTime().ToString("MMM dd HH:mm") + " " + item.Name + (item.IsDirectory ? "/" : string.Empty));
            }
        }

        private void Cat(List<string> args)
        {
            if (args.Count < 2)
            {
                throw new YlvaUserException("missing operand");
            }

            for (int i = 1; i < args.Count; i++)
            {
                machine.Append(vfs.ReadFile(machine.WorkingDirectory, args[i]).TrimEnd('\n'));
            }
        }

        private void Echo(List<string> args)
        {
            int redirect = args.IndexOf(">");
            int appendRedirect = args.IndexOf(">>");
            bool append = false;
            if (appendRedirect >= 0 && (redirect < 0 || appendRedirect < redirect))
            {
                redirect = appendRedirect;
                append = true;
            }

            if (redirect >= 0)
            {
                if (redirect + 1 >= args.Count)
                {
                    throw new YlvaUserException("missing redirection target");
                }

                string content = string.Join(" ", args.GetRange(1, redirect - 1).ToArray()) + "\n";
                vfs.WriteFile(machine.WorkingDirectory, args[redirect + 1], content, append);
                return;
            }

            machine.Append(string.Join(" ", args.Skip(1).ToArray()));
        }

        private void Touch(List<string> args)
        {
            if (args.Count < 2)
            {
                throw new YlvaUserException("missing operand");
            }

            for (int i = 1; i < args.Count; i++)
            {
                vfs.Touch(machine.WorkingDirectory, args[i]);
            }
        }

        private void Mkdir(List<string> args)
        {
            if (args.Count < 2)
            {
                throw new YlvaUserException("missing operand");
            }

            bool recursive = args.Contains("-p");
            for (int i = 1; i < args.Count; i++)
            {
                if (args[i].StartsWith("-", StringComparison.Ordinal))
                {
                    continue;
                }

                vfs.MakeDirectory(machine.WorkingDirectory, args[i], recursive);
            }
        }

        private void Rm(List<string> args)
        {
            if (args.Count < 2)
            {
                throw new YlvaUserException("missing operand");
            }

            bool recursive = args.Contains("-r") || args.Contains("-rf") || args.Contains("-fr");
            for (int i = 1; i < args.Count; i++)
            {
                if (args[i].StartsWith("-", StringComparison.Ordinal))
                {
                    continue;
                }

                vfs.Remove(machine.WorkingDirectory, args[i], recursive);
            }
        }

        private void History()
        {
            for (int i = 0; i < machine.History.Count; i++)
            {
                machine.Append((i + 1).ToString().PadLeft(4) + "  " + machine.History[i]);
            }
        }

        private void Vm(List<string> args)
        {
            if (machine.Vm == null)
            {
                machine.Append("VM backend is unavailable.");
                return;
            }

            string subcommand = args.Count > 1 ? args[1] : "status";
            switch (subcommand)
            {
                case "status":
                    VmStatus();
                    return;
                case "paths":
                    machine.Append(machine.Vm.DescribePaths());
                    return;
                case "start":
                    VmStart();
                    return;
                case "stop":
                    machine.Vm.Stop();
                    machine.Append("VM stopped.");
                    return;
                case "memory":
                case "mem":
                    if (args.Count < 3)
                    {
                        throw new YlvaUserException("usage: vm memory <MiB>");
                    }

                    machine.Vm.SetMemoryMiB(ParsePositiveInt(args[2], "memory"));
                    machine.Append("VM memory set to " + machine.Vm.Config.MemoryMiB + " MiB.");
                    return;
                case "disk":
                    if (args.Count < 3)
                    {
                        throw new YlvaUserException("usage: vm disk <MiB>");
                    }

                    machine.Vm.SetDiskMiB(ParsePositiveInt(args[2], "disk"));
                    machine.Append("VM disk target set to " + machine.Vm.Config.DiskMiB + " MiB.");
                    machine.Append("The qcow2 image will be created or expanded on next VM start.");
                    return;
                case "autostart":
                    if (args.Count < 3)
                    {
                        throw new YlvaUserException("usage: vm autostart on|off");
                    }

                    bool enabled = args[2] == "on" || args[2] == "true" || args[2] == "1";
                    if (!enabled && !(args[2] == "off" || args[2] == "false" || args[2] == "0"))
                    {
                        throw new YlvaUserException("usage: vm autostart on|off");
                    }

                    machine.Vm.SetAutoStart(enabled);
                    machine.Append("VM autostart " + (enabled ? "enabled." : "disabled."));
                    return;
                default:
                    throw new YlvaUserException("unknown vm command: " + subcommand);
            }
        }

        private void VmStatus()
        {
            YlvaVmStatusSnapshot snapshot = machine.Vm.Snapshot();
            machine.Append("VM status: " + snapshot.Status);
            machine.Append("Memory: " + snapshot.MemoryMiB + " MiB");
            machine.Append("Disk target: " + snapshot.DiskMiB + " MiB");
            machine.Append("Desktop: " + snapshot.DesktopWidth + "x" + snapshot.DesktopHeight + " @ " + snapshot.DesktopRefreshFps + " fps");
            if (snapshot.VncPort > 0)
            {
                machine.Append("VNC: 127.0.0.1:" + snapshot.VncPort);
            }

            if (!string.IsNullOrEmpty(snapshot.Message))
            {
                machine.Append(snapshot.Message);
            }
        }

        private void VmStart()
        {
            string message;
            if (machine.Vm.TryStart(out message))
            {
                machine.Append(message);
                return;
            }

            machine.Append(message);
            machine.Append("Use `vm paths` to see the required files.");
        }

        private void YlvaOs(List<string> args)
        {
            if (machine.Vm == null)
            {
                machine.Append("VM backend is unavailable.");
                return;
            }

            if (args.Count == 4 && args[1] == "set" && (args[2] == "memory" || args[2] == "mem"))
            {
                machine.Vm.SetMemoryMiB(ParsePositiveInt(args[3], "memory"));
                machine.Append("YlvaOS memory target set to " + machine.Vm.Config.MemoryMiB + " MiB. Reboot YlvaOS to apply.");
                return;
            }

            if (args.Count == 4 && args[1] == "set" && args[2] == "disk")
            {
                machine.Vm.SetDiskMiB(ParsePositiveInt(args[3], "disk"));
                machine.Append("YlvaOS disk target set to " + machine.Vm.Config.DiskMiB + " MiB. Reboot YlvaOS to apply.");
                return;
            }

            machine.Append("usage: YlvaOS set memory <MiB> | YlvaOS set disk <MiB>");
        }

        private void File(List<string> args)
        {
            if (args.Count < 2)
            {
                throw new YlvaUserException("missing operand");
            }

            for (int i = 1; i < args.Count; i++)
            {
                try
                {
                    machine.Append(args[i] + ": " + elfRunner.Describe(args[i]));
                }
                catch (YlvaUserException ex)
                {
                    machine.Append(args[i] + ": " + ex.Message);
                }
            }
        }

        private void Elfrun(List<string> args)
        {
            if (args.Count < 2)
            {
                throw new YlvaUserException("usage: elfrun /path/to/program [args...]");
            }

            string path = args[1];
            List<string> execArgs = new List<string> { path };
            execArgs.AddRange(args.Skip(2));
            int exitCode = elfRunner.Execute(path, execArgs);
            if (exitCode != 0)
            {
                machine.Append("process exited with status " + exitCode);
            }
        }

        private void Man(List<string> args)
        {
            if (args.Count < 2)
            {
                machine.Append("What manual page do you want?");
                return;
            }

            machine.Append(args[1] + "(1) - YlvaOS sandbox command");
            machine.Append("This userspace implements a Linux-like command surface backed by ylvafs and ELF64 syscall emulation.");
        }

        private static bool IsShellBuiltin(string name)
        {
            switch (name)
            {
                case "help":
                case "whoami":
                case "pwd":
                case "cd":
                case "ls":
                case "cat":
                case "echo":
                case "touch":
                case "mkdir":
                case "rm":
                case "clear":
                case "date":
                case "history":
                case "vm":
                case "YlvaOS":
                case "file":
                case "elfrun":
                case "mount":
                case "df":
                case "ps":
                case "reboot":
                case "shutdown":
                case "poweroff":
                case "exit":
                case "man":
                    return true;
                default:
                    return false;
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

        private static List<string> Tokenize(string input, out string error)
        {
            error = null;
            List<string> tokens = new List<string>();
            string current = string.Empty;
            char quote = '\0';
            bool escaping = false;

            foreach (char ch in input)
            {
                if (escaping)
                {
                    current += ch;
                    escaping = false;
                    continue;
                }

                if (ch == '\\')
                {
                    escaping = true;
                    continue;
                }

                if (quote != '\0')
                {
                    if (ch == quote)
                    {
                        quote = '\0';
                    }
                    else
                    {
                        current += ch;
                    }

                    continue;
                }

                if (ch == '"' || ch == '\'')
                {
                    quote = ch;
                    continue;
                }

                if (char.IsWhiteSpace(ch))
                {
                    if (current.Length > 0)
                    {
                        tokens.Add(current);
                        current = string.Empty;
                    }

                    continue;
                }

                current += ch;
            }

            if (escaping)
            {
                current += '\\';
            }

            if (quote != '\0')
            {
                error = "unterminated quote";
                return tokens;
            }

            if (current.Length > 0)
            {
                tokens.Add(current);
            }

            return tokens;
        }
    }
}
