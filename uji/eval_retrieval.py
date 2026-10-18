"""Hitung recall@k retrieval RAG untuk kasus uji yang punya isian `bagian:`.

Tidak memanggil LLM penjawab, hanya embedding query (satu panggilan batch) dan indeks rag/index.json.
Per kasus, setiap giliran customer diperlakukan seperti di bot (pesan + pesan customer sebelumnya),
lalu potongan dari semua giliran digabung.

Contoh (dari folder project):
  .venv\\Scripts\\python.exe uji\\eval_retrieval.py
  .venv\\Scripts\\python.exe uji\\eval_retrieval.py --k 3 --tanpa-hybrid
"""
import argparse
import sys
from pathlib import Path

FOLDER = Path(__file__).resolve().parent
sys.path.insert(0, str(FOLDER.parent))
import bot  # noqa: E402  (memuat .env dan KNOWLEDGE yang sama dengan bot)
import rag  # noqa: E402
from jalankan_uji import baca_daftar, file_daftar  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="Evaluasi retrieval RAG (recall@k).")
    ap.add_argument("--daftar", help="default uji/pertanyaan_uji.md, atau file contoh")
    ap.add_argument("--k", type=int, default=rag.konfigurasi()["top_k"])
    ap.add_argument("--tanpa-hybrid", action="store_true", help="ranking hanya dari cosine vektor")
    args = ap.parse_args()
    args.daftar = args.daftar or file_daftar()

    kasus = [k for k in baca_daftar(args.daftar) if k["bagian"]]
    if not kasus:
        sys.exit("Tidak ada kasus dengan isian bagian:.")
    indeks = rag.muat_indeks(bot.KNOWLEDGE)
    ada = {p["id"] for p in indeks["potongan"]}
    query = []
    for k in kasus:
        k["query"] = [("\n".join(x for x in (pesan, k["customer"][i - 1] if i else "") if x))
                      for i, pesan in enumerate(k["customer"])]
        query += k["query"]
    vektor = rag.embed(query, "RETRIEVAL_QUERY", model=indeks["model"], dimensi=indeks["dimensi"])
    min_skor, total, i = rag.konfigurasi()["min_skor"], 0.0, 0
    print(f"Model {indeks['model']}, dimensi {indeks['dimensi']}, k={args.k}, "
          f"{'tanpa hybrid' if args.tanpa_hybrid else f'hybrid {rag.BOBOT_VEKTOR}/{rag.BOBOT_KATA}'}, "
          f"ambang cosine {min_skor}\n")
    for k in kasus:
        diambil, puncak = [], []
        for q in k["query"]:
            urutan = rag.peringkat(q, vektor[i], indeks, hybrid=not args.tanpa_hybrid)
            i += 1
            diambil += [x["id"] for x in urutan[:args.k]]
            puncak.append(max(x["vektor"] for x in urutan))
        label = [[a.strip() for a in item.split("|")] for item in k["bagian"].split(",") if item.strip()]
        salah_id = [a for item in label for a in item if a not in ada]
        kena = [item for item in label if any(a in diambil for a in item)]
        recall = len(kena) / len(label)
        total += recall
        hilang = ["|".join(item) for item in label if item not in kena]
        catatan = " ".join(filter(None, [
            f"HILANG: {', '.join(hilang)}" if hilang else "",
            f"ID TIDAK DIKENAL: {', '.join(salah_id)}" if salah_id else "",
            f"(cosine puncak {min(puncak):.3f} < ambang)" if min(puncak) < min_skor else ""]))
        print(f"{k['id']:<7} recall {recall:.2f}  cosine puncak {', '.join(f'{p:.3f}' for p in puncak)}  {catatan}")
        print(f"        diambil: {', '.join(dict.fromkeys(diambil))}")
    print(f"\nRata-rata recall@{args.k}: {total / len(kasus):.3f} dari {len(kasus)} kasus")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
