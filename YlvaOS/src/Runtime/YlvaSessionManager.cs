using System;
using System.IO;
using System.Reflection;
using BepInEx.Logging;
using Newtonsoft.Json;
using UnityEngine;

namespace YlvaOS
{
    internal sealed class YlvaSessionManager
    {
        private readonly ManualLogSource log;
        private YlvaState state;
        private YlvaMachine machine;
        private YlvaVmBackend vm;

        private YlvaSessionManager(ManualLogSource log)
        {
            this.log = log;
            RootDirectory = ResolveRootDirectory();
            StatePath = Path.Combine(RootDirectory, ModInfo.StateFileName);
            Vm = new YlvaVmBackend(RootDirectory, ResolvePluginDirectory(), log);
            Load();
        }

        public static YlvaSessionManager Instance { get; private set; }

        public string RootDirectory { get; private set; }
        public string StatePath { get; private set; }
        public YlvaVmBackend Vm
        {
            get { return vm; }
            private set { vm = value; }
        }

        public static void Initialize(ManualLogSource log)
        {
            Instance = new YlvaSessionManager(log);
        }

        public YlvaMachine OpenSession()
        {
            if (state == null)
            {
                Load();
            }

            if (state.Phase == YlvaBootPhase.Shell && (Vm == null || !Vm.IsRunning))
            {
                state.PoweredOff = true;
                state.Authenticated = false;
                state.Phase = state.SetupComplete ? YlvaBootPhase.LoginUserName : YlvaBootPhase.SetupUserName;
            }

            if (machine == null || !object.ReferenceEquals(machine.State, state))
            {
                machine = new YlvaMachine(state, Vm);
            }

            if (!state.HasBooted || state.PoweredOff)
            {
                machine.ColdBoot();
                Save();
            }

            return machine;
        }

        public void Save()
        {
            if (state == null)
            {
                return;
            }

            try
            {
                Directory.CreateDirectory(RootDirectory);
                string json = JsonConvert.SerializeObject(state, Formatting.Indented);
                File.WriteAllText(StatePath, json);
            }
            catch (Exception ex)
            {
                if (log != null)
                {
                    log.LogError("Failed to save YlvaOS state: " + ex);
                }
            }
        }

        public void StopVm()
        {
            if (Vm != null)
            {
                Vm.Stop();
            }
        }

        private void Load()
        {
            try
            {
                Directory.CreateDirectory(RootDirectory);
                if (!File.Exists(StatePath))
                {
                    state = YlvaState.CreateDefault();
                    Save();
                    return;
                }

                string json = File.ReadAllText(StatePath);
                state = JsonConvert.DeserializeObject<YlvaState>(json) ?? YlvaState.CreateDefault();
                state.Normalize();
                if (!state.SetupComplete)
                {
                    state.Phase = YlvaBootPhase.SetupUserName;
                    state.Authenticated = false;
                    state.PoweredOff = true;
                }

                new YlvaVfs(state).EnsureDefaultTree();
            }
            catch (Exception ex)
            {
                if (log != null)
                {
                    log.LogWarning("Failed to load YlvaOS state; using a fresh sandbox: " + ex);
                }

                state = YlvaState.CreateDefault();
            }
        }

        private static string ResolveRootDirectory()
        {
            string persistentDataPath = Application.persistentDataPath;
            if (string.IsNullOrEmpty(persistentDataPath))
            {
                string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
                string appData = Directory.GetParent(local).FullName;
                persistentDataPath = Path.Combine(appData, "LocalLow", "Lafrontier", "Elin");
            }

            return Path.Combine(persistentDataPath, ModInfo.StateDirectoryName);
        }

        private static string ResolvePluginDirectory()
        {
            try
            {
                string location = Assembly.GetExecutingAssembly().Location;
                if (!string.IsNullOrEmpty(location))
                {
                    return Path.GetDirectoryName(location);
                }
            }
            catch
            {
            }

            return string.Empty;
        }
    }
}
