from flask import Flask, render_template, jsonify, request
import json
import os

app = Flask(__name__)
SHARED_FILE = "/app/shared/battery.json"
COMMAND_FILE = "/app/shared/command.json"

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/battery')
def get_battery_data():
    if os.path.exists(SHARED_FILE):
        try:
            with open(SHARED_FILE, "r") as f:
                data = json.load(f)
            return jsonify(data)
        except:
            pass
    return jsonify({
        "voltage": 0.0, "current": 0.0, "soc": 0, "soh": 0,
        "max_v": 0.0, "min_v": 0.0, "avg_temp": 0.0, "ur20_status": []
    })

# トグル操作を受け取ってio_control側に指令を出すAPI
@app.route('/api/control', methods=['POST'])
def control_device():
    try:
        req_data = request.get_json()
        val = req_data.get('value', False)
        
        command_data = {
            "address": 2048, # 0x800 (10進数で2048)
            "value": val
        }
        
        os.makedirs(os.path.dirname(COMMAND_FILE), exist_ok=True)
        with open(COMMAND_FILE, "w") as f:
            json.dump(command_data, f)
            
        return jsonify({"status": "success", "value": val})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)