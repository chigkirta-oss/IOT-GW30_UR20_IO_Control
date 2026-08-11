import os
import json
import time
import traceback
from datetime import datetime

try:
    import serial
except Exception:
    serial = None

from pyModbusTCP.client import ModbusClient


# =========================
# Settings
# =========================

SHARED_DIR = "/app/shared"
COMMAND_FILE = os.path.join(SHARED_DIR, "command.json")
STATUS_FILE = os.path.join(SHARED_DIR, "battery.json")

UR20_IP = os.getenv("UR20_IP", "192.168.0.222")
UR20_PORT = int(os.getenv("UR20_PORT", "502"))
UR20_ADDR = int(os.getenv("UR20_ADDR", "2048"))
UR20_UNIT_ID = int(os.getenv("UR20_UNIT_ID", "1"))

POLL_INTERVAL_SEC = float(os.getenv("POLL_INTERVAL_SEC", "0.2"))

# Function Code 4 keep alive
KEEPALIVE_INTERVAL_SEC = float(os.getenv("KEEPALIVE_INTERVAL_SEC", "3.0"))
KEEPALIVE_ADDR = int(os.getenv("KEEPALIVE_ADDR", "0"))
KEEPALIVE_COUNT = int(os.getenv("KEEPALIVE_COUNT", "1"))

SERIAL_PORT = os.getenv("SERIAL_PORT", "/dev/ttyAMA2")
SERIAL_BAUDRATE = int(os.getenv("SERIAL_BAUDRATE", "9600"))
SERIAL_TIMEOUT = float(os.getenv("SERIAL_TIMEOUT", "0.05"))

DEFAULT_MASK = 0


# =========================
# Utility
# =========================

def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def ensure_shared_dir():
    os.makedirs(SHARED_DIR, exist_ok=True)


def atomic_write_json(path, data):
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def load_json_safe(path, default):
    if not os.path.exists(path):
        return default

    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"[WARN] Failed to read JSON: {path}: {e}", flush=True)
        return default


def clamp_mask(value):
    try:
        value = int(value)
    except Exception:
        value = 0

    if value < 0:
        value = 0

    if value > 0xFFFF:
        value = 0xFFFF

    return value


def outputs_to_mask(outputs):
    mask = 0

    if not isinstance(outputs, list):
        return mask

    for ch in range(min(16, len(outputs))):
        if bool(outputs[ch]):
            mask |= (1 << ch)

    return mask


def mask_to_outputs(mask):
    return [bool(mask & (1 << ch)) for ch in range(16)]


def read_command():
    data = load_json_safe(COMMAND_FILE, {"mask": DEFAULT_MASK})

    if "mask" in data:
        return clamp_mask(data.get("mask", DEFAULT_MASK))

    if "outputs" in data:
        return clamp_mask(outputs_to_mask(data.get("outputs", [])))

    return DEFAULT_MASK


def init_command_file():
    if not os.path.exists(COMMAND_FILE):
        atomic_write_json(COMMAND_FILE, {
            "mask": DEFAULT_MASK,
            "mask_hex": f"0x{DEFAULT_MASK:04X}",
            "outputs": mask_to_outputs(DEFAULT_MASK),
            "updated_at": now_iso(),
            "source": "io_initial_default"
        })


def default_serial_status():
    return {
        "enabled": True,
        "port": SERIAL_PORT,
        "baudrate": SERIAL_BAUDRATE,
        "opened": False,
        "last_error": "not initialized",
        "last_rx_ascii": "",
        "last_rx_hex": ""
    }


def default_keepalive_status():
    return {
        "enabled": True,
        "function_code": 4,
        "address": KEEPALIVE_ADDR,
        "count": KEEPALIVE_COUNT,
        "interval_sec": KEEPALIVE_INTERVAL_SEC,
        "last_ok": False,
        "last_value": None,
        "last_error": "not initialized",
        "last_time": None
    }


def write_status(mask, connected, last_write_ok, modbus_error=None,
                 serial_status=None, keepalive_status=None):
    if serial_status is None:
        serial_status = default_serial_status()

    if keepalive_status is None:
        keepalive_status = default_keepalive_status()

    status = {
        "timestamp": now_iso(),

        # Existing UI compatibility fields
        "voltage": 0.0,
        "current": 0.0,
        "soc": 0,
        "soh": 0,
        "max_v": 0.0,
        "min_v": 0.0,
        "avg_temp": 0.0,

        "ur20": {
            "ip": UR20_IP,
            "port": UR20_PORT,
            "address": UR20_ADDR,
            "unit_id": UR20_UNIT_ID,
            "connected": connected,
            "last_write_ok": last_write_ok,
            "mask": mask,
            "mask_hex": f"0x{mask:04X}",
            "outputs": mask_to_outputs(mask),
            "error": modbus_error
        },

        # Internal status. UI does not display this.
        "serial": serial_status,
        "ur20_keepalive": keepalive_status,

        # Existing UI compatibility field
        "ur20_status": mask_to_outputs(mask)
    }

    atomic_write_json(STATUS_FILE, status)


# =========================
# Serial keeper
# =========================

class SerialKeeper:
    def __init__(self):
        self.ser = None
        self.last_error = None
        self.last_rx_ascii = ""
        self.last_rx_hex = ""
        self.last_open_try = 0

    def open_if_needed(self):
        if serial is None:
            self.last_error = "pyserial is not installed"
            return False

        if self.ser is not None and getattr(self.ser, "is_open", False):
            return True

        now = time.time()
        if now - self.last_open_try < 5:
            return False

        self.last_open_try = now

        try:
            self.ser = serial.Serial(
                SERIAL_PORT,
                SERIAL_BAUDRATE,
                timeout=SERIAL_TIMEOUT
            )
            self.last_error = None
            print(f"[SERIAL] opened {SERIAL_PORT} baud={SERIAL_BAUDRATE}", flush=True)
            return True

        except Exception as e:
            self.ser = None
            self.last_error = str(e)
            print(f"[SERIAL] open failed: {self.last_error}", flush=True)
            return False

    def poll(self):
        opened = self.open_if_needed()

        if not opened:
            return self.status()

        try:
            try:
                waiting = self.ser.in_waiting
            except Exception:
                waiting = 0

            if waiting and waiting > 0:
                data = self.ser.read(waiting)
            else:
                data = self.ser.read(1)

            if data:
                self.last_rx_hex = data.hex()
                self.last_rx_ascii = data.decode("ascii", errors="ignore")
                print(
                    f"[SERIAL] rx_hex={self.last_rx_hex} rx_ascii={self.last_rx_ascii}",
                    flush=True
                )

        except Exception as e:
            self.last_error = str(e)
            print(f"[SERIAL] read failed: {self.last_error}", flush=True)

            try:
                if self.ser is not None:
                    self.ser.close()
            except Exception:
                pass

            self.ser = None

        return self.status()

    def status(self):
        try:
            opened = self.ser is not None and self.ser.is_open
        except Exception:
            opened = False

        return {
            "enabled": True,
            "port": SERIAL_PORT,
            "baudrate": SERIAL_BAUDRATE,
            "opened": opened,
            "last_error": self.last_error,
            "last_rx_ascii": self.last_rx_ascii,
            "last_rx_hex": self.last_rx_hex
        }


# =========================
# UR20 keeper
# =========================

def ensure_modbus_open(client):
    if not client.is_open:
        return client.open()
    return True


def run_keepalive(client):
    status = {
        "enabled": True,
        "function_code": 4,
        "address": KEEPALIVE_ADDR,
        "count": KEEPALIVE_COUNT,
        "interval_sec": KEEPALIVE_INTERVAL_SEC,
        "last_ok": False,
        "last_value": None,
        "last_error": None,
        "last_time": now_iso()
    }

    try:
        ensure_modbus_open(client)

        vals = client.read_input_registers(KEEPALIVE_ADDR, KEEPALIVE_COUNT)

        status["last_time"] = now_iso()

        if vals is not None:
            status["last_ok"] = True
            status["last_value"] = vals
            status["last_error"] = None
            print(
                f"[UR20] FC4 keepalive OK addr={KEEPALIVE_ADDR}, value={vals}",
                flush=True
            )
        else:
            status["last_ok"] = False
            status["last_value"] = None
            status["last_error"] = "read_input_registers returned None"
            print(
                f"[UR20] FC4 keepalive NG addr={KEEPALIVE_ADDR}: returned None",
                flush=True
            )

    except Exception as e:
        status["last_ok"] = False
        status["last_value"] = None
        status["last_error"] = str(e)
        status["last_time"] = now_iso()
        print(f"[UR20] FC4 keepalive exception: {e}", flush=True)

        try:
            client.close()
        except Exception:
            pass

    return status


# =========================
# Main
# =========================

def main():
    ensure_shared_dir()
    init_command_file()

    print("===================================", flush=True)
    print("UR20 IO controller starting", flush=True)
    print(f"UR20_IP       : {UR20_IP}", flush=True)
    print(f"UR20_PORT     : {UR20_PORT}", flush=True)
    print(f"UR20_ADDR     : {UR20_ADDR}", flush=True)
    print(f"UR20_UNIT_ID  : {UR20_UNIT_ID}", flush=True)
    print(f"POLL_INTERVAL : {POLL_INTERVAL_SEC}", flush=True)
    print(f"SERIAL_PORT   : {SERIAL_PORT}", flush=True)
    print(f"KEEPALIVE_FC  : 4", flush=True)
    print(f"KEEPALIVE_ADDR: {KEEPALIVE_ADDR}", flush=True)
    print(f"KEEPALIVE_SEC : {KEEPALIVE_INTERVAL_SEC}", flush=True)
    print("===================================", flush=True)

    client = ModbusClient(
        host=UR20_IP,
        port=UR20_PORT,
        unit_id=UR20_UNIT_ID,
        auto_open=True,
        auto_close=False,
        timeout=2
    )

    serial_keeper = SerialKeeper()

    last_written_mask = None
    connected = False
    last_write_ok = False
    last_modbus_error = None

    last_keepalive_time = 0
    keepalive_status = default_keepalive_status()

    while True:
        try:
            serial_status = serial_keeper.poll()
            mask = read_command()

            connected = ensure_modbus_open(client)

            # Cyclic Modbus keep alive using Function Code 4.
            now_ts = time.time()
            if now_ts - last_keepalive_time >= KEEPALIVE_INTERVAL_SEC:
                last_keepalive_time = now_ts
                keepalive_status = run_keepalive(client)

            # Output write only when command value changes.
            if mask != last_written_mask:
                print(
                    f"[UR20] write addr={UR20_ADDR}, mask={mask}, hex=0x{mask:04X}",
                    flush=True
                )

                ensure_modbus_open(client)
                last_write_ok = client.write_multiple_registers(UR20_ADDR, [mask])

                if last_write_ok:
                    print(f"[UR20] OK write success: 0x{mask:04X}", flush=True)
                    last_written_mask = mask
                    connected = True
                    last_modbus_error = None
                else:
                    print(
                        f"[UR20] NG write_multiple_registers returned False: 0x{mask:04X}",
                        flush=True
                    )
                    connected = False
                    last_modbus_error = "write_multiple_registers returned False"

            write_status(
                mask=mask,
                connected=connected,
                last_write_ok=last_write_ok,
                modbus_error=last_modbus_error,
                serial_status=serial_status,
                keepalive_status=keepalive_status
            )

        except Exception as e:
            last_modbus_error = str(e)
            connected = False
            last_write_ok = False

            print("[ERROR] main loop exception", flush=True)
            traceback.print_exc()

            try:
                if client.is_open:
                    client.close()
            except Exception:
                pass

            current_mask = last_written_mask if last_written_mask is not None else DEFAULT_MASK

            try:
                write_status(
                    mask=current_mask,
                    connected=False,
                    last_write_ok=False,
                    modbus_error=last_modbus_error,
                    serial_status=serial_keeper.status(),
                    keepalive_status=keepalive_status
                )
            except Exception:
                traceback.print_exc()

        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()