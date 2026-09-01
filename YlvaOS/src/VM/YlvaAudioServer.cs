using System;
using System.Net;
using System.Net.Sockets;
using System.Threading;
using BepInEx.Logging;

namespace YlvaOS
{
    internal sealed class YlvaAudioServer : IDisposable
    {
        public const int SampleRate = 44100;
        public const int Channels = 2;

        private const int BufferSeconds = 2;
        private const int MaxLatencySamples = SampleRate * Channels / 2;

        private readonly ManualLogSource log;
        private readonly object bufferLock = new object();
        private readonly float[] samples = new float[SampleRate * Channels * BufferSeconds];
        private TcpListener listener;
        private CancellationTokenSource cancellation;
        private int readIndex;
        private int writeIndex;
        private int bufferedSamples;
        private bool disposed;

        public YlvaAudioServer(ManualLogSource log)
        {
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

        public void Fill(float[] output)
        {
            if (output == null || output.Length == 0)
            {
                return;
            }

            int written = 0;
            lock (bufferLock)
            {
                while (written < output.Length && bufferedSamples > 0)
                {
                    output[written++] = samples[readIndex];
                    readIndex = (readIndex + 1) % samples.Length;
                    bufferedSamples--;
                }
            }

            while (written < output.Length)
            {
                output[written++] = 0f;
            }
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
            ClearBuffer();
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
                    ClearBuffer();
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
                        log.LogWarning("YlvaOS audio listener stopped unexpectedly.");
                    }

                    return;
                }
                catch (Exception ex)
                {
                    if (!token.IsCancellationRequested && log != null)
                    {
                        log.LogWarning("YlvaOS audio accept failed: " + ex.Message);
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

                byte[] bytes = new byte[8192];
                bool hasCarry = false;
                byte carry = 0;
                while (!token.IsCancellationRequested)
                {
                    int read;
                    try
                    {
                        read = stream.Read(bytes, 0, bytes.Length);
                    }
                    catch
                    {
                        return;
                    }

                    if (read <= 0)
                    {
                        return;
                    }

                    AppendPcm16(bytes, read, ref hasCarry, ref carry);
                }
            }
        }

        private void AppendPcm16(byte[] bytes, int length, ref bool hasCarry, ref byte carry)
        {
            int index = 0;
            lock (bufferLock)
            {
                if (hasCarry && length > 0)
                {
                    AppendSampleLocked(ToFloat(carry, bytes[0]));
                    index = 1;
                    hasCarry = false;
                }

                while (index + 1 < length)
                {
                    AppendSampleLocked(ToFloat(bytes[index], bytes[index + 1]));
                    index += 2;
                }

                if (index < length)
                {
                    carry = bytes[index];
                    hasCarry = true;
                }

                TrimLatencyLocked();
            }
        }

        private void AppendSampleLocked(float sample)
        {
            if (bufferedSamples == samples.Length)
            {
                readIndex = (readIndex + 1) % samples.Length;
                bufferedSamples--;
            }

            samples[writeIndex] = sample;
            writeIndex = (writeIndex + 1) % samples.Length;
            bufferedSamples++;
        }

        private void TrimLatencyLocked()
        {
            while (bufferedSamples > MaxLatencySamples)
            {
                readIndex = (readIndex + 1) % samples.Length;
                bufferedSamples--;
            }
        }

        private void ClearBuffer()
        {
            lock (bufferLock)
            {
                readIndex = 0;
                writeIndex = 0;
                bufferedSamples = 0;
            }
        }

        private static float ToFloat(byte low, byte high)
        {
            short value = (short)(low | (high << 8));
            return value / 32768f;
        }
    }
}
