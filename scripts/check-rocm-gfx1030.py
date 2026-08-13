"""Diagnostic: does AMD ship any maths kernels for an RX 6900 XT on Windows?

This is retained from the GPU research as a DIAGNOSTIC, not as a gate. dictate
does not use ROCm and does not need this to run. It is here because "why not
faster-whisper / ROCm, which everyone else uses?" is a reasonable question, and
this answers it from AMD's own package in about thirty seconds without
downloading a gigabyte.

It reads only the zip INDEX of AMD's ROCm-for-Windows library wheel over HTTP
range requests, and counts the per-GPU-architecture kernel files inside.

Expected result, as measured on 2026-08-13:

    gfx1100  191 files      <- RDNA3
    gfx1101  207 files
    ...
    FILES FOR YOUR CARD (gfx1030): 0

Zero means AMD ships no matrix-multiply kernels for RDNA2 on Windows, so
CTranslate2/faster-whisper on ROCm cannot work on this card no matter how it is
configured - Whisper is almost entirely matrix multiplies. That is why dictate
uses whisper.cpp's Vulkan backend, which comes with the ordinary Adrenalin
driver and does not care about AMD's ROCm support matrix.

A NON-ZERO number would be news: it would mean AMD has re-added RDNA2 since, and
the ROCm route is worth re-examining. Say so if you see it.

Usage:
    python scripts/check-rocm-gfx1030.py
"""

from __future__ import annotations

import collections
import io
import re
import sys
import urllib.request
import zipfile

URL = ("https://repo.radeon.com/rocm/windows/rocm-rel-7.2/"
       "rocm_sdk_libraries_custom-7.2.0.dev0-py3-none-win_amd64.whl")
MY_CARD = "gfx1030"          # RX 6900 XT, Navi 21, RDNA2


class HttpRangeFile(io.RawIOBase):
    """A seekable file-like object backed by HTTP range requests, so zipfile can
    read the central directory without fetching the whole 492 MB archive."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.pos = 0
        head = urllib.request.Request(url, method="HEAD")
        self.size = int(urllib.request.urlopen(head, timeout=30)
                        .headers["Content-Length"])

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = 0) -> int:
        if whence == 0:
            self.pos = offset
        elif whence == 1:
            self.pos += offset
        else:
            self.pos = self.size + offset
        return self.pos

    def readinto(self, buffer) -> int:
        data = self.read(len(buffer))
        buffer[:len(data)] = data
        return len(data)

    def read(self, amount: int = -1) -> bytes:
        if amount is None or amount < 0:
            amount = self.size - self.pos
        if amount == 0 or self.pos >= self.size:
            return b""
        end = min(self.pos + amount, self.size) - 1
        request = urllib.request.Request(
            self.url, headers={"Range": f"bytes={self.pos}-{end}"})
        data = urllib.request.urlopen(request, timeout=60).read()
        self.pos += len(data)
        return data


def main() -> int:
    print("Reading the index of AMD's ROCm 7.2 library package for Windows.")
    print("(Only the zip index is fetched, not the 492 MB of contents.)\n")
    try:
        archive = zipfile.ZipFile(io.BufferedReader(HttpRangeFile(URL), 262144))
    except Exception as exc:
        print(f"Could not read AMD's package: {exc}")
        print("\nThis check needs internet access to repo.radeon.com. It is only a")
        print("diagnostic - dictate does not use ROCm and runs fine without it.")
        return 2

    counts: collections.Counter[str] = collections.Counter()
    for info in archive.infolist():
        for arch in re.findall(r"gfx[0-9a-z]+", info.filename):
            counts[arch] += 1

    print("GPU architectures AMD ships maths kernels for, in ROCm 7.2 for Windows:")
    for arch, count in sorted(counts.items()):
        print(f"   {arch:10s} {count} files")
    if not counts:
        print("   (none found - the package layout may have changed)")

    mine = counts.get(MY_CARD, 0)
    print()
    print(f"FILES FOR YOUR CARD ({MY_CARD}): {mine}")
    print()
    if mine == 0:
        print("As expected. AMD ships no matrix-multiply kernels for RDNA2 on")
        print("Windows, so faster-whisper on ROCm cannot work on this card.")
        print("dictate uses whisper.cpp's Vulkan backend instead, which comes with")
        print("the normal Adrenalin driver. Nothing to do.")
    else:
        print("*** THIS IS NEWS. AMD has re-added RDNA2 since this was last")
        print("*** checked (2026-08-13). The ROCm route may now be viable.")
        print("*** Worth reporting - it does not break anything, but it opens an")
        print("*** option that was closed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
