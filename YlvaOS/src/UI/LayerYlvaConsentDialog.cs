using System;
using UnityEngine;
using UnityEngine.Events;
using UnityEngine.UI;

namespace YlvaOS
{
    internal sealed class LayerYlvaConsentDialog : ELayer
    {
        private Action onAgree;
        private Action onDismiss;
        private bool agreed;
        private Font font;

        public void Configure(Action onAgree, Action onDismiss)
        {
            this.onAgree = onAgree;
            this.onDismiss = onDismiss;
            option = CreateOption();
            onKill = YlvaOsController.CreateUnityEvent();
            closeOthers = false;
            defaultActionMode = false;
        }

        protected override void Awake()
        {
            if (option == null)
            {
                option = CreateOption();
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
            EInput.Consume(consumeAxis: true, _skipFrame: 2);
        }

        public override void OnUpdateInput()
        {
            EInput.Consume(consumeAxis: true, _skipFrame: 1);
        }

        public override bool OnBack()
        {
            return true;
        }

        public override void OnRightClick()
        {
        }

        public override void OnKill()
        {
            if (!agreed && onDismiss != null)
            {
                onDismiss();
            }

            base.OnKill();
        }

        private void BuildUi()
        {
            font = ResolveFont();
            bool isJapanese = IsJapanese();

            RectTransform overlay = CreateRect("YlvaOSConsentOverlay", rectLayers);
            Stretch(overlay, 0f, 0f, 0f, 0f);
            Image overlayImage = overlay.gameObject.AddComponent<Image>();
            overlayImage.color = new Color(0f, 0f, 0f, 0.70f);

            RectTransform window = CreateRect("YlvaOSConsentWindow", overlay);
            window.anchorMin = new Vector2(0.24f, 0.22f);
            window.anchorMax = new Vector2(0.76f, 0.78f);
            window.offsetMin = Vector2.zero;
            window.offsetMax = Vector2.zero;
            Image windowImage = window.gameObject.AddComponent<Image>();
            windowImage.color = new Color(0.055f, 0.063f, 0.067f, 0.98f);

            Text titleText = CreateText("Title", window, 20, TextAnchor.MiddleLeft, new Color(0.86f, 0.97f, 0.88f, 1f));
            titleText.fontStyle = FontStyle.Bold;
            Stretch(titleText.rectTransform, 24f, 18f, 24f, 0f);
            titleText.rectTransform.anchorMin = new Vector2(0f, 1f);
            titleText.rectTransform.anchorMax = new Vector2(1f, 1f);
            titleText.rectTransform.pivot = new Vector2(0.5f, 1f);
            titleText.rectTransform.sizeDelta = new Vector2(-48f, 34f);
            titleText.text = isJapanese ? "YlvaOS 利用警告" : "YlvaOS Usage Warning";

            Text bodyText = CreateText("Body", window, 16, TextAnchor.UpperLeft, new Color(0.90f, 0.95f, 0.91f, 1f));
            bodyText.lineSpacing = 1.10f;
            Stretch(bodyText.rectTransform, 24f, 66f, 24f, 88f);
            bodyText.text = isJapanese ? JapaneseWarningText : EnglishWarningText;

            Button agreeButton = CreateButton("Agree", window, isJapanese ? "同意" : "Agree");
            RectTransform agreeRect = agreeButton.GetComponent<RectTransform>();
            agreeRect.anchorMin = new Vector2(0.5f, 0f);
            agreeRect.anchorMax = new Vector2(0.5f, 0f);
            agreeRect.pivot = new Vector2(0.5f, 0f);
            agreeRect.anchoredPosition = new Vector2(0f, 26f);
            agreeRect.sizeDelta = new Vector2(190f, 42f);
            agreeButton.onClick.AddListener(Accept);
        }

        private void Accept()
        {
            agreed = true;
            Action agreeHandler = onAgree;
            if (option != null)
            {
                option.canClose = true;
            }

            Close();

            if (agreeHandler != null)
            {
                agreeHandler();
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
            image.color = new Color(0.22f, 0.31f, 0.28f, 1f);
            Button button = rect.gameObject.AddComponent<Button>();
            button.targetGraphic = image;

            Text text = CreateText("Label", rect, 17, TextAnchor.MiddleCenter, new Color(0.96f, 1f, 0.96f, 1f));
            text.fontStyle = FontStyle.Bold;
            Stretch(text.rectTransform, 0f, 0f, 0f, 0f);
            text.text = label;
            return button;
        }

        private static Layer.Option CreateOption()
        {
            Layer.Option option = YlvaOsController.CreateLayerOption();
            option.canClose = false;
            option.hideOthers = false;
            option.pauseGame = true;
            return option;
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

        private static bool IsJapanese()
        {
            try
            {
                return Lang.isJP;
            }
            catch
            {
                return false;
            }
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

        private const string JapaneseWarningText =
            "\"YlvaOS\"がインストールされました。このMODは同梱されているファイルの都合上、Windowsでのみ動作します。\n" +
            "\n" +
            "＊このMODは実際にPC上でLinux互換のOSを動かすMODです、このMODを使用したことによる損害や責任は、aoi_nasukoは一切受け付けません。Linux、あるいは派生OSが理解できる方のみご利用することを強くお勧めします＊\n" +
            "\n" +
            "このMODを使用するためには、上記の注意文に同意してください。";

        private const string EnglishWarningText =
            "\"YlvaOS\" has been installed. Because of the files bundled with this MOD, it works on Windows only.\n" +
            "\n" +
            "*This MOD runs an actual Linux-compatible OS on your PC. aoi_nasuko accepts no responsibility for any damage caused by using this MOD. Use of this MOD is strongly recommended only for users who understand Linux or derivative operating systems.*\n" +
            "\n" +
            "To use this MOD, please agree to the notice above.";
    }
}
