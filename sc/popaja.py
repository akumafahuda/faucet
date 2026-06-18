import json
import time
import sys
import threading
import logging
from collections import deque
import requests

BASE = "https://faucet.popaja.com"
SOLVER = "http://localhost:3000"
DEFAULT_REF = "mafatifulh@gmail.com"
COOLDOWN = 605
MAX_SOLVER = 5
SITEKEY = "0x4AAAAAADVtnPjW6RpchdBM"
TIMEOUT = 10

SOURCES = [
    "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&proxy_format=ipport&format=text&protocol=all",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks4.txt",
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt"
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)

solver_sem = threading.Semaphore(MAX_SOLVER)

class ProxyPool:
    def __init__(self):
        self.pool = deque()
        self.lock = threading.Lock()
        self.last_update = 0

    def refresh(self):
        now = time.time()
        if now - self.last_update < 600:
            return
        
        new_proxies = set()
        for src in SOURCES:
            try:
                r = requests.get(src, timeout=15)
                if r.status_code == 200:
                    proto = "socks5" if "socks5" in src else "socks4" if "socks4" in src else "http"
                    for line in r.text.strip().splitlines():
                        line = line.strip()
                        if line and ":" in line:
                            new_proxies.add(f"{proto}://{line}")
            except Exception:
                continue
                
        if new_proxies:
            with self.lock:
                self.pool = deque([{"http": p, "https": p} for p in new_proxies])
                self.last_update = now
            logging.info(f"Proxy pool updated: {len(new_proxies)} active proxies.")

    def get(self):
        with self.lock:
            if not self.pool:
                return None
            p = self.pool.popleft()
            self.pool.append(p)
            return p

    def remove(self, proxy):
        with self.lock:
            if proxy in self.pool:
                self.pool.remove(proxy)

pool = ProxyPool()

class Worker:
    def __init__(self, email, ref):
        self.email = email
        self.ref = ref
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{BASE}/",
        })
        self.proxy = None
        self.last_claim = 0
        self.initialized = False
        self.rotate()

    def rotate(self):
        if self.proxy:
            pool.remove(self.proxy)
        self.proxy = pool.get()
        if self.proxy:
            self.session.proxies.update(self.proxy)
        else:
            self.session.proxies.clear()

    def request(self, method, url, parse_json=True, **kwargs):
        t_out = kwargs.pop("timeout", TIMEOUT)
        for _ in range(5):
            try:
                target = requests if "localhost" in url or "127.0.0.1" in url else self.session
                r = target.request(method, url, timeout=t_out, **kwargs)
                if r.status_code in [200, 202]:
                    return r.json() if parse_json else r
                raise Exception(f"HTTP {r.status_code}")
            except Exception as e:
                if "localhost" not in url:
                    self.rotate()
                else:
                    raise e
        raise Exception("Max retries reached")

    def init(self):
        try:
            self.request("GET", f"{BASE}/?r={self.ref}", parse_json=False)
            t = str(int(time.time() * 1000))
            res = self.request("GET", f"{BASE}/api.php", params={"action": "get_user_stats", "email": self.email, "t": t})
            
            srv_val = res.get("faucet", {}).get("last_claim", 0)
            if srv_val:
                val = float(srv_val)
                if val > 999999999999:
                    val /= 1000
                self.last_claim = val
            else:
                self.last_claim = 0
            return True
        except Exception:
            return False

    def solve(self):
        with solver_sem:
            px_str = self.proxy.get("http") if self.proxy else None
            payload = {"url": f"{BASE}/", "sitekey": SITEKEY, "proxy": px_str}
            res = self.request("POST", f"{SOLVER}/solve", json=payload, timeout=90)
            jid = res.get("id")
            if not jid:
                raise Exception("Empty solver ID")
            
            while True:
                time.sleep(3)
                status_res = self.request("GET", f"{SOLVER}/solve/result/{jid}", timeout=10)
                status = status_res.get("status")
                if status == "pending":
                    continue
                if status == "done":
                    token = status_res.get("token")
                    if token:
                        return token
                    raise Exception("Empty token")
                raise Exception(f"Solver error: {status_res.get('error', 'unknown')}")

    def claim(self, amount, currency):
        try:
            token = self.solve()
            payload = {
                "action": "claim_instant",
                "amount": amount,
                "currency": currency,
                "referral_amount": "0",
                "referrer": self.ref,
                "to": self.email,
                "turnstile_token": token,
            }
            res = self.request("POST", f"{BASE}/api.php", json=payload)
            if res.get("status") == 200:
                self.last_claim = time.time()
                logging.info(f"[{self.email}] Claim success. Balance: {res.get('balance')} {currency}")
                return True
            
            msg = res.get("message", "Unknown error")
            if "wait 10 minutes" in msg.lower():
                self.last_claim = time.time()
            else:
                self.rotate()
            logging.error(f"[{self.email}] Claim rejected: {msg}")
            return False
        except Exception as e:
            logging.error(f"[{self.email}] Claim crash: {e}")
            self.rotate()
            return False

    def run(self, amount, currency):
        while True:
            if not self.initialized:
                if not self.init():
                    time.sleep(5)
                    continue
                self.initialized = True
                logging.info(f"[{self.email}] Account initialized successfully.")

            if self.last_claim == 0:
                elapsed = COOLDOWN + 1
            else:
                elapsed = time.time() - self.last_claim

            if elapsed < COOLDOWN:
                time.sleep(min(COOLDOWN - elapsed, 30))
                continue
            
            logging.info(f"[{self.email}] Triggering claim process...")
            self.claim(amount, currency)
            time.sleep(5)

def load_accs():
    accs = []
    try:
        with open("accounts.txt", "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(",")
                email = parts[0].strip()
                ref = parts[1].strip() if len(parts) > 1 else DEFAULT_REF
                accs.append(Worker(email, ref))
    except FileNotFoundError:
        sys.exit(1)
    return accs

def proxy_loop():
    while True:
        time.sleep(600)
        pool.refresh()

def main():
    pool.refresh()
    
    try:
        r = requests.get(f"{BASE}/api.php?action=get_settings", timeout=15)
        currency = r.json().get("admin_currency", "DGB")
    except Exception:
        currency = "DGB"
    amount = "2000000"
    
    workers = load_accs()
    if not workers:
        sys.exit(1)
        
    threading.Thread(target=proxy_loop, daemon=True).start()
    
    for w in workers:
        threading.Thread(target=w.run, args=(amount, currency), daemon=True).start()
        time.sleep(0.2)
            
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        sys.exit(0)

if __name__ == "__main__":
    main()
