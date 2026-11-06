import hmac
import os
from functools import wraps
from flask import Flask, render_template, jsonify, request
import checks
import configmap
import machines
import pipeline_builder
import presets
import task_templates
import log

API_TOKEN = os.environ.get("API_TOKEN")
LABEL_TASK = os.environ.get("LABEL_TASK")

app = Flask(__name__)

def get_log_contents():
    try:
        with open(log.INFO_LOG_FILE, 'r') as f:
            lines = f.readlines()
            return ''.join(lines[-100:])
    except FileNotFoundError:
        return "Log file not found."

def get_runs():
    # "until" says what releases the machine: the whole run or the LABEL_TASK task
    runs = [{"pipelinerun": r, "machine": m, "until": "run"} for r, m in configmap.get_wait_finish_runs() or []]
    runs += [{"pipelinerun": r, "machine": m, "until": "task"} for r, m in configmap.get_wait_finish_task() or []]
    return runs

def require_token(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not API_TOKEN:
            return jsonify(error="machine API disabled, set API_TOKEN to enable it"), 403
        auth = request.headers.get("Authorization", "")
        if not hmac.compare_digest(auth, f"Bearer {API_TOKEN}"):
            return jsonify(error="unauthorized"), 401
        return f(*args, **kwargs)
    return wrapper

@app.errorhandler(machines.MachineError)
@app.errorhandler(task_templates.TemplateError)
def handle_machine_error(e):
    return jsonify(error=str(e)), e.http_code

@app.route('/')
def machine_status():
    return render_template('status.html', machines=machines.list_machines(), runs=get_runs(),
                           log_contents=get_log_contents(), api_enabled=bool(API_TOKEN), label_task=LABEL_TASK,
                           presets_enabled=presets.enabled(),
                           presets=presets.list_presets() if presets.enabled() else {})

@app.route('/log')
def log_data():
    return jsonify(log=get_log_contents())

@app.route('/api/machines', methods=['GET'])
def list_machines():
    return jsonify(machines.list_machines())

@app.route('/api/runs', methods=['GET'])
def list_runs():
    return jsonify(get_runs())

@app.route('/api/token-check', methods=['GET'])
@require_token
def token_check():
    # lets the web UI tell a wrong token apart before the user fills the form
    return "", 204

@app.route('/api/machines', methods=['POST'])
@require_token
def add_machine():
    body = request.get_json(silent=True) or {}
    name = body.get("name")
    machines.add_machine(name, body.get("host"), body.get("username"), body.get("private_key"))
    return jsonify(name=name, status="free"), 201

@app.route('/api/machines/<name>/check', methods=['POST'])
@require_token
def check_machine(name):
    # needs the token: it makes the controller SSH out, and the answer shows the host
    return jsonify(checks.check_machine(name))

@app.route('/api/machines/<name>', methods=['DELETE'])
@require_token
def remove_machine(name):
    machines.remove_machine(name)
    return "", 204

@app.route('/api/pipelines', methods=['GET'])
def list_pipelines():
    return jsonify(presets.list_pipelines())

@app.route('/api/templates', methods=['GET'])
def list_templates():
    return jsonify(task_templates.list_templates())

@app.route('/api/pipelines/<name>', methods=['GET'])
def get_pipeline(name):
    return jsonify(pipeline_builder.get_model(name))

@app.route('/api/pipelines/<name>', methods=['PUT'])
@require_token
def save_pipeline(name):
    return jsonify(pipeline_builder.save_pipeline(name, request.get_json(silent=True)))

@app.route('/api/pipelines/<name>', methods=['DELETE'])
@require_token
def delete_pipeline(name):
    pipeline_builder.delete_pipeline(name)
    return "", 204

@app.route('/api/presets', methods=['GET'])
def list_presets():
    return jsonify(presets.list_presets())

@app.route('/api/presets/<name>', methods=['PUT'])
@require_token
def save_preset(name):
    return jsonify(presets.save_preset(name, request.get_json(silent=True)))

@app.route('/api/presets/<name>', methods=['DELETE'])
@require_token
def delete_preset(name):
    presets.delete_preset(name)
    return "", 204

@app.route('/api/presets/<name>/start', methods=['POST'])
@require_token
def start_preset(name):
    return jsonify(presets.start_preset(name)), 201

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
