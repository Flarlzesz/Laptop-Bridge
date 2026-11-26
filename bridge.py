#!/usr/bin/env python3
"""
RoboHack bridge: robot serial records -> Supabase.

Reads the sensor suite's JSON lines (spec: "Sidewalk Hazard Sensor Suite: Serial Record Format",
schema rh26 v1), checks and decodes them, and stores one row per record in Supabase.
The website (PHP) reads those rows and shows them live.

  Replay a recorded file at real speed (no robot needed):
      python3 bridge.py --replay ~/Documents/sample_records.jsonl
  Same, but don't send anything (prints what it would store):
      python3 bridge.py --replay ~/Documents/sample_records.jsonl --dry-run
  Live from the Arduino over USB (needs: pip3 install pyserial):
      python3 bridge.py --port /dev/cu.usbmodem1101

Settings come from environment variables or from ~/.robohack-bridge.env (see bridge/env.example):
  SUPABASE_URL, SUPABASE_SECRET_KEY, SERIAL_PORT
The env file lives OUTSIDE the website folder on purpose: Caddy would serve a file inside it.

Replay and --dry-run need only the Python standard library.
"""
import argparse
import datetime as dt
import json
import os
import queue
import signal
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request

SCHEMA, SCHEMA_VER = "rh26", 1
SURFACES = ("dry", "wet", "salted", "ice", "snow")
FIX_CODES = (0, 1, 2, 4, 5)

# Fields of a record (spec Table 2) -> (min, max) of the valid range. None = no range check.
FIELDS = {
    "seq": (0, 4294967295), "t_ms": (0, 4294967295), "fix": (0, 5),
    "lat_e7": (-900000000, 900000000), "lon_e7": (-1800000000, 1800000000),
    "z_mv": (-2048, 2047), "opt_a": (0, 2047), "opt_b": (0, 2047), "opt_d": (0, 2047),
    "temp_c100": (-5500, 12500),
    "ax_mg": (-4000, 4000), "ay_mg": (-4000, 4000), "az_mg": (-4000, 4000),
    "slip": (0, 1),
}
# Database column limits (smallint / integer), so one wild value can never make an insert fail
COLUMN_LIMITS = {"fix": 32767, "temp_c100": 32767, "ax_mg": 32767, "ay_mg": 32767, "az_mg": 32767,
                 "slip": 32767, "z_mv": 2**31 - 1, "opt_a": 2**31 - 1, "opt_b": 2**31 - 1, "opt_d": 2**31 - 1,
                 "lat_e7": 2**31 - 1, "lon_e7": 2**31 - 1}
TEMP_POWER_ON = 8500          # DS18B20 85 °C power-on value -> null (spec Table 2)

# ------------------------------------------------------------------ classifier (PROVISIONAL)
# Spec 3.1/3.2: impedance separates wet and salted; dry, ice and snow overlap (Table 3) and are
# told apart with the optical ratio R and temperature. These thresholds are guesses until the
# electrode and optical head are characterised on the bench. Tune them here.
CLASSIFIER = {
    "z_salted_min": 700,      # mV, Table 3 salted 800-1500
    "z_wet_min": 250,         # mV, Table 3 wet 300-600
    "z_dry_ice_snow_max": 150,  # mV, Table 3 dry/ice/snow 0-100
    "warm_c": 1.0,            # above this surface temperature ice and snow are unlikely
    "r_ice_max": 0.85,        # ice absorbs more at 940 nm -> lower R (assumed)
    "r_snow_min": 1.10,       # snow scatters strongly -> higher R (assumed)
}


def classify(z, r, temp_c, faults):
    """Surface guess from impedance (mV), reflectance ratio and temperature (°C). Never uses truth."""
    c = CLASSIFIER
    if z is None or "z_saturated" in faults:
        return "unknown"
    if z >= c["z_salted_min"]:
        return "salted"
    if z >= c["z_wet_min"]:
        return "wet"
    if z > c["z_dry_ice_snow_max"]:
        return "unknown"                       # between the assumed bands
    if temp_c is not None and temp_c > c["warm_c"]:
        return "dry"
    if r is None:
        return "unknown"                       # optical channel unusable
    if r <= c["r_ice_max"]:
        return "ice"
    if r >= c["r_snow_min"]:
        return "snow"
    return "dry"


# ------------------------------------------------------------------ settings
def load_env(path):
    """KEY=VALUE lines; real environment variables win."""
    values = {}
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    values[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("SUPABASE_URL", "SUPABASE_SECRET_KEY", "SERIAL_PORT"):
        if os.environ.get(k):
            values[k] = os.environ[k]
    return values


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# ------------------------------------------------------------------ Supabase (REST, standard library only)
class Supabase:
    def __init__(self, url, key):
        self.url = url.rstrip("/")
        self.key = key
        self.ctx = ssl.create_default_context()
        # python.org builds on macOS often ship without CA certificates; add the system ones.
        if os.path.isfile("/etc/ssl/cert.pem"):
            self.ctx.load_verify_locations("/etc/ssl/cert.pem")

    def request(self, method, path, body=None, prefer=None):
        headers = {"apikey": self.key, "Content-Type": "application/json", "Accept": "application/json"}
        if prefer:
            headers["Prefer"] = prefer
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + "/rest/v1/" + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10, context=self.ctx) as res:
                text = res.read().decode()
                return json.loads(text) if text else None
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Supabase {e.code}: {e.read().decode()[:300]}") from None

    def new_session(self, name, source, ver):
        rows = self.request("POST", "sessions", {"name": name, "source": source, "schema_ver": ver},
                            prefer="return=representation")
        return rows[0]["id"]

    def end_session(self, sid):
        self.request("PATCH", f"sessions?id=eq.{sid}", {"ended_at": now_iso()}, prefer="return=minimal")

    def upsert_readings(self, rows):
        # unique (session_id, seq): re-sending after a network error is harmless
        self.request("POST", "readings?on_conflict=session_id,seq", rows,
                     prefer="resolution=merge-duplicates,return=minimal")


class DryRun:
    """Stand-in for Supabase with --dry-run: prints instead of sending."""
    def __init__(self, verbose):
        self.verbose, self.next_id = verbose, 1

    def new_session(self, name, source, ver):
        sid, self.next_id = self.next_id, self.next_id + 1
        log(f"[dry-run] new session #{sid} '{name}' (schema v{ver})")
        return sid

    def end_session(self, sid):
        log(f"[dry-run] session #{sid} ended")

    def upsert_readings(self, rows):
        if self.verbose:
            for r in rows:
                print("   ", json.dumps(r, separators=(",", ":")))


# ------------------------------------------------------------------ sender thread
class Sender(threading.Thread):
    """Sends rows in small batches (every 0.4 s) and keeps them while the network is down."""
    MAX_BUFFER = 50000          # ~2.8 h at 5 Hz

    def __init__(self, db):
        super().__init__(daemon=True)
        self.db, self.q, self.sent, self.failing = db, queue.Queue(), 0, False
        self.pending = []                              # batch being sent (kept on failure)
        self.stop_event = threading.Event()

    def put(self, row):
        if self.q.qsize() >= self.MAX_BUFFER:
            self.q.get_nowait()                        # oldest is dropped rather than running out of memory
        self.q.put(row)

    def run(self):
        pending = self.pending
        while not (self.stop_event.is_set() and self.q.empty() and not pending):
            deadline = time.time() + 0.4
            while len(pending) < 200 and time.time() < deadline:
                try:
                    pending.append(self.q.get(timeout=max(0.0, deadline - time.time())))
                except queue.Empty:
                    break
            if not pending:
                continue
            try:
                self.db.upsert_readings(pending)
                self.sent += len(pending)
                if self.failing:
                    log(f"Supabase reachable again, {len(pending)} buffered rows sent")
                self.failing = False
                pending.clear()
            except Exception as e:                     # network or API error: keep rows, retry
                if not self.failing:
                    log(f"send failed, buffering and retrying: {e}")
                self.failing = True
                time.sleep(2)

    def finish(self, timeout=15):
        self.stop_event.set()
        self.join(timeout)


# ------------------------------------------------------------------ record handling
class Bridge:
    def __init__(self, db, sender, args):
        self.db, self.sender, self.args = db, sender, args
        self.session = None
        self.prev = None                   # previous stored record (seq, lat, lon)
        self.warned = set()
        self.stats = {"records": 0, "sessions": 0, "skipped": 0, "diagnostic": 0, "lost": 0}

    def warn_once(self, key, msg):
        if key not in self.warned:
            self.warned.add(key)
            log("warning: " + msg)

    def start_session(self, ver, reason):
        if self.session:
            self.db.end_session(self.session)
        name = self.args.name or ("Replay " + os.path.basename(self.args.replay) if self.args.replay else "Bench run")
        name += " · " + time.strftime("%Y-%m-%d %H:%M")
        self.session = self.db.new_session(name, self.args.source, ver)
        self.prev = None
        self.stats["sessions"] += 1
        log(f"session #{self.session} started ({reason})")

    def handle_line(self, raw):
        """One line from the robot. Spec 2.2: '#' lines are diagnostics, non-JSON lines are ignored."""
        received_at = now_iso()                # laptop clock on arrival (spec 6)
        line = raw.strip()
        if not line:
            return
        if line.startswith("#"):
            self.stats["diagnostic"] += 1
            if self.args.verbose:
                log("robot: " + line)
            return
        try:
            obj = json.loads(line)
        except ValueError:
            self.stats["skipped"] += 1
            return
        if not isinstance(obj, dict):
            self.stats["skipped"] += 1
            return

        if "schema" in obj:                    # header: controller (re)started, seq restarts at 0
            if obj.get("schema") != SCHEMA:
                self.warn_once("schema", f"unknown schema {obj.get('schema')!r}, expected {SCHEMA!r}")
            ver = obj.get("ver") if isinstance(obj.get("ver"), int) else SCHEMA_VER
            if ver != SCHEMA_VER:
                self.warn_once("ver", f"schema version {ver}; this bridge was written for v{SCHEMA_VER}")
            self.start_session(ver, "controller start-up line")
            return

        if not isinstance(obj.get("seq"), int):
            self.stats["skipped"] += 1
            return
        if self.session is None:               # started while the robot was already running
            self.start_session(SCHEMA_VER, "joined a running controller")

        row = self.to_row(obj, received_at)
        if self.prev is not None and row["seq"] > self.prev[0] + 1:
            self.stats["lost"] += row["seq"] - self.prev[0] - 1
        self.prev = (row["seq"], row["lat_e7"], row["lon_e7"])
        self.sender.put(row)
        self.stats["records"] += 1
        if self.args.verbose or self.stats["records"] % 25 == 0:
            self.print_progress(row)

    def to_row(self, rec, received_at):
        faults = []
        unknown = set(rec) - set(FIELDS) - {"truth"}
        if unknown:
            self.warn_once("keys:" + ",".join(sorted(unknown)),
                           f"fields not in schema v1 are not stored: {', '.join(sorted(unknown))}")

        v = {}
        for key, (lo, hi) in FIELDS.items():
            x = rec.get(key)
            if isinstance(x, bool) or (x is not None and not isinstance(x, (int, float))):
                x, faults = None, faults + [key + "_invalid"]
            elif isinstance(x, float):
                x = round(x)                   # spec: integers only
            if x is not None and abs(x) > COLUMN_LIMITS.get(key, 2**63):
                x, faults = None, faults + [key + "_invalid"]
            v[key] = x

        # GNSS (spec Table 2, 5.3): no fix -> no position
        fix = v["fix"] if v["fix"] in FIX_CODES else 0
        if v["fix"] not in FIX_CODES and v["fix"] is not None:
            faults.append("fix_invalid")
        lat, lon = v["lat_e7"], v["lon_e7"]
        if fix == 0 or lat is None or lon is None:
            lat = lon = None
        elif not (-900000000 <= lat <= 900000000 and -1800000000 <= lon <= 1800000000):
            lat = lon = None
            faults.append("gnss_invalid")
        elif self.prev and self.prev[1] == lat and self.prev[2] == lon:
            faults.append("gnss_repeat")       # receiver repeated the previous position

        # impedance (spec 3.1)
        z = v["z_mv"]
        if z is None:
            faults.append("z_missing")
        elif z >= 2047 or z <= -2048:
            faults.append("z_saturated")

        # optical (spec 3.2): R = (opt_a - opt_d) / (opt_b - opt_d)
        a, b, d = v["opt_a"], v["opt_b"], v["opt_d"]
        reflectance = None
        if None in (a, b, d):
            faults.append("opt_missing")
        elif a == b == d == 0:
            faults.append("opt_dead")
        else:
            if max(a, b, d) >= 2047:
                faults.append("opt_saturated")
            if b - d > 0:
                reflectance = round((a - d) / (b - d), 4)
            else:
                faults.append("opt_no_reference")

        # temperature: 85 °C power-on value means "not read" (spec Table 2)
        temp = v["temp_c100"]
        if temp == TEMP_POWER_ON:
            temp = None
            faults.append("temp_power_on")
        elif temp is None:
            faults.append("temp_absent")
        elif not (-5500 <= temp <= 12500):
            faults.append("temp_out_of_range")

        for k in ("ax_mg", "ay_mg", "az_mg"):
            if v[k] is not None and abs(v[k]) >= 4000:
                faults.append("accel_saturated")
                break

        truth = rec.get("truth") if rec.get("truth") in SURFACES else None   # test bench only (spec 5.1)
        if self.args.truth_as_prediction:      # TEST ONLY: colours the map while sensors are placeholders
            pred = truth or "unknown"
        else:
            pred = classify(z, reflectance, None if temp is None else temp / 100, faults)

        return {
            "session_id": self.session, "seq": v["seq"], "received_at": received_at, "t_ms": v["t_ms"],
            "fix": fix, "lat_e7": lat, "lon_e7": lon,
            "z_mv": z, "opt_a": a, "opt_b": b, "opt_d": d, "temp_c100": temp,
            "ax_mg": v["ax_mg"], "ay_mg": v["ay_mg"], "az_mg": v["az_mg"],
            "slip": v["slip"] if v["slip"] in (0, 1) else 0,
            "reflectance": reflectance, "surface_pred": pred,
            "fault_flags": sorted(set(faults)), "truth": truth,
        }

    def print_progress(self, row):
        pos = "no fix" if row["lat_e7"] is None else f"{row['lat_e7'] / 1e7:.5f}, {row['lon_e7'] / 1e7:.5f}"
        faults = (" faults=" + ",".join(row["fault_flags"])) if row["fault_flags"] else ""
        state = " (sending is failing, rows are buffered)" if self.sender.failing else ""
        log(f"seq {row['seq']:>5}  {pos:<21} {row['surface_pred']:<7} truth={row['truth'] or '-':<6}"
            f" sent={self.sender.sent}{faults}{state}")

    def close(self):
        if self.session:
            try:
                self.db.end_session(self.session)
            except Exception as e:
                log(f"could not mark the session as ended: {e}")


# ------------------------------------------------------------------ input sources
def replay_lines(path, speed, loop, stop):
    """Lines of a recorded file, paced by t_ms (as the robot sent them); 200 ms if t_ms is missing."""
    while not stop.is_set():
        prev_t = None
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            for line in f:
                if stop.is_set():
                    return
                try:
                    t = json.loads(line).get("t_ms")
                except (ValueError, AttributeError):
                    t = None
                if isinstance(t, int):
                    wait = (t - prev_t) / 1000 if prev_t is not None and t > prev_t else 0.0
                    prev_t = t
                    if 0 < wait < 5 and speed > 0:
                        stop.wait(wait / speed)
                elif speed > 0 and line.strip().startswith("{") and '"schema"' not in line:
                    stop.wait(0.2 / speed)
                yield line
        if not loop:
            return
        log("end of file, replaying again (new session)")


def serial_lines(port, baud, stop):
    try:
        import serial  # pyserial, only needed for the real robot
    except ImportError:
        sys.exit("pyserial is missing. Install it with:  pip3 install pyserial")
    while not stop.is_set():
        try:
            with serial.Serial(port, baud, bytesize=8, parity="N", stopbits=1, timeout=1) as ser:  # spec 2.1: 8N1
                log(f"listening on {port} at {baud} baud")
                while not stop.is_set():
                    raw = ser.readline()
                    if raw:
                        yield raw.decode("utf-8", errors="replace")
        except serial.SerialException as e:
            log(f"serial port problem ({e}); retrying in 2 s")
            stop.wait(2)


# ------------------------------------------------------------------ main
def main():
    p = argparse.ArgumentParser(description="RoboHack bridge: robot serial records -> Supabase")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--replay", metavar="FILE", help="read a recorded .jsonl file instead of the serial port")
    src.add_argument("--port", help="serial port (default: SERIAL_PORT from the env file)")
    p.add_argument("--baud", type=int, default=115200, help="serial speed (spec: 115200)")
    p.add_argument("--speed", type=float, default=1.0, help="replay speed: 1 = real time, 10 = ten times faster, 0 = no waiting")
    p.add_argument("--loop", action="store_true", help="replay the file again and again")
    p.add_argument("--name", help="session name (default: file name or 'Bench run')")
    p.add_argument("--source", choices=("bench", "field"), default="bench")
    p.add_argument("--truth-as-prediction", action="store_true",
                   help="TEST ONLY: use the bench 'truth' label as the surface (the real classifier must never do this)")
    p.add_argument("--dry-run", action="store_true", help="don't send to Supabase, only show what would be stored")
    p.add_argument("--env", default=os.path.expanduser("~/.robohack-bridge.env"), help="settings file")
    p.add_argument("-v", "--verbose", action="store_true", help="print every row and robot diagnostic line")
    args = p.parse_args()

    env = load_env(args.env)
    if args.dry_run:
        db = DryRun(args.verbose)
    else:
        url, key = env.get("SUPABASE_URL"), env.get("SUPABASE_SECRET_KEY")
        if not url or not key:
            sys.exit(f"SUPABASE_URL and SUPABASE_SECRET_KEY are missing. Put them in {args.env} "
                     f"(see bridge/env.example), or run with --dry-run.")
        if key.startswith("sb_publishable_"):
            sys.exit("That is the publishable (read-only) key. The bridge needs the SECRET key (sb_secret_...).")
        db = Supabase(url, key)
        try:
            db.request("GET", "sessions?select=id&limit=1")
        except Exception as e:
            hint = " Did you run database/schema.sql?" if "PGRST205" in str(e) else ""
            sys.exit(f"Cannot use Supabase: {e}.{hint}")

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    sender = Sender(db)
    sender.start()
    bridge = Bridge(db, sender, args)
    if args.truth_as_prediction:
        log("TEST MODE: surface comes from the bench 'truth' label, not from the sensors")

    if args.replay:
        if not os.path.isfile(os.path.expanduser(args.replay)):
            sys.exit(f"File not found: {args.replay}")
        log(f"replaying {args.replay} at {args.speed}x" + (" (dry run)" if args.dry_run else ""))
        lines = replay_lines(args.replay, args.speed, args.loop, stop)
    else:
        port = args.port or env.get("SERIAL_PORT")
        if not port:
            sys.exit("No serial port. Use --port /dev/cu.usbmodem... or set SERIAL_PORT (list ports: ls /dev/cu.*)")
        lines = serial_lines(port, args.baud, stop)

    try:
        for line in lines:
            bridge.handle_line(line)
            if stop.is_set():
                break
    finally:
        log("stopping, sending what is left…")
        sender.finish()
        bridge.close()
        s = bridge.stats
        left = sender.q.qsize() + len(sender.pending)
        log(f"done: {s['records']} records in {s['sessions']} session(s), {sender.sent} sent"
            + (f", {left} NOT sent" if left else "")
            + f", {s['lost']} lost on the link, {s['skipped']} non-record lines skipped, {s['diagnostic']} diagnostic lines")


if __name__ == "__main__":
    main()
