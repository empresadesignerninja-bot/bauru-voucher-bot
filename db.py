"""
Camada de banco de dados do Sistema de Vouchers Bauru.
Usa SQLite (arquivo unico, portatil, ilimitado).

Tabelas:
  units      -> unidades do restaurante (cada gerente pertence a uma)
  managers   -> usuarios com login/senha (admin ou gerente de unidade)
  campaigns  -> campanhas/parcerias (radio X, promo Y) que geram vouchers
  vouchers   -> cada voucher unico com codigo, validade, status
  audit_log  -> registro de quem validou o que, quando, em qual unidade

Senhas sao armazenadas com hash (pbkdf2 + salt) - nunca em texto puro.
"""
import sqlite3
import os
import hashlib
import secrets
import datetime as dt

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "bauru.db")

# ----- helpers de senha -----
def hash_password(password: str, salt: bytes = None) -> tuple[str, str]:
    """Retorna (hash_hex, salt_hex). Usa PBKDF2-SHA256 com 200k iteracoes."""
    if salt is None:
        salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200000)
    return dk.hex(), salt.hex()

def verify_password(password: str, stored_hash: str, stored_salt: str) -> bool:
    h, _ = hash_password(password, bytes.fromhex(stored_salt))
    return h == stored_hash

# ----- conexao / schema -----
def get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS units (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        city TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS managers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE,
        full_name TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        password_salt TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'manager',   -- 'admin' ou 'manager'
        unit_id INTEGER REFERENCES units(id),   -- NULL para admin
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS campaigns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        partner TEXT,                            -- ex: 'Radio Cultura', 'Parceria X'
        description TEXT,
        created_by INTEGER REFERENCES managers(id),
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS vouchers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL UNIQUE,               -- codigo unico (impresso no QR)
        campaign_id INTEGER REFERENCES campaigns(id),
        unit_id INTEGER REFERENCES units(id),    -- unidade onde foi emitido/valido
        description TEXT,
        value REAL,
        beneficiary TEXT,                        -- nome do cliente/ouvinte, opcional
        status TEXT NOT NULL DEFAULT 'active',   -- 'active', 'used', 'expired', 'cancelled'
        created_at TEXT NOT NULL,
        valid_until TEXT,                        -- ISO date ou NULL = sem expiracao
        used_at TEXT,
        used_by INTEGER REFERENCES managers(id),
        used_unit_id INTEGER REFERENCES units(id)
    );

    CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        action TEXT NOT NULL,                    -- 'validate', 'generate', 'cancel', 'login', etc
        manager_id INTEGER REFERENCES managers(id),
        unit_id INTEGER REFERENCES units(id),
        voucher_id INTEGER REFERENCES vouchers(id),
        detail TEXT,
        created_at TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_voucher_code ON vouchers(code);
    CREATE INDEX IF NOT EXISTS idx_voucher_status ON vouchers(status);
    CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at);
    """)
    conn.commit()
    # migracoes (colunas extras p/ round-trip com a planilha do Restaurante Bauru)
    _migrate_columns(conn, "vouchers", {
        "data_entrega": "TEXT",
        "phone": "TEXT",
        "responsible": "TEXT",
        "cpf": "TEXT",
        "source": "TEXT DEFAULT 'bot'",
    })
    conn.commit()
    conn.close()

def _migrate_columns(conn, table, cols):
    """Adiciona colunas se nao existirem (idempotente)."""
    cur = conn.execute(f"PRAGMA table_info({table})").fetchall()
    existing = {r[1] for r in cur}
    for name, typ in cols.items():
        if name not in existing:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
            except Exception:
                pass

# ----- units -----
def add_unit(name, city=None):
    conn = get_conn()
    cur = conn.execute("INSERT INTO units (name, city, created_at) VALUES (?,?,?)",
                       (name, city, now_iso()))
    conn.commit(); conn.close()
    return cur.lastrowid

def list_units():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM units ORDER BY name").fetchall()
    conn.close(); return [dict(r) for r in rows]

def get_unit(unit_id):
    conn = get_conn()
    r = conn.execute("SELECT * FROM units WHERE id=?", (unit_id,)).fetchone()
    conn.close(); return dict(r) if r else None

# ----- managers -----
def get_manager(manager_id):
    if not manager_id:
        return None
    conn = get_conn()
    r = conn.execute("SELECT * FROM managers WHERE id=?", (manager_id,)).fetchone()
    conn.close(); return dict(r) if r else None

def reset_manager_password(manager_id, new_password):
    """Define nova senha (hash) para um gerente. Usado quando esquece a senha."""
    h, s = hash_password(new_password)
    conn = get_conn()
    conn.execute("UPDATE managers SET password_hash=?, password_salt=? WHERE id=?",
                 (h, s, manager_id))
    conn.commit(); conn.close()

def update_manager(manager_id, **fields):
    """Atualiza campos do gerente (full_name, username, role, unit_id, active)."""
    allowed = {"full_name", "username", "role", "unit_id", "active"}
    sets = [f"{k}=?" for k in fields if k in allowed]
    vals = [v for k, v in fields.items() if k in allowed]
    if not sets:
        return
    conn = get_conn()
    conn.execute(f"UPDATE managers SET {', '.join(sets)} WHERE id=?", vals + [manager_id])
    conn.commit(); conn.close()

def deactivate_manager(manager_id, by_manager_id=None):
    """Remove (desativa) um gerente. Admin nao pode ser removido por seguranca."""
    m = get_manager(manager_id)
    if not m:
        return {"ok": False, "message": "Gerente não encontrado."}
    if m["role"] == "admin":
        return {"ok": False, "message": "Não é possível remover um administrador."}
    conn = get_conn()
    conn.execute("UPDATE managers SET active=0 WHERE id=?", (manager_id,))
    conn.commit(); conn.close()
    log_audit("deactivate_manager", manager_id=by_manager_id, detail=f"removeu #{manager_id} ({m['username']})")
    return {"ok": True, "message": f"Gerente {m['full_name']} removido (desativado)."}

def add_manager(username, full_name, password, role="manager", unit_id=None):
    h, s = hash_password(password)
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO managers (username, full_name, password_hash, password_salt, role, unit_id, active, created_at) "
        "VALUES (?,?,?,?,?,?,1,?)",
        (username, full_name, h, s, role, unit_id, now_iso()))
    conn.commit(); conn.close()
    return cur.lastrowid

def get_manager_name(manager_id):
    if not manager_id:
        return None
    conn = get_conn()
    r = conn.execute("SELECT full_name FROM managers WHERE id=?", (manager_id,)).fetchone()
    conn.close()
    return r["full_name"] if r else f"#{manager_id}"

def get_manager_by_username(username):
    conn = get_conn()
    r = conn.execute("SELECT * FROM managers WHERE username=?", (username,)).fetchone()
    conn.close(); return dict(r) if r else None

def authenticate(username, password):
    m = get_manager_by_username(username)
    if not m or not m["active"]:
        return None
    if not verify_password(password, m["password_hash"], m["password_salt"]):
        return None
    return m

def list_managers():
    conn = get_conn()
    rows = conn.execute(
        "SELECT m.*, u.name as unit_name FROM managers m LEFT JOIN units u ON m.unit_id=u.id ORDER BY m.role, m.username"
    ).fetchall()
    conn.close(); return [dict(r) for r in rows]

# ----- campaigns -----
def add_campaign(name, partner=None, description=None, created_by=None):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO campaigns (name, partner, description, created_by, created_at) VALUES (?,?,?,?,?)",
        (name, partner, description, created_by, now_iso()))
    conn.commit(); conn.close()
    return cur.lastrowid

def list_campaigns():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM campaigns ORDER BY created_at DESC").fetchall()
    conn.close(); return [dict(r) for r in rows]

# ----- vouchers -----
def generate_voucher_code():
    return "BAU" + secrets.token_hex(5).upper()  # 10 chars apos prefixo

def add_voucher(campaign_id, unit_id, description=None, value=None, beneficiary=None,
                valid_until=None, created_by=None, code=None, data_entrega=None,
                phone=None, responsible=None, cpf=None, source="bot", conn=None):
    own = conn is None
    if own:
        conn = get_conn()
    # se um codigo foi fornecido (ex: importacao de TXT), usa-o; senao gera
    if code:
        code = code.strip().upper()
        # garantir unicidade
        if conn.execute("SELECT 1 FROM vouchers WHERE code=?", (code,)).fetchone():
            return None
    else:
        code = generate_voucher_code()
        while conn.execute("SELECT 1 FROM vouchers WHERE code=?", (code,)).fetchone():
            code = generate_voucher_code()
    cur = conn.execute(
        "INSERT INTO vouchers (code, campaign_id, unit_id, description, value, beneficiary, status, created_at, valid_until, data_entrega, phone, responsible, cpf, source) "
        "VALUES (?,?,?,?,?,?, 'active', ?, ?, ?, ?, ?, ?, ?)",
        (code, campaign_id, unit_id, description, value, beneficiary, now_iso(), valid_until,
         data_entrega, phone, responsible, cpf, source))
    vid = cur.lastrowid
    log_audit("generate", manager_id=created_by, unit_id=unit_id, voucher_id=vid,
              detail=f"Campaign {campaign_id}, code {code}", conn=conn)
    if own:
        conn.commit(); conn.close()
    return code

def bulk_generate(count, campaign_id, unit_id, description=None, value=None,
                  valid_days=None, created_by=None):
    """Gera N vouchers de uma vez (uma unica transacao). Retorna lista de codigos."""
    codes = []
    base = dt.datetime.now()
    conn = get_conn()
    try:
        for _ in range(count):
            vu = None
            if valid_days:
                vu = (base + dt.timedelta(days=valid_days)).date().isoformat()
            codes.append(add_voucher(campaign_id, unit_id, description, value, None, vu, created_by, conn=conn))
        conn.commit()
    finally:
        conn.close()
    return codes

def get_voucher_by_code(code):
    conn = get_conn()
    r = conn.execute("SELECT * FROM vouchers WHERE code=?", (code.strip().upper(),)).fetchone()
    conn.close(); return dict(r) if r else None

def list_all_vouchers(limit=30):
    """Lista vouchers cadastrados (mais recentes primeiro), para o menu 'Ver Vouchers'."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM vouchers ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def voucher_detail(v):
    """Monta texto legivel de detalhes de um voucher (usado em 'Ver Vouchers' / lookup)."""
    unit = get_unit(v["unit_id"])
    used = get_unit(v["used_unit_id"]) if v.get("used_unit_id") else None
    status = {"active": "🟢 Disponível", "used": "✅ Utilizado",
              "cancelled": "⛔ Cancelado", "lost": "❓ Perdido",
              "expired": "⌛ Expirado"}.get(v["status"], v["status"])
    lines = [
        f"🎟️ *Voucher {v['code']}*",
        f"Empresa/Beneficiário: {v.get('description') or v.get('beneficiary') or '-'}",
        f"Valor: R$ {v.get('value') or '0'}",
        f"Unidade destino: {unit['name'] if unit else '-'}",
        f"Status: {status}",
    ]
    if v.get("data_entrega"):
        lines.append(f"Data entrega: {v['data_entrega']}")
    if v.get("phone"):
        lines.append(f"Telefone: {v['phone']}")
    if v.get("responsible"):
        lines.append(f"Responsável: {v['responsible']}")
    if v.get("used_at"):
        lines.append(f"Utilizado em: {v['used_at'][:16].replace('T', ' ')}")
        lines.append(f"Unidade utilização: {used['name'] if used else '-'}")
    return "\n".join(lines)

def count_used_this_month(cpf):
    """Conta quantos vouchers com esse CPF ja foram USADOS no mes atual."""
    if not cpf:
        return 0
    cpf = cpf.strip()
    now = dt.datetime.now()
    mes = now.strftime("%Y-%m")  # prefixo YYYY-MM
    conn = get_conn()
    # used_at esta em ISO 'YYYY-MM-DD HH:MM:SS'; filtra pelo prefixo do mes
    rows = conn.execute(
        "SELECT COUNT(*) c FROM vouchers WHERE cpf=? AND status='used' AND used_at LIKE ?",
        (cpf, mes + "%")).fetchone()["c"]
    conn.close()
    return rows

def validate_voucher(code, manager_id, unit_id):
    """
    Tenta validar um voucher.
    Retorna dict com 'ok' (bool) e 'message'/'voucher'.
    Regras: existe? nao usado? nao cancelado? nao expirado?
    """
    v = get_voucher_by_code(code)
    if not v:
        return {"ok": False, "message": "❌ Voucher não encontrado no sistema."}
    if v["status"] == "used":
        return {"ok": False, "message": "⚠️ Este voucher JÁ FOI UTILIZADO.",
                "voucher": v, "already_used": True}
    if v["status"] == "cancelled":
        return {"ok": False, "message": "⛔ Este voucher foi CANCELADO.", "voucher": v}
    # limite de 2 vouchers por CPF por mes (se o voucher tiver CPF cadastrado)
    if v.get("cpf"):
        usados_mes = count_used_this_month(v["cpf"])
        if usados_mes >= 2:
            return {"ok": False,
                    "message": f"🚫 Limite atingido: o CPF {v['cpf']} já utilizou 2 vouchers neste mês. Não é possível validar outro.",
                    "voucher": v, "cpf_limit": True}
    if v["valid_until"]:
        try:
            if dt.date.fromisoformat(v["valid_until"]) < dt.date.today():
                conn = get_conn()
                conn.execute("UPDATE vouchers SET status='expired' WHERE id=?", (v["id"],))
                conn.commit(); conn.close()
                return {"ok": False, "message": "⌛ Voucher EXPIRADO.", "voucher": v}
        except Exception:
            pass
    # validar (unica conexao para UPDATE + log)
    conn = get_conn()
    now = now_iso()
    try:
        conn.execute(
            "UPDATE vouchers SET status='used', used_at=?, used_by=?, used_unit_id=? WHERE id=?",
            (now, manager_id, unit_id, v["id"]))
        log_audit("validate", manager_id=manager_id, unit_id=unit_id, voucher_id=v["id"],
                  detail=f"code {v['code']}", conn=conn)
        conn.commit()
    finally:
        conn.close()
    v["status"] = "used"; v["used_at"] = now; v["used_by"] = manager_id; v["used_unit_id"] = unit_id
    return {"ok": True, "message": "✅ Voucher VALIDADO com sucesso!", "voucher": v}

def cancel_voucher(code, manager_id, reason=None):
    v = get_voucher_by_code(code)
    if not v:
        return {"ok": False, "message": "Voucher não encontrado."}
    if v["status"] == "used":
        return {"ok": False, "message": "Não é possível cancelar um voucher já usado."}
    conn = get_conn()
    try:
        conn.execute("UPDATE vouchers SET status='cancelled' WHERE id=?", (v["id"],))
        log_audit("cancel", manager_id=manager_id, unit_id=v["unit_id"], voucher_id=v["id"],
                  detail=reason or "sem motivo", conn=conn)
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "message": "Voucher cancelado."}

# ----- audit -----
def log_audit(action, manager_id=None, unit_id=None, voucher_id=None, detail=None, conn=None):
    own = conn is None
    if own:
        conn = get_conn()
    conn.execute(
        "INSERT INTO audit_log (action, manager_id, unit_id, voucher_id, detail, created_at) VALUES (?,?,?,?,?,?)",
        (action, manager_id, unit_id, voucher_id, detail, now_iso()))
    if own:
        conn.commit(); conn.close()

def get_audit(limit=100, manager_id=None, unit_id=None, action=None):
    conn = get_conn()
    q = "SELECT * FROM audit_log WHERE 1=1"
    params = []
    if manager_id: q += " AND manager_id=?"; params.append(manager_id)
    if unit_id: q += " AND unit_id=?"; params.append(unit_id)
    if action: q += " AND action=?"; params.append(action)
    q += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(q, params).fetchall()
    conn.close(); return [dict(r) for r in rows]

# ----- relatorios -----
def report_overview():
    """Conta totais de vouchers por status."""
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) c FROM vouchers").fetchone()["c"]
    rows = conn.execute("SELECT status, COUNT(*) c FROM vouchers GROUP BY status").fetchall()
    by_status = {r["status"]: r["c"] for r in rows}
    # por campanha (inclui vouchers sem campanha = NULL)
    camp = conn.execute(
        "SELECT COALESCE(c.name, 'Sem campanha') as name, COUNT(v.id) total, "
        "SUM(CASE WHEN v.status='used' THEN 1 ELSE 0 END) used "
        "FROM vouchers v LEFT JOIN campaigns c ON v.campaign_id=c.id GROUP BY COALESCE(c.name,'Sem campanha') "
        "ORDER BY total DESC"
    ).fetchall()
    # por unidade (validacoes)
    units = conn.execute(
        "SELECT u.name, COUNT(v.id) validated FROM units u LEFT JOIN vouchers v ON v.used_unit_id=u.id "
        "GROUP BY u.id ORDER BY validated DESC"
    ).fetchall()
    conn.close()
    return {
        "total": total,
        "by_status": by_status,
        "by_campaign": [dict(r) for r in camp],
        "by_unit": [dict(r) for r in units],
    }

def report_validations(limit=20):
    """Retorna as ultimas validacoes com: codigo, cliente/beneficiario, cpf,
    unidade onde foi validado, quem validou (gerente) e quando."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT v.code, v.description, v.beneficiary, v.cpf, "
        "uu.name as unidade_uso, m.full_name as gerente, v.used_at "
        "FROM vouchers v "
        "LEFT JOIN units uu ON v.used_unit_id=uu.id "
        "LEFT JOIN managers m ON v.used_by=m.id "
        "WHERE v.status='used' "
        "ORDER BY v.used_at DESC LIMIT ?",
        (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def now_iso():
    return dt.datetime.now().isoformat(timespec="seconds")

# ----- importacao/exportacao de vouchers via TXT (formato da planilha Bauru) -----
# A planilha exporta: delimitador '|', encoding CP1252 (ANSI), datas DD/MM/AAAA,
# status em portugues (Disponivel/Utilizado/Cancelado/Perdido).
# Colunas: CODIGO|EMPRESA|VALOR|DATA_ENTREGA|UNIDADE_DESTINO|STATUS|
#          DATA_UTILIZACAO|UNIDADE_UTILIZADA|CLIENTE|TELEFONE|RESPONSAVEL

STATUS_MAP_PT = {
    "disponivel": "active",
    "utilizado": "used",
    "cancelado": "cancelled",
    "perdido": "lost",
}
# saida deve bater EXATO com a planilha (grafia/com capitalizacao)
STATUS_MAP_INV = {
    "active": "Disponível",
    "used": "Utilizado",
    "cancelled": "Cancelado",
    "lost": "Perdido",
}

def _read_txt(path):
    """Le arquivo TXT da planilha: CP1252, delimitador |, \r\n ok."""
    with open(path, "rb") as f:
        raw = f.read()
    try:
        text = raw.decode("cp1252")
    except Exception:
        text = raw.decode("latin-1")
    return text

def _parse_date_br(s):
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except Exception:
            continue
    return None

def _parse_datetime_br(s):
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(s, fmt).isoformat(timespec="seconds")
        except Exception:
            continue
    return None

def import_vouchers_txt(path):
    """Importa vouchers do TXT da planilha para o banco.
    Respeita o codigo, empresa, valor, data_entrega, unidade, cliente, telefone,
    responsavel e o status ja existente (se utilizado/cancelado, mantem).
    """
    units = {u["name"].strip().lower(): u["id"] for u in list_units()}
    text = _read_txt(path)
    lines = [l for l in text.splitlines() if l.strip()]
    imported, skipped, errors = [], [], []
    header_ok = False
    for i, line in enumerate(lines, 1):
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 5:
            if i > 1:
                errors.append({"line": i, "raw": line, "reason": "poucas colunas"})
            continue
        # cabeçalho
        if parts[0].upper() == "CODIGO":
            header_ok = True
            continue
        code = parts[0].strip().upper()
        empresa = parts[1]
        valor = parts[2]
        # data_entrega em parts[3]; unidade em parts[4]
        data_entrega = _parse_date_br(parts[3]) if len(parts) > 3 else None
        unidade = parts[4] if len(parts) > 4 else ""
        # status (se houver) em parts[5]
        status_pt = parts[5].strip().lower() if len(parts) > 5 else ""
        # cliente/telefone/responsavel (parts 8,9,10)
        cliente = parts[8] if len(parts) > 8 else ""
        telefone = parts[9] if len(parts) > 9 else ""
        responsavel = parts[10] if len(parts) > 10 else ""
        cpf = parts[11] if len(parts) > 11 else ""
        if not code:
            continue
        if get_voucher_by_code(code):
            skipped.append(code)
            continue
        uid = units.get(unidade.strip().lower())
        if uid is None:
            errors.append({"line": i, "raw": line, "reason": f"unidade '{unidade}' nao encontrada"})
            continue
        val = None
        if valor:
            try:
                val = float(valor.replace(",", ".").replace("r$", "").strip())
            except Exception:
                val = None
        # status inicial conforme planilha
        status_inicial = STATUS_MAP_PT.get(status_pt, "active")
        new_code = add_voucher(None, uid, description=empresa or None, value=val,
                               beneficiary=cliente or None, valid_until=None,
                               created_by=None, code=code, data_entrega=data_entrega,
                               phone=telefone or None, responsible=responsavel or None,
                               cpf=cpf or None, source="planilha")
        if new_code is None:
            skipped.append(code)
            continue
        # se ja estava utilizado/cancelado na planilha, reflete no banco
        if status_inicial in ("used", "cancelled", "lost"):
            conn = get_conn()
            try:
                conn.execute("UPDATE vouchers SET status=? WHERE code=?", (status_inicial, new_code))
                # data de utilizacao vinda da planilha (coluna 7, DD/MM/AAAA HH:MM)
                if status_inicial == "used" and len(parts) > 6 and parts[6].strip():
                    du = _parse_datetime_br(parts[6])
                    if du:
                        conn.execute("UPDATE vouchers SET used_at=? WHERE code=?", (du, new_code))
                conn.commit()
            finally:
                conn.close()
        imported.append(new_code)
    return {"imported": imported, "skipped": skipped, "errors": errors, "header_ok": header_ok}

def export_vouchers_txt(path, only_active=False):
    """Gera TXT no formato da planilha (| , CP1252, DD/MM/AAAA, status PT).
    Colunas: CODIGO|EMPRESA|VALOR|DATA_ENTREGA|UNIDADE_DESTINO|STATUS|
             DATA_UTILIZACAO|UNIDADE_UTILIZADA|CLIENTE|TELEFONE|RESPONSAVEL
    """
    conn = get_conn()
    q = ("SELECT v.code, v.description, v.value, v.data_entrega, u.name as unidade, v.status, "
         "v.used_at, uu.name as unidade_uso, v.beneficiary, v.phone, v.responsible, v.cpf "
         "FROM vouchers v "
         "LEFT JOIN units u ON v.unit_id=u.id "
         "LEFT JOIN units uu ON v.used_unit_id=uu.id")
    if only_active:
        q += " WHERE v.status='active'"
    q += " ORDER BY v.created_at"
    rows = conn.execute(q).fetchall()
    conn.close()
    def fmt_date(d):
        """Formata data/hora ISO para o padrao da planilha: DD/MM/AAAA HH:MM."""
        if not d:
            return ""
        s = str(d)
        if "T" in s:
            s = s.replace("T", " ")[:16]
        try:
            if len(s) > 10:
                return dt.datetime.strptime(s, "%Y-%m-%d %H:%M").strftime("%d/%m/%Y %H:%M")
            return dt.datetime.strptime(s, "%Y-%m-%d").strftime("%d/%m/%Y")  # fallback
        except Exception:
            return s
    lines = ["CODIGO|EMPRESA|VALOR|DATA_ENTREGA|UNIDADE_DESTINO|STATUS|"
             "DATA_UTILIZACAO|UNIDADE_UTILIZADA|CLIENTE|TELEFONE|RESPONSAVEL|CPF"]
    for r in rows:
        valor = f"{r['value']:.2f}".replace(".", ",") if r["value"] is not None else ""
        de = fmt_date(r["data_entrega"])
        status = STATUS_MAP_INV.get(r["status"], "Disponível")
        used = fmt_date(r["used_at"])
        lines.append("|".join([
            r["code"], r["description"] or "", valor, de, r["unidade"] or "",
            status, used, r["unidade_uso"] or "",
            r["beneficiary"] or "", r["phone"] or "", r["responsible"] or "",
            r["cpf"] or ""
        ]))
    # grava em CP1252 (a planilha espera ANSI); newline='' evita \r\r\n no Windows
    with open(path, "w", encoding="cp1252", errors="replace", newline="") as f:
        f.write("\r\n".join(lines) + "\r\n")
    return len(rows)

def export_template_txt(path):
    """Gera um modelo de TXT (formato da planilha) para o usuario preencher."""
    lines = ["CODIGO|EMPRESA|VALOR|DATA_ENTREGA|UNIDADE_DESTINO|STATUS|"
             "DATA_UTILIZACAO|UNIDADE_UTILIZADA|CLIENTE|TELEFONE|RESPONSAVEL|CPF",
             "MIX0001|Radio Mix|100,00|10/08/2026|Barra da Tijuca|Disponivel|||Joao|21999990000|Robson|12345678901",
             "ODIA0001|FM O Dia|150,00|10/08/2027|Barra da Tijuca|Disponivel||||||"]
    with open(path, "w", encoding="cp1252", errors="replace", newline="") as f:
        f.write("\r\n".join(lines) + "\r\n")
    return path


# Lista real de unidades do Restaurante Bauru (fonte: restaurantebauru.com.br)
BAURU_UNITS = [
    ("25 de Agosto", "Duque de Caxias/RJ"),
    ("Belford Roxo", "Belford Roxo/RJ"),
    ("Del Castilho", "Rio de Janeiro/RJ"),
    ("Vila São Luiz", "Duque de Caxias/RJ"),
    ("Ilha do Governador", "Rio de Janeiro/RJ"),
    ("Largo do Bicão", "Rio de Janeiro/RJ"),
    ("Parque Alvorada", "Duque de Caxias/RJ"),
    ("Santa Cruz da Serra", "Duque de Caxias/RJ"),
    ("Freguesia", "Rio de Janeiro/RJ"),
    ("Tijuca", "Rio de Janeiro/RJ"),
    ("São João de Meriti", "São João de Meriti/RJ"),
    ("Nova Iguaçu", "Nova Iguaçu/RJ"),       # em breve
    ("Barra da Tijuca", "Rio de Janeiro/RJ"),
    ("Orlando", "Orlando/FL (EUA)"),          # em breve
]

def seed_demo():
    """Cria dados iniciais (idempotente). Usa as unidades reais do Restaurante Bauru."""
    init_db()
    if not get_manager_by_username("admin"):
        add_manager("admin", "Administrador Bauru", "admin123", role="admin")
    if not list_units():
        for name, city in BAURU_UNITS:
            add_unit(name, city)
        cid = add_campaign("Campanha Inaugural", partner="Restaurante Bauru",
                           description="Voucher de boas-vindas", created_by=1)
        u1 = list_units()[0]["id"]
        bulk_generate(10, cid, u1, description="Voucher boas-vindas", value=10.0,
                     valid_days=30, created_by=1)
    init_db()

if __name__ == "__main__":
    seed_demo()
    print("DB inicializado e dados de demo criados em", DB_PATH)
    print("Unidades:", [u["name"] for u in list_units()])
    print("Overview:", report_overview())
