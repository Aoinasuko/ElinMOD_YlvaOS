using System.Collections;
using BepInEx;
using BepInEx.Logging;
using HarmonyLib;

namespace YlvaOS
{
    [BepInPlugin(ModInfo.Guid, ModInfo.Name, ModInfo.PluginVersion)]
    public sealed class Plugin : BaseUnityPlugin
    {
        private Harmony harmony;

        internal static ManualLogSource Log { get; private set; }

        private void Awake()
        {
            Log = Logger;
            YlvaSessionManager.Initialize(Logger);

            harmony = new Harmony(ModInfo.Guid);
            harmony.PatchAll(typeof(Plugin).Assembly);

            Logger.LogInfo("Ylva OS loaded.");
        }

        private IEnumerator Start()
        {
            for (int i = 0; i < 1200; i++)
            {
                if (YlvaStartupProvisioner.TryRunAndNotify(Logger))
                {
                    yield break;
                }

                yield return null;
            }

            Logger.LogWarning("YlvaOS startup provisioning could not show a dialog before timeout.");
        }

        private void OnDestroy()
        {
            if (harmony != null)
            {
                harmony.UnpatchSelf();
                harmony = null;
            }

            if (YlvaSessionManager.Instance != null)
            {
                YlvaSessionManager.Instance.StopVm();
                YlvaSessionManager.Instance.Save();
            }
        }
    }
}
