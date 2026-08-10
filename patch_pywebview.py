#!/usr/bin/env python3
"""
Patch pywebview's winforms backend for .NET Core compatibility.

pywebview's winforms.py defines OpenFolderDialog with class-body COM
reflection lookups that break under pythonnet 3.x + .NET Core (the internal
type System.Windows.Forms.FileDialogNative+IFileDialog was renamed, so
GetType() returns None and the class definition throws AttributeError).

This app never uses file dialogs, so we guard the lookups and let the class
import cleanly. Idempotent: safe to run after every pip install/upgrade.

Usage:  py -3 patch_pywebview.py
"""

import sys
from pathlib import Path

WINFORMS = (
    Path(sys.prefix)
    / "Lib"
    / "site-packages"
    / "webview"
    / "platforms"
    / "winforms.py"
)

OLD = """class OpenFolderDialog:
    foldersFilter = 'Folders|\\n'
    flags = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic
    windowsFormsAssembly = Assembly.LoadWithPartialName('System.Windows.Forms')
    iFileDialogType = windowsFormsAssembly.GetType('System.Windows.Forms.FileDialogNative+IFileDialog')
    OpenFileDialogType = windowsFormsAssembly.GetType('System.Windows.Forms.OpenFileDialog')
    FileDialogType = windowsFormsAssembly.GetType('System.Windows.Forms.FileDialog')
    createVistaDialogMethodInfo = OpenFileDialogType.GetMethod('CreateVistaDialog', flags)
    onBeforeVistaDialogMethodInfo = OpenFileDialogType.GetMethod('OnBeforeVistaDialog', flags)
    getOptionsMethodInfo = FileDialogType.GetMethod('GetOptions', flags)
    setOptionsMethodInfo = iFileDialogType.GetMethod('SetOptions', flags)
    fosPickFoldersBitFlag = windowsFormsAssembly.GetType(
        'System.Windows.Forms.FileDialogNative+FOS').GetField('FOS_PICKFOLDERS').GetValue(None)

    vistaDialogEventsConstructorInfo = windowsFormsAssembly.GetType(
        'System.Windows.Forms.FileDialog+VistaDialogEvents').GetConstructor(flags, None, [FileDialogType], [])
    adviseMethodInfo = iFileDialogType.GetMethod('Advise')
    unadviseMethodInfo = iFileDialogType.GetMethod('Unadvise')
    showMethodInfo = iFileDialogType.GetMethod('Show')
"""

NEW = """class OpenFolderDialog:
    foldersFilter = 'Folders|\\n'
    flags = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic
    windowsFormsAssembly = Assembly.LoadWithPartialName('System.Windows.Forms')
    iFileDialogType = windowsFormsAssembly.GetType('System.Windows.Forms.FileDialogNative+IFileDialog')
    OpenFileDialogType = windowsFormsAssembly.GetType('System.Windows.Forms.OpenFileDialog')
    FileDialogType = windowsFormsAssembly.GetType('System.Windows.Forms.FileDialog')
    createVistaDialogMethodInfo = OpenFileDialogType.GetMethod('CreateVistaDialog', flags) if OpenFileDialogType else None
    onBeforeVistaDialogMethodInfo = OpenFileDialogType.GetMethod('OnBeforeVistaDialog', flags) if OpenFileDialogType else None
    getOptionsMethodInfo = FileDialogType.GetMethod('GetOptions', flags) if FileDialogType else None
    setOptionsMethodInfo = iFileDialogType.GetMethod('SetOptions', flags) if iFileDialogType else None
    fosPickFoldersBitFlag = None

    vistaDialogEventsConstructorInfo = None
    adviseMethodInfo = iFileDialogType.GetMethod('Advise') if iFileDialogType else None
    unadviseMethodInfo = iFileDialogType.GetMethod('Unadvise') if iFileDialogType else None
    showMethodInfo = iFileDialogType.GetMethod('Show') if iFileDialogType else None
"""


def main():
    if not WINFORMS.exists():
        print(f"NOT FOUND: {WINFORMS}")
        return 1
    src = WINFORMS.read_text(encoding="utf-8")
    if OLD not in src:
        if NEW in src:
            print("Already patched - nothing to do.")
            return 0
        print("WARNING: expected pattern not found; file may have changed.")
        return 1
    WINFORMS.write_text(src.replace(OLD, NEW), encoding="utf-8")
    print(f"Patched {WINFORMS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
