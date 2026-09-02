using System;
using System.Collections.Generic;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using BepInEx.Logging;

namespace YlvaOS
{
    internal sealed class YlvaHostInputServer : IDisposable
    {
        private readonly string token;
        private readonly ManualLogSource log;
        private readonly object clientsLock = new object();
        private readonly List<TcpClient> clients = new List<TcpClient>();
        private readonly HashSet<TcpClient> readyClients = new HashSet<TcpClient>();
        private TcpListener listener;
        private CancellationTokenSource cancellation;
        private bool disposed;

        public YlvaHostInputServer(string token, ManualLogSource log)
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

        public bool TrySendCommand(string command, out string message)
        {
            if (string.IsNullOrWhiteSpace(command))
            {
                message = "host input command is empty.";
                return false;
            }

            byte[] data = Encoding.ASCII.GetBytes("YLVAOS_HOST " + token + " " + command + "\n");
            List<TcpClient> snapshot;
            lock (clientsLock)
            {
                snapshot = new List<TcpClient>(readyClients);
            }

            if (snapshot.Count == 0)
            {
                message = "YlvaOS host input agent is not ready.";
                return false;
            }

            bool sent = false;
            foreach (TcpClient client in snapshot)
            {
                try
                {
                    NetworkStream stream = client.GetStream();
                    stream.Write(data, 0, data.Length);
                    stream.Flush();
                    sent = true;
                }
                catch
                {
                    RemoveClient(client);
                }
            }

            message = sent ? "YlvaOS host input command was sent." : "YlvaOS host input channel is not writable.";
            return sent;
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

            lock (clientsLock)
            {
                foreach (TcpClient client in clients)
                {
                    try
                    {
                        client.Close();
                    }
                    catch
                    {
                    }
                }

                clients.Clear();
                readyClients.Clear();
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
                    AddClient(client);
                    System.Threading.Tasks.Task.Run(() => ReadUntilClosed(client, token));
                }
                catch (ObjectDisposedException)
                {
                    return;
                }
                catch (SocketException)
                {
                    if (!token.IsCancellationRequested && log != null)
                    {
                        log.LogWarning("YlvaOS host input listener stopped unexpectedly.");
                    }

                    return;
                }
                catch (Exception ex)
                {
                    if (!token.IsCancellationRequested && log != null)
                    {
                        log.LogWarning("YlvaOS host input accept failed: " + ex.Message);
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

        private void ReadUntilClosed(TcpClient client, CancellationToken token)
        {
            using (client)
            {
                try
                {
                    NetworkStream stream = client.GetStream();
                    byte[] buffer = new byte[256];
                    StringBuilder line = new StringBuilder();
                    while (!token.IsCancellationRequested)
                    {
                        int read = stream.Read(buffer, 0, buffer.Length);
                        if (read <= 0)
                        {
                            return;
                        }

                        for (int index = 0; index < read; index++)
                        {
                            char ch = (char)buffer[index];
                            if (ch == '\n')
                            {
                                HandleClientLine(client, line.ToString().TrimEnd('\r'));
                                line.Length = 0;
                                continue;
                            }

                            if (line.Length < 512)
                            {
                                line.Append(ch);
                            }
                        }
                    }
                }
                catch
                {
                }
                finally
                {
                    RemoveClient(client);
                }
            }
        }

        private void AddClient(TcpClient client)
        {
            lock (clientsLock)
            {
                clients.Add(client);
            }
        }

        private void HandleClientLine(TcpClient client, string line)
        {
            if (line == "YLVAOS_HOST " + token + " ready")
            {
                lock (clientsLock)
                {
                    readyClients.Add(client);
                }
            }
        }

        private void RemoveClient(TcpClient client)
        {
            lock (clientsLock)
            {
                clients.Remove(client);
                readyClients.Remove(client);
            }

            try
            {
                client.Close();
            }
            catch
            {
            }
        }
    }
}
