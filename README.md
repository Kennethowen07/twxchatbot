# twxchatbot

Asisten customer service WhatsApp berbasis LLM. Bot menjawab pertanyaan calon pelanggan dan pelanggan dari
basis pengetahuan (file Markdown), membaca harga dan kupon promo dari file teks yang bisa diubah tanpa restart,
dan menyerahkan chat ke admin manusia lewat penanda khusus. Admin dan bot berbagi satu nomor WhatsApp.

Repo ini berisi kode dan file **contoh fiktif** ("Contoh Academy"). Prompt, knowledge, harga, dan daftar uji asli
bersifat privat dan tidak ikut repo.

## Arsitektur dan alur pesan

```
Pelanggan WhatsApp
      │
      ▼
Penyedia WhatsApp (Meta Cloud API / YCloud / 360dialog)
      │  webhook POST (tanda tangan diverifikasi)
      ▼
whatsapp.py ── balas 200 cepat, masuk antrean, dedupe id pesan
      │
      ├─ event "admin mengetik" (echo coexistence) ──► bot diam di chat itu
      ├─ pesan non-teks ──► balasan sopan "baru bisa membaca teks"
      └─ pesan teks
            │
            ▼
      bot.jawab()
        1. chat sedang diam (sudah dioper / admin mengetik)?  → tidak membalas
        2. susun system prompt:
             aturan & persona (prompt.md)
           + data knowledge: penuh (mode full) atau potongan relevan (mode rag, rag.py)
           + blok harga & promo (harga_promo.txt, dicek kode: kupon aktif/lewat, pemicu kata)
           + status jam admin (dihitung kode)
        3. panggil LLM (endpoint kompatibel OpenAI)
        4. ada penanda [OPER]? → buang penanda, catat log OPER, bot diam 12 jam
        5. permintaan diskon/paket pendek kedua kali tanpa kupon aktif? → kode menambah saran materi gratis
        6. LLM gagal (kuota, timeout, error)? → pesan cadangan, log LLM_GAGAL
      │
      ▼
whatsapp.py ── kirim balasan lewat API penyedia (dipotong per 4096 karakter)
```

## Fitur

- **Dua mode knowledge.** `full` memasukkan seluruh file knowledge ke prompt (default). `rag` memecah knowledge per
  judul, membuat embedding (Gemini `gemini-embedding-001`, 768 dimensi), lalu mengambil potongan teratas dengan skor
  hybrid (cosine vektor + BM25 kata). Bagian "data belum tersedia" selalu ikut. Bila embedding gagal, pesan itu
  dijawab dengan knowledge penuh.
- **Hasil eksperimen RAG (secara umum).** Untuk basis pengetahuan kecil (beberapa ribu kata), RAG menghemat kira-kira
  sepertiga token prompt, tetapi lebih lemah pada pertanyaan gabungan yang butuh beberapa bagian sekaligus. Karena
  itu mode `full` tetap dipakai sebagai default.
- **Harga dan promo dari file.** Kode yang mengecek masa berlaku kupon (zona WIB), bukan LLM. Info kupon hanya masuk
  prompt bila pesan menyinggung promo. File rusak → bot tidak menyebut harga dan menawarkan admin.
- **Oper ke admin.** LLM hanya menawarkan; setelah pelanggan setuju, LLM menulis `[OPER]`. Kode membuang penanda,
  mencatat log, dan membuat bot diam 12 jam di chat itu.
- **Bot diam saat admin mengetik.** Pada coexistence (aplikasi WhatsApp Business + API di satu nomor), pesan yang
  dikirim admin dari aplikasi datang sebagai event echo; bot berhenti membalas di chat itu.
- **Adapter WhatsApp.** `WA_PROVIDER=meta|ycloud|360dialog`. Tiap penyedia punya verifikasi request, pengurai
  payload ke event netral, dan cara kirim sendiri; logika umum (antrean, dedupe, pesan cadangan, log) sama.
- **Pesan cadangan.** Pelanggan tidak pernah dibiarkan tanpa balasan saat LLM gagal.
- **Kerangka uji.** Daftar kasus dalam Markdown sederhana (bisa diedit tanpa coding), runner yang menyimpan
  percakapan, token per giliran, dan potongan RAG untuk dinilai manusia, serta evaluasi recall retrieval.

## Menjalankan

Butuh Python 3.12+ dan API key Gemini (atau Ollama lokal untuk sekadar cek alur).

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows (Linux/macOS: source .venv/bin/activate)
pip install -r requirements.txt

cp .env.example .env              # lalu isi GEMINI_API_KEY dan variabel lain
```

File privat dan pasangannya:

| File privat (tidak di repo) | File contoh (di repo) |
|---|---|
| `prompt.md` | `prompt.example.md` |
| `knowledge.md` | `knowledge.example.md` |
| `harga_promo.txt` | `harga_promo.example.txt` |
| `uji/pertanyaan_uji.md` | `uji/pertanyaan_uji.example.md` |
| `uji/promo_<kondisi>.txt` | `uji/promo_<kondisi>.example.txt` |

Kalau file privat tidak ada, bot otomatis memakai file contoh dan mencatat WARNING "memakai file contoh". Untuk
data sendiri, salin file contoh tanpa `.example` lalu ubah isinya. Format `prompt.md` dijelaskan di bagian atas
`prompt.example.md`.

```bash
python bot.py                                   # chat di terminal
python whatsapp.py                              # server webhook (port WEBHOOK_PORT, default 8000)
cloudflared tunnel --url http://localhost:8000  # buka ke internet untuk tes, path webhook: /webhook
python uji/jalankan_uji.py --mode full          # jalankan daftar uji (opsi --id, --kategori, --jeda, --mode)
python uji/eval_retrieval.py --k 4              # recall@k retrieval RAG (opsi --tanpa-hybrid)
```

## Struktur file

```
bot.py                  inti: susun prompt, panggil LLM, oper, diam, harga & promo, mode knowledge
rag.py                  chunking, embedding Gemini (REST), indeks rag/index.json, skor hybrid
berkas.py               pemuat prompt.md dan fallback ke file *.example
whatsapp.py             server webhook + adapter meta / ycloud / 360dialog
prompt.example.md       contoh teks instruksi LLM, kata pemicu, link (format per bagian)
knowledge.example.md    contoh basis pengetahuan
harga_promo.example.txt contoh harga dan kupon
uji/jalankan_uji.py     runner daftar uji, hasil ke uji/hasil/
uji/eval_retrieval.py   evaluasi recall retrieval
uji/pertanyaan_uji.example.md, uji/promo_*.example.txt   contoh kasus uji dan fixture harga
.env.example            daftar variabel konfigurasi
requirements.txt        dependensi Python (hanya klien openai)
```

Folder `rag/` (indeks), `logs/`, dan `uji/hasil/` dibuat saat runtime dan tidak ikut repo.
