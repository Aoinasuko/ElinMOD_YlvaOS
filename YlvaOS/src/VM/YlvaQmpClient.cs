using System;
using System.IO;
using System.Net.Sockets;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace YlvaOS
{
    internal sealed class YlvaQmpClient : IDisposable
    {
        private readonly object sync = new object();
        private TcpClient client;
        private StreamReader reader;
        private StreamWriter writer;
        private bool capabilitiesNegotiated;

        public bool TryConnect(string host, int port, int timeoutMs, out string message)
        {
            lock (sync)
            {
                if (client != null && client.Connected && capabilitiesNegotiated)
                {
                    message = "QMP is already connected.";
                    return true;
                }

                DisposeCore();

                try
                {
                    TcpClient next = new TcpClient();
                    IAsyncResult connect = next.BeginConnect(host, port, null, null);
                    if (!connect.AsyncWaitHandle.WaitOne(timeoutMs))
                    {
                        next.Close();
                        message = "QMP connection timed out.";
                        return false;
                    }

                    next.EndConnect(connect);
                    next.ReceiveTimeout = Math.Max(1000, timeoutMs);
                    next.SendTimeout = Math.Max(1000, timeoutMs);
                    NetworkStream stream = next.GetStream();
                    reader = new StreamReader(stream);
                    writer = new StreamWriter(stream) { AutoFlush = true, NewLine = "\r\n" };
                    client = next;

                    JObject greeting;
                    if (!ReadMessage(out greeting, out message))
                    {
                        DisposeCore();
                        return false;
                    }

                    JObject response;
                    if (!ExecuteLocked("qmp_capabilities", null, out response, out message))
                    {
                        DisposeCore();
                        return false;
                    }

                    capabilitiesNegotiated = true;
                    message = "QMP is connected.";
                    return true;
                }
                catch (Exception ex)
                {
                    DisposeCore();
                    message = ex.Message;
                    return false;
                }
            }
        }

        public bool TryEnableUserNetwork(out string message)
        {
            lock (sync)
            {
                if (client == null || !client.Connected || !capabilitiesNegotiated)
                {
                    message = "QMP is not connected.";
                    return false;
                }

                JObject response;
                JObject netdevArgs = new JObject
                {
                    ["type"] = "user",
                    ["id"] = "ylva_net"
                };

                if (!ExecuteLocked("netdev_add", netdevArgs, out response, out message) && !IsDuplicateId(response))
                {
                    return false;
                }

                JObject deviceArgs = new JObject
                {
                    ["driver"] = "virtio-net-pci",
                    ["netdev"] = "ylva_net",
                    ["id"] = "ylva_nic"
                };

                if (!ExecuteLocked("device_add", deviceArgs, out response, out message) && !IsDuplicateId(response))
                {
                    return false;
                }

                message = "YlvaOS network adapter is connected through QEMU user-mode NAT.";
                return true;
            }
        }

        public bool TrySendPointerButtonState(int x, int y, int width, int height, int previousMask, int buttonMask, out string message)
        {
            lock (sync)
            {
                if (client == null || !client.Connected || !capabilitiesNegotiated)
                {
                    message = "QMP is not connected.";
                    return false;
                }

                JArray events = new JArray
                {
                    CreateAbsolutePointerEvent("x", ScaleAbsolutePointerCoordinate(x, width)),
                    CreateAbsolutePointerEvent("y", ScaleAbsolutePointerCoordinate(y, height))
                };

                AppendPointerButtonTransition(events, previousMask, buttonMask, 1, "left");
                AppendPointerButtonTransition(events, previousMask, buttonMask, 2, "middle");
                AppendPointerButtonTransition(events, previousMask, buttonMask, 4, "right");
                AppendPointerButtonTransition(events, previousMask, buttonMask, 8, "wheel-up");
                AppendPointerButtonTransition(events, previousMask, buttonMask, 16, "wheel-down");

                JObject response;
                return ExecuteLocked(
                    "input-send-event",
                    new JObject { ["events"] = events },
                    out response,
                    out message);
            }
        }

        public void Dispose()
        {
            lock (sync)
            {
                DisposeCore();
            }
        }

        private bool ExecuteLocked(string command, JObject arguments, out JObject response, out string message)
        {
            response = null;
            try
            {
                JObject request = new JObject
                {
                    ["execute"] = command
                };
                if (arguments != null)
                {
                    request["arguments"] = arguments;
                }

                writer.WriteLine(request.ToString(Formatting.None));

                while (true)
                {
                    JObject received;
                    if (!ReadMessage(out received, out message))
                    {
                        return false;
                    }

                    if (received["event"] != null)
                    {
                        continue;
                    }

                    response = received;
                    JToken error = received["error"];
                    if (error != null)
                    {
                        message = FormatError(error);
                        return false;
                    }

                    if (received["return"] != null)
                    {
                        message = command + " succeeded.";
                        return true;
                    }
                }
            }
            catch (Exception ex)
            {
                message = ex.Message;
                return false;
            }
        }

        private bool ReadMessage(out JObject messageObject, out string message)
        {
            messageObject = null;
            try
            {
                while (true)
                {
                    string line = reader.ReadLine();
                    if (line == null)
                    {
                        message = "QMP connection closed.";
                        return false;
                    }

                    if (line.Trim().Length == 0)
                    {
                        continue;
                    }

                    messageObject = JObject.Parse(line);
                    message = "QMP message received.";
                    return true;
                }
            }
            catch (Exception ex)
            {
                message = ex.Message;
                return false;
            }
        }

        private static bool IsDuplicateId(JObject response)
        {
            if (response == null)
            {
                return false;
            }

            JToken error = response["error"];
            if (error == null)
            {
                return false;
            }

            string text = error.ToString(Formatting.None);
            return text.IndexOf("duplicate", StringComparison.OrdinalIgnoreCase) >= 0 ||
                text.IndexOf("already exists", StringComparison.OrdinalIgnoreCase) >= 0 ||
                text.IndexOf("Duplicate ID", StringComparison.OrdinalIgnoreCase) >= 0;
        }

        private static JObject CreateAbsolutePointerEvent(string axis, int value)
        {
            return new JObject
            {
                ["type"] = "abs",
                ["data"] = new JObject
                {
                    ["axis"] = axis,
                    ["value"] = value
                }
            };
        }

        private static void AppendPointerButtonTransition(JArray events, int previousMask, int buttonMask, int bit, string button)
        {
            bool wasDown = (previousMask & bit) != 0;
            bool isDown = (buttonMask & bit) != 0;
            if (wasDown == isDown)
            {
                return;
            }

            events.Add(new JObject
            {
                ["type"] = "btn",
                ["data"] = new JObject
                {
                    ["button"] = button,
                    ["down"] = isDown
                }
            });
        }

        private static int ScaleAbsolutePointerCoordinate(int value, int extent)
        {
            int maximum = Math.Max(1, extent - 1);
            int clamped = Math.Max(0, Math.Min(maximum, value));
            return (int)((long)clamped * 0x7fffL / maximum);
        }

        private static string FormatError(JToken error)
        {
            if (error == null)
            {
                return "QMP command failed.";
            }

            JToken description = error["desc"];
            if (description != null)
            {
                return description.ToString();
            }

            return error.ToString(Formatting.None);
        }

        private void DisposeCore()
        {
            capabilitiesNegotiated = false;

            try
            {
                if (reader != null)
                {
                    reader.Dispose();
                }
            }
            catch
            {
            }

            try
            {
                if (writer != null)
                {
                    writer.Dispose();
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

            reader = null;
            writer = null;
            client = null;
        }
    }
}
