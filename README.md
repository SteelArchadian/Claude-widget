# Command Center
Small notification center for your Claude Code chats (Windows). Never takes focus.

- Popups (no-activate, topmost, fading) with a soft chime when a chat has something you must act on. Click opens that chat in the Claude desktop app via `claude://code/continue?session=local_<id>`.
- Panel (tray icon click): notifications waiting on you, optional tracker lines read from a shared tools folder (inbox list, bench queue, activity log), and sliders for transparency / size / time / volume plus a "Move popups" drag mode.
- Chats push with `python notify.py "<chat>" "<one line>" [--session local_id] [--kind question|ready|blocked|broken]`. It writes a file into `<mcwDir>/notify/inbox`; the widget lists that folder every 2 s.
- Control: `pythonw command_center.pyw --cmd=show|test|quit`. `--tray` starts hidden.
- Settings: `%LOCALAPPDATA%\Command Center\config.json`. `mcwDir` is the shared tools folder (default: the app's data folder). Set `updateRepo` to "owner/name" to enable the update check; it only offers the release page and never installs silently.

Python 3.10 + tkinter + pystray + Pillow. GPL-3.0-only. Made with AI assistance.
