using System;
using System.Collections.Generic;
using BepInEx.Logging;

namespace YlvaOS
{
    internal static class YlvaComputerTraitInstaller
    {
        private const string SourceTraitName = "YlvaOS_Computer";
        private static bool installed;
        private static bool loggedWaiting;

        public static bool TryInstall(ManualLogSource log)
        {
            if (installed)
            {
                return true;
            }

            int patched = 0;
            bool sourceReady = TryPatchSource(EClass.sources, ref patched);
            TryPatchSource(EClass.editorSources, ref patched);

            if (!sourceReady)
            {
                if (!loggedWaiting && log != null)
                {
                    log.LogDebug("Waiting for Elin SourceThing rows before installing YlvaOS computer trait.");
                    loggedWaiting = true;
                }

                return false;
            }

            if (patched > 0)
            {
                installed = true;
                if (log != null)
                {
                    log.LogInfo("Installed YlvaOS_Computer trait on " + patched + " computer SourceThing row(s).");
                }

                return true;
            }

            return false;
        }

        private static bool TryPatchSource(SourceManager sourceManager, ref int patched)
        {
            if (sourceManager == null || sourceManager.things == null || sourceManager.things.rows == null)
            {
                return false;
            }

            bool sourceReady = sourceManager.things.rows.Count > 0;
            foreach (SourceThing.Row row in sourceManager.things.rows)
            {
                if (row == null || row.trait == null || row.trait.Length == 0)
                {
                    continue;
                }

                string[] nextTraits = ReplaceComputerTrait(row.trait);
                if (ReferenceEquals(nextTraits, row.trait))
                {
                    continue;
                }

                row.trait = nextTraits;
                sourceManager.things.SetRow(row);
                patched++;
            }

            return sourceReady;
        }

        private static string[] ReplaceComputerTrait(string[] traits)
        {
            List<string> result = null;
            for (int i = 0; i < traits.Length; i++)
            {
                string trait = traits[i] ?? string.Empty;
                if (!IsComputerTrait(trait))
                {
                    if (result != null)
                    {
                        result.Add(trait);
                    }

                    continue;
                }

                if (result == null)
                {
                    result = new List<string>();
                    for (int j = 0; j < i; j++)
                    {
                        result.Add(traits[j]);
                    }
                }

                if (!result.Contains(SourceTraitName))
                {
                    result.Add(SourceTraitName);
                }
            }

            return result == null ? traits : result.ToArray();
        }

        private static bool IsComputerTrait(string trait)
        {
            return string.Equals(trait, "Computer", StringComparison.Ordinal) ||
                   string.Equals(trait, "TraitComputer", StringComparison.Ordinal) ||
                   string.Equals(trait, SourceTraitName, StringComparison.Ordinal);
        }
    }
}
