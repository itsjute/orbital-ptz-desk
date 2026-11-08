# PyInstaller build of "Orbital.exe" (one folder, no console window).
#
#   pyinstaller packaging/orbital.spec                       -> without stabilization packages
#   set PTZ_BUILD_FULL=1 & pyinstaller packaging/orbital.spec -> with numpy and OpenCV
#
# The release workflow builds both and lets packaging/split_components.py sort the files into
# installer components, so that "Stabilization" can be left out at install time. numpy and cv2
# are collected as plain files (not into the archive inside the exe) for exactly that reason.
import os

FULL = os.environ.get("PTZ_BUILD_FULL") == "1"
ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))  # noqa: F821 (defined by PyInstaller)

datas = [(os.path.join(ROOT, n), ".") for n in ("index.html", "lucide-icons.json", "config.example.ini", "version.txt")
         if os.path.exists(os.path.join(ROOT, n))]

a = Analysis(  # noqa: F821
    [os.path.join(ROOT, "app.py")],
    pathex=[ROOT],
    datas=datas,
    hiddenimports=["numpy", "cv2"] if FULL else [],
    excludes=["tkinter", "_tkinter"] + ([] if FULL else ["numpy", "cv2"]),
    module_collection_mode={"numpy": "py", "cv2": "py"} if FULL else {},
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Orbital",
    console=False,
    icon=os.path.join(SPECPATH, "orbital.ico"),  # noqa: F821
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="Orbital", upx=False)  # noqa: F821
