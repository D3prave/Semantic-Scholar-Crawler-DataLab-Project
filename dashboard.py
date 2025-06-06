import asyncio
import time
import subprocess
from collections import deque
import asyncpg
import asyncssh
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI()

#CONFIG
DB_HOST       = "localhost"
DB_NAME       = "crawler_test"
DB_USER       = "crawler_user"
DB_PASS       = "test123"

oracle_ram_cache   = None
oracle_ram_time    = 0
oracle2_ram_cache  = None
oracle2_ram_time   = 0
aws_ram_cache      = None
aws_ram_time       = 0

speed_history = deque(maxlen=30)

#Get RAM
async def get_remote_ram(host, key_path):
    try:
        async with asyncssh.connect(
            host,
            username="ubuntu",
            client_keys=[key_path],
            known_hosts=None
        ) as conn:
            res = await conn.run("top -bn1 | grep 'Mem'", check=True)
        parts = res.stdout.split(":", 1)[1].split(",")
        stats = {}
        for p in parts:
            toks = p.strip().split()
            if len(toks) >= 2:
                val, lbl = toks[0], toks[1]
                stats[lbl] = float(val)
        used  = int(stats.get("used", 0))
        total = int(stats.get("total", 0))
        pct   = round(used / total * 100) if total else 0
        return {"used": used, "total": total, "ram_percent": pct}
    except Exception as e:
        return {"error": str(e)}

async def remote_ram_background_updater():
    global oracle_ram_cache, oracle_ram_time
    global oracle2_ram_cache, oracle2_ram_time
    global aws_ram_cache, aws_ram_time

    while True:
        oracle_ram_cache,   oracle_ram_time   = await get_remote_ram("IP_1", "KEY1.key"),   time.time()
        oracle2_ram_cache, oracle2_ram_time = await get_remote_ram("IP2", "KEY2.key"), time.time()
        aws_ram_cache,     aws_ram_time      = await get_remote_ram("IP3",   "KEY3.pem"),     time.time()
        await asyncio.sleep(60)

async def speed_background_updater():
    global speed_history
    while True:
        try:
            conn = await asyncpg.connect(
                host=DB_HOST, user=DB_USER, password=DB_PASS, database=DB_NAME
            )
            count = await conn.fetchval("SELECT COUNT(*) FROM processed_papers")
            await conn.close()
            speed_history.append((time.time(), count))
        except Exception as e:
            print(f"[WARN] Speed tracker DB error: {e}")
        await asyncio.sleep(15)

def get_mac_memory_pressure():
    lvl = int(subprocess.check_output(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"]).strip())
    avail = int(subprocess.check_output(["sysctl", "-n", "kern.memorystatus_level"]).strip())
    state_map = {1: "Normal", 2: "Warning", 4: "Critical"}
    state = state_map.get(lvl, f"Unknown({lvl})")
    percent = 100 - avail
    return {"state": state, "pressure_percent": percent}

@app.get("/status")
async def status():
    try:
        conn = await asyncpg.connect(
            host=DB_HOST, user=DB_USER, password=DB_PASS, database=DB_NAME
        )
        processed_len = await conn.fetchval("SELECT COUNT(*) FROM processed_papers")
        citation_count = await conn.fetchval("SELECT COUNT(*) FROM citations")
        await conn.close()
    except Exception as e:
        print(f"[ERROR] DB fetch failed: {e}")
        processed_len = -1
        citation_count = -1

    now = time.time()
    recent = [(t, c) for t, c in speed_history if now - t <= 300]
    if len(recent) >= 2:
        t0, c0 = recent[0]
        t1, c1 = recent[-1]
        pps = (c1 - c0) / (t1 - t0) if (t1 - t0) > 0 else 0.0
    else:
        pps = 0.0

    mp = get_mac_memory_pressure()

    return JSONResponse({
        "processed":               processed_len,
        "citations":               citation_count,
        "memory_pressure":         mp["state"],
        "memory_pressure_percent": mp["pressure_percent"],
        "oracle_ram":              oracle_ram_cache or {},
        "oracle2_ram":             oracle2_ram_cache or {},
        "aws_ram":                 aws_ram_cache or {},
        "rate_papers_per_sec":     round(pps),
        "papers_per_hour":         round(pps * 3600),
        "time_per_1000":           round(1000/pps) if pps>0 else 0
    })

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    html = """
    <!DOCTYPE html>
    <html><head><title>Crawler Dashboard</title>
    <style>
      body   { font-family:sans-serif; margin:40px; background:#f6f6f6; color:#333 }
      .box  { background:#fff; padding:20px; margin:auto; width:640px;
              box-shadow:0 0 8px rgba(0,0,0,0.1); border-radius:8px }
      h1    { font-size:24px; margin-bottom:20px }
      hr    { border:0; border-top:1px solid #ccc; margin:20px 0 }
      .metric { font-size:18px; margin:10px 0; display:flex; align-items:center }
      .dot    { display:inline-block; width:12px; height:12px;
                border-radius:50%; margin-right:8px; background:gray; }
    </style>
    <script>
      async function fetchStatus() {
        const res = await fetch('/status');
        const d   = await res.json();

        document.getElementById('processed').textContent = d.processed.toLocaleString();
        document.getElementById('citations').textContent = d.citations.toLocaleString();

        document.getElementById('pressure').textContent =
          `${d.memory_pressure} (${d.memory_pressure_percent}%)`;
        const dot = document.getElementById('pressure_dot');
        const colorMap = { 'Normal': 'green', 'Warning': 'yellow', 'Critical': 'red' };
        dot.style.background = colorMap[d.memory_pressure] || 'gray';

        const pad = o => o.error
          ? `Error: ${o.error}`
          : `${o.used} / ${o.total} MB (${o.ram_percent}%)`;
        document.getElementById('oracle_ram').textContent  = pad(d.oracle_ram);
        document.getElementById('oracle2_ram').textContent = pad(d.oracle2_ram);
        document.getElementById('aws_ram').textContent     = pad(d.aws_ram);

        document.getElementById('pps').textContent   = `${d.rate_papers_per_sec} papers/sec`;
        document.getElementById('pph').textContent   = `${d.papers_per_hour.toLocaleString()} papers/hour`;
        document.getElementById('t1000').textContent = `${d.time_per_1000} sec/1000`;
      }
      window.onload = () => { fetchStatus(); setInterval(fetchStatus, 15000); };
    </script>
    </head><body>
      <div class="box">
        <h1>Crawler Dashboard</h1>
        <div class="metric">✅ Processed: <strong id="processed">…</strong></div>
        <div class="metric">🔗 Citations: <strong id="citations">…</strong></div>
        <hr>
        <div class="metric">
          📊 Memory Pressure: <strong id="pressure">…</strong>
          <span id="pressure_dot" class="dot"></span>
        </div>
        <div class="metric">🧠 Oracle #1 RAM: <strong id="oracle_ram">…</strong></div>
        <div class="metric">🧠 Oracle #2 RAM: <strong id="oracle2_ram">…</strong></div>
        <div class="metric">🧠 AWS #1 RAM: <strong id="aws_ram">…</strong></div>
        <hr>
        <div class="metric">⚡ Papers/sec: <strong id="pps">…</strong></div>
        <div class="metric">📈 Papers/hour: <strong id="pph">…</strong></div>
        <div class="metric">🕒 Time/1000 papers: <strong id="t1000">…</strong></div>
      </div>
    </body></html>
    """
    return HTMLResponse(html)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(remote_ram_background_updater())
    asyncio.create_task(speed_background_updater())
