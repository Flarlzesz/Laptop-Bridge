# Robot laptop: setup and running the bridge

The laptop on the robot runs one Python script, `bridge.py`. It reads the Arduino over USB and
uploads every record to Supabase. The website reads from Supabase, so the laptop never needs the website.

```
Arduino ──USB──▶ laptop: bridge.py ──internet──▶ Supabase (wlgencggupxqtqmflydr.supabase.co) ◀── website
```

## 1. One-time setup

**1. Python 3.** Check with `python3 --version`. If it's missing, install it from python.org (macOS).

**2. Copy the script.** Copy the `bridge/` folder from this project to the laptop, e.g. to `~/robohack-bridge/`.
It needs only `bridge.py` and `requirements.txt`. Do not copy `config/` or anything else.

**3. Install the serial library** (needed for the real robot only, not for replays):

```bash
python3 -m pip install pyserial
```

If you get "externally-managed-environment", use `python3 -m pip install --user pyserial`.

**4. Create the settings file** in your home folder (not inside a website folder):

```bash
cp ~/robohack-bridge/env.example ~/.robohack-bridge.env && chmod 600 ~/.robohack-bridge.env
```

Open `~/.robohack-bridge.env` and fill in:

| Setting | Value |
|---|---|
| `SUPABASE_URL` | `https://wlgencggupxqtqmflydr.supabase.co` (already filled in) |
| `SUPABASE_SECRET_KEY` | Supabase → Project Settings → API Keys → **Secret key** (`sb_secret_…`) |
| `SERIAL_PORT` | The Arduino port, see step 5 |

The secret key can write and delete everything. Keep it only in this file, never in git, chat or the website.

**5. Find the Arduino port.** Plug in the Arduino and run `ls /dev/cu.*`. Use the `usbmodem…` or
`usbserial…` entry, e.g. `/dev/cu.usbmodem1101`. On Linux, it is usually `/dev/ttyACM0`.

## 2. Test before the robot drives

```bash
cd ~/robohack-bridge
python3 bridge.py --replay ~/Documents/sample_records.jsonl --dry-run --speed 0   # no upload, checks the script
python3 bridge.py --replay ~/Documents/sample_records.jsonl --speed 3             # real upload of a recording
```

With the second command running, the website's Live Map should show the trail within a second or two.

## 3. Running on the robot

```bash
cd ~/robohack-bridge
caffeinate -i python3 bridge.py --source field --name "Old Town run 1"
```

- `caffeinate -i` stops the Mac from sleeping, because sleep pauses the upload.
- The port comes from `SERIAL_PORT`. Pass `--port /dev/cu.usbmodem…` to override it.
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
| `SUPABASE_URL and SUPABASE_SECRET_KEY are missing` | Fill in `~/.robohack-bridge.env` (step 4). |
| `401` / `Invalid API key` | Wrong key. Use the **secret** key, not the publishable one. |
| `No serial port` / `serial port problem` | Check the cable and run `ls /dev/cu.*` again (step 5). |
| `No module named serial` | Run step 3. |
| `CERTIFICATE_VERIFY_FAILED` | Run `/Applications/Python 3.x/Install Certificates.command`. |
| Website shows "No data N s" | The bridge stopped, the laptop slept, or the robot sends nothing. Look at the terminal. |

All options: `python3 bridge.py --help`. More detail on the data and the website: `docs/SUPABASE.md`.
