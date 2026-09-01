using System;
using BepInEx.Logging;

namespace YlvaOS
{
    internal static class YlvaStartupProvisioner
    {
        private static bool completed;
        private static bool consentDialogOpen;

        public static bool TryRunAndNotify(ManualLogSource log)
        {
            if (completed)
            {
                return true;
            }

            if (YlvaSessionManager.Instance == null || YlvaSessionManager.Instance.Vm == null)
            {
                return false;
            }

            if (!CanShowDialog())
            {
                return false;
            }

            if (!YlvaSessionManager.Instance.Vm.Config.StartupWarningAccepted)
            {
                return ShowConsentDialog(log);
            }

            RunProvisioning(log);
            return true;
        }

        private static bool CanShowDialog()
        {
            try
            {
                return EClass.ui != null;
            }
            catch
            {
                return false;
            }
        }

        private static bool ShowConsentDialog(ManualLogSource log)
        {
            if (consentDialogOpen)
            {
                return true;
            }

            try
            {
                if (EClass.ui.GetLayer<LayerYlvaConsentDialog>(fromTop: true) != null)
                {
                    consentDialogOpen = true;
                    return true;
                }

                consentDialogOpen = true;
                UnityEngine.GameObject layerObject = new UnityEngine.GameObject("LayerYlvaConsentDialog", typeof(UnityEngine.RectTransform));
                LayerYlvaConsentDialog layer = layerObject.AddComponent<LayerYlvaConsentDialog>();
                layer.Configure(
                    delegate
                    {
                        consentDialogOpen = false;
                        YlvaSessionManager.Instance.Vm.AcceptStartupWarning();
                        RunProvisioning(log);
                    },
                    delegate
                    {
                        consentDialogOpen = false;
                    });
                EClass.ui.AddLayer(layer);
                return true;
            }
            catch (Exception ex)
            {
                consentDialogOpen = false;
                if (log != null)
                {
                    log.LogError("Failed to show YlvaOS startup warning: " + ex);
                }

                try
                {
                    Msg.SayRaw("YlvaOS startup warning could not be shown. See BepInEx log.");
                }
                catch
                {
                }

                return false;
            }
        }

        private static void RunProvisioning(ManualLogSource log)
        {
            completed = true;
            YlvaProvisioningResult result = YlvaSessionManager.Instance.Vm.PrepareStartupAssets();

            if (log != null)
            {
                log.LogInfo("YlvaOS provisioning finished: " + result.StatusLine);
                if (result.Errors.Count > 0)
                {
                    foreach (string error in result.Errors)
                    {
                        log.LogWarning("YlvaOS provisioning error: " + error);
                    }
                }
            }
        }
    }
}
