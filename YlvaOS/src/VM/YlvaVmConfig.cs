using System;

namespace YlvaOS
{
    internal sealed class YlvaVmConfig
    {
        private const int DefaultMemoryMiB = 4096;
        private const int PreviousDefaultMemoryMiB = 2048;
        private const string OldDefaultKernelAppend = "console=ttyS0 root=/dev/vda rw hostname=YlvaOS";
        private const string DefaultKernelAppend = "console=ttyS0 root=/dev/vda rootfstype=ext4 rw modules=virtio_pci,virtio_blk,ext4 hostname=YlvaOS";

        public YlvaVmConfig()
        {
            SchemaVersion = 4;
            MemoryMiB = DefaultMemoryMiB;
            DiskMiB = 16384;
            DesktopWidth = 1024;
            DesktopHeight = 768;
            DesktopRefreshFps = 24;
            AutoStartAfterLogin = true;
            StartupWarningAccepted = false;
            KernelFileName = "vmlinuz";
            InitrdFileName = "initrd.img";
            DiskFileName = "disk.qcow2";
            KernelAppend = DefaultKernelAppend;
        }

        public int SchemaVersion { get; set; }
        public int MemoryMiB { get; set; }
        public int DiskMiB { get; set; }
        public int DesktopWidth { get; set; }
        public int DesktopHeight { get; set; }
        public int DesktopRefreshFps { get; set; }
        public bool AutoStartAfterLogin { get; set; }
        public bool StartupWarningAccepted { get; set; }
        public string KernelFileName { get; set; }
        public string InitrdFileName { get; set; }
        public string DiskFileName { get; set; }
        public string KernelAppend { get; set; }

        public void Normalize()
        {
            if (SchemaVersion <= 0)
            {
                SchemaVersion = 1;
            }

            if (SchemaVersion < 2 && (string.IsNullOrEmpty(KernelAppend) || KernelAppend == OldDefaultKernelAppend))
            {
                KernelAppend = DefaultKernelAppend;
            }

            if (SchemaVersion < 4 && MemoryMiB == PreviousDefaultMemoryMiB)
            {
                MemoryMiB = DefaultMemoryMiB;
            }

            SchemaVersion = 4;

            MemoryMiB = Clamp(MemoryMiB <= 0 ? DefaultMemoryMiB : MemoryMiB, 256, 32768);
            DiskMiB = Clamp(DiskMiB <= 0 ? 16384 : DiskMiB, 1024, 262144);
            DesktopWidth = Clamp(DesktopWidth <= 0 ? 1024 : DesktopWidth, 640, 2560);
            DesktopHeight = Clamp(DesktopHeight <= 0 ? 768 : DesktopHeight, 480, 1600);
            DesktopRefreshFps = Clamp(DesktopRefreshFps <= 0 ? 24 : DesktopRefreshFps, 5, 60);

            if (string.IsNullOrEmpty(KernelFileName))
            {
                KernelFileName = "vmlinuz";
            }

            if (string.IsNullOrEmpty(InitrdFileName))
            {
                InitrdFileName = "initrd.img";
            }

            if (string.IsNullOrEmpty(DiskFileName))
            {
                DiskFileName = "disk.qcow2";
            }

            if (string.IsNullOrEmpty(KernelAppend))
            {
                KernelAppend = DefaultKernelAppend;
            }
        }

        private static int Clamp(int value, int min, int max)
        {
            if (value < min)
            {
                return min;
            }

            if (value > max)
            {
                return max;
            }

            return value;
        }
    }

    internal sealed class YlvaVmPaths
    {
        public string RootDirectory { get; set; }
        public string ConfigPath { get; set; }
        public string VmDirectory { get; set; }
        public string AssetsDirectory { get; set; }
        public string ToolsDirectory { get; set; }
        public string ImportDirectory { get; set; }
        public string SnapshotDirectory { get; set; }
        public string UpdateDirectory { get; set; }
        public string DiskPath { get; set; }
        public string KernelPath { get; set; }
        public string InitrdPath { get; set; }
        public string QemuSystemPath { get; set; }
        public string QemuImgPath { get; set; }
    }

    internal enum YlvaVmStatus
    {
        Stopped,
        Starting,
        Running,
        Stopping,
        MissingAssets,
        Error
    }

    internal sealed class YlvaVmStatusSnapshot
    {
        public YlvaVmStatus Status { get; set; }
        public string Message { get; set; }
        public int MemoryMiB { get; set; }
        public int DiskMiB { get; set; }
        public int DesktopWidth { get; set; }
        public int DesktopHeight { get; set; }
        public int DesktopRefreshFps { get; set; }
        public int VncPort { get; set; }
        public string DiskPath { get; set; }
        public string KernelPath { get; set; }
        public string InitrdPath { get; set; }
        public string QemuSystemPath { get; set; }
    }

    internal sealed class YlvaProvisioningResult
    {
        public YlvaProvisioningResult()
        {
            Actions = new System.Collections.Generic.List<string>();
            Missing = new System.Collections.Generic.List<string>();
            Errors = new System.Collections.Generic.List<string>();
        }

        public System.Collections.Generic.List<string> Actions { get; private set; }
        public System.Collections.Generic.List<string> Missing { get; private set; }
        public System.Collections.Generic.List<string> Errors { get; private set; }
        public bool Ready { get; set; }
        public string RootDirectory { get; set; }
        public string PathsText { get; set; }

        public bool ShouldShowDialog
        {
            get
            {
                return Actions.Count > 0 || Missing.Count > 0 || Errors.Count > 0;
            }
        }

        public string StatusLine
        {
            get
            {
                if (Ready)
                {
                    return "ready";
                }

                return "missing=" + Missing.Count + ", errors=" + Errors.Count;
            }
        }

        public string ToDialogText()
        {
            System.Text.StringBuilder builder = new System.Text.StringBuilder();
            builder.AppendLine("YlvaOS VM setup");
            builder.AppendLine();

            if (Actions.Count > 0)
            {
                builder.AppendLine("Prepared:");
                foreach (string action in Actions)
                {
                    builder.AppendLine("- " + action);
                }

                builder.AppendLine();
            }

            if (Ready)
            {
                builder.AppendLine("YlvaOS has the VM files needed to boot real Linux.");
                builder.AppendLine("Open a computer and log in. The VM will start automatically.");
            }
            else
            {
                builder.AppendLine("Some VM files are still missing.");
                foreach (string missing in Missing)
                {
                    builder.AppendLine("- " + missing);
                }

                if (Errors.Count > 0)
                {
                    builder.AppendLine();
                    builder.AppendLine("Errors:");
                    foreach (string error in Errors)
                    {
                        builder.AppendLine("- " + error);
                    }
                }

                builder.AppendLine();
                builder.AppendLine("Run `vm paths` inside YlvaOS for the same paths.");
            }

            builder.AppendLine();
            builder.AppendLine(PathsText ?? string.Empty);
            return builder.ToString().TrimEnd();
        }
    }
}
