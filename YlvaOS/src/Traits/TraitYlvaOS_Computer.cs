using System;

public sealed class TraitYlvaOS_Computer : TraitComputer
{
    public override bool CanUse(Chara c)
    {
        return IsPlayer(c);
    }

    public override bool OnUse(Chara c)
    {
        return Open(c);
    }

    public override void TrySetAct(ActPlan p)
    {
        if (p == null || owner == null || !owner.IsInstalled || !IsPlayer(p.cc))
        {
            return;
        }

        p.TrySetAct(LangUse, () => Open(p.cc), owner);
    }

    private bool Open(Chara c)
    {
        if (!IsPlayer(c) || owner == null)
        {
            return false;
        }

        try
        {
            YlvaOS.YlvaOsController.OpenComputer(owner);
        }
        catch (Exception ex)
        {
            if (YlvaOS.Plugin.Log != null)
            {
                YlvaOS.Plugin.Log.LogError("YlvaOS computer trait failed: " + ex);
            }
        }

        return true;
    }

    private static bool IsPlayer(Chara c)
    {
        return c != null && c.IsPC;
    }
}
