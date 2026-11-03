<#
VAMPVIZ installer: one command, and nothing needs admin. Press Win+R and paste:

    powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/benjaminhoffman-bit/vampviz/main/install.ps1 | iex"

or double-click Install.cmd in the extracted download. Run it again to update; your settings stay.
Nothing here needs admin: Python goes in your user folder, the libraries in your user site-packages,
VAMPVIZ in %LOCALAPPDATA%\VAMPVIZ, with Start menu and desktop shortcuts.
Uninstall: the "Uninstall VAMPVIZ" shortcut, or Install.cmd -Uninstall.
#>
param(
    [switch]$Uninstall,
    [string]$Dest = "$env:LOCALAPPDATA\VAMPVIZ",
    [string]$Programs = [Environment]::GetFolderPath('Programs'),
    [string]$Desktop = [Environment]::GetFolderPath('Desktop'),
    [switch]$NoStart  # for tests: no launch, no questions, no pause at the end
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$Files = 'vampviz.pyw', 'shaders.py', 'appicon.py', 'README.md', 'LICENSE.txt', 'install.ps1', 'Install.cmd'
$Libs = 'numpy', 'moderngl', 'glfw', 'pyaudiowpatch'
$Links = "$Programs\VAMPVIZ.lnk", "$Desktop\VAMPVIZ.lnk", "$Programs\Uninstall VAMPVIZ.lnk"
$AppId = 'SilvaDes.VAMPVIZ'

function Say([string]$text) { Write-Host "  $text" }
function Step([string]$text) { Write-Host ''; Write-Host "== $text" -ForegroundColor Cyan }

function Find-Python {
    $tries = @()
    if (Get-Command py -ErrorAction SilentlyContinue) { $tries += , @('py', '-3') }
    if (Get-Command python -ErrorAction SilentlyContinue) { $tries += , @('python') }
    Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe", "$env:ProgramFiles\Python3*\python.exe" `
        -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | ForEach-Object { $tries += , @($_.FullName) }
    foreach ($try in $tries) {
        try {
            $exe = & $try[0] $try[1..9] -c 'import sys; assert sys.version_info >= (3, 10); print(sys.executable)' 2>$null
            if ($LASTEXITCODE -eq 0 -and $exe) {
                $exe = ([string]($exe | Select-Object -Last 1)).Trim()
                if (Test-Path -LiteralPath $exe) { return $exe }
            }
        } catch { }
    }
    return $null
}

function Add-Helpers {
    # stamp a shortcut with the AppUserModelID, so Windows shows VAMPVIZ as its own taskbar app and a pin starts it
    if ('VampVizAppId' -as [type]) { return }
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
[ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IVampVizPropertyStore {
  [PreserveSig] int GetCount(out uint count);
  [PreserveSig] int GetAt(uint index, out VampVizPropertyKey key);
  [PreserveSig] int GetValue(ref VampVizPropertyKey key, out VampVizPropVariant value);
  [PreserveSig] int SetValue(ref VampVizPropertyKey key, ref VampVizPropVariant value);
  [PreserveSig] int Commit();
}
[StructLayout(LayoutKind.Sequential, Pack = 4)] public struct VampVizPropertyKey { public Guid Fmtid; public uint Pid; }
[StructLayout(LayoutKind.Explicit)] public struct VampVizPropVariant { [FieldOffset(0)] public ushort Vt; [FieldOffset(8)] public IntPtr Ptr; }
public static class VampVizAppId {
  [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
  static extern int SHGetPropertyStoreFromParsingName(string path, IntPtr bindCtx, int flags, ref Guid iid, out IVampVizPropertyStore store);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern IntPtr FindWindowW(string cls, string title);
  [DllImport("user32.dll")] static extern bool PostMessageW(IntPtr hwnd, uint msg, IntPtr w, IntPtr l);
  public static bool Quit() {  // WM_CLOSE to the tray window: VAMPVIZ quits for real and saves its settings
    IntPtr tray = FindWindowW("VampVizTray", null);
    return tray != IntPtr.Zero && PostMessageW(tray, 0x10, IntPtr.Zero, IntPtr.Zero);
  }
  public static void Set(string lnk, string id) {
    Guid iid = new Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99");
    IVampVizPropertyStore store;
    Marshal.ThrowExceptionForHR(SHGetPropertyStoreFromParsingName(lnk, IntPtr.Zero, 2, ref iid, out store));
    VampVizPropertyKey key = new VampVizPropertyKey { Fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), Pid = 5 };
    VampVizPropVariant value = new VampVizPropVariant { Vt = 31, Ptr = Marshal.StringToCoTaskMemUni(id) };
    try { Marshal.ThrowExceptionForHR(store.SetValue(ref key, ref value)); Marshal.ThrowExceptionForHR(store.Commit()); }
    finally { Marshal.FreeCoTaskMem(value.Ptr); Marshal.ReleaseComObject(store); }
  }
}
'@
}

function Stop-VampViz {
    $running = @(Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" |
        Where-Object { $_.CommandLine -like "*$Destampviz.pyw*" } | ForEach-Object { Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue })
    if (-not $running) { return }
    try { [void][VampVizAppId]::Quit() } catch { }
    foreach ($p in $running) {
        if (-not $p.WaitForExit(5000)) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
    }
}

function Add-Link([string]$path, [string]$target, [string]$arguments, [string]$about, [bool]$stamp) {
    $link = (New-Object -ComObject WScript.Shell).CreateShortcut($path)
    $link.TargetPath, $link.Arguments, $link.WorkingDirectory = $target, $arguments, $Dest
    $link.IconLocation, $link.Description = "$Dest\assets\vampviz.ico,0", $about
    $link.Save()
    if ($stamp) { try { [VampVizAppId]::Set($path, $AppId) } catch { } }  # only the taskbar grouping is lost
}

try {
    try { Add-Helpers } catch { }
    if ($Uninstall) {
        Step 'Removing VAMPVIZ'
        Set-Location $env:TEMP; [Environment]::CurrentDirectory = $env:TEMP
        Stop-VampViz
        Remove-Item $Links -ErrorAction SilentlyContinue
        $keep = if ($NoStart) { 'y' } else { Read-Host '  Keep your settings, in case you install it again? (Y/n)' }
        if ($keep -match '^\s*n') { Remove-Item $Dest -Recurse -Force -ErrorAction SilentlyContinue }
        else { Get-ChildItem $Dest -Exclude 'vampviz.json' -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force }
        Say 'VAMPVIZ is removed. If you pinned it to the taskbar, right-click it there and choose Unpin from taskbar.'
    } else {
        Step 'Python'
        $python = Find-Python
        if (-not $python -and (Get-Command winget -ErrorAction SilentlyContinue)) {
            Say 'Installing Python 3.12 with winget (a minute or two)...'
            try {
                winget install --id Python.Python.3.12 --exact --scope user --silent --accept-package-agreements `
                    --accept-source-agreements --disable-interactivity | Out-Null
            } catch { }
            $python = Find-Python
        }
        if (-not $python) { throw 'Python could not be installed. Get it from python.org, then run this again.' }
        Say "Using $python"
        $pythonw = Join-Path (Split-Path $python) 'pythonw.exe'

        Step 'Libraries'
        & $python -m pip install --user --quiet --disable-pip-version-check $Libs
        if ($LASTEXITCODE -ne 0) { throw "pip could not install $($Libs -join ', ')" }
        Say "$($Libs -join ', ')"

        Step 'VAMPVIZ'
        New-Item -ItemType Directory -Force $Dest | Out-Null
        $here = if ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot 'vampviz.pyw'))) { (Resolve-Path $PSScriptRoot).Path } else { '' }
        $from = if ($here -and $here -ne (Resolve-Path $Dest).Path) { $here } else { '' }  # the installed copy updates
        if (-not $from) {
            Say 'Downloading the latest VAMPVIZ from GitHub...'
            $zip, $unzipped = (Join-Path $env:TEMP 'vampviz-main.zip'), (Join-Path $env:TEMP 'vampviz-download')
            Invoke-WebRequest 'https://github.com/benjaminhoffman-bit/vampviz/archive/refs/heads/main.zip' -OutFile $zip -UseBasicParsing
            Remove-Item $unzipped -Recurse -Force -ErrorAction SilentlyContinue
            Expand-Archive $zip $unzipped -Force
            $from = Join-Path $unzipped 'vampviz-main'
        }
        Stop-VampViz
        foreach ($file in $Files) { Copy-Item (Join-Path $from $file) $Dest -Force }
        & $python "$Dest\vampviz.pyw" --icon  # draws assets\vampviz.ico
        Say "Installed in $Dest"

        Step 'Shortcuts'
        New-Item -ItemType Directory -Force $Programs, $Desktop | Out-Null
        Add-Link $Links[0] $pythonw "`"$Dest\vampviz.pyw`"" 'VAMPVIZ: a visualizer for whatever your PC is playing' $true
        Add-Link $Links[1] $pythonw "`"$Dest\vampviz.pyw`"" 'VAMPVIZ: a visualizer for whatever your PC is playing' $true
        $where = ''
        foreach ($name in 'Dest', 'Programs', 'Desktop') {
            if ($PSBoundParameters.ContainsKey($name)) { $where += " -$name `"$((Resolve-Path (Get-Variable $name -ValueOnly)).Path)`"" }
        }
        Add-Link $Links[2] "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" `
            "-NoProfile -ExecutionPolicy Bypass -File `"$Dest\install.ps1`" -Uninstall$where" 'Remove VAMPVIZ' $false
        Say 'VAMPVIZ is in the Start menu and on the desktop. To keep it on the taskbar, right-click its taskbar'
        Say 'button once it opens and choose Pin to taskbar.'
        if (-not $NoStart) { Start-Process explorer.exe "`"$($Links[0])`"" }
    }
} catch {
    Write-Host ''
    Write-Host "  Something went wrong: $($_.Exception.Message)" -ForegroundColor Red
}
if (-not $NoStart) { Write-Host ''; [void](Read-Host '  Press Enter to close this window') }
