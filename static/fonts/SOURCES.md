# Self-hosted typography

Downloaded from the primary projects on 2026-09-05. These fonts are served by the
local workbench; the page makes no Google Fonts/CDN requests and installs no
system fonts. Both original SIL Open Font License 1.1 files are bundled here.

## Display: ZCOOL KuaiLe / 站酷快乐体

- Local file: `zcool-kuaile-regular.woff2` (**871,928 bytes**); CSS alias: `XHS Comic`.
- Version: **2.001**, regular weight; 7,053 Unicode mappings / 7,055 glyphs.
- Primary distribution: [Google Fonts, ofl/zcoolkuaile](https://github.com/google/fonts/tree/6d49995f5d8f1c7d476760a580ffb89ce1e52c5d/ofl/zcoolkuaile).
- Original file: [ZCOOLKuaiLe-Regular.ttf](https://raw.githubusercontent.com/google/fonts/6d49995f5d8f1c7d476760a580ffb89ce1e52c5d/ofl/zcoolkuaile/ZCOOLKuaiLe-Regular.ttf).
- Upstream project: [googlefonts/zcool-kuaile](https://github.com/googlefonts/zcool-kuaile).
- Original license: [ZCOOL-KuaiLe-OFL.txt](./ZCOOL-KuaiLe-OFL.txt).
- Copyright 2018 The ZCOOL KuaiLe Project Authors.

## Body: LXGW WenKai / 霞鹜文楷

- Local file: `lxgw-wenkai-regular.woff2` (**8,016,756 bytes**); CSS alias: `XHS Hand`.
- Version: **1.522**, regular weight, released **2026-03-17**.
- Preserves the complete font: 46,490 Unicode mappings / 46,867 glyphs, including
  uncommon Chinese characters for dynamically collected titles and text.
- Primary project and release: [lxgw/LxgwWenKai v1.522](https://github.com/lxgw/LxgwWenKai/releases/tag/v1.522).
- Original file: [LXGWWenKai-Regular.ttf](https://github.com/lxgw/LxgwWenKai/releases/download/v1.522/LXGWWenKai-Regular.ttf).
- Original license: [LXGW-WenKai-OFL.txt](./LXGW-WenKai-OFL.txt).
- Copyright 2021–2026 LXGW; Copyright 2020 The Klee Project Authors.

## Web conversion

Both original TTF files were converted to WOFF2 using FontTools **4.64.0** and
Brotli **1.2.0**, without subsetting or modifying glyph outlines. Font metadata,
character coverage and copyright records are retained. The CSS aliases are local
application names; the original font names remain in the files. Conversion command:

```powershell
python -m fontTools.ttLib.woff2 compress ORIGINAL.ttf -o OUTPUT.woff2
```

The project only needs the resulting WOFF2 files at runtime. FontTools and Brotli
are build-time utilities, not application requirements. Native KaiTi / Microsoft
YaHei fonts remain fallbacks for unavailable fonts and unsupported characters.

Combined font payload: **8,888,684 bytes (8.48 MiB)**. Both converted files were
reopened with FontTools to verify their original character/glyph counts, version
records, and embedded OFL metadata.
