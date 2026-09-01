using System;
using System.Net.Sockets;
using System.Text;
using System.Threading.Tasks;
using BepInEx.Logging;

namespace YlvaOS
{
    internal sealed class YlvaVncClient : IDisposable
    {
        private const int EncodingRaw = 0;
        private const int EncodingCopyRect = 1;
        private const int EncodingDesktopSize = -223;

        private readonly ManualLogSource log;
        private readonly object frameLock = new object();
        private readonly object writeLock = new object();
        private TcpClient client;
        private NetworkStream stream;
        private byte[] framebufferRgba;
        private int framebufferWidth;
        private int framebufferHeight;
        private int frameVersion;
        private int copiedFrameVersion;
        private bool disposed;
        private bool stopping;

        public YlvaVncClient(ManualLogSource log)
        {
            this.log = log;
        }

        public bool IsConnected { get; private set; }

        public bool TryConnect(string host, int port, int timeoutMs)
        {
            if (disposed)
            {
                return false;
            }

            if (IsConnected)
            {
                return true;
            }

            try
            {
                CloseSocket();
                stopping = false;
                TcpClient next = new TcpClient();
                IAsyncResult result = next.BeginConnect(host, port, null, null);
                if (!result.AsyncWaitHandle.WaitOne(Math.Max(1, timeoutMs)))
                {
                    next.Close();
                    return false;
                }

                next.EndConnect(result);
                next.NoDelay = true;
                client = next;
                stream = next.GetStream();
                Handshake();
                IsConnected = true;
                System.Threading.Tasks.Task.Run(() => ReadLoop());
                return true;
            }
            catch (Exception ex)
            {
                if (log != null)
                {
                    log.LogWarning("YlvaOS VNC connect failed: " + ex.Message);
                }

                CloseSocket();
                return false;
            }
        }

        public bool TryCopyFrame(out int width, out int height, out byte[] rgba)
        {
            lock (frameLock)
            {
                if (framebufferRgba == null || frameVersion == copiedFrameVersion)
                {
                    width = 0;
                    height = 0;
                    rgba = null;
                    return false;
                }

                width = framebufferWidth;
                height = framebufferHeight;
                rgba = new byte[framebufferRgba.Length];
                Buffer.BlockCopy(framebufferRgba, 0, rgba, 0, rgba.Length);
                copiedFrameVersion = frameVersion;
                return true;
            }
        }

        public void SendKey(uint keySym, bool down)
        {
            byte[] message = new byte[8];
            message[0] = 4;
            message[1] = down ? (byte)1 : (byte)0;
            WriteUInt32(message, 4, keySym);
            TryWrite(message);
        }

        public void SendPointer(int x, int y, int buttonMask)
        {
            int width;
            int height;
            lock (frameLock)
            {
                width = framebufferWidth;
                height = framebufferHeight;
            }

            if (width <= 0 || height <= 0)
            {
                return;
            }

            x = Clamp(x, 0, width - 1);
            y = Clamp(y, 0, height - 1);

            byte[] message = new byte[6];
            message[0] = 5;
            message[1] = (byte)(buttonMask & 0xff);
            WriteUInt16(message, 2, (ushort)x);
            WriteUInt16(message, 4, (ushort)y);
            TryWrite(message);
        }

        public void Dispose()
        {
            disposed = true;
            stopping = true;
            CloseSocket();
        }

        private void Handshake()
        {
            string protocol = ReadAscii(12);
            WriteAscii("RFB 003.008\n");

            int major;
            int minor;
            ParseProtocol(protocol, out major, out minor);
            if (major == 3 && minor == 3)
            {
                uint securityType = ReadUInt32();
                if (securityType != 1)
                {
                    throw new InvalidOperationException("VNC server does not offer no-auth security.");
                }
            }
            else
            {
                int count = ReadByteStrict();
                if (count == 0)
                {
                    uint length = ReadUInt32();
                    string reason = ReadAscii((int)length);
                    throw new InvalidOperationException("VNC security rejected: " + reason);
                }

                bool supportsNone = false;
                for (int i = 0; i < count; i++)
                {
                    if (ReadByteStrict() == 1)
                    {
                        supportsNone = true;
                    }
                }

                if (!supportsNone)
                {
                    throw new InvalidOperationException("VNC server does not offer no-auth security.");
                }

                WriteByte(1);
                uint result = ReadUInt32();
                if (result != 0)
                {
                    throw new InvalidOperationException("VNC security handshake failed.");
                }
            }

            WriteByte(1);
            int width = ReadUInt16();
            int height = ReadUInt16();
            ReadExact(16);
            uint nameLength = ReadUInt32();
            if (nameLength > 0)
            {
                ReadExact((int)Math.Min(nameLength, 65536));
                if (nameLength > 65536)
                {
                    ReadExact((int)(nameLength - 65536));
                }
            }

            ResizeFramebuffer(width, height);
            SetPixelFormat();
            SetEncodings();
            IsConnected = true;
            RequestFramebufferUpdate(false);
        }

        private void ReadLoop()
        {
            try
            {
                while (!stopping)
                {
                    int type = ReadByteStrict();
                    switch (type)
                    {
                        case 0:
                            ReadFramebufferUpdate();
                            RequestFramebufferUpdate(true);
                            break;
                        case 2:
                            break;
                        case 3:
                            ReadExact(3);
                            uint length = ReadUInt32();
                            if (length > 0)
                            {
                                ReadExact((int)Math.Min(length, 1024 * 1024));
                                if (length > 1024 * 1024)
                                {
                                    ReadExact((int)(length - 1024 * 1024));
                                }
                            }

                            break;
                        default:
                            throw new InvalidOperationException("Unsupported VNC server message: " + type);
                    }
                }
            }
            catch (Exception ex)
            {
                if (!stopping && log != null)
                {
                    log.LogWarning("YlvaOS VNC reader stopped: " + ex.Message);
                }
            }
            finally
            {
                IsConnected = false;
                CloseSocket();
            }
        }

        private void ReadFramebufferUpdate()
        {
            ReadByteStrict();
            int rectangles = ReadUInt16();
            for (int i = 0; i < rectangles; i++)
            {
                int x = ReadUInt16();
                int y = ReadUInt16();
                int width = ReadUInt16();
                int height = ReadUInt16();
                int encoding = ReadInt32();

                switch (encoding)
                {
                    case EncodingRaw:
                        ApplyRawRectangle(x, y, width, height);
                        break;
                    case EncodingCopyRect:
                        ApplyCopyRect(x, y, width, height);
                        break;
                    case EncodingDesktopSize:
                        ResizeFramebuffer(width, height);
                        break;
                    default:
                        throw new InvalidOperationException("Unsupported VNC rectangle encoding: " + encoding);
                }
            }
        }

        private void ApplyRawRectangle(int x, int y, int width, int height)
        {
            if (width <= 0 || height <= 0)
            {
                return;
            }

            byte[] raw = ReadExact(width * height * 4);
            lock (frameLock)
            {
                if (framebufferRgba == null)
                {
                    return;
                }

                int fbWidth = framebufferWidth;
                int fbHeight = framebufferHeight;
                for (int row = 0; row < height; row++)
                {
                    int targetY = fbHeight - 1 - (y + row);
                    if (targetY < 0 || targetY >= fbHeight)
                    {
                        continue;
                    }

                    int source = row * width * 4;
                    int target = (targetY * fbWidth + x) * 4;
                    for (int column = 0; column < width; column++)
                    {
                        int px = x + column;
                        if (px < 0 || px >= fbWidth)
                        {
                            source += 4;
                            continue;
                        }

                        framebufferRgba[target] = raw[source + 2];
                        framebufferRgba[target + 1] = raw[source + 1];
                        framebufferRgba[target + 2] = raw[source];
                        framebufferRgba[target + 3] = 255;
                        source += 4;
                        target += 4;
                    }
                }

                frameVersion++;
            }
        }

        private void ApplyCopyRect(int x, int y, int width, int height)
        {
            int sourceX = ReadUInt16();
            int sourceY = ReadUInt16();
            if (width <= 0 || height <= 0)
            {
                return;
            }

            lock (frameLock)
            {
                if (framebufferRgba == null)
                {
                    return;
                }

                int fbWidth = framebufferWidth;
                int fbHeight = framebufferHeight;
                byte[] copy = new byte[width * height * 4];
                for (int row = 0; row < height; row++)
                {
                    int storedSourceY = fbHeight - 1 - (sourceY + row);
                    if (storedSourceY < 0 || storedSourceY >= fbHeight)
                    {
                        continue;
                    }

                    int source = (storedSourceY * fbWidth + sourceX) * 4;
                    int target = row * width * 4;
                    int bytes = Math.Min(width, Math.Max(0, fbWidth - sourceX)) * 4;
                    if (source >= 0 && source + bytes <= framebufferRgba.Length && bytes > 0)
                    {
                        Buffer.BlockCopy(framebufferRgba, source, copy, target, bytes);
                    }
                }

                for (int row = 0; row < height; row++)
                {
                    int storedTargetY = fbHeight - 1 - (y + row);
                    if (storedTargetY < 0 || storedTargetY >= fbHeight)
                    {
                        continue;
                    }

                    int source = row * width * 4;
                    int target = (storedTargetY * fbWidth + x) * 4;
                    int bytes = Math.Min(width, Math.Max(0, fbWidth - x)) * 4;
                    if (target >= 0 && target + bytes <= framebufferRgba.Length && bytes > 0)
                    {
                        Buffer.BlockCopy(copy, source, framebufferRgba, target, bytes);
                    }
                }

                frameVersion++;
            }
        }

        private void ResizeFramebuffer(int width, int height)
        {
            if (width <= 0 || height <= 0 || width > 4096 || height > 4096)
            {
                throw new InvalidOperationException("Invalid VNC framebuffer size: " + width + "x" + height);
            }

            lock (frameLock)
            {
                framebufferWidth = width;
                framebufferHeight = height;
                framebufferRgba = new byte[width * height * 4];
                for (int i = 3; i < framebufferRgba.Length; i += 4)
                {
                    framebufferRgba[i] = 255;
                }

                frameVersion++;
            }
        }

        private void SetPixelFormat()
        {
            byte[] message = new byte[20];
            message[0] = 0;
            message[4] = 32;
            message[5] = 24;
            message[6] = 0;
            message[7] = 1;
            WriteUInt16(message, 8, 255);
            WriteUInt16(message, 10, 255);
            WriteUInt16(message, 12, 255);
            message[14] = 16;
            message[15] = 8;
            message[16] = 0;
            TryWrite(message);
        }

        private void SetEncodings()
        {
            byte[] message = new byte[4 + 3 * 4];
            message[0] = 2;
            WriteUInt16(message, 2, 3);
            WriteInt32(message, 4, EncodingRaw);
            WriteInt32(message, 8, EncodingCopyRect);
            WriteInt32(message, 12, EncodingDesktopSize);
            TryWrite(message);
        }

        private void RequestFramebufferUpdate(bool incremental)
        {
            int width;
            int height;
            lock (frameLock)
            {
                width = framebufferWidth;
                height = framebufferHeight;
            }

            if (width <= 0 || height <= 0)
            {
                return;
            }

            byte[] message = new byte[10];
            message[0] = 3;
            message[1] = incremental ? (byte)1 : (byte)0;
            WriteUInt16(message, 6, (ushort)width);
            WriteUInt16(message, 8, (ushort)height);
            TryWrite(message);
        }

        private void TryWrite(byte[] message)
        {
            if (message == null || message.Length == 0)
            {
                return;
            }

            lock (writeLock)
            {
                if (stream == null)
                {
                    return;
                }

                try
                {
                    stream.Write(message, 0, message.Length);
                    stream.Flush();
                }
                catch
                {
                    IsConnected = false;
                }
            }
        }

        private void WriteByte(byte value)
        {
            TryWrite(new[] { value });
        }

        private void WriteAscii(string text)
        {
            TryWrite(Encoding.ASCII.GetBytes(text));
        }

        private int ReadByteStrict()
        {
            int value = stream.ReadByte();
            if (value < 0)
            {
                throw new InvalidOperationException("VNC connection closed.");
            }

            return value;
        }

        private byte[] ReadExact(int length)
        {
            byte[] data = new byte[length];
            int offset = 0;
            while (offset < length)
            {
                int read = stream.Read(data, offset, length - offset);
                if (read <= 0)
                {
                    throw new InvalidOperationException("VNC connection closed.");
                }

                offset += read;
            }

            return data;
        }

        private string ReadAscii(int length)
        {
            return Encoding.ASCII.GetString(ReadExact(length));
        }

        private ushort ReadUInt16()
        {
            byte[] data = ReadExact(2);
            return (ushort)((data[0] << 8) | data[1]);
        }

        private uint ReadUInt32()
        {
            byte[] data = ReadExact(4);
            return ((uint)data[0] << 24) | ((uint)data[1] << 16) | ((uint)data[2] << 8) | data[3];
        }

        private int ReadInt32()
        {
            return unchecked((int)ReadUInt32());
        }

        private static void WriteUInt16(byte[] target, int offset, ushort value)
        {
            target[offset] = (byte)(value >> 8);
            target[offset + 1] = (byte)value;
        }

        private static void WriteUInt32(byte[] target, int offset, uint value)
        {
            target[offset] = (byte)(value >> 24);
            target[offset + 1] = (byte)(value >> 16);
            target[offset + 2] = (byte)(value >> 8);
            target[offset + 3] = (byte)value;
        }

        private static void WriteInt32(byte[] target, int offset, int value)
        {
            WriteUInt32(target, offset, unchecked((uint)value));
        }

        private static void ParseProtocol(string protocol, out int major, out int minor)
        {
            major = 3;
            minor = 8;
            if (string.IsNullOrEmpty(protocol) || protocol.Length < 11)
            {
                return;
            }

            int.TryParse(protocol.Substring(4, 3), out major);
            int.TryParse(protocol.Substring(8, 3), out minor);
        }

        private void CloseSocket()
        {
            IsConnected = false;
            try
            {
                if (stream != null)
                {
                    stream.Close();
                }
            }
            catch
            {
            }

            try
            {
                if (client != null)
                {
                    client.Close();
                }
            }
            catch
            {
            }

            stream = null;
            client = null;
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
}
