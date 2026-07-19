# 1.5.0

- Renamed the application to **Liux Defragger**.
- Added `/usr/bin/liux-defragger` and a new application-menu entry.
- Administrator authentication is requested at GUI launch rather than on the first privileged operation.
- The authenticated helper is retained and reused until the GUI closes.
- Kept `fat32defrag-gui` as a compatibility launcher for upgrades and existing shortcuts.
- Filesystem engines and on-disk formats are unchanged from 1.4.0.
