using System.Collections;
using BepInEx;
using BepInEx.Logging;
using HarmonyLib;

namespace YlvaOS
{
    [BepInPlugin(ModInfo.Guid, ModInfo.Name, ModInfo.PluginVersion)]
    public sealed class Plugin : BaseUnityPlugin
    {
        internal static ManualLogSource Log { get; private set; }
        private Harmony harmony;

        private void Awake()
        {
            Log = Logger;
            YlvaSessionManager.Initialize(Logger);

            try
            {
                harmony = new Harmony(ModInfo.Guid);
                harmony.PatchAll(typeof(Plugin).Assembly);
            }
            catch (System.Exception ex)
            {
                Logger.LogError("YlvaOS Harmony patches failed: " + ex);
            }

            Logger.LogInfo("Ylva OS loaded.");
        }

        private IEnumerator Start()
        {
            bool traitInstalled = false;
            bool provisioned = false;
            for (int i = 0; i < 1200; i++)
            {
                if (!traitInstalled)
                {
                    traitInstalled = YlvaComputerTraitInstaller.TryInstall(Logger);
                }

                if (!provisioned)
                {
                    provisioned = YlvaStartupProvisioner.TryRunAndNotify(Logger);
                }

                if (traitInstalled && provisioned)
                {
                    yield break;
                }

                yield return null;
            }

            if (!traitInstalled)
            {
                Logger.LogWarning("YlvaOS could not find a SourceThing row using TraitComputer before timeout.");
            }

            if (!provisioned)
            {
                Logger.LogWarning("YlvaOS startup provisioning could not show a dialog before timeout.");
            }
        }

        private void OnDestroy()
        {
            try
            {
                harmony?.UnpatchSelf();
            }
            catch (System.Exception ex)
            {
                Logger.LogError("YlvaOS Harmony unpatch failed: " + ex);
            }

            if (YlvaSessionManager.Instance != null)
            {
                YlvaSessionManager.Instance.StopVm();
                YlvaSessionManager.Instance.Save();
            }
        }
    }
}
