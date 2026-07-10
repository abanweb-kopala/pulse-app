"""
Pulse relay server
------------------
A minimal WebSocket relay that lets one device ("the device") be
controlled in real time by another device ("the controller").

No commands or vibration data are stored — the server only pairs two
sockets by a short room code and forwards messages between them.

Run:
    pip install -r requirements.txt
    python server.py

Then deploy behind HTTPS (see README section "Remote control setup").
"""

import random
import string
import time

from flask import Flask, jsonify
from flask_socketio import SocketIO, join_room, leave_room, emit, disconnect

app = Flask(__name__)
app.config["SECRET_KEY"] = "change-this-too"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# room_code -> { "device_sid": str|None, "controller_sid": str|None, "created": float }
ROOMS = {}
ROOM_TTL_SECONDS = 60 * 60 * 4  # rooms auto-expire after 4h of inactivity
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no ambiguous chars


def new_code():
    while True:
        code = "".join(random.choice(CODE_ALPHABET) for _ in range(6))
        if code not in ROOMS:
            return code


def prune_rooms():
    now = time.time()
    dead = [c for c, r in ROOMS.items() if now - r["created"] > ROOM_TTL_SECONDS]
    for c in dead:
        ROOMS.pop(c, None)


@app.route("/health")
def health():
    prune_rooms()
    return jsonify({"ok": True, "rooms": len(ROOMS)})


@socketio.on("create_room")
def on_create_room():
    prune_rooms()
    code = new_code()
    ROOMS[code] = {"device_sid": None, "controller_sid": None, "created": time.time()}
    _attach(code, "device")
    emit("room_created", {"code": code})


@socketio.on("join_room_as_controller")
def on_join_room(data):
    code = (data or {}).get("code", "").strip().upper()
    room = ROOMS.get(code)
    if not room or room["device_sid"] is None:
        emit("join_error", {"message": "That code isn't active. Ask for a fresh one."})
        return
    if room["controller_sid"] is not None:
        emit("join_error", {"message": "Someone is already controlling this device."})
        return
    _attach(code, "controller")
    emit("joined", {"code": code})
    socketio.emit("controller_joined", {}, room=room["device_sid"])


def _attach(code, role):
    from flask import request
    room = ROOMS[code]
    room[f"{role}_sid"] = request.sid
    join_room(code)
    socketio_session = {"code": code, "role": role}
    _SID_INFO[request.sid] = socketio_session


_SID_INFO = {}


@socketio.on("command")
def on_command(data):
    """Controller -> device. e.g. {type: 'start'|'stop'|'intensity', ...}"""
    from flask import request
    info = _SID_INFO.get(request.sid)
    if not info or info["role"] != "controller":
        return
    room = ROOMS.get(info["code"])
    if not room or not room["device_sid"]:
        return
    socketio.emit("command", data, room=room["device_sid"])


@socketio.on("device_status")
def on_device_status(data):
    """Device -> controller. e.g. {active, patternName, intensity}"""
    from flask import request
    info = _SID_INFO.get(request.sid)
    if not info or info["role"] != "device":
        return
    room = ROOMS.get(info["code"])
    if not room or not room["controller_sid"]:
        return
    socketio.emit("device_status", data, room=room["controller_sid"])


@socketio.on("stop_sharing")
def on_stop_sharing():
    from flask import request
    info = _SID_INFO.get(request.sid)
    if not info:
        return
    room = ROOMS.get(info["code"])
    if room and room.get("controller_sid"):
        socketio.emit("peer_left", {"reason": "device_stopped"}, room=room["controller_sid"])
    ROOMS.pop(info["code"], None)


@socketio.on("disconnect")
def on_disconnect():
    from flask import request
    info = _SID_INFO.pop(request.sid, None)
    if not info:
        return
    room = ROOMS.get(info["code"])
    if not room:
        return
    other_role = "controller" if info["role"] == "device" else "device"
    other_sid = room.get(f"{other_role}_sid")
    if other_sid:
        socketio.emit("peer_left", {"reason": f"{info['role']}_disconnected"}, room=other_sid)
    if info["role"] == "device":
        ROOMS.pop(info["code"], None)
    else:
        room["controller_sid"] = None


if __name__ == "__main__":
    # allow_unsafe_werkzeug is fine here: this is a tiny 2-user relay,
    # not a public production service. For a public deploy, run behind
    # gunicorn + gevent instead (see README).
    socketio.run(app, host="0.0.0.0", port=5050, allow_unsafe_werkzeug=True)
