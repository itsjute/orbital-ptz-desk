# Third-party notices

## Lucide icons

`index.html` embeds SVG paths of the following [Lucide](https://lucide.dev) icons (lucide-static 0.460.0):
activity, aperture, camera, cctv, check, circle, copy, film, flower-2, focus, folder-open, grid-3x3,
house, keyboard, lock, maximize, mic, minus, mountain, pencil, plus, refresh-cw, rotate-cw, route,
ruler, save, scan-eye, settings, sliders-horizontal, spline, square, sun, terminal, video,
volume-2, volume-x, x, zoom-in.
`lucide-icons.json` contains the complete Lucide icon set (version 0.460.0, SVG contents and
search tags) for the preset icons; it is generated from the lucide repository. 

```
ISC License

Copyright (c) for portions of Lucide are held by Cole Bemis 2013-2022 as part of Feather (MIT).
All other copyright (c) for Lucide are held by Lucide Contributors 2022.

Permission to use, copy, modify, and/or distribute this software for any purpose with or without
fee is hereby granted, provided that the above copyright notice and this permission notice appear
in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH REGARD TO THIS
SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE
AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT,
NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE
OF THIS SOFTWARE.
```

## Bundled with the Windows installer and the portable ZIP

These are not part of this repository; the release workflow downloads or packages them, and each
keeps its own licence:

- [go2rtc](https://github.com/AlexxIT/go2rtc) by Alexey Khit – MIT licence. Video service (RTSP → WebRTC).
- [FFmpeg](https://ffmpeg.org) – optional component, GPL build from
  [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds). Its licence text is installed
  under `licenses/`; the source code is available from ffmpeg.org and the build repository.
- [NumPy](https://numpy.org) (BSD 3-Clause) and [OpenCV](https://opencv.org) (Apache 2.0) – optional component.
- [Python](https://www.python.org) runtime (PSF licence), packaged with
  [PyInstaller](https://pyinstaller.org) (GPL 2 with an exception that allows distributing the
  packaged program under its own licence).
- [pywebview](https://pywebview.flowrl.com) (BSD 3-Clause) and [pythonnet](https://github.com/pythonnet/pythonnet) (MIT)
  for the app window, which uses Microsoft Edge WebView2 from Windows.

## Running from source

- [go2rtc](https://github.com/AlexxIT/go2rtc) (MIT) – downloaded separately.
- [FFmpeg](https://ffmpeg.org) (LGPL/GPL) – optional, installed separately by the user.
- [NumPy](https://numpy.org) (BSD) and [OpenCV](https://opencv.org) (Apache 2.0) – optional, installed with pip.
- [pywebview](https://pywebview.flowrl.com) (BSD 3-Clause) – optional, for `app.py`.
- Fonts [Barlow](https://fonts.google.com/specimen/Barlow) and [JetBrains Mono](https://www.jetbrains.com/lp/mono/) (SIL Open Font License) – loaded from Google Fonts.

## Logo

The lettering of the Orbital logo (`docs/logo-*.svg`, the page header) is set in
[Poppins](https://fonts.google.com/specimen/Poppins) Medium by Indian Type Foundry and converted to
outlines; Poppins is licensed under the SIL Open Font License 1.1. The planet symbol was traced
with a Python port of [potrace](https://potrace.sourceforge.net) – only the output is included.
