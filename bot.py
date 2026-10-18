"""Asisten CS WhatsApp berbasis LLM: prompt, harga dan promo, oper ke admin, dan mode knowledge full/RAG.

Semua teks instruksi untuk LLM, daftar kata pemicu, dan link ada di prompt.md (privat) atau
prompt.example.md (contoh). Jalankan langsung untuk mode terminal:  python bot.py
"""
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from openai import OpenAI

import berkas


def muat_env(path):
    try:
        isi = Path(path).read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError):
        return
    for baris in isi:
        baris = baris.strip()
        if not baris or baris.startswith("#") or "=" not in baris:
            continue
        kunci, _, nilai = baris.partition("=")
        kunci, nilai = kunci.strip(), nilai.strip()
        if not kunci:
            continue
        if len(nilai) >= 2 and nilai[0] == nilai[-1] and nilai[0] in "\"'":
            nilai = nilai[1:-1]
        os.environ.setdefault(kunci, nilai)


def pilih_llm():
    provider = (os.environ.get("LLM_PROVIDER") or "gemini").strip().lower()
    if provider == "gemini":
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not api_key:
            sys.exit("GEMINI_API_KEY belum diisi. Isi di file .env (contoh di .env.example), "
                     "atau pakai LLM_PROVIDER=ollama.")
        base_url, model = "https://generativelanguage.googleapis.com/v1beta/openai/", "gemini-3.1-flash-lite"
    elif provider == "ollama":
        base_url, api_key, model = "http://localhost:11434/v1", "ollama", "qwen3:8b"
    else:
        sys.exit(f"LLM_PROVIDER '{provider}' tidak dikenal. Pilih gemini atau ollama.")
    return provider, base_url, api_key, os.environ.get("LLM_MODEL", "").strip() or model


muat_env(Path(__file__).parent / ".env")
PROVIDER, BASE_URL, API_KEY, MODEL = pilih_llm()
client = OpenAI(base_url=BASE_URL, api_key=API_KEY, timeout=30, max_retries=1)
MAX_HISTORY = 8
KNOWLEDGE = berkas.pilih("knowledge.md", "knowledge.example.md").read_text(encoding="utf-8")
FILE_HARGA_PROMO = berkas.pilih("harga_promo.txt", "harga_promo.example.txt")
WIB = timezone(timedelta(hours=7))
BULAN = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli",
         "Agustus", "September", "Oktober", "November", "Desember"]
HARGA_TIDAK_TERSEDIA = berkas.isi("HARGA_TIDAK_TERSEDIA")
PEMICU_PROMO = tuple(berkas.daftar("PEMICU_PROMO", ","))
POLA_MINTA = berkas.pola("POLA_MINTA")
POLA_HARGA = berkas.pola("POLA_HARGA")
POLA_INFO = berkas.pola("POLA_INFO")
RESET_HITUNGAN = timedelta(hours=12)
DISCORD_HARGA = berkas.isi("ARAHAN_INFO")
LINK_YOUTUBE = berkas.isi("LINK_YOUTUBE")
LINK_YOUTUBE_CEK = re.sub(r"^https?://(www\.)?", "", LINK_YOUTUBE)
SARAN_YOUTUBE = tuple(x for x in berkas.isi("SARAN_YOUTUBE", link=LINK_YOUTUBE).splitlines() if x.strip())
PESAN_GANGGUAN = berkas.isi("PESAN_GANGGUAN")
NAMA_BOT = berkas.isi("NAMA_BOT")
DURASI_DIAM = timedelta(hours=12)
JAM_ADMIN = tuple(int(x) for x in berkas.isi("JAM_ADMIN").split("-"))
PENANDA_OPER = re.compile(r"(\*{1,2})?\[\s*OPER\s*\](?(1)\1)", re.IGNORECASE)
JUDUL_DATA = berkas.isi("JUDUL_DATA")
JUDUL_HARGA = berkas.isi("JUDUL_HARGA")

SYSTEM_PROMPT = berkas.isi("SYSTEM_PROMPT")
SYSTEM_PROMPT_DASAR = SYSTEM_PROMPT
SYSTEM_PROMPT += f"\n\n{JUDUL_DATA}\n" + KNOWLEDGE

KNOWLEDGE_MODE = (os.environ.get("KNOWLEDGE_MODE") or "full").strip().lower()
if KNOWLEDGE_MODE not in ("full", "rag"):
    sys.exit(f"KNOWLEDGE_MODE '{KNOWLEDGE_MODE}' tidak dikenal. Pilih full atau rag.")
rag_terakhir = {}
token_terakhir = {}

riwayat = {}
waktu_oper = {}
hitung_minta = {}
pesan_terakhir = {}


def rupiah(angka):
    return "Rp " + f"{angka:,}".replace(",", ".")


def waktu_wib(waktu):
    return f"{waktu.day} {BULAN[waktu.month - 1]} {waktu.year} pukul {waktu:%H:%M} WIB"


def baca_harga_promo(path=FILE_HARGA_PROMO, sekarang=None, pesan="", status=None):
    sekarang = sekarang or datetime.now(WIB)
    status = {} if status is None else status
    status["kupon_aktif"] = None
    try:
        isi = Path(path).read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError) as e:
        logging.warning("harga_promo: file tidak bisa dibaca (%s)", e)
        return HARGA_TIDAK_TERSEDIA

    harga, kupon, seksi = {}, [], None
    for no, baris in enumerate(isi, 1):
        baris = baris.strip()
        if not baris or baris.startswith("#"):
            continue
        if baris.startswith("[") and baris.endswith("]"):
            seksi = baris[1:-1].strip().upper()
            continue
        kolom = [k.strip() for k in baris.split("|")]
        if seksi == "HARGA":
            if (len(kolom) != 2 or not kolom[0] or kolom[0] in harga
                    or not kolom[1].isdecimal() or int(kolom[1]) == 0):
                logging.warning("harga_promo baris %d: harga rusak: %s", no, baris)
                return HARGA_TIDAK_TERSEDIA
            harga[kolom[0]] = int(kolom[1])
        elif seksi == "KUPON":
            kupon.append((no, baris, kolom))
        else:
            logging.warning("harga_promo baris %d: di luar seksi [HARGA]/[KUPON], diabaikan: %s", no, baris)

    if not harga:
        logging.warning("harga_promo: seksi [HARGA] kosong atau tidak ada")
        return HARGA_TIDAK_TERSEDIA

    aktif, lewat = [], []
    for no, baris, kolom in kupon:
        if len(kolom) != 4 or not kolom[3]:
            logging.warning("harga_promo baris %d: kupon salah format atau tanpa waktu berakhir, diabaikan: %s", no, baris)
            continue
        kode, persen, paket, berakhir = kolom
        paket = [p.strip() for p in paket.split(",") if p.strip()]
        try:
            persen = float(persen.rstrip("%"))
            berakhir = datetime.strptime(berakhir, "%Y-%m-%d %H:%M").replace(tzinfo=WIB)
        except ValueError:
            logging.warning("harga_promo baris %d: persen atau waktu berakhir kupon salah, diabaikan: %s", no, baris)
            continue
        if not kode or not 0 < persen <= 100 or not paket or any(p not in harga for p in paket):
            logging.warning("harga_promo baris %d: kode, persen, atau paket kupon salah, diabaikan: %s", no, baris)
            continue
        (aktif if sekarang < berakhir else lewat).append((kode, persen, paket, berakhir))

    teks = [berkas.isi("HARGA_JUDUL")]
    teks += [berkas.isi("HARGA_BARIS", paket=p, harga=rupiah(h)) for p, h in harga.items()]
    status["kupon_aktif"] = bool(aktif)
    pesan = pesan.lower()
    promo = any(kata in pesan for kata in PEMICU_PROMO + tuple(k[0].lower() for k in aktif + lewat))
    discord = promo or any(p.search(pesan) for p in (POLA_HARGA, POLA_MINTA, POLA_INFO))
    if discord:
        teks.append("\n" + DISCORD_HARGA)
    if not promo:
        teks.append("\n" + berkas.isi("LARANGAN_PROMO_SELAIN_ARAHAN" if discord else "LARANGAN_PROMO"))
        return "\n".join(teks)
    if aktif:
        teks.append("\n" + berkas.isi("KUPON_AKTIF_JUDUL"))
        teks += [berkas.isi("KUPON_AKTIF_BARIS", kode=kode, persen=f"{persen:g}", paket=", ".join(paket),
                            berakhir=waktu_wib(b)) for kode, persen, paket, b in aktif]
        teks.append(berkas.isi("KUPON_AKTIF_ATURAN"))
    else:
        teks.append("\n" + berkas.isi("TANPA_PROMO"))
    kode_aktif = {k[0] for k in aktif}
    kode_lewat = list(dict.fromkeys(k[0] for k in lewat if k[0] not in kode_aktif))
    if kode_lewat:
        teks.append("\n" + berkas.isi("KUPON_LEWAT", kode=", ".join(kode_lewat)))
    teks.append(berkas.isi("KUPON_TAK_DIKENAL"))
    return "\n".join(teks)


def catat_permintaan(user_id, pesan, sekarang=None):
    sekarang = sekarang or datetime.now(WIB)
    terakhir = pesan_terakhir.get(user_id)
    if terakhir and sekarang - terakhir > RESET_HITUNGAN:
        hitung_minta.pop(user_id, None)
    pesan_terakhir[user_id] = sekarang
    minta = bool(POLA_MINTA.search(pesan))
    if minta:
        hitung_minta[user_id] = hitung_minta.get(user_id, 0) + 1
    return minta and hitung_minta[user_id] >= 2


def status_admin(sekarang=None):
    sekarang = (sekarang or datetime.now(WIB)).astimezone(WIB)
    buka, tutup = JAM_ADMIN
    jam = berkas.isi("JAM_ADMIN_TEKS", buka=f"{buka:02d}", tutup=f"{tutup:02d}")
    return berkas.isi("STATUS_ADMIN_BUKA" if buka <= sekarang.hour < tutup else "STATUS_ADMIN_TUTUP", jam=jam)


def buang_penanda(balasan):
    bersih = PENANDA_OPER.sub("", balasan)
    return "\n".join(b.rstrip() for b in bersih.splitlines()).strip(), bersih != balasan


def tandai_diam(user_id, alasan, sekarang=None):
    waktu_oper[user_id] = sekarang or datetime.now(WIB)
    logging.warning("DIAM_MULAI user_id=%s alasan=%s", user_id, alasan)


def prompt_dasar(user_id, pesan, chat):
    if KNOWLEDGE_MODE != "rag":
        return SYSTEM_PROMPT
    import rag
    sebelumnya = next((m["content"] for m in reversed(chat[:-1]) if m["role"] == "user"), "")
    try:
        hasil = rag.ambil(pesan, sebelumnya, KNOWLEDGE)
    except Exception as e:
        logging.error("RAG_GAGAL user_id=%s %s, pakai knowledge penuh", user_id, type(e).__name__)
        rag_terakhir[user_id] = {"mode": "fallback", "terpilih": [], "relevan": None, "ms": None}
        return SYSTEM_PROMPT
    logging.info("RAG_AMBIL user_id=%s %s relevan=%s %sms", user_id, rag.ringkas_skor(hasil["terpilih"]),
                 hasil["relevan"], hasil["ms"])
    rag_terakhir[user_id] = {"mode": "rag", "terpilih": hasil["terpilih"], "relevan": hasil["relevan"], "ms": hasil["ms"]}
    return SYSTEM_PROMPT_DASAR + f"\n\n{JUDUL_DATA}\n" + hasil["teks"]


def jawab(user_id, pesan, sekarang=None):
    sekarang = sekarang or datetime.now(WIB)
    oper = waktu_oper.get(user_id)
    if oper and sekarang - oper < DURASI_DIAM:
        logging.warning("DIAM user_id=%s, dioper %s", user_id, oper.strftime("%Y-%m-%d %H:%M"))
        return None

    youtube = catat_permintaan(user_id, pesan, sekarang)
    chat = riwayat.setdefault(user_id, [])
    chat.append({"role": "user", "content": pesan})

    status = {}
    prompt = (prompt_dasar(user_id, pesan, chat) + f"\n\n{JUDUL_HARGA}\n"
              + baca_harga_promo(pesan=pesan, status=status) + "\n\n" + status_admin(sekarang))
    messages = [{"role": "system", "content": prompt}] + chat[-MAX_HISTORY:]
    token_terakhir[user_id] = None
    try:
        resp = client.chat.completions.create(model=MODEL, messages=messages)
        pakai = getattr(resp, "usage", None)
        if pakai:
            token_terakhir[user_id] = (pakai.prompt_tokens, pakai.completion_tokens)
        isi = resp.choices[0].message.content or ""
        if not isi.strip():
            raise ValueError("balasan LLM kosong")
    except Exception as e:
        logging.error("LLM_GAGAL user_id=%s %s status=%s", user_id, type(e).__name__, getattr(e, "status_code", "-"))
        chat.pop()
        if POLA_MINTA.search(pesan):
            hitung_minta[user_id] -= 1
        return PESAN_GANGGUAN
    balasan, dioper = buang_penanda(isi)
    if dioper:
        waktu_oper[user_id] = sekarang
        logging.warning("OPER user_id=%s", user_id)
    elif youtube and status.get("kupon_aktif") is False and LINK_YOUTUBE_CEK not in balasan:
        balasan += "\n\n" + random.choice(SARAN_YOUTUBE)

    chat.append({"role": "assistant", "content": balasan})
    return balasan


if __name__ == "__main__":
    print(f"Provider {PROVIDER}, model {MODEL}")
    print(f"{NAMA_BOT} siap. Ketik 'keluar' untuk berhenti.")
    while True:
        pesan = input("Kamu > ").strip()
        if pesan.lower() == "keluar":
            break
        if not pesan:
            continue
        mulai = time.time()
        balasan = jawab("terminal", pesan)
        print(f"({NAMA_BOT} diam, chat sudah dioper ke admin)" if balasan is None else f"{NAMA_BOT} > {balasan}")
        print(f"({time.time() - mulai:.1f} detik)\n")
