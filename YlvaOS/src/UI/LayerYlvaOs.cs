using System;
using System.Collections.Generic;
using System.Text;
using UnityEngine;
using UnityEngine.Events;
using UnityEngine.EventSystems;
using UnityEngine.UI;

namespace YlvaOS
{
    internal sealed class LayerYlvaOs : ELayer
    {
        private const int MaxInputLength = 256;
        private const int MaxClipboardPasteLength = 4096;
        private const int VmPasteCharsPerFrame = 3;
        private const float VmPasteIntervalSeconds = 0.0125f;
        private const float DesktopSyntheticClickHoldSeconds = 0.12f;
        private const int DesktopClickDragTolerancePixels = 6;
        private static readonly DesktopKeyBinding[] DesktopKeyBindings = new DesktopKeyBinding[]
        {
            new DesktopKeyBinding(KeyCode.LeftShift, 0xffe1),
            new DesktopKeyBinding(KeyCode.RightShift, 0xffe2),
            new DesktopKeyBinding(KeyCode.LeftControl, 0xffe3),
            new DesktopKeyBinding(KeyCode.RightControl, 0xffe4),
            new DesktopKeyBinding(KeyCode.LeftAlt, 0xffe9),
            new DesktopKeyBinding(KeyCode.RightAlt, 0xffea),
            new DesktopKeyBinding(KeyCode.Escape, 0xff1b),
            new DesktopKeyBinding(KeyCode.Backspace, 0xff08),
            new DesktopKeyBinding(KeyCode.Tab, 0xff09),
            new DesktopKeyBinding(KeyCode.Return, 0xff0d),
            new DesktopKeyBinding(KeyCode.KeypadEnter, 0xff8d),
            new DesktopKeyBinding(KeyCode.Space, 0x0020),
            new DesktopKeyBinding(KeyCode.LeftArrow, 0xff51),
            new DesktopKeyBinding(KeyCode.UpArrow, 0xff52),
            new DesktopKeyBinding(KeyCode.RightArrow, 0xff53),
            new DesktopKeyBinding(KeyCode.DownArrow, 0xff54),
            new DesktopKeyBinding(KeyCode.Home, 0xff50),
            new DesktopKeyBinding(KeyCode.End, 0xff57),
            new DesktopKeyBinding(KeyCode.PageUp, 0xff55),
            new DesktopKeyBinding(KeyCode.PageDown, 0xff56),
            new DesktopKeyBinding(KeyCode.Insert, 0xff63),
            new DesktopKeyBinding(KeyCode.Delete, 0xffff),
            new DesktopKeyBinding(KeyCode.Alpha0, 0x0030),
            new DesktopKeyBinding(KeyCode.Alpha1, 0x0031),
            new DesktopKeyBinding(KeyCode.Alpha2, 0x0032),
            new DesktopKeyBinding(KeyCode.Alpha3, 0x0033),
            new DesktopKeyBinding(KeyCode.Alpha4, 0x0034),
            new DesktopKeyBinding(KeyCode.Alpha5, 0x0035),
            new DesktopKeyBinding(KeyCode.Alpha6, 0x0036),
            new DesktopKeyBinding(KeyCode.Alpha7, 0x0037),
            new DesktopKeyBinding(KeyCode.Alpha8, 0x0038),
            new DesktopKeyBinding(KeyCode.Alpha9, 0x0039),
            new DesktopKeyBinding(KeyCode.A, 0x0061),
            new DesktopKeyBinding(KeyCode.B, 0x0062),
            new DesktopKeyBinding(KeyCode.C, 0x0063),
            new DesktopKeyBinding(KeyCode.D, 0x0064),
            new DesktopKeyBinding(KeyCode.E, 0x0065),
            new DesktopKeyBinding(KeyCode.F, 0x0066),
            new DesktopKeyBinding(KeyCode.G, 0x0067),
            new DesktopKeyBinding(KeyCode.H, 0x0068),
            new DesktopKeyBinding(KeyCode.I, 0x0069),
            new DesktopKeyBinding(KeyCode.J, 0x006a),
            new DesktopKeyBinding(KeyCode.K, 0x006b),
            new DesktopKeyBinding(KeyCode.L, 0x006c),
            new DesktopKeyBinding(KeyCode.M, 0x006d),
            new DesktopKeyBinding(KeyCode.N, 0x006e),
            new DesktopKeyBinding(KeyCode.O, 0x006f),
            new DesktopKeyBinding(KeyCode.P, 0x0070),
            new DesktopKeyBinding(KeyCode.Q, 0x0071),
            new DesktopKeyBinding(KeyCode.R, 0x0072),
            new DesktopKeyBinding(KeyCode.S, 0x0073),
            new DesktopKeyBinding(KeyCode.T, 0x0074),
            new DesktopKeyBinding(KeyCode.U, 0x0075),
            new DesktopKeyBinding(KeyCode.V, 0x0076),
            new DesktopKeyBinding(KeyCode.W, 0x0077),
            new DesktopKeyBinding(KeyCode.X, 0x0078),
            new DesktopKeyBinding(KeyCode.Y, 0x0079),
            new DesktopKeyBinding(KeyCode.Z, 0x007a),
            new DesktopKeyBinding(KeyCode.Minus, 0x002d),
            new DesktopKeyBinding(KeyCode.Equals, 0x003d),
            new DesktopKeyBinding(KeyCode.LeftBracket, 0x005b),
            new DesktopKeyBinding(KeyCode.RightBracket, 0x005d),
            new DesktopKeyBinding(KeyCode.Backslash, 0x005c),
            new DesktopKeyBinding(KeyCode.Semicolon, 0x003b),
            new DesktopKeyBinding(KeyCode.Quote, 0x0027),
            new DesktopKeyBinding(KeyCode.Comma, 0x002c),
            new DesktopKeyBinding(KeyCode.Period, 0x002e),
            new DesktopKeyBinding(KeyCode.Slash, 0x002f),
            new DesktopKeyBinding(KeyCode.BackQuote, 0x0060),
            new DesktopKeyBinding(KeyCode.Keypad0, 0xffb0),
            new DesktopKeyBinding(KeyCode.Keypad1, 0xffb1),
            new DesktopKeyBinding(KeyCode.Keypad2, 0xffb2),
            new DesktopKeyBinding(KeyCode.Keypad3, 0xffb3),
            new DesktopKeyBinding(KeyCode.Keypad4, 0xffb4),
            new DesktopKeyBinding(KeyCode.Keypad5, 0xffb5),
            new DesktopKeyBinding(KeyCode.Keypad6, 0xffb6),
            new DesktopKeyBinding(KeyCode.Keypad7, 0xffb7),
            new DesktopKeyBinding(KeyCode.Keypad8, 0xffb8),
            new DesktopKeyBinding(KeyCode.Keypad9, 0xffb9),
            new DesktopKeyBinding(KeyCode.KeypadPeriod, 0xffae),
            new DesktopKeyBinding(KeyCode.KeypadDivide, 0xffaf),
            new DesktopKeyBinding(KeyCode.KeypadMultiply, 0xffaa),
            new DesktopKeyBinding(KeyCode.KeypadMinus, 0xffad),
            new DesktopKeyBinding(KeyCode.KeypadPlus, 0xffab),
            new DesktopKeyBinding(KeyCode.KeypadEquals, 0xffbd),
            new DesktopKeyBinding(KeyCode.F1, 0xffbe),
            new DesktopKeyBinding(KeyCode.F2, 0xffbf),
            new DesktopKeyBinding(KeyCode.F3, 0xffc0),
            new DesktopKeyBinding(KeyCode.F4, 0xffc1),
            new DesktopKeyBinding(KeyCode.F5, 0xffc2),
            new DesktopKeyBinding(KeyCode.F6, 0xffc3),
            new DesktopKeyBinding(KeyCode.F7, 0xffc4),
            new DesktopKeyBinding(KeyCode.F8, 0xffc5),
            new DesktopKeyBinding(KeyCode.F9, 0xffc6),
            new DesktopKeyBinding(KeyCode.F10, 0xffc7),
            new DesktopKeyBinding(KeyCode.F11, 0xffc8),
            new DesktopKeyBinding(KeyCode.F12, 0xffc9)
        };

        private YlvaMachine machine;
        private Text titleText;
        private Text bodyText;
        private Text promptText;
        private Text footerText;
        private RawImage desktopImage;
        private Texture2D desktopTexture;
        private AudioSource audioSource;
        private AudioClip audioClip;
        private Font font;
        private Font terminalFont;
        private string inputText = string.Empty;
        private int historyCursor = -1;
        private string draftBeforeHistory = string.Empty;
        private bool cursorVisible = true;
        private float cursorTimer;
        private float desktopFrameTimer;
        private bool lastVmConsoleActive;
        private bool lastDesktopMode;
        private int desktopTextureWidth;
        private int desktopTextureHeight;
        private int lastDesktopMouseX = -1;
        private int lastDesktopMouseY = -1;
        private int lastDesktopButtonMask = -1;
        private int lastDesktopInputFrame = -1;
        private float leftButtonHoldUntil;
        private float middleButtonHoldUntil;
        private float rightButtonHoldUntil;
        private float vmPasteTimer;
        private bool desktopPointerInside;
        private bool hostCursorSuppressed;
        private bool previousCursorVisible = true;
        private bool previousCursorSystemDisabled;
        private bool cursorSystemSuppressionCaptured;
        private int desktopClickAnchorMask;
        private int desktopClickAnchorX = -1;
        private int desktopClickAnchorY = -1;
        private bool desktopClickAnchorDragging;
        private readonly HashSet<uint> downDesktopKeySyms = new HashSet<uint>();
        private readonly Queue<char> vmPasteQueue = new Queue<char>();
        private readonly Vector3[] desktopWorldCorners = new Vector3[4];

        public void Configure(YlvaMachine machine)
        {
            this.machine = machine;
            inputText = machine.CurrentInput;
            option = YlvaOsController.CreateLayerOption();
            onKill = YlvaOsController.CreateUnityEvent();
            closeOthers = false;
            defaultActionMode = false;
        }

        protected override void Awake()
        {
            if (option == null)
            {
                option = YlvaOsController.CreateLayerOption();
            }

            if (onKill == null)
            {
                onKill = new UnityEvent();
            }

            base.Awake();
        }

        public override void OnAfterInit()
        {
            base.OnAfterInit();
            BuildUi();
            EnsureAudioOutput();
            RefreshText();
            EInput.Consume(consumeAxis: true, _skipFrame: 2);
        }

        public override void OnUpdateInput()
        {
            if (machine != null && machine.IsDesktopMode)
            {
                ConsumeDesktopHostInput();
                machine.PumpExternalOutput();
                UpdateDesktopFrame();
                UpdateHostCursorSuppression();
                HandleDesktopInput();
                return;
            }

            EInput.Consume(consumeAxis: true, _skipFrame: 1);

            if (machine != null && machine.IsVmConsoleActive)
            {
                machine.PumpExternalOutput();
                HandleVmInput();
                return;
            }

            if (Input.GetKeyDown(KeyCode.Escape))
            {
                Close();
                return;
            }

            if (TryPasteClipboardIntoCommandInput())
            {
                return;
            }

            if (Input.GetKeyDown(KeyCode.UpArrow))
            {
                RecallHistory(-1);
                return;
            }

            if (Input.GetKeyDown(KeyCode.DownArrow))
            {
                RecallHistory(1);
                return;
            }

            string input = Input.inputString;
            if (string.IsNullOrEmpty(input))
            {
                return;
            }

            bool changed = false;
            foreach (char ch in input)
            {
                if (ch == '\b')
                {
                    if (inputText.Length > 0)
                    {
                        inputText = inputText.Substring(0, inputText.Length - 1);
                        changed = true;
                    }

                    continue;
                }

                if (ch == '\n' || ch == '\r')
                {
                    SubmitCommand();
                    return;
                }

                if (!char.IsControl(ch) && inputText.Length < MaxInputLength)
                {
                    inputText += ch;
                    changed = true;
                }
            }

            if (changed)
            {
                historyCursor = -1;
                machine.CurrentInput = inputText;
                RefreshText();
            }
        }

        public override bool OnBack()
        {
            if (machine != null && machine.IsDesktopMode)
            {
                SendDesktopKeyPress(0xff1b);
                return true;
            }

            if (machine != null && machine.IsVmConsoleActive)
            {
                machine.SendRawInput("\u001b");
                return true;
            }

            Close();
            return true;
        }

        public override void OnRightClick()
        {
        }

        public override void OnKill()
        {
            ReleaseAllDesktopKeys();
            ClearDesktopClickAnchor();
            RestoreHostCursor();
            vmPasteQueue.Clear();
            if (machine != null)
            {
                machine.CurrentInput = inputText;
            }

            if (YlvaSessionManager.Instance != null)
            {
                YlvaSessionManager.Instance.Save();
            }

            if (desktopTexture != null)
            {
                UnityEngine.Object.Destroy(desktopTexture);
                desktopTexture = null;
            }

            StopAudioOutput();
            base.OnKill();
        }

        private void Update()
        {
            bool externalOutput = machine != null && machine.PumpExternalOutput();
            if (machine != null && machine.IsDesktopMode)
            {
                ConsumeDesktopHostInput();
                UpdateHostCursorSuppression();
                HandleDesktopInput();
                int fps = machine.Vm != null ? machine.Vm.Config.DesktopRefreshFps : 24;
                float interval = 1f / Mathf.Max(5, fps);
                desktopFrameTimer += Time.unscaledDeltaTime;
                if (desktopFrameTimer >= interval)
                {
                    desktopFrameTimer = 0f;
                    UpdateDesktopFrame();
                }
            }
            else
            {
                desktopFrameTimer = 0f;
                desktopPointerInside = false;
                RestoreHostCursor();
            }

            cursorTimer += Time.unscaledDeltaTime;
            if (cursorTimer >= 0.45f || externalOutput)
            {
                cursorTimer = 0f;
                cursorVisible = !cursorVisible;
                RefreshText();
            }

            if (machine != null && machine.CloseRequested)
            {
                if (YlvaSessionManager.Instance != null)
                {
                    YlvaSessionManager.Instance.Save();
                }

                Close();
            }
        }

        private void LateUpdate()
        {
            if (machine != null && machine.IsDesktopMode)
            {
                UpdateHostCursorSuppression();
            }
        }

        private void HandleVmInput()
        {
            try
            {
                StringBuilder builder = new StringBuilder(32);

                if (PumpVmPasteQueue(force: false))
                {
                    return;
                }

                if (TryQueueClipboardForVm())
                {
                    return;
                }

                if (Input.GetKeyDown(KeyCode.Escape))
                {
                    builder.Append('\u001b');
                }

                if (Input.GetKeyDown(KeyCode.UpArrow))
                {
                    builder.Append("\u001b[A");
                }

                if (Input.GetKeyDown(KeyCode.DownArrow))
                {
                    builder.Append("\u001b[B");
                }

                if (Input.GetKeyDown(KeyCode.RightArrow))
                {
                    builder.Append("\u001b[C");
                }

                if (Input.GetKeyDown(KeyCode.LeftArrow))
                {
                    builder.Append("\u001b[D");
                }

                if (Input.GetKeyDown(KeyCode.Delete))
                {
                    builder.Append("\u001b[3~");
                }

                if (Input.GetKeyDown(KeyCode.Home))
                {
                    builder.Append("\u001b[H");
                }

                if (Input.GetKeyDown(KeyCode.End))
                {
                    builder.Append("\u001b[F");
                }

                if (Input.GetKeyDown(KeyCode.PageUp))
                {
                    builder.Append("\u001b[5~");
                }

                if (Input.GetKeyDown(KeyCode.PageDown))
                {
                    builder.Append("\u001b[6~");
                }

                if (IsControlDown() && AppendControlKey(builder))
                {
                    SendVmBatch(builder);
                    return;
                }

                string input = Input.inputString;
                if (!string.IsNullOrEmpty(input))
                {
                    foreach (char ch in input)
                    {
                        if (ch == '\b')
                        {
                            builder.Append('\u007f');
                            continue;
                        }

                        if (ch == '\n' || ch == '\r')
                        {
                            builder.Append("\r\n");
                            continue;
                        }

                        if (ch == '\t' || !char.IsControl(ch))
                        {
                            builder.Append(ch);
                        }
                    }
                }

                SendVmBatch(builder);
            }
            catch (Exception ex)
            {
                if (Plugin.Log != null)
                {
                    Plugin.Log.LogWarning("YlvaOS VM input failed: " + ex.Message);
                }
            }
        }

        private void SendVmBatch(StringBuilder builder)
        {
            if (builder == null || builder.Length == 0)
            {
                return;
            }

            machine.SendRawInput(builder.ToString());
            if (machine.PumpExternalOutput())
            {
                RefreshText();
            }
        }

        private bool UpdateDesktopFrame()
        {
            if (desktopImage == null || machine == null || !machine.IsDesktopMode)
            {
                return false;
            }

            int width;
            int height;
            byte[] rgba;
            if (!machine.TryCopyDesktopFrame(out width, out height, out rgba) || rgba == null)
            {
                return false;
            }

            if (desktopTexture == null || desktopTextureWidth != width || desktopTextureHeight != height)
            {
                if (desktopTexture != null)
                {
                    UnityEngine.Object.Destroy(desktopTexture);
                }

                desktopTexture = new Texture2D(width, height, TextureFormat.RGBA32, false);
                desktopTexture.filterMode = FilterMode.Bilinear;
                desktopTexture.wrapMode = TextureWrapMode.Clamp;
                desktopTextureWidth = width;
                desktopTextureHeight = height;
                desktopImage.texture = desktopTexture;
            }

            desktopTexture.LoadRawTextureData(rgba);
            desktopTexture.Apply(false, false);
            return true;
        }

        private void HandleDesktopInput()
        {
            if (lastDesktopInputFrame == Time.frameCount)
            {
                return;
            }

            lastDesktopInputFrame = Time.frameCount;
            try
            {
                HandleDesktopKeyboard();
                HandleDesktopMouse();
            }
            catch (Exception ex)
            {
                if (Plugin.Log != null)
                {
                    Plugin.Log.LogWarning("YlvaOS desktop input failed: " + ex.Message);
                }
            }
        }

        private void HandleDesktopKeyboard()
        {
            if (TryPasteClipboardToDesktop())
            {
                return;
            }

            foreach (DesktopKeyBinding binding in DesktopKeyBindings)
            {
                SendDesktopKeyTransition(binding.Key, binding.KeySym);
            }
        }

        private void HandleDesktopMouse()
        {
            if (desktopImage == null || desktopTextureWidth <= 0 || desktopTextureHeight <= 0)
            {
                desktopPointerInside = false;
                RestoreHostCursor();
                return;
            }

            int x;
            int y;
            if (!TryGetDesktopPointer(out x, out y))
            {
                desktopPointerInside = false;
                RestoreHostCursor();
                ClearDesktopClickAnchor();
                if (lastDesktopButtonMask > 0 && lastDesktopMouseX >= 0 && lastDesktopMouseY >= 0)
                {
                    machine.SendDesktopPointer(lastDesktopMouseX, lastDesktopMouseY, 0);
                    lastDesktopButtonMask = 0;
                }

                return;
            }

            int mask = BuildDesktopMouseButtonMask();
            Vector2 wheel = Input.mouseScrollDelta;

            desktopPointerInside = true;
            ConsumeDesktopHostInput();
            if (wheel.y > 0.01f)
            {
                SendDesktopPointerState(x, y, mask | 8, force: true);
                SendDesktopPointerState(x, y, mask, force: true);
            }
            else if (wheel.y < -0.01f)
            {
                SendDesktopPointerState(x, y, mask | 16, force: true);
                SendDesktopPointerState(x, y, mask, force: true);
            }

            SendDesktopPointerState(x, y, mask, force: false);
        }

        private int BuildDesktopMouseButtonMask()
        {
            float now = Time.unscaledTime;
            bool leftUp = Input.GetMouseButtonUp(0);
            bool middleUp = Input.GetMouseButtonUp(2);
            bool rightUp = Input.GetMouseButtonUp(1);

            if (leftUp)
            {
                leftButtonHoldUntil = 0f;
            }
            else if (Input.GetMouseButtonDown(0))
            {
                leftButtonHoldUntil = Mathf.Max(leftButtonHoldUntil, now + DesktopSyntheticClickHoldSeconds);
            }

            if (middleUp)
            {
                middleButtonHoldUntil = 0f;
            }
            else if (Input.GetMouseButtonDown(2))
            {
                middleButtonHoldUntil = Mathf.Max(middleButtonHoldUntil, now + DesktopSyntheticClickHoldSeconds);
            }

            if (rightUp)
            {
                rightButtonHoldUntil = 0f;
            }
            else if (Input.GetMouseButtonDown(1))
            {
                rightButtonHoldUntil = Mathf.Max(rightButtonHoldUntil, now + DesktopSyntheticClickHoldSeconds);
            }

            int mask = 0;
            if (Input.GetMouseButton(0) || (!leftUp && now < leftButtonHoldUntil))
            {
                mask |= 1;
            }

            if (Input.GetMouseButton(2) || (!middleUp && now < middleButtonHoldUntil))
            {
                mask |= 2;
            }

            if (Input.GetMouseButton(1) || (!rightUp && now < rightButtonHoldUntil))
            {
                mask |= 4;
            }

            return mask;
        }

        private void SendDesktopPointerState(int x, int y, int mask, bool force)
        {
            if (machine == null)
            {
                return;
            }

            StabilizeDesktopClick(ref x, ref y, mask);

            if (!force && x == lastDesktopMouseX && y == lastDesktopMouseY && mask == lastDesktopButtonMask)
            {
                return;
            }

            machine.SendDesktopPointer(x, y, mask);
            lastDesktopMouseX = x;
            lastDesktopMouseY = y;
            lastDesktopButtonMask = mask;
        }

        private void StabilizeDesktopClick(ref int x, ref int y, int mask)
        {
            int holdMask = mask & 7;
            int previousHoldMask = lastDesktopButtonMask < 0 ? 0 : lastDesktopButtonMask & 7;
            if (previousHoldMask == 0 && holdMask != 0)
            {
                desktopClickAnchorMask = holdMask;
                desktopClickAnchorX = x;
                desktopClickAnchorY = y;
                desktopClickAnchorDragging = false;
            }

            if (desktopClickAnchorMask == 0)
            {
                return;
            }

            if (holdMask != 0)
            {
                if (!desktopClickAnchorDragging && IsDesktopClickDrag(x, y))
                {
                    desktopClickAnchorDragging = true;
                }

                if (!desktopClickAnchorDragging)
                {
                    x = desktopClickAnchorX;
                    y = desktopClickAnchorY;
                }

                return;
            }

            if (!desktopClickAnchorDragging)
            {
                x = desktopClickAnchorX;
                y = desktopClickAnchorY;
            }

            ClearDesktopClickAnchor();
        }

        private bool IsDesktopClickDrag(int x, int y)
        {
            if (desktopClickAnchorX < 0 || desktopClickAnchorY < 0)
            {
                return false;
            }

            int dx = x - desktopClickAnchorX;
            int dy = y - desktopClickAnchorY;
            return dx * dx + dy * dy > DesktopClickDragTolerancePixels * DesktopClickDragTolerancePixels;
        }

        private void ClearDesktopClickAnchor()
        {
            desktopClickAnchorMask = 0;
            desktopClickAnchorX = -1;
            desktopClickAnchorY = -1;
            desktopClickAnchorDragging = false;
        }

        private bool TryGetDesktopPointer(out int x, out int y)
        {
            return TryGetDesktopPointer(Input.mousePosition, out x, out y);
        }

        private bool TryGetDesktopPointer(Vector2 screenPosition, out int x, out int y)
        {
            x = 0;
            y = 0;
            RectTransform rect = desktopImage.rectTransform;
            Vector2 local;
            Camera camera = ResolveUiCamera();
            if (!RectTransformUtility.ScreenPointToLocalPointInRectangle(rect, screenPosition, camera, out local))
            {
                return TryGetDesktopPointerFromScreenBounds(screenPosition, out x, out y);
            }

            Rect bounds = rect.rect;
            float localX = local.x - bounds.xMin;
            float localY = bounds.yMax - local.y;
            if (localX < 0f || localX > bounds.width || localY < 0f || localY > bounds.height)
            {
                return TryGetDesktopPointerFromScreenBounds(screenPosition, out x, out y);
            }

            x = Mathf.Clamp(Mathf.RoundToInt(localX / Mathf.Max(1f, bounds.width) * desktopTextureWidth), 0, desktopTextureWidth - 1);
            y = Mathf.Clamp(Mathf.RoundToInt(localY / Mathf.Max(1f, bounds.height) * desktopTextureHeight), 0, desktopTextureHeight - 1);
            return true;
        }

        private bool TryGetDesktopPointerFromScreenBounds(out int x, out int y)
        {
            return TryGetDesktopPointerFromScreenBounds(Input.mousePosition, out x, out y);
        }

        private bool TryGetDesktopPointerFromScreenBounds(Vector2 screenPosition, out int x, out int y)
        {
            x = 0;
            y = 0;
            if (desktopImage == null || desktopTextureWidth <= 0 || desktopTextureHeight <= 0)
            {
                return false;
            }

            RectTransform rect = desktopImage.rectTransform;
            Camera camera = ResolveUiCamera();
            rect.GetWorldCorners(desktopWorldCorners);

            Vector2 min = RectTransformUtility.WorldToScreenPoint(camera, desktopWorldCorners[0]);
            Vector2 max = min;
            for (int i = 1; i < desktopWorldCorners.Length; i++)
            {
                Vector2 point = RectTransformUtility.WorldToScreenPoint(camera, desktopWorldCorners[i]);
                min = Vector2.Min(min, point);
                max = Vector2.Max(max, point);
            }

            Vector2 mouse = screenPosition;
            if (mouse.x < min.x || mouse.x > max.x || mouse.y < min.y || mouse.y > max.y)
            {
                return false;
            }

            float width = Mathf.Max(1f, max.x - min.x);
            float height = Mathf.Max(1f, max.y - min.y);
            x = Mathf.Clamp(Mathf.RoundToInt((mouse.x - min.x) / width * desktopTextureWidth), 0, desktopTextureWidth - 1);
            y = Mathf.Clamp(Mathf.RoundToInt((max.y - mouse.y) / height * desktopTextureHeight), 0, desktopTextureHeight - 1);
            return true;
        }

        private void UpdateHostCursorSuppression()
        {
            bool shouldSuppress = machine != null && machine.IsDesktopMode && IsDesktopPointerInside();
            SetHostCursorSuppressed(shouldSuppress);
            if (shouldSuppress)
            {
                ConsumeDesktopHostInput();
            }
        }

        private bool IsDesktopPointerInside()
        {
            int x;
            int y;
            return desktopImage != null && desktopTextureWidth > 0 && desktopTextureHeight > 0 && TryGetDesktopPointer(out x, out y);
        }

        private void SetHostCursorSuppressed(bool suppressed)
        {
            try
            {
                if (suppressed)
                {
                    if (!hostCursorSuppressed)
                    {
                        previousCursorVisible = Cursor.visible;
                        CaptureCursorSystemState();
                    }

                    Cursor.visible = false;
                    SetElinCursorSystemDisabled(true);
                }
                else
                {
                    if (hostCursorSuppressed)
                    {
                        Cursor.visible = previousCursorVisible;
                        SetElinCursorSystemDisabled(previousCursorSystemDisabled);
                        cursorSystemSuppressionCaptured = false;
                    }
                }

                hostCursorSuppressed = suppressed;
            }
            catch
            {
                hostCursorSuppressed = false;
            }
        }

        private void CaptureCursorSystemState()
        {
            if (cursorSystemSuppressionCaptured)
            {
                return;
            }

            try
            {
                if (CursorSystem.Instance != null)
                {
                    previousCursorSystemDisabled = CursorSystem.Instance.disable;
                    cursorSystemSuppressionCaptured = true;
                }
            }
            catch
            {
                cursorSystemSuppressionCaptured = false;
            }
        }

        private static void SetElinCursorSystemDisabled(bool disabled)
        {
            try
            {
                if (CursorSystem.Instance != null)
                {
                    CursorSystem.Instance.disable = disabled;
                }
            }
            catch
            {
            }
        }

        private static void ConsumeDesktopHostInput()
        {
            try
            {
                EInput.Consume(consumeAxis: true, _skipFrame: 3);
                if (EInput.leftMouse != null)
                {
                    EInput.leftMouse.Consume();
                }

                if (EInput.rightMouse != null)
                {
                    EInput.rightMouse.Consume();
                }

                if (EInput.middleMouse != null)
                {
                    EInput.middleMouse.Consume();
                }

                EInput.ConsumeWheel();
            }
            catch
            {
            }
        }

        private void RestoreHostCursor()
        {
            SetHostCursorSuppressed(false);
        }

        private void HandleDesktopPointerEvent(PointerEventData eventData, bool down)
        {
            if (eventData == null || machine == null || !machine.IsDesktopMode)
            {
                return;
            }

            int bit = ToDesktopButtonMask(eventData.button);
            if (bit == 0)
            {
                return;
            }

            int x;
            int y;
            if (!TryGetDesktopPointer(eventData.position, out x, out y))
            {
                return;
            }

            desktopPointerInside = true;
            int mask = lastDesktopButtonMask < 0 ? 0 : lastDesktopButtonMask;
            mask = down ? mask | bit : mask & ~bit;
            if (!down)
            {
                if (bit == 1)
                {
                    leftButtonHoldUntil = 0f;
                }
                else if (bit == 2)
                {
                    middleButtonHoldUntil = 0f;
                }
                else if (bit == 4)
                {
                    rightButtonHoldUntil = 0f;
                }
            }

            SendDesktopPointerState(x, y, mask, force: true);
            ConsumeDesktopHostInput();
        }

        private void HandleDesktopPointerMove(PointerEventData eventData)
        {
            if (eventData == null || machine == null || !machine.IsDesktopMode)
            {
                return;
            }

            int x;
            int y;
            if (!TryGetDesktopPointer(eventData.position, out x, out y))
            {
                return;
            }

            desktopPointerInside = true;
            int mask = lastDesktopButtonMask < 0 ? 0 : lastDesktopButtonMask;
            SendDesktopPointerState(x, y, mask, force: false);
            ConsumeDesktopHostInput();
        }

        private void HandleDesktopScroll(PointerEventData eventData)
        {
            if (eventData == null || machine == null || !machine.IsDesktopMode)
            {
                return;
            }

            int x;
            int y;
            if (!TryGetDesktopPointer(eventData.position, out x, out y))
            {
                return;
            }

            desktopPointerInside = true;
            int mask = lastDesktopButtonMask < 0 ? 0 : lastDesktopButtonMask;
            if (eventData.scrollDelta.y > 0.01f)
            {
                SendDesktopPointerState(x, y, mask | 8, force: true);
                SendDesktopPointerState(x, y, mask, force: true);
            }
            else if (eventData.scrollDelta.y < -0.01f)
            {
                SendDesktopPointerState(x, y, mask | 16, force: true);
                SendDesktopPointerState(x, y, mask, force: true);
            }

            ConsumeDesktopHostInput();
        }

        private void SetDesktopPointerInside(bool inside)
        {
            desktopPointerInside = inside;
            if (!inside && lastDesktopButtonMask > 0 && lastDesktopMouseX >= 0 && lastDesktopMouseY >= 0)
            {
                SendDesktopPointerState(lastDesktopMouseX, lastDesktopMouseY, 0, force: true);
            }

            if (!inside)
            {
                ClearDesktopClickAnchor();
            }

            UpdateHostCursorSuppression();
        }

        private static int ToDesktopButtonMask(PointerEventData.InputButton button)
        {
            switch (button)
            {
                case PointerEventData.InputButton.Left:
                    return 1;
                case PointerEventData.InputButton.Middle:
                    return 2;
                case PointerEventData.InputButton.Right:
                    return 4;
                default:
                    return 0;
            }
        }

        private Camera ResolveUiCamera()
        {
            if (desktopImage == null)
            {
                return null;
            }

            Canvas canvas = desktopImage.canvas;
            if (canvas == null || canvas.renderMode == RenderMode.ScreenSpaceOverlay)
            {
                return null;
            }

            return canvas.worldCamera != null ? canvas.worldCamera : Camera.main;
        }

        private void SendDesktopKeyPress(uint keySym)
        {
            if (machine == null)
            {
                return;
            }

            machine.SendDesktopKey(keySym, true);
            machine.SendDesktopKey(keySym, false);
        }

        private void SendDesktopKeyTransition(KeyCode key, uint keySym)
        {
            if (Input.GetKeyDown(key) && downDesktopKeySyms.Add(keySym))
            {
                machine.SendDesktopKey(keySym, true);
            }

            if (Input.GetKeyUp(key) && downDesktopKeySyms.Remove(keySym))
            {
                machine.SendDesktopKey(keySym, false);
            }
        }

        private void ReleaseAllDesktopKeys()
        {
            if (downDesktopKeySyms.Count == 0 || machine == null)
            {
                downDesktopKeySyms.Clear();
                return;
            }

            uint[] keySyms = new uint[downDesktopKeySyms.Count];
            downDesktopKeySyms.CopyTo(keySyms);
            downDesktopKeySyms.Clear();
            foreach (uint keySym in keySyms)
            {
                machine.SendDesktopKey(keySym, false);
            }

            lastDesktopButtonMask = -1;
            lastDesktopMouseX = -1;
            lastDesktopMouseY = -1;
        }

        private bool TryPasteClipboardIntoCommandInput()
        {
            if (machine == null || !IsPasteShortcutDown())
            {
                return false;
            }

            string text;
            if (!TryReadClipboardText(out text))
            {
                return true;
            }

            bool changed = false;
            foreach (int codePoint in EnumerateCodePoints(NormalizeLineEndings(text)))
            {
                if (inputText.Length >= MaxInputLength)
                {
                    break;
                }

                if (codePoint == '\n' || codePoint == '\r' || codePoint == '\t')
                {
                    inputText += " ";
                    changed = true;
                    continue;
                }

                if (codePoint >= 0x20 && codePoint != 0x7f)
                {
                    inputText += char.ConvertFromUtf32(codePoint);
                    changed = true;
                }
            }

            if (changed)
            {
                historyCursor = -1;
                machine.CurrentInput = inputText;
                RefreshText();
            }

            return true;
        }

        private bool TryQueueClipboardForVm()
        {
            if (!IsPasteShortcutDown())
            {
                return false;
            }

            string text;
            if (!TryReadClipboardText(out text))
            {
                return true;
            }

            vmPasteQueue.Clear();
            foreach (int codePoint in EnumerateCodePoints(NormalizeLineEndings(text)))
            {
                if (codePoint == '\n')
                {
                    vmPasteQueue.Enqueue('\r');
                    continue;
                }

                if (codePoint == '\b' || codePoint == 0x7f)
                {
                    vmPasteQueue.Enqueue('\u007f');
                    continue;
                }

                if (codePoint == '\t')
                {
                    vmPasteQueue.Enqueue('\t');
                    continue;
                }

                if (codePoint >= 0x20)
                {
                    string value = char.ConvertFromUtf32(codePoint);
                    for (int i = 0; i < value.Length; i++)
                    {
                        vmPasteQueue.Enqueue(value[i]);
                    }
                }
            }

            vmPasteTimer = 0f;
            PumpVmPasteQueue(force: true);
            return true;
        }

        private bool PumpVmPasteQueue(bool force)
        {
            if (vmPasteQueue.Count == 0)
            {
                return false;
            }

            if (!force)
            {
                vmPasteTimer -= Time.unscaledDeltaTime;
                if (vmPasteTimer > 0f)
                {
                    return true;
                }
            }

            StringBuilder chunk = new StringBuilder(VmPasteCharsPerFrame);
            while (chunk.Length < VmPasteCharsPerFrame && vmPasteQueue.Count > 0)
            {
                chunk.Append(vmPasteQueue.Dequeue());
            }

            machine.SendRawInput(chunk.ToString());
            vmPasteTimer = VmPasteIntervalSeconds;
            if (machine.PumpExternalOutput())
            {
                RefreshText();
            }

            return true;
        }

        private bool TryPasteClipboardToDesktop()
        {
            if (!IsPasteShortcutDown())
            {
                return false;
            }

            string text;
            if (!TryReadClipboardText(out text))
            {
                return true;
            }

            ReleaseAllDesktopKeys();
            string message = string.Empty;
            if (machine != null && machine.TryPasteTextToDesktop(NormalizeLineEndings(text), out message))
            {
                return true;
            }

            if (Plugin.Log != null && !string.IsNullOrEmpty(message))
            {
                Plugin.Log.LogWarning("YlvaOS desktop paste fell back to VNC key events: " + message);
            }

            foreach (int codePoint in EnumerateCodePoints(NormalizeLineEndings(text)))
            {
                if (codePoint == '\n')
                {
                    SendDesktopKeyPress(0xff0d);
                    continue;
                }

                if (codePoint == '\b' || codePoint == 0x7f)
                {
                    SendDesktopKeyPress(0xff08);
                    continue;
                }

                if (codePoint == '\t')
                {
                    SendDesktopKeyPress(0xff09);
                    continue;
                }

                uint keySym = ToVncTextKeySym(codePoint);
                if (keySym != 0)
                {
                    SendDesktopKeyPress(keySym);
                }
            }

            return true;
        }

        private static bool TryReadClipboardText(out string text)
        {
            text = string.Empty;
            try
            {
                text = GUIUtility.systemCopyBuffer ?? string.Empty;
            }
            catch
            {
                return false;
            }

            if (text.Length > MaxClipboardPasteLength)
            {
                text = text.Substring(0, MaxClipboardPasteLength);
            }

            return text.Length > 0;
        }

        private static string NormalizeLineEndings(string text)
        {
            return (text ?? string.Empty).Replace("\r\n", "\n").Replace('\r', '\n');
        }

        private static IEnumerable<int> EnumerateCodePoints(string text)
        {
            if (string.IsNullOrEmpty(text))
            {
                yield break;
            }

            for (int index = 0; index < text.Length; index++)
            {
                char ch = text[index];
                if (char.IsHighSurrogate(ch) && index + 1 < text.Length && char.IsLowSurrogate(text[index + 1]))
                {
                    yield return char.ConvertToUtf32(ch, text[index + 1]);
                    index++;
                    continue;
                }

                if (!char.IsSurrogate(ch))
                {
                    yield return ch;
                }
            }
        }

        private static uint ToVncTextKeySym(int codePoint)
        {
            if (codePoint >= 0x20 && codePoint <= 0xff)
            {
                return (uint)codePoint;
            }

            if (codePoint > 0xff && codePoint <= 0x10ffff)
            {
                return 0x01000000u | (uint)codePoint;
            }

            return 0;
        }

        private void SubmitCommand()
        {
            string command = inputText;
            inputText = string.Empty;
            machine.CurrentInput = string.Empty;
            historyCursor = -1;
            draftBeforeHistory = string.Empty;

            bool close = machine.Submit(command);
            RefreshText();

            if (YlvaSessionManager.Instance != null)
            {
                YlvaSessionManager.Instance.Save();
            }

            if (close)
            {
                Close();
            }
        }

        private void RecallHistory(int direction)
        {
            IList<string> history = machine.History;
            if (history.Count == 0)
            {
                return;
            }

            if (historyCursor < 0)
            {
                draftBeforeHistory = inputText;
                historyCursor = direction < 0 ? history.Count - 1 : 0;
            }
            else
            {
                historyCursor += direction;
                if (historyCursor < 0)
                {
                    historyCursor = 0;
                }

                if (historyCursor >= history.Count)
                {
                    historyCursor = -1;
                    inputText = draftBeforeHistory;
                    machine.CurrentInput = inputText;
                    RefreshText();
                    return;
                }
            }

            inputText = history[historyCursor];
            machine.CurrentInput = inputText;
            RefreshText();
        }

        private void BuildUi()
        {
            font = ResolveFont();
            terminalFont = ResolveTerminalFont();

            RectTransform overlay = CreateRect("YlvaOSOverlay", rectLayers);
            Stretch(overlay, 0f, 0f, 0f, 0f);
            Image overlayImage = overlay.gameObject.AddComponent<Image>();
            overlayImage.color = new Color(0f, 0f, 0f, 0.58f);

            RectTransform window = CreateRect("YlvaOSWindow", overlay);
            window.anchorMin = new Vector2(0.08f, 0.08f);
            window.anchorMax = new Vector2(0.92f, 0.92f);
            window.offsetMin = Vector2.zero;
            window.offsetMax = Vector2.zero;
            Image windowImage = window.gameObject.AddComponent<Image>();
            windowImage.color = new Color(0.035f, 0.041f, 0.045f, 0.98f);

            RectTransform header = CreateRect("Header", window);
            header.anchorMin = new Vector2(0f, 1f);
            header.anchorMax = new Vector2(1f, 1f);
            header.pivot = new Vector2(0.5f, 1f);
            header.anchoredPosition = Vector2.zero;
            header.sizeDelta = new Vector2(0f, 44f);
            Image headerImage = header.gameObject.AddComponent<Image>();
            headerImage.color = new Color(0.09f, 0.12f, 0.13f, 1f);

            titleText = CreateText("Title", header, 16, TextAnchor.MiddleLeft, new Color(0.78f, 0.93f, 0.83f, 1f));
            Stretch(titleText.rectTransform, 16f, 0f, 58f, 0f);

            Button closeButton = CreateButton("Close", header, "X");
            RectTransform closeRect = closeButton.GetComponent<RectTransform>();
            closeRect.anchorMin = new Vector2(1f, 0.5f);
            closeRect.anchorMax = new Vector2(1f, 0.5f);
            closeRect.pivot = new Vector2(1f, 0.5f);
            closeRect.anchoredPosition = new Vector2(-8f, 0f);
            closeRect.sizeDelta = new Vector2(38f, 30f);
            closeButton.onClick.AddListener(Close);

            bodyText = CreateText("Terminal", window, 16, TextAnchor.UpperLeft, new Color(0.78f, 0.94f, 0.80f, 1f));
            if (terminalFont != null)
            {
                bodyText.font = terminalFont;
            }

            bodyText.lineSpacing = 1.05f;
            Stretch(bodyText.rectTransform, 18f, 58f, 18f, 58f);

            RectTransform desktopRect = CreateRect("Desktop", window);
            Stretch(desktopRect, 18f, 58f, 18f, 22f);
            desktopImage = desktopRect.gameObject.AddComponent<RawImage>();
            desktopImage.color = Color.white;
            desktopImage.raycastTarget = true;
            DesktopInputBlocker inputBlocker = desktopRect.gameObject.AddComponent<DesktopInputBlocker>();
            inputBlocker.Configure(this);
            desktopImage.gameObject.SetActive(false);

            promptText = CreateText("Prompt", window, 16, TextAnchor.MiddleLeft, new Color(0.90f, 1f, 0.92f, 1f));
            Stretch(promptText.rectTransform, 18f, 0f, 18f, 16f);
            promptText.rectTransform.anchorMin = new Vector2(0f, 0f);
            promptText.rectTransform.anchorMax = new Vector2(1f, 0f);
            promptText.rectTransform.pivot = new Vector2(0.5f, 0f);
            promptText.rectTransform.sizeDelta = new Vector2(-36f, 36f);
            promptText.rectTransform.anchoredPosition = new Vector2(0f, 16f);

            footerText = CreateText("Footer", window, 12, TextAnchor.MiddleRight, new Color(0.50f, 0.64f, 0.58f, 1f));
            footerText.text = "Esc closes";
            footerText.rectTransform.anchorMin = new Vector2(0f, 0f);
            footerText.rectTransform.anchorMax = new Vector2(1f, 0f);
            footerText.rectTransform.pivot = new Vector2(0.5f, 0f);
            footerText.rectTransform.sizeDelta = new Vector2(-36f, 14f);
            footerText.rectTransform.anchoredPosition = new Vector2(0f, 2f);
        }

        private void EnsureAudioOutput()
        {
            if (audioSource != null)
            {
                return;
            }

            try
            {
                audioSource = gameObject.AddComponent<AudioSource>();
                audioSource.playOnAwake = false;
                audioSource.loop = true;
                audioSource.spatialBlend = 0f;
                audioSource.ignoreListenerPause = true;
                audioSource.mute = false;
                audioSource.priority = 0;
                audioSource.dopplerLevel = 0f;
                audioSource.volume = 1f;
                audioClip = AudioClip.Create(
                    "YlvaOS VM Audio",
                    YlvaAudioServer.SampleRate,
                    YlvaAudioServer.Channels,
                    YlvaAudioServer.SampleRate,
                    true,
                    OnAudioRead,
                    OnAudioSetPosition);
                audioSource.clip = audioClip;
                audioSource.Play();
            }
            catch (Exception ex)
            {
                if (Plugin.Log != null)
                {
                    Plugin.Log.LogWarning("YlvaOS audio output could not be started: " + ex.Message);
                }
            }
        }

        private void StopAudioOutput()
        {
            try
            {
                if (audioSource != null)
                {
                    audioSource.Stop();
                    audioSource.clip = null;
                }

                if (audioClip != null)
                {
                    UnityEngine.Object.Destroy(audioClip);
                }
            }
            catch
            {
            }

            audioSource = null;
            audioClip = null;
        }

        private void OnAudioRead(float[] data)
        {
            if (machine != null && machine.Vm != null)
            {
                machine.Vm.FillAudio(data);
                return;
            }

            if (data != null)
            {
                Array.Clear(data, 0, data.Length);
            }
        }

        private void OnAudioSetPosition(int position)
        {
        }

        private void RefreshText()
        {
            if (machine == null || bodyText == null || promptText == null || titleText == null || desktopImage == null)
            {
                return;
            }

            bool vmConsoleActive = machine.IsVmConsoleActive;
            bool desktopMode = machine.IsDesktopMode;
            if (lastVmConsoleActive != vmConsoleActive || lastDesktopMode != desktopMode)
            {
                if (lastDesktopMode && !desktopMode)
                {
                    ReleaseAllDesktopKeys();
                }

                if (lastVmConsoleActive && !vmConsoleActive)
                {
                    vmPasteQueue.Clear();
                }

                lastVmConsoleActive = vmConsoleActive;
                lastDesktopMode = desktopMode;
                Stretch(bodyText.rectTransform, 18f, 58f, 18f, vmConsoleActive ? 22f : 58f);
                desktopImage.gameObject.SetActive(desktopMode);
                bodyText.gameObject.SetActive(!desktopMode);
                promptText.gameObject.SetActive(!vmConsoleActive && !desktopMode);
            }

            if (!desktopMode)
            {
                List<string> lines = machine.GetVisibleLines(YlvaTerminalBuffer.DefaultRows, vmConsoleActive && cursorVisible);
                bodyText.supportRichText = machine.VisibleLinesUseRichText;
                bodyText.text = string.Join("\n", lines.ToArray());
            }

            string visibleInput = machine.IsSecretInput ? new string('*', inputText.Length) : inputText;
            promptText.text = machine.Prompt + visibleInput + (cursorVisible ? "_" : " ");
            footerText.text = desktopMode
                ? "Desktop mode | Ctrl+V paste | Ctrl+Alt+T terminal | Ctrl+Alt+K or Kernel returns | close with X"
                : vmConsoleActive
                ? "Esc is sent to YlvaOS | Ctrl+V paste | ConnectNetwork enables Internet after yes | close with X"
                : "Esc closes";
            titleText.text = machine.WindowTitle;
        }

        private static bool IsControlDown()
        {
            return Input.GetKey(KeyCode.LeftControl) || Input.GetKey(KeyCode.RightControl);
        }

        private static bool IsPasteShortcutDown()
        {
            return IsControlDown() && Input.GetKeyDown(KeyCode.V);
        }

        private bool AppendControlKey(StringBuilder builder)
        {
            for (KeyCode key = KeyCode.A; key <= KeyCode.Z; key++)
            {
                if (Input.GetKeyDown(key))
                {
                    int code = ((int)key - (int)KeyCode.A) + 1;
                    builder.Append((char)code);
                    return true;
                }
            }

            return false;
        }

        private sealed class DesktopInputBlocker :
            MonoBehaviour,
            IPointerEnterHandler,
            IPointerExitHandler,
            IPointerDownHandler,
            IPointerUpHandler,
            IPointerClickHandler,
            IBeginDragHandler,
            IDragHandler,
            IEndDragHandler,
            IScrollHandler
        {
            private LayerYlvaOs owner;

            public void Configure(LayerYlvaOs owner)
            {
                this.owner = owner;
            }

            public void OnPointerEnter(PointerEventData eventData)
            {
                if (owner != null)
                {
                    owner.SetDesktopPointerInside(true);
                }

                Use(eventData);
            }

            public void OnPointerExit(PointerEventData eventData)
            {
                if (owner != null)
                {
                    owner.SetDesktopPointerInside(false);
                }

                Use(eventData);
            }

            public void OnPointerDown(PointerEventData eventData)
            {
                if (owner != null)
                {
                    owner.HandleDesktopPointerEvent(eventData, down: true);
                }

                Use(eventData);
            }

            public void OnPointerUp(PointerEventData eventData)
            {
                if (owner != null)
                {
                    owner.HandleDesktopPointerEvent(eventData, down: false);
                }

                Use(eventData);
            }

            public void OnPointerClick(PointerEventData eventData)
            {
                Use(eventData);
            }

            public void OnBeginDrag(PointerEventData eventData)
            {
                if (owner != null)
                {
                    owner.HandleDesktopPointerMove(eventData);
                }

                Use(eventData);
            }

            public void OnDrag(PointerEventData eventData)
            {
                if (owner != null)
                {
                    owner.HandleDesktopPointerMove(eventData);
                }

                Use(eventData);
            }

            public void OnEndDrag(PointerEventData eventData)
            {
                if (owner != null)
                {
                    owner.HandleDesktopPointerMove(eventData);
                }

                Use(eventData);
            }

            public void OnScroll(PointerEventData eventData)
            {
                if (owner != null)
                {
                    owner.HandleDesktopScroll(eventData);
                }

                Use(eventData);
            }

            private static void Use(PointerEventData eventData)
            {
                if (eventData != null)
                {
                    eventData.Use();
                }
            }
        }

        private struct DesktopKeyBinding
        {
            public readonly KeyCode Key;
            public readonly uint KeySym;

            public DesktopKeyBinding(KeyCode key, uint keySym)
            {
                Key = key;
                KeySym = keySym;
            }
        }

        private Text CreateText(string name, RectTransform parent, int fontSize, TextAnchor alignment, Color color)
        {
            RectTransform rect = CreateRect(name, parent);
            Text text = rect.gameObject.AddComponent<Text>();
            text.font = font;
            text.fontSize = fontSize;
            text.color = color;
            text.alignment = alignment;
            text.horizontalOverflow = HorizontalWrapMode.Wrap;
            text.verticalOverflow = VerticalWrapMode.Truncate;
            text.supportRichText = false;
            return text;
        }

        private Button CreateButton(string name, RectTransform parent, string label)
        {
            RectTransform rect = CreateRect(name, parent);
            Image image = rect.gameObject.AddComponent<Image>();
            image.color = new Color(0.15f, 0.20f, 0.20f, 1f);
            Button button = rect.gameObject.AddComponent<Button>();
            button.targetGraphic = image;

            Text text = CreateText("Label", rect, 16, TextAnchor.MiddleCenter, new Color(0.92f, 1f, 0.94f, 1f));
            Stretch(text.rectTransform, 0f, 0f, 0f, 0f);
            text.text = label;
            return button;
        }

        private static Font ResolveFont()
        {
            try
            {
                if (SkinManager.Instance != null && SkinManager.Instance.FontList != null && SkinManager.Instance.FontList.Count > 0 && SkinManager.Instance.FontList[0].font != null)
                {
                    return SkinManager.Instance.FontList[0].font;
                }
            }
            catch
            {
            }

            return Resources.GetBuiltinResource<Font>("Arial.ttf");
        }

        private static Font ResolveTerminalFont()
        {
            try
            {
                Font terminal = Font.CreateDynamicFontFromOSFont(
                    new[] { "Consolas", "MS Gothic", "Courier New", "Lucida Console" },
                    16);
                if (terminal != null)
                {
                    return terminal;
                }
            }
            catch
            {
            }

            return Resources.GetBuiltinResource<Font>("Arial.ttf");
        }

        private static RectTransform CreateRect(string name, RectTransform parent)
        {
            GameObject obj = new GameObject(name, typeof(RectTransform));
            RectTransform rect = obj.GetComponent<RectTransform>();
            rect.SetParent(parent, worldPositionStays: false);
            return rect;
        }

        private static void Stretch(RectTransform rect, float left, float top, float right, float bottom)
        {
            rect.anchorMin = Vector2.zero;
            rect.anchorMax = Vector2.one;
            rect.offsetMin = new Vector2(left, bottom);
            rect.offsetMax = new Vector2(-right, -top);
        }
    }
}
