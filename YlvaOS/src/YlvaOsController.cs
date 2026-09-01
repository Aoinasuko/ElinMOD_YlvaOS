using System;
using UnityEngine;
using UnityEngine.Events;

namespace YlvaOS
{
    internal static class YlvaOsController
    {
        public static void OpenComputer(Card computer)
        {
            try
            {
                if (EClass.ui == null)
                {
                    return;
                }

                LayerYlvaOs existing = EClass.ui.GetLayer<LayerYlvaOs>(fromTop: true);
                if (existing != null)
                {
                    return;
                }

                YlvaMachine machine = YlvaSessionManager.Instance.OpenSession();
                GameObject layerObject = new GameObject("LayerYlvaOs", typeof(RectTransform));
                LayerYlvaOs layer = layerObject.AddComponent<LayerYlvaOs>();
                layer.Configure(machine);
                EClass.ui.AddLayer(layer);
            }
            catch (Exception ex)
            {
                if (Plugin.Log != null)
                {
                    Plugin.Log.LogError("Failed to open YlvaOS: " + ex);
                }

                try
                {
                    Msg.SayRaw("YlvaOS failed to boot. See BepInEx log.");
                }
                catch
                {
                }
            }
        }

        public static Layer.Option CreateLayerOption()
        {
            return new Layer.Option
            {
                canClose = true,
                screenClickClose = false,
                screenClickCloseRight = false,
                allowGeneralInput = false,
                allowInventoryInteraction = false,
                persist = false,
                blur = false,
                hideOthers = true,
                hideInspector = true,
                pauseGame = true,
                consumeInput = true,
                hideFloatUI = true,
                hideWidgets = true,
                screenlockType = Layer.Option.ScreenlockType.None
            };
        }

        public static UnityEvent CreateUnityEvent()
        {
            return new UnityEvent();
        }
    }
}
