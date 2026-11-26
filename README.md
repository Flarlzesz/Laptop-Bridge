# Robot laptop: setup and running the bridge

The laptop on the robot runs one Python script, `bridge.py`. It reads the Arduino over USB and
uploads every record to Supabase. The website reads from Supabase, so the laptop never needs the website.
The laptop can run **macOS or Windows**.

```
Arduino ──USB──▶ laptop: bridge.py ──internet──▶ Supabase (wlgencggupxqtqmflydr.supabase.co) ◀── website
```

macOS commands go in **Terminal**. Windows commands go in **PowerShell** (Start → type "PowerShell").
Where they differ, both are shown.

## 1. One-time setup

**1. Install Python 3.**
- Check it: macOS `python3 --version`, Windows `py --version`.
- If it's missing, download it from python.org.
- On Windows, tick **"Add python.exe to PATH"** in the installer.

**2. Get the files** from https://github.com/Flarlzesz/Laptop-Bridge. Either clone it, or use the green
**Code → Download ZIP** button and unzip it to a folder named `robohack-bridge` in your home folder.

```bash
git clone https://github.com/Flarlzesz/Laptop-Bridge.git ~/robohack-bridge
```

**3. Install the serial library.** It's needed for the real robot only, not for replays.

| macOS | Windows |
|---|---|
| `python3 -m pip install pyserial` | `py -m pip install pyserial` |

On macOS, if you get "externally-managed-environment", add `--user`.

**4. Create the settings file.** It holds the secret key, so it lives in your home folder and never goes in git.

| macOS | Windows (PowerShell) |
|---|---|
| `cp ~/robohack-bridge/env.example ~/.robohack-bridge.env`<br>`chmod 600 ~/.robohack-bridge.env`<br>`open -e ~/.robohack-bridge.env` | `copy $HOME\robohack-bridge\env.example $HOME\.robohack-bridge.env`<br>`notepad $HOME\.robohack-bridge.env` |

Fill in these settings and save:

| Setting | Value |
|---|---|
| `SUPABASE_URL` | `https://wlgencggupxqtqmflydr.supabase.co` (already filled in) |
| `SUPABASE_SECRET_KEY` | Supabase → Project Settings → API Keys → **Secret key** (`sb_secret_…`) |
| `SERIAL_PORT` | The Arduino port, see step 5 |

The script reads this file every time it starts. Without it, only `--dry-run` works.
The secret key can write and delete everything: keep it only in this file, never in git, chat or the website.

**5. Find the Arduino port.** Plug in the Arduino first.
- **macOS:** run `ls /dev/cu.*`. Use the `usbmodem…` or `usbserial…` entry, e.g. `/dev/cu.usbmodem1101`.
- **Windows:** open Device Manager → **Ports (COM & LPT)**. Look for "Arduino Mega" or "USB Serial Device (COM3)" and use `COM3` (your number may differ).

## 2. Test before the robot drives

Go to the folder first: `cd ~/robohack-bridge` on macOS, `cd $HOME\robohack-bridge` on Windows.
On Windows, replace `python3` with `py` in the commands below.

```bash
python3 bridge.py --replay sample_records.jsonl --dry-run --speed 0   # no upload, checks the script
python3 bridge.py --replay sample_records.jsonl --speed 3             # real upload of a recording
```

Put the recording, e.g. `sample_records.jsonl`, in the same folder, or give its full path.
With the second command running, the website's Live Map should show the trail within a second or two.

## 3. Running on the robot

| macOS | Windows |
|---|---|
| `caffeinate -i python3 bridge.py --source field --name "Old Town run 1"` | `py bridge.py --source field --name "Old Town run 1"` |

- The laptop must not sleep, because sleep pauses the upload.
  - **macOS:** `caffeinate -i` takes care of that.
  - **Windows:** before the run, open Settings → System → Power and set **Sleep** to **Never** while plugged in and on battery.
- The port comes from `SERIAL_PORT`. To override it, pass `--port /dev/cu.usbmodem…` (macOS) or `--port COM3` (Windows).
- `--source field` marks a real outdoor run. The default is `bench`.
- `--name` labels the session on the website. It is optional.
- To stop, press **Ctrl+C**. The bridge uploads what is left, marks the session as ended, then exits.

While it runs:

- It prints a status line every 5 seconds.
- If the cable comes loose, it retries the port every 2 seconds.
- If the internet drops, it keeps the records (up to about 2.8 hours) and uploads them once the connection is back.

## 4. Where the data goes

Everything goes to the Supabase project at `https://wlgencggupxqtqmflydr.supabase.co`:

| Table | What the bridge writes |
|---|---|
| `sessions` | A new row each time the Arduino starts and sends its header line, with name, source, start and end time. |
| `readings` | One row per record (5 per second): the raw sensor values plus arrival time, reflectance, predicted surface and fault flags. |

Each run of the Arduino is one session. The website always shows the newest session.
At 5 Hz, one hour of driving makes about 18,000 rows, roughly 5–10 MB.

## 5. Problems

| Message | Fix |
|---|---|
| `SUPABASE_URL and SUPABASE_SECRET_KEY are missing` | Create or fill in the settings file (step 4). On Windows, check that Notepad didn't save it as `.robohack-bridge.env.txt`. |
| `401` / `Invalid API key` | Wrong key. Use the **secret** key, not the publishable one. |
| `No serial port` / `serial port problem` | Check the cable and the port name (step 5). Close the Arduino IDE's Serial Monitor, because only one program can use the port. |
| `No module named serial` / `pyserial is missing` | Run step 3. |
| `CERTIFICATE_VERIFY_FAILED` (macOS) | Run `/Applications/Python 3.x/Install Certificates.command`. |
| `python3` / `py` not found | Install Python (step 1). On Windows, reinstall with "Add python.exe to PATH" ticked. |
| Website shows "No data N s" | The bridge stopped, the laptop slept, or the robot sends nothing. Look at the terminal. |

All options: `python3 bridge.py --help` (Windows: `py bridge.py --help`).
