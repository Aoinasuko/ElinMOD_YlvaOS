using System;
using HarmonyLib;

namespace YlvaOS
{
    [HarmonyPatch(typeof(Trait), nameof(Trait.CanUse), new[] { typeof(Chara) })]
    internal static class TraitComputerCanUsePatch
    {
        private static void Postfix(Trait __instance, Chara c, ref bool __result)
        {
            if (IsPlayerComputer(__instance, c))
            {
                __result = true;
            }
        }

        internal static bool IsPlayerComputer(Trait trait, Chara c)
        {
            return trait is TraitComputer && trait.owner != null && c != null && c.IsPC;
        }
    }

    [HarmonyPatch(typeof(Trait), nameof(Trait.OnUse), new[] { typeof(Chara) })]
    internal static class TraitComputerOnUsePatch
    {
        private static bool Prefix(Trait __instance, Chara c, ref bool __result)
        {
            if (!TraitComputerCanUsePatch.IsPlayerComputer(__instance, c))
            {
                return true;
            }

            try
            {
                YlvaOsController.OpenComputer(__instance.owner);
            }
            catch (Exception ex)
            {
                if (Plugin.Log != null)
                {
                    Plugin.Log.LogError("TraitComputer.OnUse patch failed: " + ex);
                }
            }

            __result = false;
            return false;
        }
    }
}
