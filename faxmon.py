#!/usr/bin/env python3
# 팩스 실시간 모니터링 - 표준 라이브러리만 사용
import subprocess, json, re, time, os
from http.server import HTTPServer, BaseHTTPRequestHandler
 
FS_CLI = '/usr/local/freeswitch/bin/fs_cli'
FS_LOG = '/usr/local/freeswitch/var/log/freeswitch/freeswitch.log'
FAX_DIR = '/tmp/fax'
 
_prev = {'idle': 0, 'total': 0}
 
def fs_api(cmd):
    try:
        r = subprocess.run([FS_CLI,'-x',cmd], capture_output=True, text=True, timeout=5)
        return r.stdout.strip()
    except Exception:
        return ''
 
def channel_count():
    out = fs_api('show channels count')
    m = re.search(r'(\d+)\s+total', out)
    return int(m.group(1)) if m else 0
 
def fax_stats():
    sent = recv = 0
    try:
        with open(FS_LOG, 'r', errors='ignore') as f:
            data = f.read()
        sent = data.count('Fax successfully sent')
        recv = data.count('Fax successfully received')
    except Exception:
        pass
    return sent, recv
 
def rx_files():
    try:
        return len([x for x in os.listdir(FAX_DIR) if x.startswith('rx_') and x.endswith('.tif')])
    except Exception:
        return 0
 
def cpu_percent():
    global _prev
    try:
        with open('/proc/stat') as f:
            parts = f.readline().split()[1:]
        nums = list(map(int, parts))
        idle = nums[3] + nums[4]
        total = sum(nums)
        d_idle = idle - _prev['idle']
        d_total = total - _prev['total']
        _prev = {'idle': idle, 'total': total}
        if d_total <= 0: return 0
        return round(100 * (1 - d_idle / d_total), 1)
    except Exception:
        return 0
 
def mem_percent():
    try:
        info = {}
        with open('/proc/meminfo') as f:
            for line in f:
                k, v = line.split(':')
                info[k] = int(v.strip().split()[0])
        total = info['MemTotal']; avail = info.get('MemAvailable', info['MemFree'])
        return round(100 * (1 - avail / total), 1)
    except Exception:
        return 0
 
def snapshot():
    sent, recv = fax_stats()
    return {
        'time': time.strftime('%H:%M:%S'),
        'channels': channel_count(),
        'sent': sent, 'recv': recv, 'rx_files': rx_files(),
        'cpu': cpu_percent(), 'mem': mem_percent(),
    }
 
PAGE = '''<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Fax Monitor</title><style>
body{font-family:sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:24px}
h1{color:#60a5fa;font-size:22px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px;margin:20px 0}
.card{background:#1e293b;border-radius:12px;padding:20px;border-left:4px solid #3b82f6}
.card .label{color:#94a3b8;font-size:13px}
.card .value{font-size:34px;font-weight:bold;margin-top:6px}
.bar{height:8px;background:#334155;border-radius:4px;margin-top:10px;overflow:hidden}
.bar>i{display:block;height:100%;background:#3b82f6}
.time{color:#64748b;font-size:13px}
</style></head><body>
<h1>&#128225; FreeSWITCH 팩스 모니터</h1>
<div class="time">마지막 갱신: <span id="t">-</span> (2초마다 자동)</div>
<div class="grid">
  <div class="card"><div class="label">현재 활성 채널</div><div class="value" id="ch">0</div></div>
  <div class="card"><div class="label">발송 성공(누적)</div><div class="value" id="snt">0</div></div>
  <div class="card"><div class="label">수신 성공(누적)</div><div class="value" id="rcv">0</div></div>
  <div class="card"><div class="label">수신 파일 수</div><div class="value" id="rxf">0</div></div>
  <div class="card"><div class="label">CPU 사용률</div><div class="value" id="cpu">0%</div>
     <div class="bar"><i id="cpub" style="width:0%"></i></div></div>
  <div class="card"><div class="label">메모리 사용률</div><div class="value" id="mem">0%</div>
     <div class="bar"><i id="memb" style="width:0%"></i></div></div>
</div>
<script>
async function tick(){
  try{
    const r = await fetch('/api'); const d = await r.json();
    document.getElementById('t').textContent = d.time;
    document.getElementById('ch').textContent = d.channels;
    document.getElementById('snt').textContent = d.sent;
    document.getElementById('rcv').textContent = d.recv;
    document.getElementById('rxf').textContent = d.rx_files;
    document.getElementById('cpu').textContent = d.cpu + '%';
    document.getElementById('mem').textContent = d.mem + '%';
    document.getElementById('cpub').style.width = Math.min(d.cpu,100) + '%';
    document.getElementById('memb').style.width = Math.min(d.mem,100) + '%';
  }catch(e){}
}
setInterval(tick, 2000); tick();
</script></body></html>'''
 
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/api':
            body = json.dumps(snapshot()).encode()
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.end_headers(); self.wfile.write(body)
        else:
            self.send_response(200)
            self.send_header('Content-Type','text/html; charset=utf-8')
            self.end_headers(); self.wfile.write(PAGE.encode())
    def log_message(self, *a): pass
 
if __name__ == '__main__':
    cpu_percent()  # 첫 기준값 세팅
    print('Fax Monitor : http://<서버IP>:8090/')
    HTTPServer(('0.0.0.0', 8090), Handler).serve_forever()
