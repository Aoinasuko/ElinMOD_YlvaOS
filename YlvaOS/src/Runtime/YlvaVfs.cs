using System;
using System.Collections.Generic;
using System.Linq;
using System.Text;

namespace YlvaOS
{
    internal sealed class YlvaUserException : Exception
    {
        public YlvaUserException(string message)
            : base(message)
        {
        }
    }

    internal sealed class YlvaVfsListItem
    {
        public string Name { get; set; }
        public bool IsDirectory { get; set; }
        public int Size { get; set; }
        public DateTime ModifiedUtc { get; set; }
    }

    internal sealed class YlvaVfs
    {
        private const int MaxEntries = 1024;
        private const int MaxFileBytes = 64 * 1024;
        private const int MaxTotalBytes = 2 * 1024 * 1024;

        private readonly YlvaState state;

        public YlvaVfs(YlvaState state)
        {
            this.state = state;
            this.state.Normalize();
            EnsureDefaultTree();
        }

        public static List<YlvaVfsEntry> CreateDefaultEntries()
        {
            DateTime now = DateTime.UtcNow;
            List<YlvaVfsEntry> entries = new List<YlvaVfsEntry>
            {
                Directory("/", now),
                Directory("/bin", now),
                Directory("/dev", now),
                Directory("/etc", now),
                Directory("/home", now),
                Directory("/home/player", now),
                Directory("/proc", now),
                Directory("/tmp", now),
                Directory("/var", now),
                Directory("/var/log", now),
                File("/etc/os-release", "NAME=\"YlvaOS\"\nID=ylvaos\nVERSION_ID=\"0.1.0\"\nPRETTY_NAME=\"YlvaOS 0.1.0 (Linux compatibility layer)\"\n", now),
                File("/proc/version", "Linux version 0.1.0-ylva (ylva@elin) #1 SMP YlvaOS\n", now),
                File("/home/player/README.txt", "Welcome to YlvaOS.\nThis filesystem is virtual and sandboxed inside Elin LocalLow/YlvaOS/state.json.\nType `help` to see available commands.\n", now),
                File("/var/log/boot.log", string.Empty, now),
                File("/dev/null", string.Empty, now)
            };

            entries.AddRange(YlvaElfFactory.CreateDefaultEntries(now));
            return entries;
        }

        public void EnsureDefaultTree()
        {
            foreach (YlvaVfsEntry entry in CreateDefaultEntries())
            {
                if (Find(entry.Path) == null)
                {
                    state.Files.Add(entry);
                }
            }
        }

        public string NormalizePath(string cwd, string rawPath)
        {
            if (string.IsNullOrWhiteSpace(rawPath))
            {
                rawPath = ".";
            }

            if (rawPath.IndexOf('\\') >= 0)
            {
                throw new YlvaUserException("backslash paths are not supported in the sandbox");
            }

            if (rawPath.Length >= 2 && char.IsLetter(rawPath[0]) && rawPath[1] == ':')
            {
                throw new YlvaUserException("host drive paths are outside YlvaOS");
            }

            if (rawPath.IndexOf('\0') >= 0)
            {
                throw new YlvaUserException("path contains a null byte");
            }

            string combined = rawPath.StartsWith("/", StringComparison.Ordinal)
                ? rawPath
                : Combine(cwd, rawPath);

            List<string> parts = new List<string>();
            foreach (string rawPart in combined.Split('/'))
            {
                string part = rawPart.Trim();
                if (part.Length == 0 || part == ".")
                {
                    continue;
                }

                if (part == "..")
                {
                    if (parts.Count == 0)
                    {
                        throw new YlvaUserException("cannot escape sandbox root");
                    }

                    parts.RemoveAt(parts.Count - 1);
                    continue;
                }

                if (part.Length > 96)
                {
                    throw new YlvaUserException("path segment is too long");
                }

                parts.Add(part);
            }

            if (parts.Count == 0)
            {
                return "/";
            }

            return "/" + string.Join("/", parts.ToArray());
        }

        public bool DirectoryExists(string path)
        {
            YlvaVfsEntry entry = Find(path);
            return entry != null && entry.IsDirectory;
        }

        public bool FileExists(string path)
        {
            YlvaVfsEntry entry = Find(path);
            return entry != null && !entry.IsDirectory;
        }

        public YlvaVfsEntry GetEntry(string cwd, string rawPath)
        {
            return RequireEntry(NormalizePath(cwd, rawPath));
        }

        public byte[] ReadBytes(string cwd, string rawPath)
        {
            string path = NormalizePath(cwd, rawPath);
            YlvaVfsEntry entry = RequireEntry(path);
            if (entry.IsDirectory)
            {
                throw new YlvaUserException(path + ": is a directory");
            }

            return GetBytes(entry);
        }

        public string ReadFile(string cwd, string rawPath)
        {
            string path = NormalizePath(cwd, rawPath);
            YlvaVfsEntry entry = RequireEntry(path);
            if (entry.IsDirectory)
            {
                throw new YlvaUserException(path + ": is a directory");
            }

            if (entry.Data != null)
            {
                return Encoding.UTF8.GetString(entry.Data);
            }

            return entry.Content ?? string.Empty;
        }

        public void WriteFile(string cwd, string rawPath, string content, bool append)
        {
            string path = NormalizePath(cwd, rawPath);
            if (path == "/")
            {
                throw new YlvaUserException("cannot write to /");
            }

            string parent = GetParentPath(path);
            if (!DirectoryExists(parent))
            {
                throw new YlvaUserException(parent + ": no such directory");
            }

            YlvaVfsEntry entry = Find(path);
            if (entry != null && entry.IsDirectory)
            {
                throw new YlvaUserException(path + ": is a directory");
            }

            string next = append && entry != null ? (entry.Content ?? string.Empty) + content : content;
            EnsureCapacity(next, entry);

            if (entry == null)
            {
                EnsureEntryCount();
                state.Files.Add(new YlvaVfsEntry
                {
                    Path = path,
                    IsDirectory = false,
                    Executable = false,
                    Content = next,
                    Data = null,
                    ModifiedUtc = DateTime.UtcNow
                });
            }
            else
            {
                entry.Content = next;
                entry.Data = null;
                entry.Executable = false;
                entry.ModifiedUtc = DateTime.UtcNow;
            }
        }

        public void WriteBytes(string cwd, string rawPath, byte[] data, bool executable)
        {
            string path = NormalizePath(cwd, rawPath);
            if (path == "/")
            {
                throw new YlvaUserException("cannot write to /");
            }

            string parent = GetParentPath(path);
            if (!DirectoryExists(parent))
            {
                throw new YlvaUserException(parent + ": no such directory");
            }

            YlvaVfsEntry entry = Find(path);
            if (entry != null && entry.IsDirectory)
            {
                throw new YlvaUserException(path + ": is a directory");
            }

            EnsureCapacity(data ?? new byte[0], entry);
            if (entry == null)
            {
                EnsureEntryCount();
                state.Files.Add(new YlvaVfsEntry
                {
                    Path = path,
                    IsDirectory = false,
                    Executable = executable,
                    Content = null,
                    Data = data ?? new byte[0],
                    ModifiedUtc = DateTime.UtcNow
                });
            }
            else
            {
                entry.Content = null;
                entry.Data = data ?? new byte[0];
                entry.Executable = executable;
                entry.ModifiedUtc = DateTime.UtcNow;
            }
        }

        public void Touch(string cwd, string rawPath)
        {
            string path = NormalizePath(cwd, rawPath);
            YlvaVfsEntry entry = Find(path);
            if (entry == null)
            {
                WriteFile(cwd, rawPath, string.Empty, append: false);
                return;
            }

            if (entry.IsDirectory)
            {
                throw new YlvaUserException(path + ": is a directory");
            }

            entry.ModifiedUtc = DateTime.UtcNow;
        }

        public void MakeDirectory(string cwd, string rawPath, bool recursive)
        {
            string path = NormalizePath(cwd, rawPath);
            if (path == "/")
            {
                return;
            }

            YlvaVfsEntry existing = Find(path);
            if (existing != null)
            {
                if (existing.IsDirectory)
                {
                    return;
                }

                throw new YlvaUserException(path + ": file exists");
            }

            string parent = GetParentPath(path);
            if (!DirectoryExists(parent))
            {
                if (!recursive)
                {
                    throw new YlvaUserException(parent + ": no such directory");
                }

                MakeDirectory("/", parent, recursive: true);
            }

            EnsureEntryCount();
            state.Files.Add(new YlvaVfsEntry
            {
                Path = path,
                IsDirectory = true,
                Content = string.Empty,
                ModifiedUtc = DateTime.UtcNow
            });
        }

        public void Remove(string cwd, string rawPath, bool recursive)
        {
            string path = NormalizePath(cwd, rawPath);
            if (path == "/")
            {
                throw new YlvaUserException("refusing to remove /");
            }

            YlvaVfsEntry entry = RequireEntry(path);
            if (entry.IsDirectory)
            {
                List<YlvaVfsEntry> children = GetDescendants(path).ToList();
                if (children.Count > 0 && !recursive)
                {
                    throw new YlvaUserException(path + ": directory not empty");
                }

                foreach (YlvaVfsEntry child in children)
                {
                    state.Files.Remove(child);
                }
            }

            state.Files.Remove(entry);
        }

        public List<YlvaVfsListItem> ListDirectory(string cwd, string rawPath)
        {
            string path = NormalizePath(cwd, rawPath);
            YlvaVfsEntry entry = RequireEntry(path);
            if (!entry.IsDirectory)
            {
                return new List<YlvaVfsListItem>
                {
                    ToListItem(entry)
                };
            }

            string prefix = path == "/" ? "/" : path + "/";
            List<YlvaVfsListItem> result = new List<YlvaVfsListItem>();
            foreach (YlvaVfsEntry candidate in state.Files)
            {
                if (candidate.Path == path || !candidate.Path.StartsWith(prefix, StringComparison.Ordinal))
                {
                    continue;
                }

                string rest = candidate.Path.Substring(prefix.Length);
                if (rest.Length == 0 || rest.IndexOf('/') >= 0)
                {
                    continue;
                }

                result.Add(ToListItem(candidate));
            }

            return result
                .OrderByDescending(item => item.IsDirectory)
                .ThenBy(item => item.Name, StringComparer.Ordinal)
                .ToList();
        }

        private static YlvaVfsEntry Directory(string path, DateTime modifiedUtc)
        {
            return new YlvaVfsEntry
            {
                Path = path,
                IsDirectory = true,
                Executable = false,
                Content = string.Empty,
                Data = null,
                ModifiedUtc = modifiedUtc
            };
        }

        private static YlvaVfsEntry File(string path, string content, DateTime modifiedUtc)
        {
            return new YlvaVfsEntry
            {
                Path = path,
                IsDirectory = false,
                Executable = false,
                Content = content,
                Data = null,
                ModifiedUtc = modifiedUtc
            };
        }

        internal static YlvaVfsEntry FileBinary(string path, byte[] data, DateTime modifiedUtc, bool executable)
        {
            return new YlvaVfsEntry
            {
                Path = path,
                IsDirectory = false,
                Executable = executable,
                Content = null,
                Data = data ?? new byte[0],
                ModifiedUtc = modifiedUtc
            };
        }

        private static string Combine(string cwd, string rawPath)
        {
            if (string.IsNullOrEmpty(cwd))
            {
                cwd = "/";
            }

            if (cwd.EndsWith("/", StringComparison.Ordinal))
            {
                return cwd + rawPath;
            }

            return cwd + "/" + rawPath;
        }

        private static string GetParentPath(string path)
        {
            int index = path.LastIndexOf('/');
            if (index <= 0)
            {
                return "/";
            }

            return path.Substring(0, index);
        }

        private YlvaVfsEntry Find(string normalizedPath)
        {
            return state.Files.FirstOrDefault(entry => entry.Path == normalizedPath);
        }

        private YlvaVfsEntry RequireEntry(string normalizedPath)
        {
            YlvaVfsEntry entry = Find(normalizedPath);
            if (entry == null)
            {
                throw new YlvaUserException(normalizedPath + ": no such file or directory");
            }

            return entry;
        }

        private IEnumerable<YlvaVfsEntry> GetDescendants(string normalizedDirectory)
        {
            string prefix = normalizedDirectory == "/" ? "/" : normalizedDirectory + "/";
            return state.Files
                .Where(entry => entry.Path != normalizedDirectory && entry.Path.StartsWith(prefix, StringComparison.Ordinal))
                .ToList();
        }

        private YlvaVfsListItem ToListItem(YlvaVfsEntry entry)
        {
            return new YlvaVfsListItem
            {
                Name = entry.Path == "/" ? "/" : entry.Path.Substring(entry.Path.LastIndexOf('/') + 1),
                IsDirectory = entry.IsDirectory,
                Size = entry.IsDirectory ? 0 : GetBytes(entry).Length,
                ModifiedUtc = entry.ModifiedUtc
            };
        }

        private void EnsureEntryCount()
        {
            if (state.Files.Count >= MaxEntries)
            {
                throw new YlvaUserException("filesystem entry limit reached");
            }
        }

        private void EnsureCapacity(string nextContent, YlvaVfsEntry replacing)
        {
            EnsureCapacity(Encoding.UTF8.GetBytes(nextContent ?? string.Empty), replacing);
        }

        private void EnsureCapacity(byte[] nextContent, YlvaVfsEntry replacing)
        {
            int nextBytes = nextContent == null ? 0 : nextContent.Length;
            if (nextBytes > MaxFileBytes)
            {
                throw new YlvaUserException("file size limit is 64 KiB");
            }

            int total = nextBytes;
            foreach (YlvaVfsEntry entry in state.Files)
            {
                if (entry == replacing || entry.IsDirectory)
                {
                    continue;
                }

                total += GetBytes(entry).Length;
            }

            if (total > MaxTotalBytes)
            {
                throw new YlvaUserException("filesystem quota is 2 MiB");
            }
        }

        private static byte[] GetBytes(YlvaVfsEntry entry)
        {
            if (entry == null)
            {
                return new byte[0];
            }

            if (entry.Data != null)
            {
                return entry.Data;
            }

            return Encoding.UTF8.GetBytes(entry.Content ?? string.Empty);
        }
    }
}
