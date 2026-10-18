"""Pemuat file privat (prompt, knowledge, harga) dengan fallback ke file contoh.

File privat tidak ikut repo. Kalau tidak ada, dipakai pasangan *.example dan dicatat WARNING.

Format prompt.md: teks bebas di atas bagian pertama diabaikan (dokumentasi). Setiap bagian diawali
baris `## NAMA_BAGIAN` (huruf besar, angka, garis bawah), isinya sampai bagian berikutnya. Baris kosong
di awal dan akhir isi dibuang, selebihnya dipakai persis. Placeholder ditulis {nama} dan diisi kode
lewat isi(nama, nama=nilai). Daftar bagian dan placeholdernya ada di prompt.example.md.
"""
import logging
import re
from pathlib import Path

FOLDER = Path(__file__).parent
FILE_PROMPT = ("prompt.md", "prompt.example.md")
_bagian = {}


def pilih(asli, contoh, folder=FOLDER):
    """Path file privat kalau ada, kalau tidak path file contoh (dengan WARNING)."""
    asli, contoh = Path(folder) / asli, Path(folder) / contoh
    if asli.exists() or not contoh.exists():
        return asli
    logging.warning("memakai file contoh %s karena %s tidak ada", contoh.name, asli.name)
    return contoh


def urai(teks):
    hasil, nama, isi = {}, None, []
    for baris in teks.splitlines():
        cocok = re.fullmatch(r"## ([A-Z0-9_]+)[ \t]*", baris)
        if cocok:
            if nama:
                hasil[nama] = "\n".join(isi).strip("\n")
            nama, isi = cocok.group(1), []
        elif nama:
            isi.append(baris)
    if nama:
        hasil[nama] = "\n".join(isi).strip("\n")
    return hasil


def bagian(path=None):
    if path or not _bagian:
        path = Path(path) if path else pilih(*FILE_PROMPT)
        _bagian.clear()
        _bagian.update(urai(path.read_text(encoding="utf-8")))
    return _bagian


def isi(nama, **ganti):
    teks = bagian()[nama]
    for kunci, nilai in ganti.items():
        teks = teks.replace("{" + kunci + "}", str(nilai))
    return teks


def daftar(nama, pemisah=None):
    """Bagian berisi daftar: satu item per baris, atau dipisah `pemisah` kalau diberikan."""
    teks = isi(nama)
    item = teks.split(pemisah) if pemisah else teks.splitlines()
    return [x.strip() for x in item if x.strip()]


def pola(nama):
    """Bagian berisi regex yang boleh dipecah ke beberapa baris; baris digabung tanpa pemisah."""
    return re.compile("".join(x.strip() for x in isi(nama).splitlines()), re.IGNORECASE)
