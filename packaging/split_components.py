"""
Sort a PyInstaller build into installer components.

    python split_components.py <full build> <base build> <stage folder>

<full build> was built with numpy and OpenCV, <base build> without. Every file of the full build
that the base build has too goes to <stage>/main, the rest (numpy, OpenCV and what only they
need) to <stage>/stabilize. All files are taken from the full build, so the program in "main"
already knows everything the optional part needs.
"""
import os
import shutil
import sys


def files(root):
    out = set()
    for d, _dirs, names in os.walk(root):
        for n in names:
            out.add(os.path.relpath(os.path.join(d, n), root))
    return out


def main(full, base, stage):
    f, b = files(full), files(base)
    if not f or not b:
        sys.exit("empty build folder")
    only_base = sorted(b - f)
    if only_base:
        print(f"warning: {len(only_base)} files only in the base build, e.g. {only_base[:3]}")
    shutil.rmtree(stage, ignore_errors=True)
    sizes = {"main": 0, "stabilize": 0}
    for rel in sorted(f):
        part = "main" if rel in b else "stabilize"
        src, dst = os.path.join(full, rel), os.path.join(stage, part, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        sizes[part] += os.path.getsize(src)
    n_opt = len(f - b)
    print(f"main: {len(f) - n_opt} files, {sizes['main'] / 1e6:.1f} MB")
    print(f"stabilize: {n_opt} files, {sizes['stabilize'] / 1e6:.1f} MB")
    tops = sorted({r.split(os.sep)[1] if r.startswith("_internal" + os.sep) else r.split(os.sep)[0] for r in f - b})
    print("stabilize top-level entries:", ", ".join(tops[:40]))
    if not any(t.startswith("numpy") for t in tops) or not any(t.startswith("cv2") for t in tops):
        sys.exit("numpy/cv2 not found among the optional files – check the build")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    main(*sys.argv[1:])
