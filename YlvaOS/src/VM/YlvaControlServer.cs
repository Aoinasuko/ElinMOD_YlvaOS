using System;
using System.Collections.Concurrent;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using BepInEx.Logging;

namespace YlvaOS
{
    internal sealed class YlvaControlServer : IDisposable
    {
        private readonly string token;
        private readonly ManualLogSource log;
        private readonly ConcurrentQueue<string> messages = new ConcurrentQueue<string>();
        private TcpListener listener;
        private CancellationTokenSource cancellation;
        private bool disposed;

        public YlvaControlServer(string token, ManualLogSource log)
        {
            this.token = token ?? string.Empty;
            this.log = log;
        }

        public int Port { get; private set; }

        public void Start()
        {
            if (listener != null)
            {
                return;
            }

            cancellation = new CancellationTokenSource();
            listener = new TcpListener(IPAddress.Loopback, 0);
            listener.Start(1);
            Port = ((IPEndPoint)listener.LocalEndpoint).Port;
            System.Threading.Tasks.Task.Run(() => AcceptLoop(cancellation.Token));
        }

        public int DrainMessages(Action<string> handle, int maxMessages)
        {
            if (handle == null)
            {
                return 0;
            }

            int count = 0;
            string message;
            while (count < maxMessages && messages.TryDequeue(out message))
            {
                handle(message);
                count++;
            }

            return count;
        }

        public void Dispose()
        {
            if (disposed)
            {
                return;
            }

            disposed = true;
            try
            {
                if (cancellation != null)
                {
                    cancellation.Cancel();
                }
            }
            catch
            {
            }

            try
            {
                if (listener != null)
                {
                    listener.Stop();
                }
            }
            catch
            {
            }

            listener = null;
            Port = 0;
        }

        private void AcceptLoop(CancellationToken token)
        {
            while (!token.IsCancellationRequested)
            {
                TcpClient client = null;
                try
                {
                    client = listener.AcceptTcpClient();
                    client.NoDelay = true;
                    System.Threading.Tasks.Task.Run(() => ReadClient(client, token));
                }
                catch (ObjectDisposedException)
                {
                    return;
                }
                catch (SocketException)
                {
                    if (!token.IsCancellationRequested && log != null)
                    {
                        log.LogWarning("YlvaOS control listener stopped unexpectedly.");
                    }

                    return;
                }
                catch (Exception ex)
                {
                    if (!token.IsCancellationRequested && log != null)
                    {
                        log.LogWarning("YlvaOS control accept failed: " + ex.Message);
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
                }
            }
        }

        private void ReadClient(TcpClient client, CancellationToken token)
        {
            using (client)
            {
                NetworkStream stream;
                try
                {
                    stream = client.GetStream();
                }
                catch
                {
                    return;
                }

                byte[] buffer = new byte[256];
                StringBuilder line = new StringBuilder(256);
                while (!token.IsCancellationRequested)
                {
                    int read;
                    try
                    {
                        read = stream.Read(buffer, 0, buffer.Length);
                    }
                    catch
                    {
                        return;
                    }

                    if (read <= 0)
                    {
                        return;
                    }

                    for (int i = 0; i < read; i++)
                    {
                        char ch = (char)buffer[i];
                        if (ch == '\n')
                        {
                            ProcessLine(line.ToString().Trim());
                            line.Length = 0;
                            continue;
                        }

                        if (ch != '\r' && line.Length < 1024)
                        {
                            line.Append(ch);
                        }
                    }
                }
            }
        }

        private void ProcessLine(string line)
        {
            if (string.IsNullOrWhiteSpace(line))
            {
                return;
            }

            const string prefix = "YLVAOS ";
            if (!line.StartsWith(prefix, StringComparison.Ordinal))
            {
                return;
            }

            string body = line.Substring(prefix.Length);
            int separator = body.IndexOf(' ');
            if (separator <= 0)
            {
                return;
            }

            string suppliedToken = body.Substring(0, separator);
            if (!ConstantTimeEquals(suppliedToken, token))
            {
                if (log != null)
                {
                    log.LogWarning("Rejected YlvaOS control message with an invalid token.");
                }

                return;
            }

            string command = body.Substring(separator + 1).Trim();
            if (command.Length > 0)
            {
                messages.Enqueue(command);
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
    }
}
