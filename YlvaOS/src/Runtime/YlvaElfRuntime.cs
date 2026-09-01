using System;
using System.Collections.Generic;
using System.Linq;
using System.Text;

namespace YlvaOS
{
    internal static class YlvaElfFactory
    {
        private const ulong BaseAddress = 0x400000UL;
        private const int HeaderSize = 64;
        private const int ProgramHeaderSize = 56;
        private const int CodeOffset = 0x100;

        public static List<YlvaVfsEntry> CreateDefaultEntries(DateTime modifiedUtc)
        {
            return new List<YlvaVfsEntry>
            {
                YlvaVfs.FileBinary("/bin/hello", BuildWriteExit("Hello from an ELF64 binary running through Linux syscall emulation.\n", 0), modifiedUtc, executable: true),
                YlvaVfs.FileBinary("/bin/uname", BuildWriteExit("Linux\n", 0), modifiedUtc, executable: true),
                YlvaVfs.FileBinary("/bin/id", BuildWriteExit("uid=1000(player) gid=1000(player) groups=1000(player)\n", 0), modifiedUtc, executable: true),
                YlvaVfs.FileBinary("/bin/true", BuildExit(0), modifiedUtc, executable: true),
                YlvaVfs.FileBinary("/bin/false", BuildExit(1), modifiedUtc, executable: true),
                YlvaVfs.FileBinary("/bin/syscall-demo", BuildWriteExit("sys_write(1, buf, len) and sys_exit(0) completed inside YlvaOS.\n", 0), modifiedUtc, executable: true)
            };
        }

        private static byte[] BuildExit(int exitCode)
        {
            List<byte> code = new List<byte>();
            EmitMovR64Imm32(code, 0xC0, 60);
            EmitMovR64Imm32(code, 0xC7, exitCode);
            EmitSyscall(code);
            return BuildExecutable(code, new byte[0]);
        }

        private static byte[] BuildWriteExit(string text, int exitCode)
        {
            byte[] data = Encoding.UTF8.GetBytes(text ?? string.Empty);
            List<byte> code = new List<byte>();

            EmitMovR64Imm32(code, 0xC0, 1);
            EmitMovR64Imm32(code, 0xC7, 1);
            int leaOffset = code.Count;
            EmitLeaRsiRipRelative(code, 0);
            EmitMovR64Imm32(code, 0xC2, data.Length);
            EmitSyscall(code);
            EmitMovR64Imm32(code, 0xC0, 60);
            EmitMovR64Imm32(code, 0xC7, exitCode);
            EmitSyscall(code);

            ulong dataAddress = BaseAddress + (ulong)(CodeOffset + code.Count);
            ulong ripAfterLea = BaseAddress + (ulong)(CodeOffset + leaOffset + 7);
            int displacement = checked((int)(dataAddress - ripAfterLea));
            PatchInt32(code, leaOffset + 3, displacement);

            return BuildExecutable(code, data);
        }

        private static byte[] BuildExecutable(List<byte> code, byte[] data)
        {
            int dataOffset = CodeOffset + code.Count;
            int fileSize = dataOffset + data.Length;
            byte[] file = new byte[fileSize];

            WriteElfHeader(file, BaseAddress + CodeOffset, fileSize);
            WriteProgramHeader(file, fileSize);
            Array.Copy(code.ToArray(), 0, file, CodeOffset, code.Count);
            Array.Copy(data, 0, file, dataOffset, data.Length);
            return file;
        }

        private static void EmitMovR64Imm32(List<byte> code, byte registerModRm, int value)
        {
            code.Add(0x48);
            code.Add(0xC7);
            code.Add(registerModRm);
            WriteInt32(code, value);
        }

        private static void EmitLeaRsiRipRelative(List<byte> code, int displacement)
        {
            code.Add(0x48);
            code.Add(0x8D);
            code.Add(0x35);
            WriteInt32(code, displacement);
        }

        private static void EmitSyscall(List<byte> code)
        {
            code.Add(0x0F);
            code.Add(0x05);
        }

        private static void WriteElfHeader(byte[] file, ulong entry, int fileSize)
        {
            file[0] = 0x7F;
            file[1] = (byte)'E';
            file[2] = (byte)'L';
            file[3] = (byte)'F';
            file[4] = 2;
            file[5] = 1;
            file[6] = 1;
            file[7] = 3;
            WriteUInt16(file, 16, 2);
            WriteUInt16(file, 18, 0x3E);
            WriteUInt32(file, 20, 1);
            WriteUInt64(file, 24, entry);
            WriteUInt64(file, 32, HeaderSize);
            WriteUInt64(file, 40, 0);
            WriteUInt32(file, 48, 0);
            WriteUInt16(file, 52, HeaderSize);
            WriteUInt16(file, 54, ProgramHeaderSize);
            WriteUInt16(file, 56, 1);
            WriteUInt16(file, 58, 0);
            WriteUInt16(file, 60, 0);
            WriteUInt16(file, 62, 0);
        }

        private static void WriteProgramHeader(byte[] file, int fileSize)
        {
            int offset = HeaderSize;
            WriteUInt32(file, offset + 0, 1);
            WriteUInt32(file, offset + 4, 7);
            WriteUInt64(file, offset + 8, 0);
            WriteUInt64(file, offset + 16, BaseAddress);
            WriteUInt64(file, offset + 24, BaseAddress);
            WriteUInt64(file, offset + 32, (ulong)fileSize);
            WriteUInt64(file, offset + 40, (ulong)fileSize);
            WriteUInt64(file, offset + 48, 0x1000);
        }

        private static void WriteInt32(List<byte> code, int value)
        {
            byte[] bytes = BitConverter.GetBytes(value);
            code.AddRange(bytes);
        }

        private static void PatchInt32(List<byte> code, int offset, int value)
        {
            byte[] bytes = BitConverter.GetBytes(value);
            for (int i = 0; i < 4; i++)
            {
                code[offset + i] = bytes[i];
            }
        }

        private static void WriteUInt16(byte[] data, int offset, ushort value)
        {
            byte[] bytes = BitConverter.GetBytes(value);
            Array.Copy(bytes, 0, data, offset, 2);
        }

        private static void WriteUInt32(byte[] data, int offset, uint value)
        {
            byte[] bytes = BitConverter.GetBytes(value);
            Array.Copy(bytes, 0, data, offset, 4);
        }

        private static void WriteUInt64(byte[] data, int offset, ulong value)
        {
            byte[] bytes = BitConverter.GetBytes(value);
            Array.Copy(bytes, 0, data, offset, 8);
        }
    }

    internal sealed class YlvaElfRunner
    {
        private readonly YlvaMachine machine;
        private readonly YlvaVfs vfs;

        public YlvaElfRunner(YlvaMachine machine, YlvaVfs vfs)
        {
            this.machine = machine;
            this.vfs = vfs;
        }

        public bool TryExecuteCommand(string commandName, IList<string> args)
        {
            string rawPath = commandName.IndexOf('/') >= 0 ? commandName : "/bin/" + commandName;
            YlvaVfsEntry entry;
            try
            {
                entry = vfs.GetEntry(machine.WorkingDirectory, rawPath);
            }
            catch (YlvaUserException)
            {
                return false;
            }

            if (entry.IsDirectory || !entry.Executable)
            {
                return false;
            }

            Execute(rawPath, args);
            return true;
        }

        public int Execute(string rawPath, IList<string> args)
        {
            byte[] bytes = vfs.ReadBytes(machine.WorkingDirectory, rawPath);
            YlvaElfImage image = YlvaElfImage.Load(bytes);
            YlvaLinuxProcess process = new YlvaLinuxProcess(machine, vfs, image, args);
            return process.Run();
        }

        public string Describe(string rawPath)
        {
            byte[] bytes = vfs.ReadBytes(machine.WorkingDirectory, rawPath);
            YlvaElfImage image = YlvaElfImage.Load(bytes);
            return image.Description;
        }
    }

    internal sealed class YlvaElfImage
    {
        public ulong Entry { get; private set; }
        public YlvaMemory Memory { get; private set; }
        public string Description { get; private set; }

        public static YlvaElfImage Load(byte[] bytes)
        {
            if (bytes == null || bytes.Length < 64)
            {
                throw new YlvaUserException("not an ELF file");
            }

            if (bytes[0] != 0x7F || bytes[1] != 'E' || bytes[2] != 'L' || bytes[3] != 'F')
            {
                throw new YlvaUserException("not an ELF file");
            }

            if (bytes[4] != 2 || bytes[5] != 1)
            {
                throw new YlvaUserException("only ELF64 little-endian executables are supported");
            }

            ushort type = ReadUInt16(bytes, 16);
            ushort machine = ReadUInt16(bytes, 18);
            ulong entry = ReadUInt64(bytes, 24);
            ulong phoff = ReadUInt64(bytes, 32);
            ushort phentsize = ReadUInt16(bytes, 54);
            ushort phnum = ReadUInt16(bytes, 56);

            if (type != 2)
            {
                throw new YlvaUserException("only ET_EXEC ELF files are supported");
            }

            if (machine != 0x3E)
            {
                throw new YlvaUserException("only x86_64 ELF files are supported");
            }

            if (phoff > (ulong)bytes.Length || phentsize < 56)
            {
                throw new YlvaUserException("invalid ELF program header table");
            }

            YlvaMemory memory = new YlvaMemory();
            for (int i = 0; i < phnum; i++)
            {
                int offset = checked((int)phoff + i * phentsize);
                if (offset + 56 > bytes.Length)
                {
                    throw new YlvaUserException("truncated ELF program header");
                }

                uint pType = ReadUInt32(bytes, offset + 0);
                if (pType != 1)
                {
                    continue;
                }

                ulong pOffset = ReadUInt64(bytes, offset + 8);
                ulong pVaddr = ReadUInt64(bytes, offset + 16);
                ulong pFilesz = ReadUInt64(bytes, offset + 32);
                ulong pMemsz = ReadUInt64(bytes, offset + 40);

                if (pOffset + pFilesz > (ulong)bytes.Length)
                {
                    throw new YlvaUserException("truncated ELF load segment");
                }

                if (pMemsz > 256 * 1024)
                {
                    throw new YlvaUserException("ELF segment exceeds sandbox memory limit");
                }

                byte[] segment = new byte[pMemsz];
                Array.Copy(bytes, (long)pOffset, segment, 0, (long)pFilesz);
                memory.Load(pVaddr, segment);
            }

            return new YlvaElfImage
            {
                Entry = entry,
                Memory = memory,
                Description = "ELF 64-bit LSB executable, x86-64, statically linked, YlvaOS/Linux syscall ABI"
            };
        }

        private static ushort ReadUInt16(byte[] bytes, int offset)
        {
            return BitConverter.ToUInt16(bytes, offset);
        }

        private static uint ReadUInt32(byte[] bytes, int offset)
        {
            return BitConverter.ToUInt32(bytes, offset);
        }

        private static ulong ReadUInt64(byte[] bytes, int offset)
        {
            return BitConverter.ToUInt64(bytes, offset);
        }
    }

    internal sealed class YlvaMemory
    {
        private readonly Dictionary<ulong, byte> bytes = new Dictionary<ulong, byte>();

        public void Load(ulong address, byte[] data)
        {
            for (int i = 0; i < data.Length; i++)
            {
                bytes[address + (ulong)i] = data[i];
            }
        }

        public byte Read8(ulong address)
        {
            byte value;
            return bytes.TryGetValue(address, out value) ? value : (byte)0;
        }

        public int ReadInt32(ulong address)
        {
            return BitConverter.ToInt32(ReadBytes(address, 4), 0);
        }

        public byte[] ReadBytes(ulong address, int count)
        {
            byte[] result = new byte[count];
            for (int i = 0; i < count; i++)
            {
                result[i] = Read8(address + (ulong)i);
            }

            return result;
        }

        public void WriteBytes(ulong address, byte[] data)
        {
            for (int i = 0; i < data.Length; i++)
            {
                bytes[address + (ulong)i] = data[i];
            }
        }

        public string ReadCString(ulong address, int maxLength)
        {
            List<byte> data = new List<byte>();
            for (int i = 0; i < maxLength; i++)
            {
                byte b = Read8(address + (ulong)i);
                if (b == 0)
                {
                    break;
                }

                data.Add(b);
            }

            return Encoding.UTF8.GetString(data.ToArray());
        }
    }

    internal sealed class YlvaLinuxProcess
    {
        private const int MaxInstructions = 10000;
        private const int MaxWriteBytes = 8192;
        private readonly YlvaMachine machine;
        private readonly YlvaVfs vfs;
        private readonly YlvaMemory memory;
        private readonly IList<string> args;
        private bool exited;
        private int exitCode;

        private ulong rax;
        private ulong rdi;
        private ulong rsi;
        private ulong rdx;
        private ulong rip;
        private ulong brk = 0x800000UL;

        public YlvaLinuxProcess(YlvaMachine machine, YlvaVfs vfs, YlvaElfImage image, IList<string> args)
        {
            this.machine = machine;
            this.vfs = vfs;
            this.memory = image.Memory;
            this.args = args ?? new List<string>();
            rip = image.Entry;
        }

        public int Run()
        {
            int instructions = 0;
            while (!exited)
            {
                if (++instructions > MaxInstructions)
                {
                    throw new YlvaUserException("ELF instruction limit exceeded");
                }

                Step();
            }

            return exitCode;
        }

        private void Step()
        {
            byte op = memory.Read8(rip);
            if (op == 0x0F && memory.Read8(rip + 1) == 0x05)
            {
                rip += 2;
                HandleSyscall();
                return;
            }

            if (op == 0x48)
            {
                byte op2 = memory.Read8(rip + 1);
                byte op3 = memory.Read8(rip + 2);

                if (op2 == 0xC7)
                {
                    int value = memory.ReadInt32(rip + 3);
                    switch (op3)
                    {
                        case 0xC0:
                            rax = unchecked((ulong)(long)value);
                            break;
                        case 0xC2:
                            rdx = unchecked((ulong)(long)value);
                            break;
                        case 0xC6:
                            rsi = unchecked((ulong)(long)value);
                            break;
                        case 0xC7:
                            rdi = unchecked((ulong)(long)value);
                            break;
                        default:
                            throw UnsupportedInstruction();
                    }

                    rip += 7;
                    return;
                }

                if (op2 == 0x8D && op3 == 0x35)
                {
                    int displacement = memory.ReadInt32(rip + 3);
                    rsi = unchecked((ulong)((long)(rip + 7) + displacement));
                    rip += 7;
                    return;
                }

                if (op2 == 0x31 && op3 == 0xFF)
                {
                    rdi = 0;
                    rip += 3;
                    return;
                }
            }

            if (op >= 0xB8 && op <= 0xBF)
            {
                ulong value = BitConverter.ToUInt32(memory.ReadBytes(rip + 1, 4), 0);
                switch (op)
                {
                    case 0xB8:
                        rax = value;
                        break;
                    case 0xBA:
                        rdx = value;
                        break;
                    case 0xBE:
                        rsi = value;
                        break;
                    case 0xBF:
                        rdi = value;
                        break;
                    default:
                        throw UnsupportedInstruction();
                }

                rip += 5;
                return;
            }

            throw UnsupportedInstruction();
        }

        private void HandleSyscall()
        {
            switch (rax)
            {
                case 1:
                    rax = SysWrite((int)rdi, rsi, checked((int)rdx));
                    return;
                case 12:
                    rax = SysBrk(rdi);
                    return;
                case 39:
                    rax = 100;
                    return;
                case 60:
                    exitCode = (int)rdi;
                    exited = true;
                    return;
                case 63:
                    rax = SysUname(rdi);
                    return;
                case 79:
                    rax = SysGetCwd(rdi, checked((int)rsi));
                    return;
                case 80:
                    rax = SysChdir(rdi);
                    return;
                case 102:
                case 104:
                case 107:
                case 108:
                    rax = 1000;
                    return;
                case 231:
                    exitCode = (int)rdi;
                    exited = true;
                    return;
                default:
                    machine.Append("ylva-kernel: unsupported syscall " + rax);
                    rax = Errno(38);
                    return;
            }
        }

        private ulong SysWrite(int fd, ulong buffer, int count)
        {
            if (count < 0)
            {
                return Errno(22);
            }

            if (count > MaxWriteBytes)
            {
                count = MaxWriteBytes;
            }

            byte[] data = memory.ReadBytes(buffer, count);
            if (fd == 1 || fd == 2)
            {
                string text = Encoding.UTF8.GetString(data);
                machine.Append(text.TrimEnd('\n'));
                return (ulong)count;
            }

            return Errno(9);
        }

        private ulong SysBrk(ulong address)
        {
            if (address == 0)
            {
                return brk;
            }

            if (address < 0x800000UL || address > 0x840000UL)
            {
                return brk;
            }

            brk = address;
            return brk;
        }

        private ulong SysUname(ulong address)
        {
            List<byte> uts = new List<byte>();
            AddUtsField(uts, "Linux");
            AddUtsField(uts, "ylva");
            AddUtsField(uts, "0.1.0-ylva");
            AddUtsField(uts, "#1 SMP YlvaOS");
            AddUtsField(uts, "x86_64");
            AddUtsField(uts, "ylva.local");
            memory.WriteBytes(address, uts.ToArray());
            return 0;
        }

        private ulong SysGetCwd(ulong address, int size)
        {
            if (size <= 0)
            {
                return Errno(22);
            }

            byte[] cwd = Encoding.UTF8.GetBytes(machine.WorkingDirectory + "\0");
            if (cwd.Length > size)
            {
                return Errno(34);
            }

            memory.WriteBytes(address, cwd);
            return address;
        }

        private ulong SysChdir(ulong address)
        {
            string path = memory.ReadCString(address, 4096);
            string normalized = vfs.NormalizePath(machine.WorkingDirectory, path);
            if (!vfs.DirectoryExists(normalized))
            {
                return Errno(2);
            }

            machine.WorkingDirectory = normalized;
            return 0;
        }

        private static void AddUtsField(List<byte> data, string text)
        {
            byte[] bytes = Encoding.ASCII.GetBytes(text ?? string.Empty);
            int length = Math.Min(bytes.Length, 64);
            for (int i = 0; i < 65; i++)
            {
                data.Add(i < length ? bytes[i] : (byte)0);
            }
        }

        private Exception UnsupportedInstruction()
        {
            return new YlvaUserException("unsupported x86_64 instruction at 0x" + rip.ToString("x"));
        }

        private static ulong Errno(int errno)
        {
            return unchecked((ulong)(long)-errno);
        }
    }
}
