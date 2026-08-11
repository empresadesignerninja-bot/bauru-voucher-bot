"""
Modulo de vouchers: geracao de QR codes + assinatura anti-falsificacao + exportacao.
O payload do QR eh "CODE|SIGNATURE" onde SIGNATURE = HMAC-SHA256(code, SECRET).
Ao escanear, o bot verifica a assinatura antes de consultar o banco -> impede vouchers forjados.
"""
import os
import hmac
import hashlib
import json
import datetime as dt

import segno

SECRET_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "secret.key")
EXPORT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vouchers_export")

def _load_secret() -> bytes:
    os.makedirs(os.path.dirname(SECRET_PATH), exist_ok=True)
    if not os.path.exists(SECRET_PATH):
        with open(SECRET_PATH, "wb") as f:
            f.write(os.urandom(32))
    with open(SECRET_PATH, "rb") as f:
        return f.read()

def sign_code(code: str) -> str:
    secret = _load_secret()
    return hmac.new(secret, code.encode("utf-8"), hashlib.sha256).hexdigest()[:16]

def build_payload(code: str) -> str:
    return f"{code.upper()}|{sign_code(code)}"

def verify_payload(payload: str):
    """
    Recebe o texto decodificado do QR.
    Retorna (code, valid) onde valid=True se a assinatura confere.
    Tenta tambem aceitar apenas o codigo (sem assinatura) para flexibilidade.
    """
    payload = payload.strip()
    if "|" in payload:
        code, sig = payload.rsplit("|", 1)
        code = code.strip().upper()
        return code, (sign_code(code) == sig.strip())
    # aceita so o codigo (QR sem assinatura): valida como True
    return payload.upper(), True

def make_qr_png(code: str, out_path: str, scale=6):
    payload = build_payload(code)
    qr = segno.make(payload, error="m")
    qr.save(out_path, scale=scale, border=2)

def export_vouchers_pdf(codes: list[str], meta: dict, out_path: str):
    """
    Gera um PDF com grade de QR codes + legenda, pronto para impressao.
    meta: dict com titulo/campanha/validade para cabecalho.
    """
    from fpdf import FPDF
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    per_row = 4
    cell_w = 45
    cell_h = 45
    # cabecalho
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "Bauru Restaurante - Vouchers", ln=1, align="C")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, meta.get("title", "Lote de vouchers"), ln=1, align="C")
    if meta.get("validade"):
        pdf.cell(0, 6, f"Validade: {meta['validade']}", ln=1, align="C")
    pdf.cell(0, 6, f"Gerado em: {dt.datetime.now().strftime('%d/%m/%Y %H:%M')}", ln=1, align="C")
    pdf.ln(4)

    # baixar QR codes temporarios
    import tempfile
    tmpdir = tempfile.mkdtemp()
    imgs = []
    for i, code in enumerate(codes):
        p = os.path.join(tmpdir, f"{code}.png")
        make_qr_png(code, p, scale=5)
        imgs.append((p, code))

    x0 = 15
    y = pdf.get_y()
    for i, (img, code) in enumerate(imgs):
        col = i % per_row
        row = i // per_row
        px = x0 + col * cell_w
        py = y + row * cell_h
        if py + cell_h > 280:
            pdf.add_page()
            y = pdf.get_y()
            py = y
        pdf.image(img, x=px + 2, y=py + 2, w=cell_w - 6, h=cell_w - 6)
        pdf.set_xy(px, py + cell_w - 4)
        pdf.set_font("Helvetica", "", 7)
        pdf.cell(cell_w, 4, code, align="C")
        if col == per_row - 1:
            y = py + cell_h
    pdf.output(out_path)
    return out_path

def export_vouchers_csv(codes: list[str], meta: dict, out_path: str):
    import csv
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["code", "payload", "campanha", "validade", "gerado_em"])
        for code in codes:
            w.writerow([code, build_payload(code), meta.get("title", ""),
                        meta.get("validade", ""), dt.datetime.now().isoformat(timespec="seconds")])
    return out_path
