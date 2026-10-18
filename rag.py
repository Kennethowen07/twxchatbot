"""Mode RAG eksperimen untuk asisten (KNOWLEDGE_MODE=rag).

Memecah file knowledge jadi potongan, membuat embedding lewat Gemini API (gemini-embedding-001),
menyimpan indeks di rag/index.json, lalu mengambil potongan relevan dengan skor hybrid
(cosine vektor + BM25 kata). Tanpa dependensi tambahan.
"""
import hashlib
import json
import logging
import math
import os
import re
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib import error, request

import berkas

FOLDER_INDEKS = Path(__file__).parent / "rag"
FILE_INDEKS = FOLDER_INDEKS / "index.json"
URL_EMBED = "https://generativelanguage.googleapis.com/v1beta/models/{model}:batchEmbedContents"
VERSI_POTONG = 1
DIMENSI = 768
BATAS_BATCH = 100
TIMEOUT_EMBED = 15
BOBOT_VEKTOR, BOBOT_KATA = 0.7, 0.3
BM25_K1, BM25_B = 1.5, 0.75
JUDUL_PINNED = berkas.isi("RAG_JUDUL_PINNED")
BAGIAN_BERSAMA = set(berkas.daftar("RAG_BAGIAN_BERSAMA"))
KATA_BUANG = set("""yg yang dan di ke dari untuk dengan ini itu ada apa aja ya kak kakak min admin sy saya aku
gak ga nggak tidak bisa mau dong sih deh nih kok kalo kalau atau juga lagi udah sudah nya the is a""".split())
TIDAK_RELEVAN = berkas.isi("RAG_TIDAK_RELEVAN")


def konfigurasi():
    k = os.environ.get("RAG_TOP_K", "").strip() or "4"
    skor = os.environ.get("RAG_MIN_SKOR", "").strip() or "0.55"
    return {"model": os.environ.get("EMBEDDING_MODEL", "").strip() or "gemini-embedding-001",
            "top_k": int(k) if k.isdecimal() and int(k) > 0 else 4,
            "min_skor": float(skor) if re.fullmatch(r"\d*\.?\d+", skor) else 0.55,
            "api_key": os.environ.get("GEMINI_API_KEY", "").strip()}


# ---------- chunking ----------

def slug(teks):
    teks = teks.lower().replace("+", " plus ")
    return re.sub(r"[^a-z0-9]+", "-", teks).strip("-")


def judul_blok(paragraf):
    baris = paragraf.splitlines()
    pertama = baris[0].strip()
    return (len(baris) > 1 and len(pertama) <= 60 and not re.match(r"^(\d+\.|-)\s", pertama)
            and not pertama.endswith((".", ",", ":")) and pertama)


def pecah_bagian(judul, isi):
    paragraf = [p.strip() for p in re.split(r"\n\s*\n", isi) if p.strip()]
    berjudul = [p for p in paragraf if judul_blok(p)]
    if len(berjudul) < 2:
        return [(judul, "\n\n".join(paragraf))]
    pengantar, blok, bersama = [], [], []
    for p in paragraf:
        if judul_blok(p):
            blok.append([p.splitlines()[0].strip(), p])
        elif not blok:
            pengantar.append(p)
        elif judul in BAGIAN_BERSAMA:
            bersama.append(p)
        else:
            blok[-1][1] += "\n\n" + p
    return [(f"{judul} > {j}", "\n\n".join(pengantar + [teks] + bersama)) for j, teks in blok]


def potong(teks_md):
    bagian, h2, judul, isi, awal = [], None, None, [], []
    def simpan():
        if judul and "\n".join(isi).strip():
            bagian.append((judul, "\n".join(isi).strip()))
    for baris in teks_md.splitlines():
        if baris.startswith("# "):
            continue
        if baris.startswith("## ") or baris.startswith("### "):
            simpan()
            if baris.startswith("## "):
                h2 = baris[3:].strip()
                judul = h2
            else:
                judul = f"{h2} > {baris[4:].strip()}"
            isi = []
        elif judul is None:
            awal.append(baris)
        else:
            isi.append(baris)
    simpan()
    hasil = []
    for judul, teks in bagian:
        pinned = judul.startswith(JUDUL_PINNED)
        if pinned and "\n".join(awal).strip():
            teks = "\n".join(awal).strip() + "\n\n" + teks
        for j, t in ([(judul, teks)] if pinned else pecah_bagian(judul, teks)):
            hasil.append({"id": "/".join(slug(x) for x in j.split(" > ")), "judul": j, "teks": t, "pinned": pinned})
    return hasil


# ---------- embedding ----------

def normalisasi(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def embed(teks, tugas, judul=None, model=None, api_key=None, dimensi=DIMENSI):
    konfig = konfigurasi()
    model, api_key = model or konfig["model"], api_key or konfig["api_key"]
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY kosong")
    hasil = []
    for mulai in range(0, len(teks), BATAS_BATCH):
        req = []
        for i, t in enumerate(teks[mulai:mulai + BATAS_BATCH], mulai):
            # Field level atas (taskType/title/outputDimensionality) dipakai sengaja: di batchEmbedContents,
            # embedContentConfig ternyata diabaikan (hasil 3072 dimensi, query == dokumen), dicek 18 Okt 2026.
            item = {"model": f"models/{model}", "content": {"parts": [{"text": t}]},
                    "taskType": tugas, "outputDimensionality": dimensi}
            if judul and judul[i]:
                item["title"] = judul[i]
            req.append(item)
        data = json.dumps({"requests": req}).encode()
        r = request.Request(URL_EMBED.format(model=model), data=data, method="POST",
                            headers={"x-goog-api-key": api_key, "Content-Type": "application/json"})
        with request.urlopen(r, timeout=TIMEOUT_EMBED) as resp:
            hasil += [normalisasi(e["values"]) for e in json.load(resp)["embeddings"]]
    return hasil


# ---------- indeks ----------

_cache = {}


def hash_teks(teks):
    return hashlib.sha256(teks.encode("utf-8")).hexdigest()


def bangun_indeks(teks_md, model, dimensi, fungsi_embed=None):
    fungsi_embed = fungsi_embed or embed
    potongan = potong(teks_md)
    isi = [p for p in potongan if not p["pinned"]]
    vektor = fungsi_embed([p["teks"] for p in isi], "RETRIEVAL_DOCUMENT", [p["judul"] for p in isi],
                          model=model, dimensi=dimensi)
    for p, v in zip(isi, vektor):
        p["vektor"] = [round(x, 6) for x in v]
    return {"model": model, "dimensi": dimensi, "versi_potong": VERSI_POTONG, "hash": hash_teks(teks_md),
            "dibuat": datetime.now().isoformat(timespec="seconds"), "potongan": potongan}


def muat_indeks(teks_md, model=None, dimensi=DIMENSI, path=None, fungsi_embed=None):
    model, path = model or konfigurasi()["model"], Path(path or FILE_INDEKS)
    kunci = (hash_teks(teks_md), model, dimensi, VERSI_POTONG, str(path))
    if _cache.get("kunci") == kunci:
        return _cache["indeks"]
    try:
        indeks = json.loads(path.read_text(encoding="utf-8"))
        cocok = ((indeks.get("hash"), indeks.get("model"), indeks.get("dimensi"), indeks.get("versi_potong")) == kunci[:4]
                 and all(len(p["vektor"]) == dimensi for p in indeks["potongan"] if not p["pinned"]))
    except (OSError, ValueError, KeyError, TypeError):
        indeks, cocok = None, False
    if not cocok:
        logging.warning("RAG_INDEKS dibangun ulang (%s)", "file tidak ada/rusak" if indeks is None else "hash/model/dimensi berubah")
        indeks = bangun_indeks(teks_md, model, dimensi, fungsi_embed)
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(indeks, ensure_ascii=False), encoding="utf-8")
    _cache.update(kunci=kunci, indeks=indeks)
    return indeks


# ---------- retrieval ----------

def token(teks):
    return [t for t in re.findall(r"[a-z0-9]+", teks.lower()) if t not in KATA_BUANG]


def bm25(query, dokumen):
    dok = [token(d) for d in dokumen]
    n, rata = len(dok), (sum(map(len, dok)) / len(dok) if dok else 0) or 1
    df = Counter(t for d in dok for t in set(d))
    skor = []
    for d in dok:
        tf, s = Counter(d), 0.0
        for t in set(token(query)):
            if tf[t]:
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                s += idf * tf[t] * (BM25_K1 + 1) / (tf[t] + BM25_K1 * (1 - BM25_B + BM25_B * len(d) / rata))
        skor.append(s)
    return skor


def peringkat(query, vektor_query, indeks, hybrid=True):
    isi = [p for p in indeks["potongan"] if not p["pinned"]]
    kata = bm25(query, [p["judul"] + "\n" + p["teks"] for p in isi])
    puncak = max(kata, default=0) or 1
    hasil = []
    for p, k in zip(isi, kata):
        v = sum(a * b for a, b in zip(vektor_query, p["vektor"]))
        k = k / puncak
        hasil.append({"id": p["id"], "vektor": v, "kata": k,
                      "gabung": BOBOT_VEKTOR * v + BOBOT_KATA * k if hybrid else v})
    return sorted(hasil, key=lambda x: x["gabung"], reverse=True)


def ambil(pesan, sebelumnya, teks_md, top_k=None, min_skor=None, hybrid=True, fungsi_embed=None, path=None):
    """Mengembalikan dict berisi teks data knowledge terpilih dan info skor. Melempar error kalau embedding gagal."""
    konfig = konfigurasi()
    top_k = top_k or konfig["top_k"]
    min_skor = konfig["min_skor"] if min_skor is None else min_skor
    mulai = time.time()
    indeks = muat_indeks(teks_md, path=path, fungsi_embed=fungsi_embed)
    query = "\n".join(x for x in (pesan, sebelumnya) if x)
    vektor_query = (fungsi_embed or embed)([query], "RETRIEVAL_QUERY", model=indeks["model"], dimensi=indeks["dimensi"])[0]
    urutan = peringkat(query, vektor_query, indeks, hybrid)
    relevan = bool(urutan) and max(x["vektor"] for x in urutan) >= min_skor
    terpilih = urutan[:top_k] if relevan else []
    per_id = {p["id"]: p for p in indeks["potongan"]}
    bagian = [f"## {per_id[x['id']]['judul']}\n{per_id[x['id']]['teks']}" for x in terpilih]
    bagian += [f"## {p['judul']}\n{p['teks']}" for p in indeks["potongan"] if p["pinned"]]
    if not relevan:
        bagian.append(TIDAK_RELEVAN)
    return {"teks": "\n\n".join(bagian), "terpilih": terpilih, "urutan": urutan, "relevan": relevan,
            "ms": round((time.time() - mulai) * 1000)}


def ringkas_skor(terpilih):
    return "[" + ", ".join(f"{x['id']}:{x['vektor']:.3f}/{x['kata']:.2f}/{x['gabung']:.3f}" for x in terpilih) + "]"
