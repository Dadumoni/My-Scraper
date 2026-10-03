import os
import resource
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.getenv("PORT", "8000"))
PING_INTERVAL = int(os.getenv("PING_INTERVAL", "240"))  # seconds
RESCRAPE_MINUTES = int(os.getenv("RESCRAPE_MINUTES", "0"))  # 0 = scraper ek hi baar chalega
MAX_RESTARTS = int(os.getenv("MAX_RESTARTS", "5"))  # crash par max kitni baar dobara chalana
RESTART_DELAY = int(os.getenv("RESTART_DELAY", "60"))  # seconds
SCRAPER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scraper.py")


class HealthHandler(BaseHTTPRequestHandler):
    def _send(self, with_body):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", "2")
        self.end_headers()
        if with_body:
            self.wfile.write(b"ok")

    def do_GET(self):
        self._send(True)

    def do_HEAD(self):
        self._send(False)

    def log_message(self, *args):
        pass  # har ping ka log nahi chahiye


def get_self_url():
    url = os.getenv("SELF_URL")
    if url:
        return url
    domain = os.getenv("KOYEB_PUBLIC_DOMAIN")
    if domain:
        return f"https://{domain}/health"
    return None


def ping_loop(url):
    time.sleep(30)  # server start hone do
    while True:
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                print(f"[ping] {url} -> {resp.status}", flush=True)
        except Exception as e:
            print(f"[ping] failed: {e}", flush=True)
        time.sleep(PING_INTERVAL)


def run_scraper():
    print("[run] scraper.py start", flush=True)
    code = subprocess.call([sys.executable, SCRAPER_PATH])
    peak_mb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss // 1024
    print(f"[run] scraper.py finished, exit code: {code}, peak memory: {peak_mb} MB", flush=True)
    return code


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"[run] health server on port {PORT} (/health)", flush=True)

    url = get_self_url()
    if url:
        threading.Thread(target=ping_loop, args=(url,), daemon=True).start()
        print(f"[run] auto ping every {PING_INTERVAL}s -> {url}", flush=True)
    else:
        print("[run] SELF_URL / KOYEB_PUBLIC_DOMAIN nahi mila, auto ping band hai", flush=True)

    crashes = 0
    while True:
        code = run_scraper()
        if code != 0:
            crashes += 1
            if crashes > MAX_RESTARTS:
                print("[run] bahut baar crash hua, ab dobara nahi chalaunga", flush=True)
                break
            print(f"[run] {RESTART_DELAY}s baad dobara chalega ({crashes}/{MAX_RESTARTS})", flush=True)
            time.sleep(RESTART_DELAY)
            continue
        crashes = 0
        if RESCRAPE_MINUTES <= 0:
            break
        time.sleep(RESCRAPE_MINUTES * 60)

    # scraper khatam hone ke baad bhi service zinda rahe
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
