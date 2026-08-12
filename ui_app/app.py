import os
import json
from datetime import datetime
from flask import Flask, render_template, jsonify, request

app = Flask(__name__)

SHARED_DIR = "/app/shared"
COMMAND_FILE = os.path.join(SHARED_DIR, "command.json")
STATUS_FILE = os.path.join(SHARED_DIR, "battery.json")


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
    except Exception:
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


def mask_to_outputs(mask):
    mask = clamp_mask(mask)
    return [bool(mask & (1 << ch)) for ch in range(16)]


def outputs_to_mask(outputs):
    mask = 0

    if not isinstance(outputs, list):
        return mask

    for ch in range(min(16, len(outputs))):
        if bool(outputs[ch]):
            mask |= (1 << ch)

    return clamp_mask(mask)


def default_command():
    return {
        "mask": 0,
        "mask_hex": "0x0000",
        "outputs": mask_to_outputs(0),
        "source": "ui_default",
        "updated_at": now_iso()
    }


def get_current_command():
    data = load_json_safe(COMMAND_FILE, default_command())

    if "mask" in data:
        mask = clamp_mask(data.get("mask", 0))
    elif "outputs" in data:
        mask = outputs_to_mask(data.get("outputs", []))
    else:
        mask = 0

    return {
        "mask": mask,
        "mask_hex": f"0x{mask:04X}",
        "outputs": mask_to_outputs(mask),
        "source": data.get("source", "unknown"),
        "updated_at": data.get("updated_at", "")
    }


def save_command(mask, source):
    mask = clamp_mask(mask)

    data = {
        "mask": mask,
        "mask_hex": f"0x{mask:04X}",
        "outputs": mask_to_outputs(mask),
        "source": source,
        "updated_at": now_iso()
    }

    atomic_write_json(COMMAND_FILE, data)
    return data


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/command", methods=["GET"])
def api_command():
    return jsonify(get_current_command())


@app.route("/api/battery", methods=["GET"])
def api_battery():
    default_status = {
        "timestamp": now_iso(),

        "voltage": 0.0,
        "current": 0.0,
        "soc": 0,
        "soh": 0,
        "max_v": 0.0,
        "min_v": 0.0,
        "avg_temp": 0.0,

        "ur20": {
            "connected": False,
            "last_write_ok": False,
            "mask": 0,
            "mask_hex": "0x0000",
            "outputs": mask_to_outputs(0),
            "physical_mask": 0,
            "physical_mask_hex": "0x0000",
            "output_bit_order": "normal",
            "error": "status file not found"
        },

        "serial": {},
        "ur20_keepalive": {},
        "ur20_status": mask_to_outputs(0)
    }

    return jsonify(load_json_safe(STATUS_FILE, default_status))


@app.route("/api/control", methods=["POST"])
def api_control():
    try:
        req = request.get_json(force=True) or {}
        current = get_current_command()
        mask = current["mask"]

        if "all" in req:
            mask = 0xFFFF if bool(req.get("all")) else 0

        elif "mask" in req:
            mask = clamp_mask(req.get("mask"))

        elif "outputs" in req:
            mask = outputs_to_mask(req.get("outputs", []))

        elif "channel" in req and "value" in req:
            ch = int(req.get("channel"))
            value = bool(req.get("value"))

            if ch < 0 or ch > 15:
                return jsonify({
                    "status": "error",
                    "message": "channel must be 0 to 15"
                }), 400

            if value:
                mask |= (1 << ch)
            else:
                mask &= ~(1 << ch)

        else:
            return jsonify({
                "status": "error",
                "message": "request must include all, mask, outputs, or channel/value"
            }), 400

        saved = save_command(mask, "ui")

        return jsonify({
            "status": "ok",
            "command": saved
        })

    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


@app.route("/api/all_on", methods=["POST"])
def api_all_on():
    saved = save_command(0xFFFF, "ui_all_on")

    return jsonify({
        "status": "ok",
        "command": saved
    })


@app.route("/api/all_off", methods=["POST"])
def api_all_off():
    saved = save_command(0, "ui_all_off")

    return jsonify({
        "status": "ok",
        "command": saved
    })


@app.route("/api/health", methods=["GET"])
def api_health():
    return jsonify({
        "status": "ok",
        "timestamp": now_iso()
    })


if __name__ == "__main__":
    ensure_shared_dir()

    if not os.path.exists(COMMAND_FILE):
        save_command(0, "ui_initial")

    app.run(host="0.0.0.0", port=5000)
