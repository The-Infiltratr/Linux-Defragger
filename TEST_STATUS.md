# Linux Defragger 1.7.0 test status

- Engine compiled and reports 1.7.0.
- All Python modules compile successfully.
- Packaged backend registry loads HFS, HFS+/HFSX and APFS.
- Synthetic HFS bitmap test: exact allocated/free totals passed.
- Synthetic HFS+ bitmap test: exact allocated/free totals passed.
- Synthetic APFS container test: geometry and conservative unknown map passed.
- Exact built Debian package was unpacked and tested, not only the source tree.

Apple write support is not enabled or claimed in this release.
