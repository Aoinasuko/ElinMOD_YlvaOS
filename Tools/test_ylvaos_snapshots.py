#!/usr/bin/env python3
"""Exercise the host-side YlvaOS snapshot manager with a tiny qcow2 disk."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


PROGRAM_CS = r"""
using System;
using System.Diagnostics;
using System.IO;
using System.Security.Cryptography;
using YlvaOS;

internal static class Program
{
    private static int Main(string[] args)
    {
        try
        {
            Run(args[0], args[1]);
            Console.WriteLine("__YLVA_SNAPSHOT_TESTS_OK__");
            return 0;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine(ex);
            return 1;
        }
    }

    private static void Run(string rootDirectory, string qemuImgPath)
    {
        string vmDirectory = Path.Combine(rootDirectory, "vm");
        Directory.CreateDirectory(vmDirectory);
        string diskPath = Path.Combine(vmDirectory, "disk.qcow2");
        RunQemuImg(qemuImgPath, "create -f qcow2 " + Quote(diskPath) + " 32M");
        string originalHash = HashFile(diskPath);

        YlvaSnapshotManager manager = new YlvaSnapshotManager(rootDirectory, diskPath, qemuImgPath, delegate { return false; });
        ExpectUserException(delegate { manager.CreateSnapshot("../escape", "bad"); }, "snapshot name");
        ExpectUserException(delegate { manager.CreateSnapshot(".hidden", "bad"); }, "snapshot name");
        ExpectUserException(delegate { manager.CreateSnapshot("bad.", "bad"); }, "snapshot name");
        ExpectUserException(delegate { manager.CreateSnapshot("CON.txt", "bad"); }, "reserved");

        YlvaSnapshotEntry created = manager.CreateSnapshot("base1", "before package install");
        Assert(created.IsValid, "created snapshot must be valid");
        Assert(File.Exists(Path.Combine(rootDirectory, "snapshots", "base1", "disk.qcow2")), "snapshot disk was not written");

        string list = manager.FormatSnapshotList();
        Assert(list.Contains("base1"), "snapshot list missing base1");
        Assert(list.Contains("before package install"), "snapshot list missing memo");
        Assert(list.Contains("0.05"), "snapshot list missing YlvaOS version");

        YlvaSnapshotManager runningManager = new YlvaSnapshotManager(rootDirectory, diskPath, qemuImgPath, delegate { return true; });
        ExpectUserException(delegate { runningManager.CreateSnapshot("live", "running"); }, "disabled while the VM is running");
        ExpectUserException(delegate { runningManager.RestoreSnapshot("base1"); }, "disabled while the VM is running");
        ExpectUserException(delegate { runningManager.DeleteSnapshot("base1"); }, "disabled while the VM is running");

        File.WriteAllText(diskPath, "not a qcow2 disk");
        YlvaSnapshotEntry restored = manager.RestoreSnapshot("base1");
        Assert(restored.IsValid, "restored snapshot must be valid");
        Assert(HashFile(diskPath) == originalHash, "restore did not replace the active root disk");

        string brokenDirectory = Path.Combine(rootDirectory, "snapshots", "broken");
        Directory.CreateDirectory(brokenDirectory);
        File.WriteAllText(
            Path.Combine(brokenDirectory, "metadata.json"),
            "{\n  \"schemaVersion\": 1,\n  \"name\": \"broken\",\n  \"createdUtc\": \"2026-09-02T00:00:00Z\",\n  \"sizeBytes\": 4,\n  \"memo\": \"corrupt fixture\",\n  \"ylvaOsVersion\": \"0.05\"\n}\n");
        File.WriteAllText(Path.Combine(brokenDirectory, "disk.qcow2"), "bad");
        string brokenList = manager.FormatSnapshotList();
        Assert(brokenList.Contains("broken"), "broken snapshot missing from list");
        Assert(brokenList.Contains("invalid"), "broken snapshot was not marked invalid");
        ExpectUserException(delegate { manager.RestoreSnapshot("broken"); }, "cannot be restored");

        manager.DeleteSnapshot("base1");
        Assert(!Directory.Exists(Path.Combine(rootDirectory, "snapshots", "base1")), "snapshot directory was not deleted");
    }

    private static void RunQemuImg(string qemuImgPath, string arguments)
    {
        ProcessStartInfo info = new ProcessStartInfo
        {
            FileName = qemuImgPath,
            Arguments = arguments,
            WorkingDirectory = Path.GetDirectoryName(qemuImgPath),
            CreateNoWindow = true,
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true
        };

        using (Process process = Process.Start(info))
        {
            string output = process.StandardOutput.ReadToEnd();
            string error = process.StandardError.ReadToEnd();
            process.WaitForExit();
            if (process.ExitCode != 0)
            {
                throw new InvalidOperationException("qemu-img failed: " + error + output);
            }
        }
    }

    private static string HashFile(string path)
    {
        using (SHA256 sha = SHA256.Create())
        using (FileStream stream = File.OpenRead(path))
        {
            return BitConverter.ToString(sha.ComputeHash(stream)).Replace("-", string.Empty);
        }
    }

    private static string Quote(string value)
    {
        return "\"" + value.Replace("\"", "\\\"") + "\"";
    }

    private static void Assert(bool condition, string message)
    {
        if (!condition)
        {
            throw new InvalidOperationException(message);
        }
    }

    private static void ExpectUserException(Action action, string expectedText)
    {
        try
        {
            action();
        }
        catch (YlvaUserException ex)
        {
            if (ex.Message.IndexOf(expectedText, StringComparison.OrdinalIgnoreCase) >= 0)
            {
                return;
            }

            throw new InvalidOperationException("Unexpected exception text: " + ex.Message);
        }

        throw new InvalidOperationException("Expected YlvaUserException containing: " + expectedText);
    }
}
"""


PROJECT_XML = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net8.0</TargetFramework>
    <ImplicitUsings>disable</ImplicitUsings>
    <Nullable>disable</Nullable>
    <EnableDefaultCompileItems>false</EnableDefaultCompileItems>
  </PropertyGroup>
  <ItemGroup>
    <Compile Include="Program.cs" />
    <Compile Include="..\\..\\YlvaOS\\src\\VM\\YlvaSnapshotManager.cs" Link="YlvaSnapshotManager.cs" />
    <Compile Include="..\\..\\YlvaOS\\src\\Runtime\\YlvaUserException.cs" Link="YlvaUserException.cs" />
    <Compile Include="..\\..\\YlvaOS\\src\\ModInfo.cs" Link="ModInfo.cs" />
  </ItemGroup>
</Project>
"""


def main() -> int:
    root = repo_root()
    qemu_img = root / "Mod_YlvaOS" / "Tools" / "qemu" / "qemu-img.exe"
    if not qemu_img.is_file():
        raise FileNotFoundError(qemu_img)

    build_dir = root / "_work" / "ylvaos-snapshot-tests"
    sandbox = build_dir / "sandbox"
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True)
    sandbox.mkdir()

    (build_dir / "Program.cs").write_text(PROGRAM_CS, encoding="utf-8")
    project = build_dir / "SnapshotTests.csproj"
    project.write_text(PROJECT_XML, encoding="utf-8")
    subprocess.run(["dotnet", "run", "--project", str(project), "--", str(sandbox), str(qemu_img)], check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
