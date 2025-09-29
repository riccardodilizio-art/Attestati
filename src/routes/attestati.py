# attestati.py
import os
import re
import io
import json
import logging
import fitz  # PyMuPDF
from flask import Blueprint, request, jsonify, send_file
from flask_cors import cross_origin
from flask_jwt_extended import jwt_required
from unidecode import unidecode

attestati_bp = Blueprint("attestati", __name__)

# =============================
#   CONFIG & COSTANTI
# =============================

BASE_DIR = os.path.dirname(__file__)
UPLOAD_FOLDER = os.path.abspath(os.path.join(BASE_DIR, '..', 'uploads'))
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

ALLOWED_EXTENSIONS = {"pdf"}

LABELS_STOP = {
    "ATTESTATO DI PARTECIPAZIONE",
    "NOME",
    "NUMERO PETTORALE",
    "POSIZIONE ASSOLUTA",
    "POSIZIONE CATEGORIA",
    "GARA/TEMPO",
    "CERTIFICATES",
}

IS_PRODUCTION = os.getenv("FLASK_ENV") == "production"

# Logging
logger = logging.getLogger(__name__)

# =============================
#   UTIL
# =============================

def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def norm(s: str) -> str:
    """Normalizza per ricerche case/accents/apostrofi-insensitive."""
    if not s:
        return ""
    s = s.replace("’", "'")
    s = unidecode(s)                      # rimuove accenti
    s = re.sub(r"['’]", " ", s)          # apostrofi -> spazio (D'ANGELO -> D ANGELO)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s

def get_gara_folder(gara_id: str) -> str:
    return os.path.join(UPLOAD_FOLDER, gara_id)

def get_pdf_file(gara_id: str) -> str:
    return os.path.join(get_gara_folder(gara_id), 'attestati.pdf')

def get_index_file(gara_id: str) -> str:
    return os.path.join(get_gara_folder(gara_id), 'index.json')

def salva_indice(indice: dict, gara_id: str) -> None:
    os.makedirs(get_gara_folder(gara_id), exist_ok=True)
    with open(get_index_file(gara_id), "w", encoding="utf-8") as f:
        json.dump(indice, f, ensure_ascii=False, indent=2)

def carica_indice(gara_id: str) -> dict:
    path = get_index_file(gara_id)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"by_bib": {}, "by_name": {}}

def get_gare_disponibili() -> list:
    gare = []
    if os.path.exists(UPLOAD_FOLDER):
        for item in os.listdir(UPLOAD_FOLDER):
            gara_path = os.path.join(UPLOAD_FOLDER, item)
            if os.path.isdir(gara_path):
                pdf_file = get_pdf_file(item)
                index_file = get_index_file(item)
                gare.append({
                    'id': item,
                    'nome': item.replace('_', ' ').title(),
                    'pdf_caricato': os.path.exists(pdf_file),
                    'indice_creato': os.path.exists(index_file)
                })
    return gare

def estrai_pagina(pdf_path: str, page_number: int) -> io.BytesIO | None:
    """Estrae la sola pagina richiesta in un buffer PDF."""
    try:
        with fitz.open(pdf_path) as doc:
            idx = page_number - 1
            if idx < 0 or idx >= doc.page_count:
                return None
            out_doc = fitz.open()
            out_doc.insert_pdf(doc, from_page=idx, to_page=idx)
            buf = io.BytesIO()
            out_doc.save(buf)
            out_doc.close()
            buf.seek(0)
            return buf
    except Exception as e:
        logger.error(f"Errore estrazione pagina: {e}")
        return None

# =============================
#   INDICIZZAZIONE
# =============================

def is_all_caps_name(line: str) -> bool:
    raw = re.sub(r"\s+", " ", line or "").strip()
    if not raw:
        return False
    raw_up = raw.upper()
    if raw_up in LABELS_STOP:
        return False
    words = [w for w in raw_up.split() if len(w) >= 2]
    if len(words) < 2:
        return False
    if not re.fullmatch(r"[A-ZÀ-Ÿ' \-]+", raw_up):
        return False
    total_alpha = sum(ch.isalpha() for ch in raw)
    cap_ratio = sum(ch.isupper() for ch in raw if ch.isalpha()) / max(1, total_alpha)
    return cap_ratio > 0.9

def extract_bib_from_text(text: str) -> str | None:
    t_flat = re.sub(r"\s+", " ", text or "", flags=re.UNICODE).strip()
    m = re.search(r"numero\s*pettorale[^0-9]{0,100}?([0-9]{1,5})", t_flat, flags=re.IGNORECASE)
    if m:
        return m.group(1)
    m2 = re.search(r"\bpettorale[^0-9]{0,50}?([0-9]{1,5})", t_flat, flags=re.IGNORECASE)
    if m2:
        return m2.group(1)
    m3 = re.search(r"\bn[°o]\s*pettorale[^0-9]{0,50}?([0-9]{1,5})", t_flat, flags=re.IGNORECASE)
    if m3:
        return m3.group(1)
    return None

def extract_name_from_blocks(blocks_texts: list[str]) -> str | None:
    candidates = []
    for blk in blocks_texts:
        for line in (blk or "").splitlines():
            if is_all_caps_name(line):
                candidates.append(line.strip())
    if not candidates:
        return None
    candidates = sorted(candidates, key=lambda s: abs(len(s) - 18))
    return candidates[0]

def extract_name_fallback_near_pettorale(text: str) -> str | None:
    lines = (text or "").splitlines()
    idxs = [i for i, ln in enumerate(lines) if re.search(r"pettorale", ln, flags=re.IGNORECASE)]
    window_lines = []
    for i in idxs[:1]:
        start = max(0, i - 5)
        end = min(len(lines), i + 6)
        window_lines.extend(lines[start:end])

    def is_caps_candidate(s):
        s2 = re.sub(r"\s+", " ", s or "").strip()
        if not s2:
            return False
        su = s2.upper()
        if su in LABELS_STOP:
            return False
        if not re.fullmatch(r"[A-ZÀ-Ÿ' \-]{6,}", su):
            return False
        if len([w for w in su.split() if len(w) >= 2]) < 2:
            return False
        return True

    caps = [ln.strip() for ln in window_lines if is_caps_candidate(ln)]
    if caps:
        caps.sort(key=lambda s: abs(len(s) - 18))
        return caps[0]
    return None

def crea_indice(pdf_path: str) -> dict:
    out = {"by_bib": {}, "by_name": {}}
    try:
        with fitz.open(pdf_path) as doc:
            for page_num, page in enumerate(doc, start=1):
                text = page.get_text() or ""
                blks = page.get_text("blocks") or []
                blocks_texts = [b[4] for b in blks if isinstance(b, (list, tuple)) and len(b) >= 5 and isinstance(b[4], str)]

                bib = extract_bib_from_text(text)
                name_raw = extract_name_from_blocks(blocks_texts) or extract_name_fallback_near_pettorale(text)

                if bib:
                    out["by_bib"][bib] = page_num
                if name_raw:
                    key = norm(name_raw)
                    out["by_name"].setdefault(key, [])
                    if page_num not in out["by_name"][key]:
                        out["by_name"][key].append(page_num)

                if not bib and not name_raw:
                    m2 = re.search(r"\b([A-Z][a-zà-ÿ]+)\s+([A-Z][a-zà-ÿ]+)\b", text)
                    if m2:
                        key = norm(f"{m2.group(1)} {m2.group(2)}")
                        out["by_name"].setdefault(key, [])
                        if page_num not in out["by_name"][key]:
                            out["by_name"][key].append(page_num)
    except Exception as e:
        logger.error(f"Errore indicizzazione PDF: {e}")
    return out

# =============================
#   ROUTES
# =============================

@attestati_bp.route("/upload", methods=["POST"])
@cross_origin()
@jwt_required()
def upload_pdf():
    if 'file' not in request.files:
        return jsonify({"error": "Nessun file selezionato"}), 400

    file = request.files["file"]
    gara_id = (request.form.get("gara_id") or "").strip()

    if file.filename == '':
        return jsonify({"error": "Nome file vuoto"}), 400
    if not gara_id:
        return jsonify({"error": "ID gara richiesto"}), 400
    if not re.match(r'^[a-zA-Z0-9_]+$', gara_id):
        return jsonify({"error": "ID gara non valido. Usa solo lettere, numeri e underscore"}), 400
    if not allowed_file(file.filename):
        return jsonify({"error": "Estensione non valida. Carica un PDF"}), 400

    gara_folder = get_gara_folder(gara_id)
    os.makedirs(gara_folder, exist_ok=True)
    file_path = get_pdf_file(gara_id)
    file.save(file_path)

    indice = crea_indice(file_path)
    salva_indice(indice, gara_id)

    return jsonify({
        "message": f"File caricato e indicizzato per la gara '{gara_id}'",
        "gara_id": gara_id,
        "totale_bib": len(indice.get("by_bib", {})),
        "totale_nomi": len(indice.get("by_name", {}))
    }), 201


@attestati_bp.route("/reindex", methods=["POST"])
@cross_origin()
@jwt_required()
def reindex():
    data = request.get_json(silent=True) or {}
    gara_id = (data.get("gara_id") or "").strip()
    if not gara_id:
        return jsonify({"error": "ID gara richiesto"}), 400
    pdf_file = get_pdf_file(gara_id)
    if not os.path.exists(pdf_file):
        return jsonify({"error": f"PDF non caricato per la gara '{gara_id}'"}), 404

    indice = crea_indice(pdf_file)
    salva_indice(indice, gara_id)
    return jsonify({
        "message": f"Indice rigenerato per '{gara_id}'",
        "totale_bib": len(indice.get("by_bib", {})),
        "totale_nomi": len(indice.get("by_name", {}))
    }), 200


@attestati_bp.route("/cerca", methods=["GET"])
@cross_origin()
def cerca_attestato():
    gara_id = (request.args.get("gara_id") or "").strip()
    query = (request.args.get("query") or "").strip()
    first_only = (request.args.get("first_only", "").lower() in {"1", "true", "yes"})

    if not gara_id:
        return jsonify({"error": "ID gara richiesto"}), 400
    if not query:
        return jsonify({"error": "Query di ricerca richiesta (pettorale o nome)"}), 400

    pdf_file = get_pdf_file(gara_id)
    if not os.path.exists(pdf_file):
        return jsonify({"error": f"PDF non caricato per la gara '{gara_id}'"}), 404

    idx = carica_indice(gara_id)
    by_bib = idx.get("by_bib", {})
    by_name = idx.get("by_name", {})

    if query.isdigit() and query in by_bib:
        page_num = by_bib[query]
        pdf_buffer = estrai_pagina(pdf_file, page_num)
        if pdf_buffer:
            return send_file(
                pdf_buffer,
                download_name=f"attestato_{gara_id}_pettorale_{query}.pdf",
                as_attachment=True,
                mimetype="application/pdf"
            )
        return jsonify({"error": "Errore nell'estrazione PDF"}), 500

    qn = norm(query)
    pages = list(by_name.get(qn, []))

    if not pages:
        for key, vals in by_name.items():
            if qn in key:
                pages.extend(vals)
        pages = sorted(set(pages))

    if not pages:
        return jsonify({"error": f"Attestato non trovato per '{query}' nella gara '{gara_id}'"}), 404

    if len(pages) > 1 and not first_only:
        return jsonify({
            "status": "multiple",
            "risultati": [
                {"page": p, "download": f"/api/attestati/singola_pagina?gara_id={gara_id}&page={p}"}
                for p in pages
            ]
        }), 200

    page_num = pages[0]
    pdf_buffer = estrai_pagina(pdf_file, page_num)
    if pdf_buffer:
        safe_q = re.sub(r"[^a-zA-Z0-9_\-]", "_", query)[:50]
        return send_file(
            pdf_buffer,
            download_name=f"attestato_{gara_id}_{safe_q}.pdf",
            as_attachment=True,
            mimetype="application/pdf"
        )
    return jsonify({"error": "Errore nell'estrazione PDF"}), 500


@attestati_bp.route("/singola_pagina", methods=["GET"])
@cross_origin()
def singola_pagina():
    gara_id = (request.args.get("gara_id") or "").strip()
    page = request.args.get("page", type=int)

    if not gara_id or not page:
        return jsonify({"error": "Parametri mancanti"}), 400

    pdf_file = get_pdf_file(gara_id)
    if not os.path.exists(pdf_file):
        return jsonify({"error": f"PDF non caricato per la gara '{gara_id}'"}), 404

    pdf_buffer = estrai_pagina(pdf_file, page)
    if pdf_buffer:
        return send_file(
            pdf_buffer,
            download_name=f"attestato_{gara_id}_p{page}.pdf",
            as_attachment=True,
            mimetype="application/pdf"
        )
    return jsonify({"error": "Errore nell'estrazione PDF"}), 500


@attestati_bp.route("/gare", methods=["GET"])
@cross_origin()
def lista_gare():
    return jsonify({"gare": get_gare_disponibili()}), 200


@attestati_bp.route("/status", methods=["GET"])
@cross_origin()
def status():
    gara_id = request.args.get("gara_id")
    if gara_id:
        pdf_file = get_pdf_file(gara_id)
        index_file = get_index_file(gara_id)
        return jsonify({
            "gara_id": gara_id,
            "pdf_caricato": os.path.exists(pdf_file),
            "indice_creato": os.path.exists(index_file)
        }), 200
    else:
        gare = get_gare_disponibili()
        return jsonify({
            "totale_gare": len(gare),
            "gare": gare
        }), 200

# =============================
#   DEBUG
# =============================

def maybe_protect(fn):
    return jwt_required()(fn) if IS_PRODUCTION else fn

@attestati_bp.route("/debug/indice", methods=["GET"])
@cross_origin()
@maybe_protect
def debug_indice():
    gara_id = (request.args.get("gara_id") or "").strip()
    if not gara_id:
        return jsonify({"error": "ID gara richiesto"}), 400
    return jsonify(carica_indice(gara_id)), 200

@attestati_bp.route("/debug/page_text", methods=["GET"])
@cross_origin()
@maybe_protect
def debug_page_text():
    gara_id = (request.args.get("gara_id") or "").strip()
    page = request.args.get("page", type=int)
    if not gara_id or not page:
        return jsonify({"error": "Parametri mancanti (gara_id, page)"}), 400
    pdf_file = get_pdf_file(gara_id)
    if not os.path.exists(pdf_file):
        return jsonify({"error": f"PDF non caricato per la gara '{gara_id}'"}), 404
    try:
        with fitz.open(pdf_file) as doc:
            idx = page - 1
            if idx < 0 or idx >= doc.page_count:
                return jsonify({"error": "Pagina fuori range"}), 400
            p = doc[idx]
            return jsonify({
                "page": page,
                "text": p.get_text(),
                "blocks": [b[4] for b in p.get_text("blocks") if isinstance(b, (list, tuple)) and len(b) >= 5 and isinstance(b[4], str)]
            }), 200
    except Exception as e:
        logger.error(f"Errore lettura pagina: {e}")
        return jsonify({"error": f"Errore lettura pagina: {e}"}), 500
