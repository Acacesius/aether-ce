# Running AETHER CE on Windows 10/11

> **Untested on real Windows.** The author could not run Windows. This guide and the Windows code paths were checked by
> code review, mocks and automated tests on Linux only. Steps may fail in ways nobody has seen yet. If something breaks,
> see [What to send back](#what-to-send-back); that is exactly the information that is needed.

CE runs as a normal Python program on your own computer. No Docker, no virtual environment, no `pip install`.
It listens on `127.0.0.1` (your computer only; other machines cannot reach it).

## 1. Install Python 3.11 or newer
1. Download Python from <https://www.python.org/downloads/>.
2. Start the installer and **tick "Add python.exe to PATH"** on the first screen, then choose Install Now.
3. Check it: open **Start**, type `cmd`, press Enter, and run `python --version`. It must say 3.11 or higher.
   (`py -3 --version` also works.)

## 2. Get CE
Either:
- **Zip:** on the repository page choose **Code > Download ZIP**, then right-click the zip and **Extract All...**
  Pick a simple folder such as `C:\aether-ce`. Do not run it from inside the zip file.
- **Git:** `git clone <repository URL> aether-ce`.

## 3. Start it
Double-click **`start.bat`** in the CE folder.
- It finds Python (`py -3`, `python` or `python3`, must be 3.11+), starts the server and opens your browser at
  **http://127.0.0.1:8080/**. Leave the black window open while you use CE; close it (or press Ctrl+C) to stop.
- *Windows protected your PC (SmartScreen)*: click **More info > Run anyway**. The file is a short, readable text script.
  If the zip was downloaded in a browser you can also right-click the zip > Properties > tick **Unblock** before extracting.
- *Alternative:* right-click `start.ps1` > Run with PowerShell. If PowerShell says scripts are disabled, run
  `powershell -ExecutionPolicy Bypass -File .\start.ps1` from the CE folder, or use `start.bat`, which needs no PowerShell setup.
- Another program already uses port 8080? Open `cmd` in the CE folder and run `start.bat --port 8090`.
- No Python found: the window says so and waits. Redo step 1.

The window prints three lines: the URL, `Owner token stored in: <path>`, and `Data dir: <path>`. The token itself is
never printed.

## 4. Sign in with the owner token
The page asks for an **Owner token**. It is stored in a file, by default
`%LOCALAPPDATA%\AetherCE\owner.token` (the exact path is in the start window). To read it:
1. Open that file in **Notepad** (File > Open, paste the path).
2. Copy the single line of text and paste it into the page's token box.

Never share this token or the file: whoever has it controls CE and the folders you give it. The page keeps it in
that browser tab only.

## 5. Add your CLI accounts as slots
CE does not hold passwords or API keys. It starts the **Claude Code** or **Codex** command line tools that you have already
signed in to, so install and log in to them first (outside CE):
- Claude Code: run `claude` in a normal terminal and complete the login.
- Codex: run `codex login`.

Then open the **Slots** tab and press **Add slot**:
- **Short id**: e.g. `claude-a` (lowercase letters, digits, `-`, `_`).
- **Kind**: Claude Code or Codex. **Label**: any name you like.
- **Config folder (absolute path)**: the folder where that CLI keeps its login. For your normal login this is the CLI's
  default folder (commonly `C:\Users\<you>\.claude` for Claude Code and `C:\Users\<you>\.codex` for Codex; if
  unsure, check the CLI's own documentation). It must not be inside CE's data folder.
- **Model** (optional; empty = the CLI default) and **Order** (lower numbers are tried first).

The card shows a warning if the `claude` / `codex` command is not found on this computer.

### Several accounts
Each account needs its own config folder, with one login per folder. Studio passes the folder to the CLI as
`CLAUDE_CONFIG_DIR` (Claude Code) or `CODEX_HOME` (Codex). Example, in `cmd`:

```
mkdir C:\Users\you\ai-accounts\claude-b
set CLAUDE_CONFIG_DIR=C:\Users\you\ai-accounts\claude-b
claude
```
Log in to the second account in that window, then add a slot whose config folder is `C:\Users\you\ai-accounts\claude-b`.
For Codex it is the same with `set CODEX_HOME=C:\Users\you\ai-accounts\codex-b` followed by `codex login`.
The folders can live anywhere except inside the CE data folder, and two slots of the same kind cannot share a folder.
Slots are saved in `slots.json` in the data folder; it contains only these settings, never credentials.

## 6. Add a first run and watch it
1. Open the **Workspace** tab and press **Add run**.
2. Enter the **folder** the CLI should work in (e.g. `C:\Users\you\project`) and the prompt. Use a dedicated folder,
   ideally a git checkout you can reset: the CLI's edits there are real.
3. Pick a slot (or automatic) and press **Add to queue**. It starts as soon as a slot is free.
4. Click the item to open its run page: the **Log** updates live, with Stages, Attempts and Evidence below.
   The **Tracker** tab lists all items with their status, slot and, when the CLI reports them, tokens and cost.

By design the Claude CLI is started without a shell or web tools, and Codex with its `workspace-write` sandbox
([details](studio/CLI_EXECUTOR.md)).

## 7. What happens when a usage limit is hit
If a CLI exits unsuccessfully and its own error output says a usage limit was reached (for example "usage limit
reached", "hit your limit", "5-hour limit reached"), Studio:
1. reads the reset time from that message when it can (an epoch, a timestamp, "in 2 hours", or a clock time like
   "resets 3pm", rolled to tomorrow if already past). If it cannot read one, it blocks the slot for **5 hours**.
   (Reset times beyond 31 days are ignored and also become 5 hours.)
2. marks that slot **blocked until** that time (shown on its card with a countdown) and does not launch on it meanwhile;
3. puts the queue item back at the front and tries it on the **next enabled, unblocked slot** in Order, recording a
   provider switch. A run that you pinned to one specific slot is not moved to another slot.
4. if no slot is available, waits for the earliest block to end.

Only text produced by the CLI itself counts, so a model merely discussing limits does not block a slot. **Clear limit**
on a card removes a block early. Blocks are saved, so they survive a restart. Detection depends on the CLIs' wording,
which can change; a limit that is not recognised appears as an ordinary failure.

## 8. Where data lives
Default data folder: `%LOCALAPPDATA%\AetherCE` (e.g. `C:\Users\you\AppData\Local\AetherCE`). Change it with
`--data-dir`. It holds the database, `owner.token`, `slots.json`, queue prompts and `cli-logs`. Your CLI logins live in
their own config folders, not here. Start options: `--port N`, `--data-dir PATH`, `--open` (already used by the scripts).

## 9. Update
Stop CE (close the window). Download the new zip (or `git pull`) and copy its files over the CE folder, replacing
the old ones. The data folder is separate and is kept, so slots, history and token remain. Start `start.bat` again.

## 10. Uninstall
Stop CE, then delete the CE folder and the data folder (`%LOCALAPPDATA%\AetherCE`). That removes everything CE
stored. Your CLI config folders are separate; delete them only if you also want to remove those logins. Python can be
removed in Settings > Apps.

## Troubleshooting
Run, in `cmd` in the CE folder:

```
python -m aether.studio doctor
```
(`py -3 -m aether.studio doctor` if `python` is not found; add `--port N` or `--data-dir PATH` if you use them.)
It prints sections of lines tagged `[ OK ]`, `[WARN]` or `[FAIL]` and ends with `Summary: N FAIL, N WARN`.
Exit code 1 means at least one FAIL.

| Section | Meaning |
|---|---|
| Platform | OS and Python version. FAIL if Python is older than 3.11. |
| Data dir | the folder exists/can be created and written. FAIL: pick another with `--data-dir`. |
| Token permission | CE tests, on a throw-away file, that it can make a file readable by only you (via `icacls` on Windows). FAIL means CE refuses to start in that folder. A WARN on the real token file means it will try again at start. |
| Port | whether `127.0.0.1:8080` is free. WARN: use `--port`. |
| Slots | each slot's CLI found on PATH, its `--version`, and whether the config folder exists. WARN for a missing folder usually means you have not logged in with that folder yet. FAIL: no usable CLI at all. |
| Dry spawn | starts and kills a harmless test process, the same way CE controls CLIs. FAIL means CE cannot manage processes here. |
| Landlock | adapter sandbox. "not applicable" on Windows is normal and expected. |

Other common problems:
- Window closes immediately: start it from `cmd` (`cd` to the folder, `start.bat`) to read the message.
- "Refusing to start: could not restrict the owner token file": the data folder is on a drive/folder where permissions
  cannot be set (some network or FAT/exFAT drives). Use `--data-dir` on a normal local NTFS folder.
- Slot says the command was not found: install the CLI and reopen `cmd`/restart CE so PATH is updated.
- A run fails right away with a login message: log in with the CLI using that slot's config folder (step 5).
- Browser shows "token not accepted": copy the whole line from `owner.token`, with no spaces.

## What to send back
Please send the output of `python -m aether.studio doctor` (your user name is already replaced by `<user>`), the
Windows version, how you got CE (zip/git), which step failed, and the exact message. A filled copy of
`feedback/TESTER_FEEDBACK.md` is welcome ([docs/FEEDBACK.md](FEEDBACK.md)).
**Never send** `owner.token`, `slots.json` or anything from the CLI config folders (they contain your logins), or screenshots showing them.
