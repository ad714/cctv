import os
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(HERE, 'dist', 'CCTV', 'CCTV.exe')
ICON = os.path.join(HERE, 'icon.ico')

MAKE_LINK = """
$ws = New-Object -ComObject WScript.Shell
$link = $ws.CreateShortcut('{path}')
$link.TargetPath = '{target}'
$link.WorkingDirectory = '{workdir}'
$link.IconLocation = '{icon}'
$link.Description = 'Hikvision DVR viewer'
$link.Save()
Write-Output 'created {path}'
"""

PIN = """
$exe = '{target}'
try {{
    $shell = New-Object -ComObject Shell.Application
    $folder = $shell.Namespace((Split-Path $exe))
    $item = $folder.ParseName((Split-Path $exe -Leaf))
    $verb = $item.Verbs() | Where-Object {{ $_.Name -replace '&','' -match 'Pin to tas' }}
    if ($verb) {{ $verb.DoIt(); Write-Output 'pinned' }} else {{ Write-Output 'noverb' }}
}} catch {{ Write-Output 'failed' }}
"""


def run(script):
    result = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
                            capture_output=True, text=True)
    return (result.stdout or result.stderr).strip()


def main():
    if not os.path.exists(TARGET):
        print('Build it first:  python build.py')
        return 1
    desktop = os.path.join(os.path.expanduser('~'), 'Desktop', 'CCTV.lnk')
    start = os.path.join(os.environ.get('APPDATA', ''), 'Microsoft', 'Windows',
                         'Start Menu', 'Programs', 'CCTV.lnk')
    workdir = os.path.dirname(TARGET)

    for path in (desktop, start):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        print(run(MAKE_LINK.format(path=path, target=TARGET, workdir=workdir, icon=ICON)))

    outcome = run(PIN.format(target=TARGET))
    if outcome == 'pinned':
        print('pinned to taskbar')
    else:
        print('Could not pin automatically (Windows restricts this).')
        print('Right-click the Desktop shortcut or the running window, then "Pin to taskbar".')
    return 0


if __name__ == '__main__':
    sys.exit(main())
