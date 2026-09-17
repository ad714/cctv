import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
NAME = 'CCTV'


def main():
    for folder in ('build', 'dist'):
        path = os.path.join(HERE, folder)
        if os.path.isdir(path):
            shutil.rmtree(path)

    command = [
        sys.executable, '-m', 'PyInstaller',
        '--noconfirm', '--clean', '--windowed',
        '--name', NAME,
        '--icon', os.path.join(HERE, 'icon.ico'),
        '--exclude-module', 'torch',
        '--exclude-module', 'torchvision',
        '--exclude-module', 'ultralytics',
        '--exclude-module', 'cv2',
        '--exclude-module', 'matplotlib',
        '--exclude-module', 'scipy',
        '--exclude-module', 'pandas',
        '--exclude-module', 'PySide6.QtWebEngineCore',
        '--exclude-module', 'PySide6.Qt3DCore',
        '--exclude-module', 'PySide6.QtQuick',
        '--exclude-module', 'PySide6.QtQml',
        os.path.join(HERE, 'app.py'),
    ]
    result = subprocess.run(command, cwd=HERE)
    if result.returncode != 0:
        return result.returncode

    target = os.path.join(HERE, 'dist', NAME, '%s.exe' % NAME)
    if not os.path.exists(target):
        print('build finished but %s is missing' % target)
        return 1
    size = sum(
        os.path.getsize(os.path.join(root, name))
        for root, _dirs, files in os.walk(os.path.join(HERE, 'dist', NAME))
        for name in files)
    print('\nBuilt %s' % target)
    print('Folder size: %.0f MB' % (size / 1048576))
    return 0


if __name__ == '__main__':
    sys.exit(main())
