# -*- coding: utf-8 -*-
# Remote Console — сервер. Запуск: python app.py (порт из переменной PORT)
import os, time, html, uuid, secrets, sqlite3, threading
from collections import deque
from datetime import timedelta
from functools import wraps
from flask import Flask, request, jsonify, session, redirect, Response
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY') or secrets.token_hex(24)
app.permanent_session_lifetime = timedelta(days=14)

CLIENT_KEY = os.environ.get('CLIENT_KEY', 'change-me-client-key')
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'panel.db')
ONLINE_TTL = 15   # секунд без связи = офлайн
FRAME_TTL = 8     # секунд свежести кадра трансляции

_lock = threading.Lock()
devices = {}      # device_id -> состояние устройства


def _init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute('CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, password TEXT NOT NULL)')
    if con.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 0:
        u = os.environ.get('ADMIN_USERNAME', 'admin')
        p = os.environ.get('ADMIN_PASSWORD', 'admin123')
        con.execute('INSERT INTO users(username, password) VALUES (?, ?)', (u, generate_password_hash(p)))
        con.commit()
    con.close()

_init_db()


def get_dev(dev_id):
    with _lock:
        d = devices.get(dev_id)
        if d is None:
            d = {'id': dev_id, 'name': dev_id, 'last_seen': 0.0,
                 'queue': deque(), 'inq': deque(),
                 'results': [], 'result_seq': 0,
                 'frame': None, 'frame_time': 0.0, 'frame_seq': 0,
                 'screen': [0, 0], 'cond': threading.Condition(_lock)}
            devices[dev_id] = d
        return d


def client_ok():
    return request.headers.get('X-Client-Key', '') == CLIENT_KEY


def is_online(d):
    return (time.time() - d['last_seen']) < ONLINE_TTL


def is_streaming(d):
    return d['frame'] is not None and (time.time() - d['frame_time']) < FRAME_TTL


def dev_summary(d):
    w, h = d['screen']
    return {'id': d['id'], 'name': d['name'], 'online': is_online(d),
            'streaming': is_streaming(d), 'screen': {'w': w, 'h': h}}


def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if 'user' not in session:
            return redirect('/login')
        return f(*a, **k)
    return w


# --------------------------- API для клиента (bat) ---------------------------

@app.route('/api/register', methods=['POST'])
def api_register():
    if not client_ok():
        return jsonify(error='bad key'), 401
    j = request.get_json(force=True, silent=True) or {}
    dev_id = (j.get('device_id') or '').strip()[:64]
    if not dev_id:
        return jsonify(error='no id'), 400
    d = get_dev(dev_id)
    with _lock:
        d['name'] = (j.get('name') or dev_id)[:64]
        d['last_seen'] = time.time()
    return jsonify(ok=True)


@app.route('/api/poll')
def api_poll():
    """Клиент забирает команды и события ввода. wait=сколько ждать (long-poll)."""
    if not client_ok():
        return jsonify(error='bad key'), 401
    try:
        wait = max(0.0, min(float(request.args.get('wait', 25)), 27.0))
    except Exception:
        wait = 25.0
    d = get_dev(request.args.get('device_id', ''))
    cmds, evs = [], []
    with d['cond']:
        if wait > 0 and not d['queue'] and not d['inq']:
            d['cond'].wait(wait)
        while d['queue']:
            cmds.append(d['queue'].popleft())
        while d['inq'] and len(evs) < 100:
            evs.append(d['inq'].popleft())
        d['last_seen'] = time.time()
    return jsonify(commands=cmds, events=evs)


@app.route('/api/result', methods=['POST'])
def api_result():
    if not client_ok():
        return jsonify(error='bad key'), 401
    j = request.get_json(force=True, silent=True) or {}
    d = get_dev(j.get('device_id', ''))
    out = str(j.get('output', ''))[:20000]
    with _lock:
        for rec in d['results']:
            if rec['id'] == j.get('cmd_id'):
                rec['status'] = 'done' if j.get('ok', True) else 'error'
                rec['output'] = out
                break
        d['result_seq'] += 1
        d['last_seen'] = time.time()
    return jsonify(ok=True)


@app.route('/api/stream', methods=['POST'])
def api_stream():
    """Клиент отправляет JPEG-кадр (бинарно), w/h — размер картинки."""
    if not client_ok():
        return jsonify(error='bad key'), 401
    d = get_dev(request.args.get('device_id', ''))
    try:
        w = int(request.args.get('w', 0)); h = int(request.args.get('h', 0))
    except Exception:
        w = h = 0
    data = request.get_data()
    with _lock:
        d['frame'] = data
        d['frame_time'] = time.time()
        d['frame_seq'] += 1
        d['screen'] = [w, h]
        d['last_seen'] = time.time()
    return jsonify(ok=True)


# --------------------------- API для сайта ---------------------------

@app.route('/api/devices')
@login_required
def api_devices():
    with _lock:
        items = [dev_summary(d) for d in devices.values()]
    items.sort(key=lambda x: (not x['online'], x['name'].lower()))
    return jsonify(devices=items)


@app.route('/api/device/<path:dev_id>')
@login_required
def api_device(dev_id):
    d = devices.get(dev_id)
    if not d:
        return jsonify(error='not found'), 404
    with _lock:
        s = dev_summary(d)
        s['results'] = [dict(r) for r in d['results']]
        s['result_seq'] = d['result_seq']
    return jsonify(s)


@app.route('/api/command', methods=['POST'])
@login_required
def api_command():
    j = request.get_json(force=True, silent=True) or {}
    ctype = j.get('type', '')
    d = get_dev(j.get('device_id', ''))
    cmd = {'id': uuid.uuid4().hex, 'type': ctype}
    if ctype == 'exec':
        cmd['shell'] = j.get('shell', 'cmd')
        cmd['command'] = str(j.get('command', ''))[:8000]
        cmd['admin'] = bool(j.get('admin'))
        rec = {'id': cmd['id'], 'cmd': cmd['command'], 'shell': cmd['shell'],
               'admin': cmd['admin'], 'status': 'sent', 'output': '', 'time': time.time()}
        with _lock:
            d['results'].insert(0, rec)
            del d['results'][40:]
            d['result_seq'] += 1
            d['queue'].append(cmd)
            d['cond'].notify_all()
    elif ctype in ('stream_start', 'stream_stop'):
        def clamp(v, lo, hi, df):
            try:
                return max(lo, min(int(v), hi))
            except Exception:
                return df
        cmd['fps'] = clamp(j.get('fps'), 1, 15, 6)
        cmd['quality'] = clamp(j.get('quality'), 20, 90, 50)
        cmd['width'] = clamp(j.get('width'), 480, 1920, 1400)
        with _lock:
            d['queue'].append(cmd)
            d['cond'].notify_all()
    else:
        return jsonify(error='bad type'), 400
    return jsonify(ok=True, cmd_id=cmd['id'])


@app.route('/api/input_push', methods=['POST'])
@login_required
def api_input_push():
    j = request.get_json(force=True, silent=True) or {}
    d = get_dev(j.get('device_id', ''))
    events = j.get('events') or []
    if isinstance(events, list):
        with _lock:
            for e in events[:120]:
                if isinstance(e, dict):
                    d['inq'].append(e)
            d['cond'].notify_all()
    return jsonify(ok=True)


@app.route('/api/change_password', methods=['POST'])
@login_required
def api_change_password():
    j = request.get_json(force=True, silent=True) or {}
    old, new = j.get('old', ''), j.get('new', '')
    if len(new) < 4:
        return jsonify(error='Новый пароль слишком короткий'), 400
    con = sqlite3.connect(DB_PATH)
    row = con.execute('SELECT password FROM users WHERE username=?', (session['user'],)).fetchone()
    if not row or not check_password_hash(row[0], old):
        con.close()
        return jsonify(error='Старый пароль неверный'), 400
    con.execute('UPDATE users SET password=? WHERE username=?', (generate_password_hash(new), session['user']))
    con.commit(); con.close()
    return jsonify(ok=True)


# --------------------------- MJPEG-трансляция ---------------------------

@app.route('/stream/<path:dev_id>')
@login_required
def stream(dev_id):
    d = get_dev(dev_id)

    def gen():
        last, idle = -1, 0
        while True:
            frame = d['frame']
            seq = d['frame_seq']
            fresh = frame is not None and (time.time() - d['frame_time']) < FRAME_TTL
            if frame is not None and fresh and seq != last:
                last = seq
                idle = 0
                yield (b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: '
                       + str(len(frame)).encode() + b'\r\n\r\n' + frame + b'\r\n')
            else:
                idle += 1
                if idle > 900:  # ~90 сек без кадров — закрываем поток
                    return
                time.sleep(0.1)

    return Response(gen(), mimetype='multipart/x-mixed-replace; boundary=frame',
                    headers={'Cache-Control': 'no-cache'})


# --------------------------- Страницы ---------------------------

@app.route('/login', methods=['GET', 'POST'])
def login():
    err = ''
    if request.method == 'POST':
        u = request.form.get('username', '').strip()
        p = request.form.get('password', '')
        con = sqlite3.connect(DB_PATH)
        row = con.execute('SELECT password FROM users WHERE username=?', (u,)).fetchone()
        con.close()
        if row and check_password_hash(row[0], p):
            session['user'] = u or 'admin'
            session.permanent = True
            return redirect('/')
        err = html.escape('Неверный логин или пароль')
    return Response(LOGIN_HTML.replace('__ERROR__',
                    ('<div class="err">' + err + '</div>') if err else ''),
                    mimetype='text/html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')


@app.route('/')
@login_required
def index():
    return Response(INDEX_HTML.replace('__USER__', html.escape(session['user'])),
                    mimetype='text/html')


@app.route('/device/<path:dev_id>')
@login_required
def device_page(dev_id):
    return Response(DEVICE_HTML, mimetype='text/html')


# =========================== HTML-страницы ===========================

FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
         '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
         '<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600'
         '&family=Manrope:wght@400;600;800&display=swap" rel="stylesheet">')

BASE_CSS = '''
*{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#0a0e0c;--panel:#101713;--line:#22352a;--txt:#d9e4dc;--mut:#7d8f83;
--acc:#7fe08d;--amber:#e8c468;--bad:#ef9b8e;--mono:'JetBrains Mono',ui-monospace,monospace}
body{background:var(--bg);color:var(--txt);font-family:'Manrope',system-ui,sans-serif;min-height:100vh}
body::before{content:'';position:fixed;inset:0;pointer-events:none;
background:radial-gradient(900px 400px at 50% -8%,rgba(127,224,141,.09),transparent 65%)}
@keyframes pulse{50%{opacity:.35}}
header{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:14px 22px;
border-bottom:1px solid #1b2a20;position:sticky;top:0;z-index:5;
background:rgba(10,14,12,.88);backdrop-filter:blur(8px)}
.brand{font:600 13px var(--mono);letter-spacing:.14em;text-transform:uppercase;display:flex;gap:10px;align-items:center}
.brand .dot{width:9px;height:9px;border-radius:50%;background:var(--acc);
box-shadow:0 0 10px rgba(127,224,141,.9);animation:pulse 2s infinite}
.brand b{color:var(--acc)}
a.out{color:var(--bad);text-decoration:none;font-size:13px;border:1px solid #3a2723;padding:6px 12px;border-radius:8px}
a.out:hover{background:rgba(239,125,109,.1)}
a.back{color:var(--mut);text-decoration:none;font-size:13px}
a.back:hover{color:var(--acc)}
'''

LOGIN_HTML = '''<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Remote Console — вход</title>''' + FONTS + '''<style>
''' + BASE_CSS + '''
body{display:flex;align-items:center;justify-content:center;padding:20px}
.card{position:relative;width:100%;max-width:400px;background:var(--panel);border:1px solid var(--line);
border-radius:14px;padding:34px 30px;box-shadow:0 24px 70px rgba(0,0,0,.5)}
.term-line{font:12px var(--mono);color:var(--mut);letter-spacing:.08em;display:flex;align-items:center;
gap:8px;margin-bottom:26px;text-transform:uppercase}
.term-line .dot{width:8px;height:8px;border-radius:50%;background:var(--acc);
box-shadow:0 0 10px rgba(127,224,141,.9);animation:pulse 2s infinite}
h1{font-size:24px;font-weight:800;margin-bottom:6px}
.sub{color:var(--mut);font-size:14px;margin-bottom:22px}
label{display:block;font:600 12px var(--mono);color:#9fb2a5;letter-spacing:.06em;margin:14px 0 6px;text-transform:uppercase}
input{width:100%;background:#0b110e;border:1px solid #253a2d;color:#e6efe8;border-radius:8px;
padding:12px 14px;font-size:15px;outline:none;transition:border .15s}
input:focus{border-color:var(--acc)}
button{width:100%;margin-top:22px;background:var(--acc);color:#08130b;border:0;border-radius:8px;
padding:13px;font:800 15px 'Manrope',sans-serif;cursor:pointer;transition:.15s}
button:hover{filter:brightness(1.08)}
.err{background:rgba(239,125,109,.12);border:1px solid rgba(239,125,109,.4);color:var(--bad);
border-radius:8px;padding:10px 12px;font-size:13px;margin-bottom:8px}
.hint{margin-top:18px;color:#5c6f63;font-size:12px;text-align:center;font-family:var(--mono)}
.cursor{display:inline-block;width:8px;height:14px;background:var(--acc);vertical-align:-2px;
animation:blink 1s steps(1) infinite}
@keyframes blink{50%{opacity:0}}
</style></head><body>
<div class="card">
  <div class="term-line"><span class="dot"></span>remote console · secure</div>
  <h1>Вход в панель</h1>
  <div class="sub">Управление подключёнными устройствами</div>
  __ERROR__
  <form method="post">
    <label>Логин</label><input name="username" required autofocus>
    <label>Пароль</label><input type="password" name="password" required>
    <button type="submit">Войти</button>
  </form>
  <div class="hint">admin@remote-console:~$ <span class="cursor"></span></div>
</div>
</body></html>'''

INDEX_HTML = '''<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Remote Console — устройства</title>''' + FONTS + '''<style>
''' + BASE_CSS + '''
.user{display:flex;gap:14px;align-items:center}
.user span{color:var(--mut);font-size:13px}
main{max-width:860px;margin:0 auto;padding:28px 22px 60px}
h1{font-size:22px;font-weight:800;margin-bottom:4px}
.sub{color:var(--mut);font-size:14px;margin-bottom:22px}
#list{display:flex;flex-direction:column;gap:10px}
.dev{display:flex;align-items:center;gap:14px;background:var(--panel);border:1px solid var(--line);
border-radius:12px;padding:16px 18px;text-decoration:none;color:inherit;transition:.15s}
.dev:hover{border-color:var(--acc);transform:translateY(-1px)}
.dev .dot{width:10px;height:10px;border-radius:50%;flex:none}
.dev.on .dot{background:var(--acc);box-shadow:0 0 10px rgba(127,224,141,.9);animation:pulse 2s infinite}
.dev.off .dot{background:#5c6f63}
.dev .nm{font-weight:700;font-size:15px}
.dev .id{color:#5c6f63;font:12px var(--mono);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dev .st{font:600 11px var(--mono);letter-spacing:.08em;text-transform:uppercase;padding:5px 10px;border-radius:20px;border:1px solid}
.dev.on .st{color:var(--acc);border-color:#2c5137}
.dev.off .st{color:var(--mut);border-color:#2a352d}
.dev .arr{color:var(--mut)}
.empty{border:1px dashed #2a3d31;border-radius:12px;padding:36px;text-align:center;color:var(--mut);font-size:14px;line-height:1.7}
details{margin-top:30px}
summary{cursor:pointer;color:var(--mut);font-size:13px}
.cpanel{margin-top:12px;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px}
.cpanel input{background:#0b110e;border:1px solid #253a2d;border-radius:8px;padding:10px 12px;color:#e6efe8;outline:none}
.cpanel input:focus{border-color:var(--acc)}
.cpanel button{background:var(--acc);border:0;color:#08130b;border-radius:8px;padding:10px 16px;font-weight:700;cursor:pointer}
.cprow{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end}
.cprow label{font:600 11px var(--mono);color:#9fb2a5;text-transform:uppercase;letter-spacing:.06em;display:block;margin-bottom:5px}
#cpMsg{font-size:13px;margin-top:8px;min-height:16px}
#cpMsg.ok{color:var(--acc)}#cpMsg.bad{color:var(--bad)}
</style></head><body>
<header>
  <div class="brand"><span class="dot"></span>remote <b>console</b></div>
  <div class="user"><span>__USER__</span><a class="out" href="/logout">выйти</a></div>
</header>
<main>
  <h1>Устройства</h1>
  <div class="sub">Подключённые клиенты. Нажмите на устройство, чтобы открыть панель управления.</div>
  <div id="list"><div class="empty">Загрузка…</div></div>
  <details>
    <summary>Сменить пароль</summary>
    <div class="cpanel">
      <form id="cpForm" class="cprow">
        <div><label>Старый пароль</label><input type="password" id="oldP" required></div>
        <div><label>Новый пароль</label><input type="password" id="newP" required minlength="4"></div>
        <button type="submit">Сохранить</button>
      </form>
      <div id="cpMsg"></div>
    </div>
  </details>
</main>
<script>
var $=function(id){return document.getElementById(id)};
function esc(s){return (s||'').replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
async function post(url,body){
  try{var r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});return await r.json();}
  catch(e){return null;}
}
async function refresh(){
  var r;try{r=await(await fetch('/api/devices')).json();}catch(e){return;}
  var list=r.devices||[];
  if(!list.length){$('list').innerHTML='<div class="empty">Пока нет устройств.<br>Запустите BAT-файл на целевой машине — устройство появится здесь автоматически.</div>';return;}
  $('list').innerHTML=list.map(function(d){
    return '<a class="dev '+(d.online?'on':'off')+'" href="/device/'+encodeURIComponent(d.id)+'">'+
      '<span class="dot"></span><span class="nm">'+esc(d.name)+'</span>'+
      '<span class="id">'+esc(d.id)+(d.streaming?' · live':'')+'</span>'+
      '<span class="st">'+(d.online?'online':'offline')+'</span><span class="arr">→</span></a>';
  }).join('');
}
setInterval(refresh,3000);refresh();
 $('cpForm').addEventListener('submit',async function(e){
  e.preventDefault();
  var res=await post('/api/change_password',{old:$('oldP').value,new:$('newP').value});
  var m=$('cpMsg');
  if(res&&res.ok){m.textContent='Пароль изменён';m.className='ok';$('oldP').value='';$('newP').value='';}
  else{m.textContent=(res&&res.error)||'Ошибка';m.className='bad';}
});
</script>
</body></html>'''

DEVICE_HTML = '''<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>Remote Console — устройство</title>''' + FONTS + '''<style>
''' + BASE_CSS + '''
.ttl{display:flex;gap:12px;align-items:center;min-width:0}
#dName{font-weight:800;font-size:15px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.st{font:600 11px var(--mono);padding:4px 10px;border-radius:20px;border:1px solid;flex:none}
.st.on{color:var(--acc);border-color:#2c5137}
.st.off{color:var(--mut);border-color:#2a352d}
main{max-width:1240px;margin:0 auto;padding:22px;display:grid;grid-template-columns:440px 1fr;gap:22px;align-items:start}
@media(max-width:920px){main{grid-template-columns:1fr}}
.sec{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px}
.sec-title{font:600 11px var(--mono);letter-spacing:.12em;text-transform:uppercase;color:var(--mut);margin-bottom:14px}
.cmd-row{display:flex;gap:8px;flex-wrap:wrap}
select{background:#0b110e;border:1px solid #253a2d;color:#e6efe8;border-radius:8px;padding:10px;
font:600 13px var(--mono);outline:none;cursor:pointer}
select:focus{border-color:var(--acc)}
#cmdInput{flex:1;min-width:160px;background:#0b110e;border:1px solid #253a2d;color:#e6efe8;border-radius:8px;
padding:10px 12px;font:13px var(--mono);outline:none}
#cmdInput:focus{border-color:var(--acc)}
.btn{background:transparent;border:1px solid #2c5137;color:var(--acc);border-radius:8px;padding:10px 15px;
font:700 13px 'Manrope',sans-serif;cursor:pointer;display:inline-flex;align-items:center;gap:7px;transition:.15s;user-select:none}
.btn:hover{background:rgba(127,224,141,.08)}
.btn.go{background:var(--acc);color:#08130b;border-color:var(--acc)}
.btn.go:hover{filter:brightness(1.08)}
.btn.sm{padding:8px 12px;font-size:12px}
.btn.active{background:var(--acc);color:#08130b;border-color:var(--acc)}
.btn svg{flex:none}
.chk{display:flex;gap:8px;align-items:center;margin:12px 2px 0;color:#9fb2a5;font-size:13px;cursor:pointer}
.chk input{accent-color:var(--acc);width:15px;height:15px}
.chk .hint{color:#5c6f63;font-size:12px}
.log{margin-top:14px;background:#080c0a;border:1px solid #1c2b21;border-radius:10px;max-height:430px;
overflow:auto;padding:12px;font:12.5px/1.55 var(--mono)}
.entry{margin-bottom:12px}
.entry .cmd{color:var(--amber);word-break:break-all}
.entry .cmd .t{color:#5c6f63}
.entry .cmd .tag{color:var(--acc)}
.entry pre{white-space:pre-wrap;word-break:break-word;color:#c4d2c6;margin-top:5px;font-family:var(--mono)}
.entry.err .cmd{color:var(--bad)}
.pending{color:var(--amber);font-style:italic}
.emptylog{color:#5c6f63}
.vwrap{position:relative;background:#050705;border:1px solid #1c2b21;border-radius:10px;overflow:hidden;min-height:180px}
.vwrap img{display:block;width:100%;height:auto}
.vwrap.live{border-color:#2c5137;box-shadow:0 0 0 1px rgba(127,224,141,.25),0 0 30px rgba(127,224,141,.06)}
.vwrap.ctrl{cursor:crosshair;touch-action:none}
.nosignal{position:absolute;inset:0;display:none;align-items:center;justify-content:center;color:#5c6f63;
font:600 13px var(--mono);letter-spacing:.12em;text-transform:uppercase;
background:repeating-linear-gradient(45deg,#080c0a 0 12px,#0a100c 12px 24px)}
.dblhint{padding:8px;text-align:center;color:#5c6f63;font-size:11px;font-family:var(--mono)}
.ctrlbar{position:absolute;top:0;left:0;right:0;display:flex;justify-content:space-between;align-items:center;
background:rgba(127,224,141,.94);color:#08130b;font:800 12px 'Manrope',sans-serif;padding:8px 12px;z-index:3}
.ctrlbar button{background:rgba(0,0,0,.18);border:0;border-radius:6px;color:#08130b;font-weight:800;padding:3px 9px;cursor:pointer}
.btnrow{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
.hidden{display:none!important}
.kbwrap{margin-top:12px}
#mobKeys{width:100%;background:#0b110e;border:1px solid #253a2d;border-radius:8px;padding:12px;color:#e6efe8;font-size:15px;outline:none}
#mobKeys:focus{border-color:var(--acc)}
@media(max-width:920px){.log{max-height:300px}}
</style></head><body>
<header>
  <a class="back" href="/">← Устройства</a>
  <div class="ttl"><span id="dName">…</span><span id="dStatus" class="st off">…</span></div>
</header>
<main>
  <section class="sec">
    <div class="sec-title">Консоль команд</div>
    <div class="cmd-row">
      <select id="shell">
        <option value="cmd">CMD</option>
        <option value="ps">PowerShell</option>
        <option value="run">Run (Win+R)</option>
      </select>
      <input id="cmdInput" placeholder="команда…" autocomplete="off" spellcheck="false">
      <button id="runBtn" class="btn go">Выполнить</button>
    </div>
    <label class="chk"><input type="checkbox" id="asAdmin"> от имени администратора
      <span class="hint">(на устройстве появится окно UAC)</span></label>
    <div id="log" class="log"><div class="emptylog">Журнал пуст</div></div>
  </section>
  <section class="sec">
    <div class="sec-title">Трансляция экрана</div>
    <div id="vwrap" class="vwrap">
      <img id="scr" alt="">
      <div id="nosignal" class="nosignal">нет сигнала</div>
      <div id="ctrlbar" class="ctrlbar hidden"><span>Режим управления активен</span><button id="exitCtrl">выйти ✕</button></div>
    </div>
    <div class="dblhint">двойной клик по экрану — вкл/выкл управление мышью и клавиатурой</div>
    <div class="btnrow">
      <button id="streamBtn" class="btn"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="4" width="20" height="13" rx="2"/><path d="M8 21h8M12 17v4"/></svg><span>Трансляция</span></button>
      <button id="kbBtn" class="btn"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M6 14h.01M18 14h.01M9 14h6"/></svg><span>Клавиатура</span></button>
      <button id="mouseBtn" class="btn"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 4l7 17 2.5-7.5L21 11z"/></svg><span>Мышь</span></button>
    </div>
    <div id="ctrlTools" class="btnrow hidden">
      <button class="btn sm" data-mc="0">ЛКМ</button>
      <button class="btn sm" data-mc="2">ПКМ</button>
      <button class="btn sm" data-wh="-120">Скролл ↑</button>
      <button class="btn sm" data-wh="120">Скролл ↓</button>
      <button class="btn sm" id="escBtn">Esc</button>
    </div>
    <div id="kbWrap" class="kbwrap hidden">
      <input id="mobKeys" placeholder="Печатайте здесь — текст уйдёт на устройство (Enter = Enter)" autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false">
    </div>
  </section>
</main>
<script>
var DEV=decodeURIComponent(location.pathname.split('/').pop());
var $=function(id){return document.getElementById(id)};
var scr=$('scr'),vwrap=$('vwrap');
var S={w:0,h:0};
var streaming=false,wantStream=false,ctrlMode=false,lastSeq=-1;
function esc(s){return (s||'').replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
async function post(url,body){
  try{var r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});return await r.json();}
  catch(e){return null;}
}

/* ---- очередь событий ввода (отправка пачкой каждые 60 мс) ---- */
var buf=[];
function push(ev){ev.ts=Date.now();buf.push(ev);}
setInterval(function(){
  if(!buf.length)return;
  var events=buf.splice(0,buf.length);
  post('/api/input_push',{device_id:DEV,events:events});
},60);

/* ---- статус устройства ---- */
async function pollStatus(){
  var r;try{r=await(await fetch('/api/device/'+encodeURIComponent(DEV))).json();}catch(e){return;}
  if(r.error){$('dName').textContent=DEV;$('dStatus').textContent='offline';return;}
  $('dName').textContent=r.name||DEV;
  var st=$('dStatus');
  st.textContent=r.online?'онлайн':'офлайн';
  st.className='st '+(r.online?'on':'off');
  if(r.screen&&r.screen.w){S.w=r.screen.w;S.h=r.screen.h;}
  streaming=!!r.streaming;
  vwrap.classList.toggle('live',streaming);
  $('nosignal').style.display=(wantStream&&!streaming&&scr.hasAttribute('src'))?'flex':'none';
  if(r.result_seq!==lastSeq){lastSeq=r.result_seq;renderLog(r.results||[]);}
}
setInterval(pollStatus,2000);pollStatus();

function renderLog(items){
  if(!items.length){$('log').innerHTML='<div class="emptylog">Журнал пуст</div>';return;}
  $('log').innerHTML=items.map(function(r){
    var t=new Date(r.time*1000).toLocaleTimeString('ru-RU');
    var tag=(r.admin?'[ADMIN] ':'')+(r.shell||'').toUpperCase();
    var head='<span class="t">'+t+'</span> <span class="tag">'+tag+'</span> '+esc(r.cmd);
    var body=r.status==='sent'?'<span class="pending">… выполняется</span>':'<pre>'+esc(r.output||'(нет вывода)')+'</pre>';
    return '<div class="entry '+(r.status==='error'?'err':'')+'"><div class="cmd">'+head+'</div>'+body+'</div>';
  }).join('');
}

/* ---- команды ---- */
async function sendCmd(){
  var command=$('cmdInput').value.trim();
  if(!command)return;
  await post('/api/command',{device_id:DEV,type:'exec',shell:$('shell').value,command:command,admin:$('asAdmin').checked});
  $('cmdInput').value='';
  setTimeout(pollStatus,400);
}
 $('runBtn').onclick=sendCmd;
 $('cmdInput').addEventListener('keydown',function(e){if(e.key==='Enter')sendCmd();});

/* ---- трансляция ---- */
 $('streamBtn').onclick=async function(){
  if(!wantStream){
    wantStream=true;
    scr.src='/stream/'+encodeURIComponent(DEV)+'?t='+Date.now();
    await post('/api/command',{device_id:DEV,type:'stream_start',fps:6,quality:50,width:1400});
    this.querySelector('span').textContent='Остановить';
    this.classList.add('active');
  }else{
    wantStream=false;
    scr.removeAttribute('src');
    $('nosignal').style.display='none';
    await post('/api/command',{device_id:DEV,type:'stream_stop'});
    this.querySelector('span').textContent='Трансляция';
    this.classList.remove('active');
  }
};
scr.onerror=function(){if(wantStream)$('nosignal').style.display='flex';};

/* ---- режим управления ---- */
function setCtrl(on){
  ctrlMode=on;
  vwrap.classList.toggle('ctrl',on);
  $('ctrlbar').classList.toggle('hidden',!on);
  $('ctrlTools').classList.toggle('hidden',!on);
  $('mouseBtn').classList.toggle('active',on);
  $('mouseBtn').querySelector('span').textContent=on?'Мышь: вкл':'Мышь';
}
vwrap.addEventListener('dblclick',function(e){e.preventDefault();setCtrl(!ctrlMode);});
 $('exitCtrl').onclick=function(e){e.stopPropagation();setCtrl(false);};
 $('mouseBtn').onclick=function(){setCtrl(!ctrlMode);};

function toScreen(cx,cy){
  if(!S.w||!S.h)return null;
  var r=scr.getBoundingClientRect();
  if(!r.width)return null;
  var x=Math.round((cx-r.left)/r.width*S.w);
  var y=Math.round((cy-r.top)/r.height*S.h);
  x=Math.max(0,Math.min(S.w-1,x));y=Math.max(0,Math.min(S.h-1,y));
  return {x:x,y:y};
}

/* мышь (десктоп) */
var lastMove=0;
scr.addEventListener('mousemove',function(e){
  if(!ctrlMode)return;
  var now=Date.now();
  if(now-lastMove<40)return;
  lastMove=now;
  var p=toScreen(e.clientX,e.clientY);
  if(p)push({t:'mm',x:p.x,y:p.y});
});
scr.addEventListener('mousedown',function(e){
  if(!ctrlMode)return;
  e.preventDefault();
  var p=toScreen(e.clientX,e.clientY);if(!p)return;
  push({t:'md',x:p.x,y:p.y,b:e.button});
});
scr.addEventListener('mouseup',function(e){
  if(!ctrlMode)return;
  e.preventDefault();
  var p=toScreen(e.clientX,e.clientY);if(!p)return;
  push({t:'mu',x:p.x,y:p.y,b:e.button});
});
scr.addEventListener('contextmenu',function(e){if(ctrlMode)e.preventDefault();});
scr.addEventListener('wheel',function(e){
  if(!ctrlMode)return;
  e.preventDefault();
  push({t:'wh',dy:(e.deltaY>0?120:-120)});
},{passive:false});

/* клавиатура (десктоп) */
var VKMAP={Enter:13,Backspace:8,Tab:9,Escape:27,Space:32,CapsLock:20,Delete:46,Insert:45,Home:36,End:35,
PageUp:33,PageDown:34,ArrowLeft:37,ArrowUp:38,ArrowRight:39,ArrowDown:40,ShiftLeft:160,ShiftRight:161,
ControlLeft:162,ControlRight:163,AltLeft:164,AltRight:165,MetaLeft:91,MetaRight:92,ContextMenu:93,NumLock:144};
/* ShiftLeft=0xA0 и т.д. — виртуальные коды Windows */
VKMAP.ShiftLeft=0xA0;VKMAP.ShiftRight=0xA1;VKMAP.ControlLeft=0xA2;VKMAP.ControlRight=0xA3;
VKMAP.AltLeft=0xA4;VKMAP.AltRight=0xA5;VKMAP.MetaLeft=0x5B;VKMAP.MetaRight=0x5C;
function codeToVK(e){
  var c=e.code||'';
  if(VKMAP[c]!==undefined)return VKMAP[c];
  if(/^Key[A-Z]$/.test(c))return c.charCodeAt(3);
  if(/^Digit\\d$/.test(c))return c.charCodeAt(5);
  if(/^Numpad\\d$/.test(c))return 0x60+(+c.slice(6));
  var np={NumpadAdd:0x6B,NumpadSubtract:0x6D,NumpadMultiply:0x6A,NumpadDivide:0x6F,NumpadDecimal:0x6E,NumpadEnter:13};
  if(np[c])return np[c];
  if(/^F\\d{1,2}$/.test(c)){var n=+c.slice(1);if(n>=1&&n<=12)return 0x6F+n;}
  return 0;
}
function typing(){var a=document.activeElement;return a&&(a.tagName==='INPUT'||a.tagName==='TEXTAREA'||a.tagName==='SELECT');}
document.addEventListener('keydown',function(e){
  if(!ctrlMode||typing())return;
  if(e.key==='Escape'){setCtrl(false);return;}
  var vk=codeToVK(e);
  if(vk){e.preventDefault();push({t:'key',k:vk,u:0});}
});
document.addEventListener('keyup',function(e){
  if(!ctrlMode||typing())return;
  var vk=codeToVK(e);
  if(vk){e.preventDefault();push({t:'key',k:vk,u:1});}
});

/* сенсорное управление мышью (мобильные) */
var tStart=null,tMoved=false,lastTapT=0;
function touchPos(t){var p=toScreen(t.clientX,t.clientY);if(p)push({t:'mm',x:p.x,y:p.y});}
scr.addEventListener('touchstart',function(e){
  if(!ctrlMode)return;e.preventDefault();
  var t=e.touches[0];
  tStart={x:t.clientX,y:t.clientY,time:Date.now()};tMoved=false;
  touchPos(t);
},{passive:false});
scr.addEventListener('touchmove',function(e){
  if(!ctrlMode)return;e.preventDefault();
  var t=e.touches[0];
  if(tStart&&Math.hypot(t.clientX-tStart.x,t.clientY-tStart.y)>12)tMoved=true;
  touchPos(t);
},{passive:false});
scr.addEventListener('touchend',function(e){
  if(!ctrlMode||!tStart)return;e.preventDefault();
  var dt=Date.now()-tStart.time;
  if(!tMoved&&dt<350){
    var now=Date.now();
    var b=(now-lastTapT<400)?2:0;   /* одиночный тап = ЛКМ, двойной = ПКМ */
    lastTapT=now;
    push({t:'mc',b:b});
  }
  tStart=null;
},{passive:false});

/* кнопки-инструменты управления */
Array.prototype.forEach.call(document.querySelectorAll('[data-mc]'),function(b){
  b.onclick=function(){push({t:'mc',b:+b.getAttribute('data-mc')});};
});
Array.prototype.forEach.call(document.querySelectorAll('[data-wh]'),function(b){
  b.onclick=function(){push({t:'wh',dy:+b.getAttribute('data-wh')});};
});
 $('escBtn').onclick=function(){
  push({t:'key',k:27,u:0});
  setTimeout(function(){push({t:'key',k:27,u:1});},60);
};

/* мобильная клавиатура */
var prevVal='',mob=$('mobKeys');
 $('kbBtn').onclick=function(){
  $('kbWrap').classList.toggle('hidden');
  if(!$('kbWrap').classList.contains('hidden'))mob.focus();
};
mob.addEventListener('input',function(){
  var val=mob.value,common=0;
  while(common<prevVal.length&&common<val.length&&prevVal[common]===val[common])common++;
  var backs=prevVal.length-common;
  var added=val.slice(common);
  for(var i=0;i<backs;i++)push({t:'key',k:8});
  if(added)push({t:'text',s:added});
  prevVal=val;
});
mob.addEventListener('keydown',function(e){
  if(e.key==='Enter'){e.preventDefault();push({t:'key',k:13,u:0});setTimeout(function(){push({t:'key',k:13,u:1});},50);}
  else if(e.key==='Backspace'&&mob.value===''){push({t:'key',k:8,u:0});setTimeout(function(){push({t:'key',k:8,u:1});},50);}
});
</script>
</body></html>'''


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    try:
        from waitress import serve
        serve(app, host='0.0.0.0', port=port, threads=32)
    except ImportError:
        app.run(host='0.0.0.0', port=port, threaded=True)