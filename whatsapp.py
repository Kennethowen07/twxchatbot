"""Server webhook WhatsApp untuk asisten CS, dengan adapter penyedia (Meta Cloud API, YCloud, 360dialog).

Pilih penyedia lewat .env WA_PROVIDER = meta (default) | ycloud | 360dialog.
Jalankan:  .venv\\Scripts\\python.exe whatsapp.py
Lalu di terminal lain:  cloudflared tunnel --url http://localhost:8000
"""
import hashlib
import hmac
import json
import logging
import os
import queue
import re
import sys
import threading
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib import error, parse, request

import berkas
import bot

GRAPH_VERSION = "v25.0"
URL_YCLOUD = "https://api.ycloud.com/v2/whatsapp/messages/sendDirectly"
URL_360DIALOG = "https://waba-v2.360dialog.io/messages"
TOLERANSI_YCLOUD = 300
FILE_LOG = Path(__file__).parent / "logs" / "whatsapp.log"
BATAS_TEKS = 4096
BATAS_BODY = 1_000_000
BALASAN_NON_TEKS = berkas.isi("BALASAN_NON_TEKS")
DIABAIKAN = {"reaction", "system"}
PETUNJUK_ERROR = {
    190: "access token tidak valid atau kedaluwarsa, buat token baru",
    131047: "lewat jendela 24 jam sejak pesan terakhir customer",
    130497: "akun bisnis dibatasi mengirim ke negara ini",
    131030: "nomor penerima belum ada di daftar nomor tes",
}

KONFIG = {}
kunci_jawab = threading.Lock()
kunci_id = threading.Lock()
id_urut = deque(maxlen=1000)
id_terproses = set()
antrean = queue.Queue()


# ---------- event netral ----------

def pesan_masuk(id_pesan, wa_id, tipe, teks=""):
    return {"jenis": "pesan_masuk", "id": id_pesan, "wa_id": wa_id, "tipe": tipe, "teks": teks}


def echo_admin(wa_id, id_pesan=None):
    return {"jenis": "echo_admin", "wa_id": wa_id, "id": id_pesan}


def status(id_pesan, nilai, wa_id=None, kode=None, judul="", detail=""):
    return {"jenis": "status", "id": id_pesan, "status": nilai, "wa_id": wa_id,
            "error": {"kode": kode, "judul": judul, "detail": detail} if nilai == "failed" else None}


def nomor_wa(nomor):
    return (nomor or "").strip().lstrip("+")


def ke_int(nilai):
    try:
        return int(nilai)
    except (TypeError, ValueError):
        return nilai


# ---------- penyedia ----------

class GagalKirim(Exception):
    pass


class Meta:
    nama = "meta"
    wajib = ("WA_TOKEN", "WA_PHONE_NUMBER_ID", "WA_APP_SECRET", "WA_VERIFY_TOKEN")

    def verifikasi_get(self, q):
        token = q.get("hub.verify_token", [""])[0]
        if (q.get("hub.mode", [""])[0] == "subscribe"
                and hmac.compare_digest(token.encode(), KONFIG["WA_VERIFY_TOKEN"].encode())):
            logging.info("verifikasi webhook dari Meta berhasil")
            return 200, q.get("hub.challenge", [""])[0]
        logging.warning("verifikasi webhook ditolak")
        return 403, ""

    def request_sah(self, body, headers):
        return tanda_tangan_sah(body, headers.get("X-Hub-Signature-256"), KONFIG["WA_APP_SECRET"])

    def urai(self, data):
        hasil = []
        for entry in data.get("entry") or []:
            for change in entry.get("changes") or []:
                value = change.get("value") or {}
                for s in value.get("statuses") or []:
                    err = (s.get("errors") or [{}])[0]
                    hasil.append(status(s.get("id"), s.get("status"), s.get("recipient_id"), err.get("code"),
                                        err.get("title", ""), (err.get("error_data") or {}).get("details", "")))
                for m in value.get("messages") or []:
                    hasil.append(pesan_masuk(m.get("id"), nomor_wa(m.get("from")), m.get("type"),
                                             (m.get("text") or {}).get("body", "")))
                for m in value.get("message_echoes") or []:
                    hasil.append(echo_admin(nomor_wa(m.get("to")), m.get("id")))
        return hasil

    def payload_teks(self, wa_id, teks):
        return {"messaging_product": "whatsapp", "recipient_type": "individual", "to": wa_id,
                "type": "text", "text": {"preview_url": False, "body": teks}}

    def kirim(self, wa_id, teks):
        hasil = post_graph(self.payload_teks(wa_id, teks))
        return (hasil.get("messages") or [{}])[0].get("id", "-")


class Dialog360(Meta):
    nama = "360dialog"
    wajib = ("D360_API_KEY", "D360_WEBHOOK_TOKEN")

    def verifikasi_get(self, q):
        return 404, ""

    def request_sah(self, body, headers):
        harap = f"Bearer {KONFIG['D360_WEBHOOK_TOKEN']}"
        return hmac.compare_digest((headers.get("Authorization") or "").strip().encode(), harap.encode())

    def kirim(self, wa_id, teks):
        hasil = post_http(URL_360DIALOG, {"D360-API-KEY": KONFIG["D360_API_KEY"]}, self.payload_teks(wa_id, teks))
        return (hasil.get("messages") or [{}])[0].get("id", "-")


class YCloud:
    nama = "ycloud"
    wajib = ("YCLOUD_API_KEY", "YCLOUD_WEBHOOK_SECRET", "WA_BUSINESS_NUMBER")

    def verifikasi_get(self, q):
        return 404, ""

    def request_sah(self, body, headers, sekarang=None):
        bagian = [p.strip().split("=", 1) for p in (headers.get("YCloud-Signature") or "").split(",")]
        t = [v for k, v in (p for p in bagian if len(p) == 2) if k == "t"]
        s = [v for k, v in (p for p in bagian if len(p) == 2) if k == "s"]
        if len(t) != 1 or not t[0].isdecimal():
            return False
        if abs((sekarang or time.time()) - int(t[0])) > TOLERANSI_YCLOUD:
            return False
        hitung = hmac.new(KONFIG["YCLOUD_WEBHOOK_SECRET"].encode(), t[0].encode() + b"." + body, hashlib.sha256).hexdigest()
        return any(hmac.compare_digest(hitung.encode(), x.lower().encode()) for x in s)

    def urai(self, data):
        jenis = data.get("type")
        if jenis == "whatsapp.inbound_message.received":
            m = data.get("whatsappInboundMessage") or {}
            return [pesan_masuk(m.get("wamid") or m.get("id") or data.get("id"), nomor_wa(m.get("from")),
                                m.get("type"), (m.get("text") or {}).get("body", ""))]
        if jenis == "whatsapp.smb.message.echoes":
            m = data.get("whatsappMessage") or {}
            return [echo_admin(nomor_wa(m.get("to")), m.get("wamid") or m.get("id"))]
        if jenis == "whatsapp.message.updated":
            m = data.get("whatsappMessage") or {}
            api = m.get("whatsappApiError") or {}
            return [status(m.get("wamid") or m.get("id"), m.get("status"), nomor_wa(m.get("to")),
                           ke_int(api.get("code") or m.get("errorCode")), m.get("errorMessage", ""),
                           (api.get("error_data") or {}).get("details", ""))]
        logging.debug("event YCloud %s diabaikan", jenis)
        return []

    def kirim(self, wa_id, teks):
        payload = {"from": KONFIG["WA_BUSINESS_NUMBER"], "to": "+" + wa_id, "type": "text",
                   "text": {"body": teks, "preview_url": False}}
        hasil = post_http(URL_YCLOUD, {"X-API-Key": KONFIG["YCLOUD_API_KEY"]}, payload)
        if hasil.get("status") == "failed":
            api = hasil.get("whatsappApiError") or {}
            kode = ke_int(api.get("code") or hasil.get("errorCode"))
            logging.error("KIRIM GAGAL wa_id=%s kode=%s %s%s", wa_id, kode, hasil.get("errorMessage", ""),
                          f" ({PETUNJUK_ERROR[kode]})" if kode in PETUNJUK_ERROR else "")
            raise GagalKirim
        return hasil.get("wamid") or hasil.get("id", "-")


PENYEDIA_TERSEDIA = {p.nama: p for p in (Meta(), YCloud(), Dialog360())}
PENYEDIA = PENYEDIA_TERSEDIA["meta"]


# ---------- umum ----------

def baca_konfigurasi():
    nama = (os.environ.get("WA_PROVIDER") or "meta").strip().lower()
    if nama not in PENYEDIA_TERSEDIA:
        sys.exit(f"WA_PROVIDER '{nama}' tidak dikenal. Pilih meta, ycloud, atau 360dialog.")
    penyedia = PENYEDIA_TERSEDIA[nama]
    kosong = [k for k in penyedia.wajib if not os.environ.get(k, "").strip()]
    if kosong:
        sys.exit(f"Untuk WA_PROVIDER={nama}, variabel .env belum diisi: {', '.join(kosong)}. "
                 "Lihat contoh di .env.example.")
    konfig = {k: os.environ[k].strip() for k in penyedia.wajib}
    if "WA_BUSINESS_NUMBER" in konfig and not re.fullmatch(r"\+\d{8,15}", konfig["WA_BUSINESS_NUMBER"]):
        sys.exit("WA_BUSINESS_NUMBER harus format E.164, diawali + lalu angka saja, misalnya +6281234567890.")
    port = os.environ.get("WEBHOOK_PORT", "").strip() or "8000"
    if not port.isdecimal():
        sys.exit(f"WEBHOOK_PORT harus angka, tertulis '{port}'.")
    konfig.update(WA_PROVIDER=nama, WEBHOOK_PORT=int(port))
    return konfig


def atur_logging():
    FILE_LOG.parent.mkdir(exist_ok=True)
    format_log = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in (logging.StreamHandler(), logging.FileHandler(FILE_LOG, encoding="utf-8")):
        handler.setFormatter(format_log)
        root.addHandler(handler)
    for nama in ("httpx", "httpx2", "openai", "httpcore"):
        logging.getLogger(nama).setLevel(logging.WARNING)


def tanda_tangan_sah(body, header, secret):
    if not header or not header.startswith("sha256="):
        return False
    hitung = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(hitung.encode(), header[7:].strip().encode())


def sudah_diproses(id_pesan):
    with kunci_id:
        if id_pesan in id_terproses:
            return True
        if len(id_urut) == id_urut.maxlen:
            id_terproses.discard(id_urut[0])
        id_urut.append(id_pesan)
        id_terproses.add(id_pesan)
        return False


def sedang_diam(wa_id):
    oper = bot.waktu_oper.get(wa_id)
    return bool(oper and datetime.now(bot.WIB) - oper < bot.DURASI_DIAM)


def bagi_teks(teks, batas=BATAS_TEKS):
    bagian = []
    while len(teks) > batas:
        potong = max(teks.rfind("\n", 0, batas), teks.rfind(" ", 0, batas))
        potong = potong if potong > 0 else batas
        bagian.append(teks[:potong].rstrip())
        teks = teks[potong:].lstrip()
    return bagian + [teks] if teks else bagian


def post_http(url, headers, payload):
    req = request.Request(url, data=json.dumps(payload).encode(), method="POST",
                          headers={**headers, "Content-Type": "application/json"})
    with request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def post_graph(payload):
    url = f"https://graph.facebook.com/{GRAPH_VERSION}/{KONFIG['WA_PHONE_NUMBER_ID']}/messages"
    return post_http(url, {"Authorization": f"Bearer {KONFIG['WA_TOKEN']}"}, payload)


def catat_error_http(e):
    try:
        data = json.loads(e.read().decode("utf-8", "replace"))
    except (ValueError, OSError):
        data = {}
    err = data.get("error", {}) if isinstance(data, dict) else {}
    if not isinstance(err, dict):
        err = {"message": str(err)}
    api = err.get("whatsappApiError") or {}
    kode = ke_int(api.get("code") or err.get("code"))
    detail = (api.get("error_data") or err.get("error_data") or {}).get("details", "")
    logging.error("KIRIM GAGAL http=%s kode=%s %s %s%s", e.code, kode, err.get("message", ""), detail,
                  f" ({PETUNJUK_ERROR[kode]})" if kode in PETUNJUK_ERROR else "")


def kirim_teks(wa_id, teks):
    for bagian in bagi_teks(teks):
        try:
            id_kirim = PENYEDIA.kirim(wa_id, bagian)
        except error.HTTPError as e:
            catat_error_http(e)
            return False
        except (error.URLError, OSError) as e:
            logging.error("KIRIM GAGAL wa_id=%s, koneksi ke %s: %s", wa_id, PENYEDIA.nama, e)
            return False
        except GagalKirim:
            return False
        logging.info("KELUAR wa_id=%s id=%s: %s", wa_id, id_kirim, bagian)
    return True


def proses_pesan(ev):
    wa_id, id_pesan, tipe = ev["wa_id"], ev["id"], ev["tipe"]
    if not wa_id:
        return
    if id_pesan and sudah_diproses(id_pesan):
        logging.info("pesan duplikat diabaikan id=%s", id_pesan)
        return
    if tipe in DIABAIKAN:
        logging.info("MASUK wa_id=%s tipe %s, diabaikan", wa_id, tipe)
        return
    if tipe != "text":
        logging.info("MASUK wa_id=%s tipe %s", wa_id, tipe)
        with kunci_jawab:
            diam = sedang_diam(wa_id)
        if diam:
            logging.info("DIAM wa_id=%s, pesan %s tidak dibalas", wa_id, tipe)
        else:
            kirim_teks(wa_id, BALASAN_NON_TEKS)
        return
    teks = (ev["teks"] or "").strip()
    logging.info("MASUK wa_id=%s id=%s: %s", wa_id, id_pesan, teks)
    if not teks:
        return
    try:
        with kunci_jawab:
            balasan = bot.jawab(wa_id, teks)
    except Exception:
        logging.exception("JAWAB_GAGAL wa_id=%s, kirim pesan cadangan", wa_id)
        balasan = bot.PESAN_GANGGUAN
    if balasan is None:
        logging.info("DIAM wa_id=%s, tidak membalas", wa_id)
        return
    kirim_teks(wa_id, balasan)


def proses_echo_admin(ev):
    if not ev["wa_id"]:
        return
    logging.warning("ADMIN_MENGETIK wa_id=%s", ev["wa_id"])
    with kunci_jawab:
        bot.tandai_diam(ev["wa_id"], "admin mengetik di aplikasi")


def proses_status(ev):
    if ev["status"] == "failed":
        err = ev["error"] or {}
        kode = err.get("kode")
        logging.warning("STATUS failed wa_id=%s id=%s kode=%s %s %s%s", ev["wa_id"], ev["id"], kode,
                        err.get("judul", ""), err.get("detail", ""),
                        f" ({PETUNJUK_ERROR[kode]})" if kode in PETUNJUK_ERROR else "")
    else:
        logging.debug("STATUS %s id=%s", ev["status"], ev["id"])


PROSES = {"pesan_masuk": proses_pesan, "echo_admin": proses_echo_admin, "status": proses_status}


def proses_payload(body):
    try:
        data = json.loads(body)
    except ValueError:
        logging.warning("payload bukan JSON, diabaikan")
        return
    if not isinstance(data, dict):
        logging.warning("payload bukan objek JSON, diabaikan")
        return
    for ev in PENYEDIA.urai(data):
        try:
            PROSES[ev["jenis"]](ev)
        except Exception:
            logging.exception("gagal memproses %s id=%s", ev["jenis"], ev.get("id"))


def pekerja():
    while True:
        body = antrean.get()
        try:
            proses_payload(body)
        except Exception:
            logging.exception("gagal memproses payload")
        finally:
            antrean.task_done()


class Webhook(BaseHTTPRequestHandler):
    def balas(self, kode, teks=""):
        data = teks.encode()
        self.send_response(kode)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = parse.urlparse(self.path)
        if url.path != "/webhook":
            return self.balas(404)
        self.balas(*PENYEDIA.verifikasi_get(parse.parse_qs(url.query)))

    def do_POST(self):
        if parse.urlparse(self.path).path != "/webhook":
            return self.balas(404)
        panjang = int(self.headers.get("Content-Length") or 0)
        if panjang > BATAS_BODY:
            return self.balas(413)
        body = self.rfile.read(panjang)
        if not PENYEDIA.request_sah(body, self.headers):
            logging.warning("POST /webhook ditolak, tanda tangan tidak cocok (%s)", PENYEDIA.nama)
            return self.balas(403)
        self.balas(200)
        antrean.put(body)

    def log_message(self, format, *args):
        logging.debug("http %s", format % args)


def jalankan(port, host="127.0.0.1"):
    threading.Thread(target=pekerja, daemon=True).start()
    server = ThreadingHTTPServer((host, port), Webhook)
    return server


PETUNJUK_START = {
    "meta": "Di Meta (WhatsApp > Configuration), isi Callback URL https://<alamat>.trycloudflare.com/webhook, "
            "Verify token sama dengan WA_VERIFY_TOKEN, subscribe field messages (dan smb_message_echoes bila coexistence).",
    "ycloud": "Di YCloud Console > Developers > Webhook, tambah endpoint https://<alamat>.trycloudflare.com/webhook "
              "dengan event whatsapp.inbound_message.received, whatsapp.message.updated, whatsapp.smb.message.echoes, "
              "lalu salin signing secret ke YCLOUD_WEBHOOK_SECRET.",
    "360dialog": "Di 360dialog Hub > API Settings, isi webhook https://<alamat>.trycloudflare.com/webhook dengan header "
                 "Authorization: Bearer <isi D360_WEBHOOK_TOKEN>.",
}


if __name__ == "__main__":
    for aliran in (sys.stdout, sys.stderr):
        aliran.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    KONFIG.update(baca_konfigurasi())
    PENYEDIA = PENYEDIA_TERSEDIA[KONFIG["WA_PROVIDER"]]
    atur_logging()
    port = KONFIG["WEBHOOK_PORT"]
    server = jalankan(port)
    logging.info("server webhook mulai, port %s, penyedia %s, provider %s, model %s",
                 port, PENYEDIA.nama, bot.PROVIDER, bot.MODEL)
    print(f"Penyedia WA {PENYEDIA.nama}, provider LLM {bot.PROVIDER}, model {bot.MODEL}")
    print(f"Webhook jalan di http://localhost:{port}/webhook, log di {FILE_LOG}")
    print(f"Jalankan di terminal lain:  cloudflared tunnel --url http://localhost:{port}")
    print(PETUNJUK_START[PENYEDIA.nama] + " Alamat trycloudflare berubah tiap cloudflared dijalankan ulang. "
          "Ctrl+C untuk berhenti.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer berhenti.")
        server.server_close()
