# Vendored fonts

docs/specs/m5b-redesign.md RD2 and SR5. The web app self-hosts two open font families. The files are the unmodified release
files from Adobe's GitHub releases (the SIL Open Font License reserves the name "Source" for unmodified fonts, so they are not
subset or renamed). The licence texts are served with the app at `/v2/licenses/` (copied from `web/public/licenses/`).
`tests/test_web_licenses.py` pins every checksum below; `.gitattributes` keeps git from rewriting the files.

| File | Family, version | Licence | sha256 | Source |
|---|---|---|---|---|
| SourceSans3-Regular.ttf.woff2 | Source Sans 3, 3.052R | OFL-1.1 | 53492fb3a0def77354f166a55d09b63a10855e91c206c7620a81cf56e97f8ec3 | https://github.com/adobe-fonts/source-sans/releases/download/3.052R/WOFF2-source-sans-3.052R.zip (`WOFF2/TTF/`) |
| SourceSans3-Semibold.ttf.woff2 | Source Sans 3, 3.052R | OFL-1.1 | 47b9b661b9f395fe7f0d0e119637fba5c8dad97bde3df60066fd24229c0792f4 | https://github.com/adobe-fonts/source-sans/releases/download/3.052R/WOFF2-source-sans-3.052R.zip (`WOFF2/TTF/`) |
| SourceSans3-Bold.ttf.woff2 | Source Sans 3, 3.052R | OFL-1.1 | 8d35c6d40e750a4ee23bbbba2bd604ad098d172907369b43281507e16b8e2e7a | https://github.com/adobe-fonts/source-sans/releases/download/3.052R/WOFF2-source-sans-3.052R.zip (`WOFF2/TTF/`) |
| SourceSerif4-Regular.ttf.woff2 | Source Serif 4, 4.005R | OFL-1.1 | 6b053e98f0838afe81f3e784727be4583a7c13bb42f198dc5202ecffee0aaee0 | https://github.com/adobe-fonts/source-serif/releases/download/4.005R/source-serif-4.005_WOFF2.zip (`source-serif-4.005_WOFF2/TTF/`) |
| OFL-SourceSans3.txt (in `web/public/licenses/`) | licence text | - | 89ad2c4f66dd29127527493e729c31e731f111cf10faf5774c3db9275ed0c22c | https://raw.githubusercontent.com/adobe-fonts/source-sans/3.052R/LICENSE.md |
| OFL-SourceSerif4.txt (in `web/public/licenses/`) | licence text | - | 75784a295293a8992f5a8d99210566e0064a012e6dab6731305e3787f15896c7 | https://raw.githubusercontent.com/adobe-fonts/source-serif/4.005R/LICENSE.md |

Release archives as downloaded on 2026-11-10: `WOFF2-source-sans-3.052R.zip` sha256
d7f6724027fe5ca0bab44d6121284f1fa8f66dec5f2864aa969aed347041ac95; `source-serif-4.005_WOFF2.zip` sha256
af10e80dcd2296748b04cb9917db9f7ba0ae65101165fd2f0c16b9812d9abd28.

To update: download the new release archive, copy the same `TTF` woff2 files unchanged, copy the repository `LICENSE.md` at the
same tag, and update the checksums here and in the test.
