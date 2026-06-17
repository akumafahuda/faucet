#!/usr/bin/env python3
"""
Popaja Faucet Multi-Account dengan Proxy (Proxyscrape)
- Thread per akun, semaphore untuk solver.
- Proxy pool di-refresh setiap 30 menit.
- Fallback jika proxy mati.
"""

import json
import time
import sys
import threading
import logging
from datetime import datetime
from typing import Dict, Any, List, Tuple, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import deque

import requests

# ========== KONFIGURASI ==========
FAUCET_BASE = "https://faucet.popaja.com"
SOLVER_BASE = "http://localhost:3000"
DEFAULT_REFERRER = "mafatifulh@gmail.com"
COOLDOWN_SECONDS = 600  # 10 menit
MAX_SOLVER_CONCURRENT = 5
LOG_FILE = "popaja.log"
ACCOUNTS_FILE = "accounts.txt"

# Proxy
PROXY_SOURCE = "https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&proxy_format=protocolipport&format=text&protocol=http"
PROXY_REFRESH_INTERVAL = 1800  # 30 menit
MAX_RETRY_WITH_PROXY = 3

# ========== SETUP LOGGING ==========
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("Popaja")

# ========== BROTLI DETECTION ==========
try:
    import brotli  # noqa
    HAS_BROTLI = True
except ImportError:
    HAS_BROTLI = False
    logger.warning("Brotli tidak terinstal. 'br' dihapus dari Accept-Encoding.")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Encoding": "gzip, deflate, br" if HAS_BROTLI else "gzip, deflate",
    "Connection": "keep-alive",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": FAUCET_BASE + "/",
}

SITEKEY = "0x4AAAAAADVtnPjW6RpchdBM"

# Semaphore untuk solver
solver_semaphore = threading.Semaphore(MAX_SOLVER_CONCURRENT)

# ========== PROXY MANAGER ==========
class ProxyManager:
    def __init__(self):
        self.proxies = deque()
        self.lock = threading.Lock()
        self.last_refresh = 0

    def refresh(self, force=False):
        """Ambil proxy dari proxyscrape, refresh jika sudah kadaluarsa atau force."""
        now = time.time()
        if not force and (now - self.last_refresh) < PROXY_REFRESH_INTERVAL:
            return
        logger.info("Mengambil daftar proxy dari proxyscrape...")
        try:
            resp = requests.get(PROXY_SOURCE, timeout=30)
            if resp.status_code == 200:
                lines = resp.text.strip().splitlines()
                proxies = [line.strip() for line in lines if line.strip()]
                if proxies:
                    with self.lock:
                        self.proxies = deque(proxies)
                        self.last_refresh = now
                    logger.info(f"Berhasil mengambil {len(proxies)} proxy.")
                    return
            logger.warning("Gagal mengambil proxy atau daftar kosong.")
        except Exception as e:
            logger.error(f"Error mengambil proxy: {e}")

    def get_proxy(self) -> Optional[str]:
        """Ambil satu proxy dari pool (round-robin)."""
        with self.lock:
            if not self.proxies:
                return None
            proxy = self.proxies.popleft()
            self.proxies.append(proxy)  # round-robin
            return proxy

    def mark_failed(self, proxy: str):
        """Proxy gagal, hapus dari pool."""
        with self.lock:
            if proxy in self.proxies:
                self.proxies.remove(proxy)
                logger.warning(f"Proxy {proxy} dihapus karena gagal.")

proxy_manager = ProxyManager()

# ========== POPAJA ACCOUNT ==========
class PopajaAccount:
    def __init__(self, email: str, referrer: str = DEFAULT_REFERRER):
        self.email = email
        self.referrer = referrer
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.last_claim_time = 0.0
        self.claims_today = 0
        self.lock = threading.Lock()
        self.proxy = None  # akan diisi saat init

    def _set_proxy(self):
        """Assign proxy ke session."""
        self.proxy = proxy_manager.get_proxy()
        if self.proxy:
            proxy_url = f"http://{self.proxy}"
            self.session.proxies = {
                "http": proxy_url,
                "https": proxy_url,
            }
            logger.debug(f"[{self.email}] Menggunakan proxy: {self.proxy}")
        else:
            self.session.proxies = {}
            logger.warning(f"[{self.email}] Tidak ada proxy, menggunakan direct.")

    def _request_json(self, method: str, url: str, **kwargs) -> Dict[str, Any]:
        """Request dengan retry jika proxy gagal."""
        for attempt in range(MAX_RETRY_WITH_PROXY + 1):
            if attempt > 0:
                # Ganti proxy
                logger.warning(f"[{self.email}] Ganti proxy (attempt {attempt})")
                self._set_proxy()
            try:
                resp = self.session.request(method, url, **kwargs)
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                try:
                    return resp.json()
                except json.JSONDecodeError as e:
                    snippet = resp.text[:200].replace('\n', ' ').replace('\r', '')
                    raise RuntimeError(f"JSON decode error: {e}\nSnippet: {snippet}")
            except (requests.exceptions.ProxyError, requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                logger.error(f"[{self.email}] Error dengan proxy {self.proxy}: {e}")
                if self.proxy:
                    proxy_manager.mark_failed(self.proxy)
                    self.proxy = None
                if attempt == MAX_RETRY_WITH_PROXY:
                    raise RuntimeError(f"Semua proxy gagal: {e}")
            except Exception as e:
                raise e  # non-proxy error, langsung raise
        raise RuntimeError("Gagal melakukan request setelah retry.")

    def _timestamp(self) -> str:
        return str(int(time.time() * 1000))

    def init_session(self):
        """Inisialisasi: ambil proxy, buka halaman, ambil stats."""
        self._set_proxy()
        url = f"{FAUCET_BASE}/?r={self.referrer}"
        # Gunakan GET biasa tanpa retry proxy? Kita pakai _request_json tapi untuk HTML, lebih baik pakai raw.
        resp = self.session.get(url, headers={"Accept": "text/html"})
        resp.raise_for_status()
        stats = self.get_user_stats()
        with self.lock:
            self.last_claim_time = stats.get("faucet", {}).get("last_claim", 0)
            self.claims_today = stats.get("faucet", {}).get("claims_today", 0)
        return stats

    def get_user_stats(self) -> Dict[str, Any]:
        url = f"{FAUCET_BASE}/api.php"
        params = {
            "action": "get_user_stats",
            "email": self.email,
            "t": self._timestamp(),
        }
        return self._request_json("GET", url, params=params)

    def solve_turnstile(self, url: str, sitekey: str) -> str:
        """Solver lokal (tidak pakai proxy karena localhost)."""
        with solver_semaphore:
            solve_payload = {"url": url, "sitekey": sitekey}
            resp = requests.post(
                f"{SOLVER_BASE}/solve",
                json=solve_payload,
                headers={"Content-Type": "application/json"},
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()
            job_id = data.get("id")
            if not job_id:
                raise RuntimeError("Solver tidak mengembalikan ID")

            while True:
                time.sleep(2)
                result_resp = requests.get(f"{SOLVER_BASE}/solve/result/{job_id}")
                if result_resp.status_code == 404:
                    raise RuntimeError("Job tidak ditemukan")
                result_resp.raise_for_status()
                result = result_resp.json()
                status = result.get("status")
                if status == "pending":
                    continue
                if status == "done":
                    token = result.get("token")
                    if token:
                        return token
                    raise RuntimeError("Token kosong")
                if status == "error":
                    error = result.get("error", "unknown error")
                    raise RuntimeError(f"Solver gagal: {error}")
                raise RuntimeError(f"Status tidak dikenal: {status}")

    def claim_instant(self, amount: str, currency: str, token: str) -> Dict[str, Any]:
        url = f"{FAUCET_BASE}/api.php"
        payload = {
            "action": "claim_instant",
            "amount": amount,
            "currency": currency,
            "referral_amount": "0",
            "referrer": self.referrer,
            "to": self.email,
            "turnstile_token": token,
        }
        headers = {"Content-Type": "application/json"}
        return self._request_json("POST", url, json=payload, headers=headers)

    def do_claim(self, amount: str, currency: str) -> Tuple[bool, str]:
        try:
            token = self.solve_turnstile(FAUCET_BASE + "/", SITEKEY)
            result = self.claim_instant(amount, currency, token)
            if result.get("status") == 200:
                with self.lock:
                    self.last_claim_time = time.time()
                    stats = self.get_user_stats()
                    self.claims_today = stats.get("faucet", {}).get("claims_today", 0)
                msg = f"Berhasil! Balance: {result.get('balance')} {currency}, Payout: {result.get('payout_id')}"
                return True, msg
            else:
                msg = result.get("message", "Gagal claim")
                if "wait 10 minutes" in msg.lower():
                    with self.lock:
                        self.last_claim_time = time.time()
                return False, msg
        except Exception as e:
            return False, str(e)

    def is_ready(self) -> bool:
        with self.lock:
            return (time.time() - self.last_claim_time) >= COOLDOWN_SECONDS

    def run_loop(self, amount: str, currency: str):
        logger.info(f"[{self.email}] Thread dimulai.")
        while True:
            while not self.is_ready():
                remaining = COOLDOWN_SECONDS - (time.time() - self.last_claim_time)
                if remaining > 0:
                    time.sleep(min(remaining, 30))
                else:
                    break

            logger.info(f"[{self.email}] Memulai claim...")
            success, msg = self.do_claim(amount, currency)
            if success:
                logger.info(f"[{self.email}] ✅ {msg}")
            else:
                logger.warning(f"[{self.email}] ❌ Gagal: {msg}")
            time.sleep(2)

# ========== LOAD ACCOUNTS ==========
def load_accounts(filename: str) -> List[PopajaAccount]:
    accounts = []
    try:
        with open(filename, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(",")
                email = parts[0].strip()
                referrer = parts[1].strip() if len(parts) > 1 else DEFAULT_REFERRER
                accounts.append(PopajaAccount(email, referrer))
    except FileNotFoundError:
        logger.error(f"File {filename} tidak ditemukan.")
        sys.exit(1)
    return accounts

# ========== MAIN ==========
def main():
    # Refresh proxy pertama
    proxy_manager.refresh(force=True)

    # Ambil pengaturan faucet
    logger.info("Mengambil pengaturan faucet...")
    temp_session = requests.Session()
    temp_session.headers.update(HEADERS)
    temp_url = f"{FAUCET_BASE}/api.php"
    resp = temp_session.get(temp_url, params={"action": "get_settings", "t": str(int(time.time()*1000))})
    resp.raise_for_status()
    settings = resp.json()
    currency = settings.get("admin_currency", "DGB")
    amount = "2000000"
    logger.info(f"Currency: {currency}, Amount: {amount}")

    # Load accounts
    accounts = load_accounts(ACCOUNTS_FILE)
    if not accounts:
        logger.error("Tidak ada akun ditemukan.")
        sys.exit(1)
    logger.info(f"Memuat {len(accounts)} akun. Referrer default: {DEFAULT_REFERRER}")

    # Inisialisasi sesi setiap akun
    for acc in accounts:
        try:
            stats = acc.init_session()
            logger.info(f"[{acc.email}] Inisialisasi OK. Terakhir claim: {datetime.fromtimestamp(acc.last_claim_time)}")
        except Exception as e:
            logger.error(f"[{acc.email}] Gagal inisialisasi: {e}")

    # Start thread per akun
    threads = []
    for acc in accounts:
        t = threading.Thread(target=acc.run_loop, args=(amount, currency), daemon=True)
        t.start()
        threads.append(t)
        time.sleep(0.2)

    # Thread untuk refresh proxy periodik
    def refresh_proxy_loop():
        while True:
            time.sleep(PROXY_REFRESH_INTERVAL)
            proxy_manager.refresh(force=True)

    refresh_thread = threading.Thread(target=refresh_proxy_loop, daemon=True)
    refresh_thread.start()

    logger.info("Semua thread berjalan. Tekan Ctrl+C untuk berhenti.")
    try:
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        logger.info("Dihentikan oleh user.")
        sys.exit(0)

if __name__ == "__main__":
    main()
