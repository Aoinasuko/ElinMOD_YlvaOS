using System;
using System.Collections.Generic;

namespace YlvaOS
{
    internal sealed class YlvaState
    {
        public YlvaState()
        {
            SchemaVersion = 2;
            MachineId = "ylva";
            HostName = "ylva";
            UserName = string.Empty;
            WorkingDirectory = "/";
            ScreenLines = new List<string>();
            History = new List<string>();
            Files = new List<YlvaVfsEntry>();
            CurrentInput = string.Empty;
            PendingUserName = string.Empty;
            PasswordSalt = string.Empty;
            PasswordHash = string.Empty;
            PoweredOff = true;
            Phase = YlvaBootPhase.None;
            DisplayMode = YlvaDisplayMode.Kernel;
        }

        public int SchemaVersion { get; set; }
        public string MachineId { get; set; }
        public string HostName { get; set; }
        public string UserName { get; set; }
        public string PasswordSalt { get; set; }
        public string PasswordHash { get; set; }
        public bool SetupComplete { get; set; }
        public bool Authenticated { get; set; }
        public string PendingUserName { get; set; }
        public YlvaBootPhase Phase { get; set; }
        public YlvaDisplayMode DisplayMode { get; set; }
        public bool HasBooted { get; set; }
        public bool PoweredOff { get; set; }
        public string WorkingDirectory { get; set; }
        public string CurrentInput { get; set; }
        public DateTime LastBootUtc { get; set; }
        public List<string> ScreenLines { get; set; }
        public List<string> History { get; set; }
        public List<YlvaVfsEntry> Files { get; set; }

        public static YlvaState CreateDefault()
        {
            YlvaState state = new YlvaState();
            state.Files = YlvaVfs.CreateDefaultEntries();
            return state;
        }

        public void Normalize()
        {
            if (SchemaVersion < 2)
            {
                SchemaVersion = 2;
            }

            if (string.IsNullOrEmpty(MachineId))
            {
                MachineId = "ylva";
            }

            if (string.IsNullOrEmpty(HostName))
            {
                HostName = "ylva";
            }

            if (UserName == null)
            {
                UserName = string.Empty;
            }

            if (PasswordSalt == null)
            {
                PasswordSalt = string.Empty;
            }

            if (PasswordHash == null)
            {
                PasswordHash = string.Empty;
            }

            if (PendingUserName == null)
            {
                PendingUserName = string.Empty;
            }

            if (string.IsNullOrEmpty(WorkingDirectory))
            {
                WorkingDirectory = SetupComplete && !string.IsNullOrEmpty(UserName) ? "/home/" + UserName : "/";
            }

            if (CurrentInput == null)
            {
                CurrentInput = string.Empty;
            }

            if (ScreenLines == null)
            {
                ScreenLines = new List<string>();
            }

            if (History == null)
            {
                History = new List<string>();
            }

            if (Files == null)
            {
                Files = new List<YlvaVfsEntry>();
            }

            if (Phase == YlvaBootPhase.None)
            {
                Phase = SetupComplete ? YlvaBootPhase.LoginUserName : YlvaBootPhase.SetupUserName;
            }

            if (!Enum.IsDefined(typeof(YlvaDisplayMode), DisplayMode))
            {
                DisplayMode = YlvaDisplayMode.Kernel;
            }
        }
    }

    internal enum YlvaBootPhase
    {
        None = 0,
        SetupUserName = 1,
        SetupPassword = 2,
        LoginUserName = 3,
        LoginPassword = 4,
        Shell = 5
    }

    internal enum YlvaDisplayMode
    {
        Kernel = 0,
        DesktopStarting = 1,
        Desktop = 2
    }

    internal sealed class YlvaVfsEntry
    {
        public string Path { get; set; }
        public bool IsDirectory { get; set; }
        public bool Executable { get; set; }
        public string Content { get; set; }
        public byte[] Data { get; set; }
        public DateTime ModifiedUtc { get; set; }
    }
}
