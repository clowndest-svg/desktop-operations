# Third-party runtime notices

These components remain separate third-party works. The application bundles unmodified dynamic runtime files in `jarvis/vendor/`; corresponding notices are collected here and copied to `_internal/licenses/` in the desktop package.

| Component measured in this checkout | License text | Upstream source |
|---|---|---|
| llama.cpp build 11425, commit e117148a4 | llama.cpp-MIT.txt | https://github.com/ggml-org/llama.cpp/tree/e117148a4 |
| LLVM OpenMP (llama.cpp distribution) | LLVM-OpenMP.txt | https://github.com/llvm/llvm-project/tree/main/openmp |
| scrcpy 5.0.1 | scrcpy-Apache-2.0.txt | https://github.com/Genymobile/scrcpy/tree/v5.0.1 |
| SDL 3.4.18 | SDL-zlib.txt | https://github.com/libsdl-org/SDL/tree/release-3.4.18 |
| FFmpeg DLLs: avcodec/avformat 63.1.102, avutil 61.1.102, swresample 7 | LGPL-2.1.txt | https://github.com/Genymobile/scrcpy/blob/v5.0.1/doc/build.md ; https://github.com/FFmpeg/FFmpeg |
| libusb 1.0.30 | LGPL-2.1.txt | https://github.com/libusb/libusb/tree/v1.0.30 |
| adb / platform-tools | Android-platform-tools-NOTICE.txt | https://android.googlesource.com/platform/packages/modules/adb/ |

No proprietary Cubism runtime, GitHub sample Live2D model, third-party female portrait or Douyin video is included by the r124 redesign. The supplied logo and generated portrait provenance is documented in `assets-src/brand/README.md` in the source repository.

LGPL components are dynamically linked and remain replaceable; the application does not restrict replacement or reverse engineering required to debug modifications to these libraries. The displayed FFmpeg version is a development ABI version; it is not an invented stable release tag. Reproducing that exact DLL build additionally requires the upstream distributor's build recipe and revision. These notices and source links are an inventory, not a claim that the exact corresponding source archive has been independently rebuilt here.
