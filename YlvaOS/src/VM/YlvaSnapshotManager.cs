using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Text;
using System.Text.RegularExpressions;

namespace YlvaOS
{
    internal sealed class YlvaSnapshotManager
    {
        private const int MaxSnapshotNameLength = 64;
        private readonly string rootDirectory;
        private readonly string diskPath;
        private readonly string qemuImgPath;
        private readonly Func<bool> isVmRunning;

        public YlvaSnapshotManager(string rootDirectory, string diskPath, string qemuImgPath, Func<bool> isVmRunning)
        {
            this.rootDirectory = rootDirectory ?? string.Empty;
            this.diskPath = diskPath ?? string.Empty;
            this.qemuImgPath = qemuImgPath ?? string.Empty;
            this.isVmRunning = isVmRunning;
        }

        public string SnapshotDirectory
        {
            get { return Path.Combine(rootDirectory, "snapshots"); }
        }

        public IList<YlvaSnapshotEntry> ListSnapshots()
        {
            EnsureSnapshotDirectory();
            List<YlvaSnapshotEntry> entries = new List<YlvaSnapshotEntry>();
            foreach (string directory in Directory.GetDirectories(SnapshotDirectory))
            {
                string name = Path.GetFileName(directory);
                if (string.IsNullOrEmpty(name) || name.StartsWith(".", StringComparison.Ordinal))
                {
                    continue;
                }

                entries.Add(ReadEntry(directory, name));
            }

            entries.Sort(delegate (YlvaSnapshotEntry left, YlvaSnapshotEntry right)
            {
                int created = right.CreatedUtc.CompareTo(left.CreatedUtc);
                return created != 0 ? created : string.Compare(left.Name, right.Name, StringComparison.OrdinalIgnoreCase);
            });
            return entries;
        }

        public string FormatSnapshotList()
        {
            IList<YlvaSnapshotEntry> entries = ListSnapshots();
            if (entries.Count == 0)
            {
                return "No YlvaOS snapshots found.";
            }

            StringBuilder builder = new StringBuilder();
            builder.AppendLine("YlvaOS snapshots:");
            builder.AppendLine("Name                 Created UTC          Size       Version  Status   Memo");
            foreach (YlvaSnapshotEntry entry in entries)
            {
                string created = entry.CreatedUtc == DateTime.MinValue ? "unknown" : entry.CreatedUtc.ToString("yyyy-MM-dd HH:mm", CultureInfo.InvariantCulture);
                builder.AppendLine(
                    Pad(Trim(entry.Name, 20), 20) + " " +
                    Pad(created, 19) + " " +
                    Pad(FormatBytes(entry.SizeBytes), 10) + " " +
                    Pad(Trim(entry.YlvaOsVersion, 8), 8) + " " +
                    Pad(entry.IsValid ? "ok" : "invalid", 8) + " " +
                    Trim(entry.Memo, 54));
                if (!entry.IsValid && !string.IsNullOrEmpty(entry.StatusMessage))
                {
                    builder.AppendLine("  " + entry.StatusMessage);
                }
            }

            return builder.ToString().TrimEnd();
        }

        public YlvaSnapshotEntry CreateSnapshot(string requestedName, string memo)
        {
            EnsureVmStopped("create a snapshot");
            EnsureSnapshotDirectory();
            EnsureQemuImgAvailable();
            if (!File.Exists(diskPath))
            {
                throw new YlvaUserException("root disk is missing; cannot create a snapshot");
            }

            string name = NormalizeSnapshotName(requestedName);
            string finalDirectory = ResolveSnapshotDirectory(name);
            if (Directory.Exists(finalDirectory))
            {
                throw new YlvaUserException("snapshot `" + name + "` already exists");
            }

            string tempDirectory = Path.Combine(SnapshotDirectory, ".tmp-" + name + "-" + Guid.NewGuid().ToString("N"));
            try
            {
                Directory.CreateDirectory(tempDirectory);
                string snapshotDisk = Path.Combine(tempDirectory, "disk.qcow2");
                CopyFileAtomically(diskPath, snapshotDisk);
                ValidateQcow2(snapshotDisk);

                FileInfo diskInfo = new FileInfo(snapshotDisk);
                YlvaSnapshotEntry entry = new YlvaSnapshotEntry
                {
                    Name = name,
                    CreatedUtc = DateTime.UtcNow,
                    SizeBytes = diskInfo.Exists ? diskInfo.Length : 0L,
                    Memo = SanitizeMemo(memo),
                    YlvaOsVersion = ModInfo.PluginVersion,
                    DiskPath = snapshotDisk,
                    MetadataPath = Path.Combine(tempDirectory, "metadata.json"),
                    IsValid = true,
                    StatusMessage = "ok"
                };
                WriteMetadataAtomically(entry.MetadataPath, entry);
                Directory.Move(tempDirectory, finalDirectory);
                return ReadEntry(finalDirectory, name);
            }
            catch
            {
                DeleteDirectoryQuietly(tempDirectory);
                throw;
            }
        }

        public YlvaSnapshotEntry RestoreSnapshot(string requestedName)
        {
            EnsureVmStopped("restore a snapshot");
            EnsureSnapshotDirectory();
            EnsureQemuImgAvailable();
            string name = NormalizeSnapshotName(requestedName);
            string snapshotDirectory = ResolveSnapshotDirectory(name);
            YlvaSnapshotEntry entry = ReadEntry(snapshotDirectory, name);
            if (!entry.IsValid)
            {
                throw new YlvaUserException("snapshot `" + name + "` cannot be restored: " + entry.StatusMessage);
            }

            ValidateQcow2(entry.DiskPath);
            Directory.CreateDirectory(Path.GetDirectoryName(diskPath));
            string temporary = diskPath + ".restore-" + Guid.NewGuid().ToString("N") + ".tmp";
            string backup = diskPath + ".before-restore-" + DateTime.UtcNow.ToString("yyyyMMddHHmmss", CultureInfo.InvariantCulture) + ".bak";
            try
            {
                CopyFileAtomically(entry.DiskPath, temporary);
                ValidateQcow2(temporary);
                if (File.Exists(diskPath))
                {
                    File.Replace(temporary, diskPath, backup, true);
                    DeleteFileQuietly(backup);
                }
                else
                {
                    File.Move(temporary, diskPath);
                }

                return entry;
            }
            catch
            {
                DeleteFileQuietly(temporary);
                throw;
            }
        }

        public YlvaSnapshotEntry DeleteSnapshot(string requestedName)
        {
            EnsureVmStopped("delete a snapshot");
            EnsureSnapshotDirectory();
            string name = NormalizeSnapshotName(requestedName);
            string snapshotDirectory = ResolveSnapshotDirectory(name);
            YlvaSnapshotEntry entry = ReadEntry(snapshotDirectory, name);
            if (!Directory.Exists(snapshotDirectory))
            {
                throw new YlvaUserException("snapshot `" + name + "` was not found");
            }

            string tombstone = Path.Combine(SnapshotDirectory, ".delete-" + name + "-" + Guid.NewGuid().ToString("N"));
            Directory.Move(snapshotDirectory, tombstone);
            DeleteDirectoryQuietly(tombstone);
            return entry;
        }

        private YlvaSnapshotEntry ReadEntry(string directory, string fallbackName)
        {
            string name = fallbackName;
            string metadataPath = Path.Combine(directory, "metadata.json");
            string snapshotDisk = Path.Combine(directory, "disk.qcow2");
            DateTime createdUtc = DateTime.MinValue;
            long sizeBytes = 0L;
            string memo = string.Empty;
            string version = string.Empty;
            bool valid = true;
            string status = "ok";

            try
            {
                if (File.Exists(metadataPath))
                {
                    string json = File.ReadAllText(metadataPath);
                    name = ReadJsonString(json, "name", fallbackName);
                    createdUtc = ReadJsonDate(json, "createdUtc", DateTime.MinValue);
                    sizeBytes = ReadJsonLong(json, "sizeBytes", 0L);
                    memo = ReadJsonString(json, "memo", string.Empty);
                    version = ReadJsonString(json, "ylvaOsVersion", string.Empty);
                }
                else
                {
                    valid = false;
                    status = "metadata.json is missing";
                }
            }
            catch (Exception ex)
            {
                valid = false;
                status = "metadata.json is unreadable: " + ex.Message;
            }

            if (!File.Exists(snapshotDisk))
            {
                valid = false;
                status = "disk.qcow2 is missing";
            }
            else
            {
                FileInfo diskInfo = new FileInfo(snapshotDisk);
                sizeBytes = diskInfo.Length;
                if (createdUtc == DateTime.MinValue)
                {
                    createdUtc = diskInfo.CreationTimeUtc;
                }

                string checkMessage;
                if (!TryValidateQcow2(snapshotDisk, out checkMessage))
                {
                    valid = false;
                    status = checkMessage;
                }
            }

            if (string.IsNullOrEmpty(version))
            {
                version = "?";
            }

            return new YlvaSnapshotEntry
            {
                Name = name,
                CreatedUtc = createdUtc,
                SizeBytes = sizeBytes,
                Memo = memo,
                YlvaOsVersion = version,
                DiskPath = snapshotDisk,
                MetadataPath = metadataPath,
                IsValid = valid,
                StatusMessage = status
            };
        }

        private void EnsureVmStopped(string action)
        {
            if (isVmRunning != null && isVmRunning())
            {
                throw new YlvaUserException("Snapshot/Restore is disabled while the VM is running. Shut down YlvaOS with `poweroff`, reopen the computer, and run the snapshot command before logging in to " + action + ".");
            }
        }

        private void EnsureSnapshotDirectory()
        {
            Directory.CreateDirectory(SnapshotDirectory);
        }

        private void EnsureQemuImgAvailable()
        {
            if (string.IsNullOrEmpty(qemuImgPath) || !File.Exists(qemuImgPath))
            {
                throw new YlvaUserException("qemu-img is missing; snapshot validation is unavailable");
            }
        }

        private string ResolveSnapshotDirectory(string name)
        {
            string root = Path.GetFullPath(SnapshotDirectory);
            string combined = Path.GetFullPath(Path.Combine(root, name));
            string rootWithSeparator = root.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar) + Path.DirectorySeparatorChar;
            if (!combined.StartsWith(rootWithSeparator, StringComparison.OrdinalIgnoreCase))
            {
                throw new YlvaUserException("snapshot name escapes the snapshots directory");
            }

            return combined;
        }

        private string NormalizeSnapshotName(string requestedName)
        {
            string name = (requestedName ?? string.Empty).Trim();
            if (name.Length == 0)
            {
                throw new YlvaUserException("snapshot name is required");
            }

            if (name.Length > MaxSnapshotNameLength)
            {
                throw new YlvaUserException("snapshot name must be " + MaxSnapshotNameLength + " characters or less");
            }

            if (name == "." || name == ".." || name.StartsWith(".", StringComparison.Ordinal) || name.EndsWith(".", StringComparison.Ordinal))
            {
                throw new YlvaUserException("snapshot name cannot start or end with a dot");
            }

            string upper = name.ToUpperInvariant();
            string deviceName = upper.Split('.')[0];
            string[] reserved =
            {
                "CON", "PRN", "AUX", "NUL",
                "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
                "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"
            };
            for (int i = 0; i < reserved.Length; i++)
            {
                if (deviceName == reserved[i])
                {
                    throw new YlvaUserException("snapshot name is reserved by Windows");
                }
            }

            for (int i = 0; i < name.Length; i++)
            {
                char ch = name[i];
                bool allowed =
                    (ch >= 'a' && ch <= 'z') ||
                    (ch >= 'A' && ch <= 'Z') ||
                    (ch >= '0' && ch <= '9') ||
                    ch == '_' ||
                    ch == '-' ||
                    ch == '.';
                if (!allowed)
                {
                    throw new YlvaUserException("snapshot name may contain only ASCII letters, digits, dot, underscore, and hyphen");
                }
            }

            return name;
        }

        private void ValidateQcow2(string path)
        {
            string message;
            if (!TryValidateQcow2(path, out message))
            {
                throw new YlvaUserException(message);
            }
        }

        private bool TryValidateQcow2(string path, out string message)
        {
            if (string.IsNullOrEmpty(qemuImgPath) || !File.Exists(qemuImgPath))
            {
                message = "qemu-img is missing";
                return false;
            }

            string output;
            if (!RunQemuImg("check " + QuoteArgument(path), out output))
            {
                message = "qemu-img check failed: " + Trim(output.Replace("\r", " ").Replace("\n", " "), 140);
                return false;
            }

            message = "ok";
            return true;
        }

        private bool RunQemuImg(string arguments, out string output)
        {
            ProcessStartInfo info = new ProcessStartInfo
            {
                FileName = qemuImgPath,
                Arguments = arguments,
                CreateNoWindow = true,
                UseShellExecute = false,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                WorkingDirectory = Directory.Exists(Path.GetDirectoryName(qemuImgPath)) ? Path.GetDirectoryName(qemuImgPath) : rootDirectory
            };

            using (Process qemuImg = Process.Start(info))
            {
                string stdout = qemuImg.StandardOutput.ReadToEnd();
                string stderr = qemuImg.StandardError.ReadToEnd();
                qemuImg.WaitForExit();
                output = (stderr.Length > 0 ? stderr : stdout).Trim();
                return qemuImg.ExitCode == 0;
            }
        }

        private static void CopyFileAtomically(string sourcePath, string destinationPath)
        {
            Directory.CreateDirectory(Path.GetDirectoryName(destinationPath));
            string temporary = destinationPath + ".tmp";
            DeleteFileQuietly(temporary);
            try
            {
                using (FileStream source = new FileStream(sourcePath, FileMode.Open, FileAccess.Read, FileShare.Read))
                using (FileStream destination = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                {
                    source.CopyTo(destination);
                }

                File.Move(temporary, destinationPath);
            }
            catch
            {
                DeleteFileQuietly(temporary);
                throw;
            }
        }

        private static void WriteMetadataAtomically(string metadataPath, YlvaSnapshotEntry entry)
        {
            string temporary = metadataPath + ".tmp";
            DeleteFileQuietly(temporary);
            File.WriteAllText(temporary, ToMetadataJson(entry), Encoding.UTF8);
            if (File.Exists(metadataPath))
            {
                File.Replace(temporary, metadataPath, null, true);
            }
            else
            {
                File.Move(temporary, metadataPath);
            }
        }

        private static string ToMetadataJson(YlvaSnapshotEntry entry)
        {
            StringBuilder builder = new StringBuilder();
            builder.AppendLine("{");
            builder.AppendLine("  \"schemaVersion\": 1,");
            builder.AppendLine("  \"name\": \"" + EscapeJson(entry.Name) + "\",");
            builder.AppendLine("  \"createdUtc\": \"" + EscapeJson(entry.CreatedUtc.ToString("o", CultureInfo.InvariantCulture)) + "\",");
            builder.AppendLine("  \"sizeBytes\": " + entry.SizeBytes.ToString(CultureInfo.InvariantCulture) + ",");
            builder.AppendLine("  \"memo\": \"" + EscapeJson(entry.Memo) + "\",");
            builder.AppendLine("  \"ylvaOsVersion\": \"" + EscapeJson(entry.YlvaOsVersion) + "\"");
            builder.AppendLine("}");
            return builder.ToString();
        }

        private static string ReadJsonString(string json, string propertyName, string fallback)
        {
            Match match = Regex.Match(
                json ?? string.Empty,
                "\"" + Regex.Escape(propertyName) + "\"\\s*:\\s*\"((?:\\\\.|[^\"\\\\])*)\"",
                RegexOptions.CultureInvariant);
            return match.Success ? UnescapeJson(match.Groups[1].Value) : fallback;
        }

        private static long ReadJsonLong(string json, string propertyName, long fallback)
        {
            Match match = Regex.Match(
                json ?? string.Empty,
                "\"" + Regex.Escape(propertyName) + "\"\\s*:\\s*([0-9]+)",
                RegexOptions.CultureInvariant);
            long parsed;
            return match.Success && long.TryParse(match.Groups[1].Value, NumberStyles.Integer, CultureInfo.InvariantCulture, out parsed) ? parsed : fallback;
        }

        private static DateTime ReadJsonDate(string json, string propertyName, DateTime fallback)
        {
            string value = ReadJsonString(json, propertyName, string.Empty);
            DateTime parsed;
            return DateTime.TryParse(value, CultureInfo.InvariantCulture, DateTimeStyles.RoundtripKind, out parsed) ? parsed.ToUniversalTime() : fallback;
        }

        private static string EscapeJson(string value)
        {
            if (value == null)
            {
                return string.Empty;
            }

            StringBuilder builder = new StringBuilder(value.Length + 16);
            foreach (char ch in value)
            {
                switch (ch)
                {
                    case '\\':
                        builder.Append("\\\\");
                        break;
                    case '"':
                        builder.Append("\\\"");
                        break;
                    case '\b':
                        builder.Append("\\b");
                        break;
                    case '\f':
                        builder.Append("\\f");
                        break;
                    case '\n':
                        builder.Append("\\n");
                        break;
                    case '\r':
                        builder.Append("\\r");
                        break;
                    case '\t':
                        builder.Append("\\t");
                        break;
                    default:
                        if (ch < 32)
                        {
                            builder.Append("\\u");
                            builder.Append(((int)ch).ToString("x4", CultureInfo.InvariantCulture));
                        }
                        else
                        {
                            builder.Append(ch);
                        }

                        break;
                }
            }

            return builder.ToString();
        }

        private static string UnescapeJson(string value)
        {
            StringBuilder builder = new StringBuilder(value == null ? 0 : value.Length);
            for (int i = 0; value != null && i < value.Length; i++)
            {
                char ch = value[i];
                if (ch != '\\' || i + 1 >= value.Length)
                {
                    builder.Append(ch);
                    continue;
                }

                char escaped = value[++i];
                switch (escaped)
                {
                    case '"':
                    case '\\':
                    case '/':
                        builder.Append(escaped);
                        break;
                    case 'b':
                        builder.Append('\b');
                        break;
                    case 'f':
                        builder.Append('\f');
                        break;
                    case 'n':
                        builder.Append('\n');
                        break;
                    case 'r':
                        builder.Append('\r');
                        break;
                    case 't':
                        builder.Append('\t');
                        break;
                    case 'u':
                        if (i + 4 < value.Length)
                        {
                            int code;
                            string hex = value.Substring(i + 1, 4);
                            if (int.TryParse(hex, NumberStyles.HexNumber, CultureInfo.InvariantCulture, out code))
                            {
                                builder.Append((char)code);
                                i += 4;
                                break;
                            }
                        }

                        builder.Append(escaped);
                        break;
                    default:
                        builder.Append(escaped);
                        break;
                }
            }

            return builder.ToString();
        }

        private static string QuoteArgument(string value)
        {
            return "\"" + (value ?? string.Empty).Replace("\"", "\\\"") + "\"";
        }

        private static string SanitizeMemo(string memo)
        {
            string value = (memo ?? string.Empty).Replace("\r", " ").Replace("\n", " ").Trim();
            return Trim(value, 160);
        }

        private static string Pad(string text, int width)
        {
            text = text ?? string.Empty;
            return text.Length >= width ? text : text + new string(' ', width - text.Length);
        }

        private static string Trim(string text, int maxLength)
        {
            if (string.IsNullOrEmpty(text) || text.Length <= maxLength)
            {
                return text ?? string.Empty;
            }

            return text.Substring(0, Math.Max(0, maxLength - 3)) + "...";
        }

        private static string FormatBytes(long bytes)
        {
            double value = Math.Max(0L, bytes);
            string[] units = { "B", "KiB", "MiB", "GiB", "TiB" };
            int unit = 0;
            while (value >= 1024.0 && unit < units.Length - 1)
            {
                value /= 1024.0;
                unit++;
            }

            return unit == 0
                ? ((long)value).ToString(CultureInfo.InvariantCulture) + " B"
                : value.ToString(value >= 10.0 ? "0.0" : "0.00", CultureInfo.InvariantCulture) + " " + units[unit];
        }

        private static void DeleteFileQuietly(string path)
        {
            try
            {
                if (!string.IsNullOrEmpty(path) && File.Exists(path))
                {
                    File.Delete(path);
                }
            }
            catch
            {
            }
        }

        private static void DeleteDirectoryQuietly(string path)
        {
            try
            {
                if (!string.IsNullOrEmpty(path) && Directory.Exists(path))
                {
                    Directory.Delete(path, true);
                }
            }
            catch
            {
            }
        }
    }

    internal sealed class YlvaSnapshotEntry
    {
        public string Name { get; set; }
        public DateTime CreatedUtc { get; set; }
        public long SizeBytes { get; set; }
        public string Memo { get; set; }
        public string YlvaOsVersion { get; set; }
        public string DiskPath { get; set; }
        public string MetadataPath { get; set; }
        public bool IsValid { get; set; }
        public string StatusMessage { get; set; }
    }
}
