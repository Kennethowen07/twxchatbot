"""Jalankan daftar pertanyaan uji lewat bot.jawab, simpan percakapan untuk dinilai manusia.

Contoh (dari folder project):
  .venv\\Scripts\\python.exe uji\\jalankan_uji.py
  .venv\\Scripts\\python.exe uji\\jalankan_uji.py --id FAQ-01,PRM-02
  .venv\\Scripts\\python.exe uji\\jalankan_uji.py --kategori promo,oper --jeda 6
  .venv\\Scripts\\python.exe uji\\jalankan_uji.py --mode rag
"""
import argparse
import functools
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

FOLDER = Path(__file__).resolve().parent
sys.path.insert(0, str(FOLDER.parent))
import berkas  # noqa: E402
bot = None


def file_daftar():
    return str(berkas.pilih("pertanyaan_uji.md", "pertanyaan_uji.example.md", folder=FOLDER))


def file_promo(kondisi):
    return berkas.pilih(f"promo_{kondisi}.txt", f"promo_{kondisi}.example.txt", folder=FOLDER)

KONDISI = ("tanpa", "aktif", "lewat", "rusak")
ISIAN_DAFTAR = ("customer", "harus", "tidak boleh")


class Catatan(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.isi = []

    def emit(self, record):
        self.isi.append(record.getMessage())


def baca_daftar(path):
    semua, kasus = [], None
    for baris in Path(path).read_text(encoding="utf-8-sig").splitlines():
        baris = baris.strip()
        if baris.startswith("## "):
            kasus = {"id": baris[3:].strip(), "kategori": "", "promo": "", "bagian": "", "customer": [], "harus": [],
                     "tidak boleh": []}
            semua.append(kasus)
            continue
        if kasus is None:
            continue
        kunci, pemisah, isi = baris.removeprefix("-").partition(":")
        kunci = kunci.strip().lower()
        if not pemisah or kunci not in kasus or kunci == "id":
            continue
        if kunci in ISIAN_DAFTAR:
            kasus[kunci].append(isi.strip())
        else:
            kasus[kunci] = isi.strip().lower()
    return semua


def masalah(kasus):
    if kasus["promo"] not in KONDISI:
        return f"promo harus salah satu dari {', '.join(KONDISI)}, tertulis '{kasus['promo']}'"
    if not kasus["customer"]:
        return "tidak ada baris customer"
    return None


def info_giliran(user_id, balasan, total_token):
    bagian = []
    token = bot.token_terakhir.get(user_id) if balasan is not None else None
    if token:
        total_token[0] += token[0] or 0
        total_token[1] += token[1] or 0
        bagian.append(f"token prompt {token[0]}, completion {token[1]}")
    rag = bot.rag_terakhir.pop(user_id, None)
    if rag and rag["mode"] == "fallback":
        bagian.append("RAG gagal, pakai knowledge penuh")
    elif rag:
        skor = ", ".join(f"{x['id']} ({x['vektor']:.3f}/{x['kata']:.2f}/{x['gabung']:.3f})" for x in rag["terpilih"])
        bagian.append(f"RAG {rag['ms']} ms, " + (f"potongan: {skor}" if rag["relevan"] else "tidak ada potongan relevan"))
    return "; ".join(bagian)


def status_singkat():
    return bot.status_admin().split(". ")[0]


def kutip(teks):
    return "\n".join("> " + b for b in teks.splitlines()) or ">"


def tulis_kasus(f, kasus, percakapan, error, log, detik, admin):
    f.write(f"\n## {kasus['id']}\n\n")
    f.write(f"- Kategori: {kasus['kategori'] or '-'}\n- Kondisi promo: {kasus['promo'] or '-'}\n- Waktu: {detik:.1f} detik\n")
    f.write(f"- {admin}\n\n")
    f.write("### Percakapan\n\n")
    for pesan, balasan, info in percakapan:
        f.write(f"**Customer:** {pesan}\n\n**{bot.NAMA_BOT}:**\n\n{kutip(balasan) if balasan is not None else '> (tidak ada balasan)'}\n\n")
        if info:
            f.write(f"_{info}_\n\n")
    if error:
        f.write(f"**ERROR:** {error}\n\n")
    if log:
        f.write("**Log bot:**\n\n" + "".join(f"- {b}\n" for b in log) + "\n")
    f.write("### Kriteria lulus\n\n")
    f.write("".join(f"- Harus: {b}\n" for b in kasus["harus"]))
    f.write("".join(f"- Tidak boleh: {b}\n" for b in kasus["tidak boleh"]))
    f.write("\n### Penilaian\n\n- Hasil (LULUS / GAGAL):\n- Catatan:\n\n---\n")
    f.flush()


def main():
    ap = argparse.ArgumentParser(description="Jalankan pertanyaan uji bot.")
    ap.add_argument("--daftar", help="file daftar kasus (default uji/pertanyaan_uji.md, atau file contoh)")
    ap.add_argument("--id", help="hanya kasus ini, pisah koma (mis. FAQ-01,PRM-02)")
    ap.add_argument("--kategori", help="hanya kategori ini, pisah koma (mis. promo,oper)")
    ap.add_argument("--jeda", type=float, default=4, help="jeda antar panggilan LLM dalam detik (default 4)")
    ap.add_argument("--mode", choices=("full", "rag"), help="KNOWLEDGE_MODE untuk run ini (default dari .env, lalu full)")
    args = ap.parse_args()
    if args.mode:
        os.environ["KNOWLEDGE_MODE"] = args.mode
    global bot
    import bot
    args.daftar = args.daftar or file_daftar()
    diam = f"({bot.NAMA_BOT} diam, chat sudah dioper ke admin)"

    semua = baca_daftar(args.daftar)
    ids = {s.strip().upper() for s in args.id.split(",")} if args.id else None
    kategori = {s.strip().lower() for s in args.kategori.split(",")} if args.kategori else None
    dipilih = [k for k in semua if (not ids or k["id"].upper() in ids)
               and (not kategori or k["kategori"] in kategori)]
    if ids:
        hilang = ids - {k["id"].upper() for k in dipilih}
        if hilang:
            print(f"Peringatan, ID tidak ditemukan: {', '.join(sorted(hilang))}")
    if not dipilih:
        sys.exit("Tidak ada kasus yang cocok.")

    folder_hasil = FOLDER / "hasil"
    folder_hasil.mkdir(exist_ok=True)
    mulai_run = datetime.now()
    path_hasil = folder_hasil / f"hasil_{mulai_run:%Y-%m-%d_%H-%M-%S}.md"

    catatan = Catatan()
    logging.getLogger().addHandler(catatan)
    asli = bot.baca_harga_promo
    ringkasan, panggilan, total_token = [], 0, [0, 0]
    print(f"Provider {bot.PROVIDER}, model {bot.MODEL}, mode {bot.KNOWLEDGE_MODE}, {len(dipilih)} kasus, "
          f"jeda {args.jeda:g} detik")

    with open(path_hasil, "w", encoding="utf-8") as f:
        f.write(f"# Hasil uji {bot.NAMA_BOT} {mulai_run:%Y-%m-%d %H:%M:%S}\n\n")
        f.write(f"- Provider: {bot.PROVIDER}\n- Model: {bot.MODEL}\n- Mode knowledge: {bot.KNOWLEDGE_MODE}\n"
                f"- Daftar: {args.daftar}\n")
        f.write(f"- Filter: id={args.id or '-'}, kategori={args.kategori or '-'}\n- Jeda: {args.jeda:g} detik\n")
        f.write(f"- Jumlah kasus: {len(dipilih)}\n- Saat mulai, {status_singkat()}\n\n---\n")
        try:
            for no, kasus in enumerate(dipilih, 1):
                percakapan, error, mulai = [], masalah(kasus), time.time()
                catatan.isi = []
                admin = status_singkat()
                if error:
                    status = "DILEWATI"
                else:
                    bot.baca_harga_promo = functools.partial(asli, file_promo(kasus["promo"]))
                    bot.riwayat.clear()
                    bot.waktu_oper.clear()
                    bot.hitung_minta.clear()
                    bot.pesan_terakhir.clear()
                    bot.rag_terakhir.clear()
                    bot.token_terakhir.clear()
                    for pesan in kasus["customer"]:
                        if panggilan:
                            time.sleep(args.jeda)
                        panggilan += 1
                        try:
                            balasan = bot.jawab(kasus["id"], pesan)
                            info = info_giliran(kasus["id"], balasan, total_token)
                            percakapan.append((pesan, diam if balasan is None else balasan, info))
                            gagal_llm = [b for b in catatan.isi if b.startswith("LLM_GAGAL")]
                            if gagal_llm:
                                error = f"{gagal_llm[-1]} (bot mengirim pesan cadangan)"
                                break
                        except Exception as e:
                            percakapan.append((pesan, None, ""))
                            error = f"{type(e).__name__}: {e}"
                            break
                    status = "ERROR" if error else "OK"
                detik = time.time() - mulai
                tulis_kasus(f, kasus, percakapan, error, catatan.isi, detik, admin)
                ringkasan.append((kasus["id"], status, error))
                print(f"[{no}/{len(dipilih)}] {kasus['id']:<8} {status:<8} {detik:5.1f} detik"
                      + (f"  {error[:150]}" if error else ""))
        except KeyboardInterrupt:
            f.write("\n**Run dihentikan manual (Ctrl+C).**\n")
            print("\nDihentikan manual.")
        finally:
            bot.baca_harga_promo = asli
            jumlah = {s: sum(1 for r in ringkasan if r[1] == s) for s in ("OK", "ERROR", "DILEWATI")}
            f.write(f"\n## Ringkasan\n\n- Selesai: {len(ringkasan)} dari {len(dipilih)} kasus\n"
                    + "".join(f"- {s}: {n}\n" for s, n in jumlah.items())
                    + f"- Mode knowledge: {bot.KNOWLEDGE_MODE}\n- Total token: prompt {total_token[0]}, "
                    f"completion {total_token[1]}\n")

    print(f"\nSelesai {len(ringkasan)}/{len(dipilih)}, "
          + ", ".join(f"{s} {n}" for s, n in jumlah.items()))
    print(f"Total token, prompt {total_token[0]}, completion {total_token[1]}")
    print(f"Hasil, {path_hasil}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
