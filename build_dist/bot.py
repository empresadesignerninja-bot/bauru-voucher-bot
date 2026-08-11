"""
Bot Telegram - Sistema de Controle de Vouchers Bauru.

Fluxo:
  /start -> login (usuario/senha) -> menu conforme perfil (admin | gerente)
  Admin: gerecia unidades, gerentes, campanhas, gera vouchers + QR (PDF/CSV),
         relatorios, exporta, auditoria.
  Gerente: valida voucher via foto do QR (ou digita codigo), ve seu desempenho.
  Ambos: /logout, /menu.

Requer var de ambiente TELEGRAM_BOT_TOKEN (ou arquivo config.json com {"token": "..."}).
"""
import os
import sys
import asyncio
import logging
import re
import datetime as dt

import cv2
import numpy as np
from dotenv import load_dotenv  # opcional; se nao tiver, ignora
from telegram.error import BadRequest

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bauru_bot")

# tenta carregar .env se existir (python-dotenv opcional)
try:
    load_dotenv()
except Exception:
    pass

# adiciona pasta ao path p/ importar db e vouchers
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db
import vouchers as vch

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TOKEN:
    cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if os.path.exists(cfg):
        import json
        TOKEN = json.load(open(cfg)).get("token")

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, Update, InputFile)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters)

# ---------- helpers de UI ----------
def kb(buttons, cols=2):
    """buttons: lista de (texto, callback_data). Monta grid."""
    rows = []
    for i in range(0, len(buttons), cols):
        rows.append([InlineKeyboardButton(t, callback_data=d)
                     for t, d in buttons[i:i+cols]])
    return InlineKeyboardMarkup(rows)

_MD_SPECIAL = re.compile(r"([_*`\[\]()~>#+\-=|{}.!])")
def md_escape(s):
    """Escapa caracteres reservados do Markdown para o Telegram."""
    if s is None:
        return ""
    return _MD_SPECIAL.sub(r"\\\1", str(s))

def user_manager(ctx: ContextTypes.DEFAULT_TYPE):
    return ctx.user_data.get("manager")

def is_admin(ctx):
    m = user_manager(ctx)
    return bool(m and m["role"] == "admin")

def main_menu(ctx):
    m = user_manager(ctx)
    if m and m["role"] == "admin":
        buttons = [
            ("🎟️ Vouchers", "vchr_menu"),
            ("📤 Exportar QR", "export_menu"),
            ("🎯 Campanhas", "camp_list"),
            ("👥 Unidades", "unit_list"),
            ("👤 Gerentes", "mgr_list"),
            ("📊 Relatórios", "report_menu"),
            ("🔍 Validar Voucher", "validate_photo"),
            ("🚪 Sair", "logout"),
        ]
    else:
        buttons = [
            ("📷 Validar Voucher (QR)", "validate_photo"),
            ("⌨️ Digitar código", "validate_type"),
            ("📊 Meu desempenho", "my_stats"),
            ("🚪 Sair", "logout"),
        ]
    return kb(buttons, cols=2)

# ---------- /start / login ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if user_manager(ctx):
        await update.message.reply_text("✅ Você já está logado.", reply_markup=main_menu(ctx))
        return
    await update.message.reply_text(
        "🔐 *Bem-vindo ao Bot de Vouchers Bauru*\n\n"
        "Informe seu usuário para acessar o sistema.",
        parse_mode="Markdown")
    ctx.user_data["await"] = "login_user"

async def logout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("👋 Logout realizado. Use /start para entrar novamente.")

async def menu_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not user_manager(ctx):
        await update.message.reply_text("Use /start para fazer login.")
        return
    await update.message.reply_text("📋 Menu principal:", reply_markup=main_menu(ctx))

# ---------- login via callback ----------
async def login_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("🔑 Digite seu usuário:")
    ctx.user_data["await"] = "login_user"

# ---------- handler de texto (login + inputs pendentes) ----------
async def text_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip()
    mgr = user_manager(ctx)
    if not mgr:
        aw = ctx.user_data.get("await")
        if aw == "login_user":
            ctx.user_data["login_user"] = txt
            ctx.user_data["await"] = "login_pass"
            await update.message.reply_text("🔑 Digite sua senha:")
            return
        if aw == "login_pass":
            user = ctx.user_data.get("login_user")
            ok = db.authenticate(user, txt)
            db.log_audit("login", manager_id=ok["id"] if ok else None, detail=f"user={user}")
            if ok:
                ctx.user_data["manager"] = dict(ok)
                ctx.user_data["await"] = None
                unit = db.get_unit(ok["unit_id"]) if ok["unit_id"] else None
                await update.message.reply_text(
                    f"✅ Login OK, *{ok['full_name']}* ({ok['role']})."
                    + (f"\nUnidade: {unit['name']}" if unit else ""),
                    parse_mode="Markdown", reply_markup=main_menu(ctx))
            else:
                await update.message.reply_text("❌ Usuário ou senha inválidos. Use /start.")
                ctx.user_data.clear()
            return
        # nao logado e sem await
        await update.message.reply_text("Use /start para acessar o sistema.")
        return

    # logado: tratar inputs pendentes
    aw = ctx.user_data.get("await")
    if aw == "validate_type":
        code, valid = vch.verify_payload(txt)
        ctx.user_data["await"] = None
        await show_voucher_info(update, ctx, code, valid)
        return
    if aw == "vchr_lookup":
        ctx.user_data["await"] = None
        code, _ = vch.verify_payload(txt)
        v = db.get_voucher_by_code(code)
        if not v:
            await update.message.reply_text(
                f"❌ Nenhum voucher com o código `{code}`.",
                parse_mode="Markdown", reply_markup=kb([("🔙 Ver Vouchers", "vchr_list")], cols=1))
            return
        detail = db.voucher_detail(v)
        await update.message.reply_text(detail, parse_mode="Markdown",
                                       reply_markup=kb([("🔙 Ver Vouchers", "vchr_list")], cols=1))
        return
    if aw == "add_unit_name":
        ctx.user_data["gen"] = {"unit_name": txt}
        ctx.user_data["await"] = "add_unit_city"
        await update.message.reply_text("🏙️ Cidade (opcional, digite '-' para pular):")
        return
    if aw == "add_unit_city":
        name = ctx.user_data["gen"]["unit_name"]
        city = None if txt == "-" else txt
        db.add_unit(name, city)
        db.log_audit("add_unit", manager_id=mgr["id"], detail=name)
        ctx.user_data["await"] = None
        await update.message.reply_text(f"✅ Unidade *{name}* criada.", parse_mode="Markdown",
                                        reply_markup=main_menu(ctx))
        return
    if aw == "add_mgr_user":
        ctx.user_data["gen"] = {"username": txt}
        ctx.user_data["await"] = "add_mgr_name"
        await update.message.reply_text("👤 Nome completo:")
        return
    if aw == "add_mgr_name":
        ctx.user_data["gen"]["full_name"] = txt
        ctx.user_data["await"] = "add_mgr_pass"
        await update.message.reply_text("🔑 Senha temporária:")
        return
    if aw == "add_mgr_pass":
        ctx.user_data["gen"]["password"] = txt
        # escolher unidade
        units = db.list_units()
        if not units:
            await update.message.reply_text("⚠️ Crie uma unidade antes de cadastrar gerente.")
            ctx.user_data["await"] = None
            return
        btns = [(f"{u['name']}", f"sel_unit:{u['id']}") for u in units]
        btns.append(("🔙 Cancelar", "cancel_await"))
        ctx.user_data["await"] = "add_mgr_unit"
        await update.message.reply_text("🏢 Selecione a unidade:", reply_markup=kb(btns, cols=1))
        return
    if aw == "mgr_resetpw_new":
        g = ctx.user_data.get("gen", {})
        mid = g.get("mgr_id")
        if not mid:
            await update.message.reply_text("❌ Sessão inválida. Use /menu.")
            ctx.user_data["await"] = None
            return
        db.reset_manager_password(mid, txt)
        db.log_audit("reset_password", manager_id=mgr["id"], detail=f"#{mid}")
        ctx.user_data["await"] = None
        m = db.get_manager(mid)
        await update.message.reply_text(
            f"✅ Senha de *{m['full_name']}* redefinida com sucesso.\n"
            f"Nova senha temporária: `{txt}`\n(Passe ao gerente e peça para trocar depois.)",
            parse_mode="Markdown", reply_markup=main_menu(ctx))
        return
    if aw == "mgr_edit_value":
        g = ctx.user_data.get("gen", {})
        mid, field = g.get("mgr_id"), g.get("field")
        if not mid or not field:
            await update.message.reply_text("❌ Sessão inválida. Use /menu.")
            ctx.user_data["await"] = None
            return
        if field == "username":
            # checar duplicidade
            if db.get_manager_by_username(txt) and db.get_manager_by_username(txt)["id"] != mid:
                await update.message.reply_text("❌ Esse usuário já existe. Tente outro.")
                return
        db.update_manager(mid, **{field: txt})
        db.log_audit("edit_manager", manager_id=mgr["id"], detail=f"#{mid} {field}")
        ctx.user_data["await"] = None
        await update.message.reply_text("✅ Dados atualizados.",
                                        reply_markup=kb([("🔙 Ver gerente", f"mgr_view:{mid}")], cols=1))
        return
    if aw == "add_camp_name":
        ctx.user_data["gen"] = {"name": txt}
        ctx.user_data["await"] = "add_camp_partner"
        await update.message.reply_text("🤝 Parceiro (ex: Rádio X) ou '-':")
        return
    if aw == "add_camp_partner":
        ctx.user_data["gen"]["partner"] = None if txt == "-" else txt
        ctx.user_data["await"] = "add_camp_desc"
        await update.message.reply_text("📝 Descrição (ou '-'):")
        return
    if aw == "add_camp_desc":
        ctx.user_data["gen"]["description"] = None if txt == "-" else txt
        g = ctx.user_data["gen"]
        db.add_campaign(g["name"], g["partner"], g["description"], created_by=mgr["id"])
        db.log_audit("add_campaign", manager_id=mgr["id"], detail=g["name"])
        ctx.user_data["await"] = None
        await update.message.reply_text(f"✅ Campanha *{g['name']}* criada.", parse_mode="Markdown",
                                        reply_markup=main_menu(ctx))
        return
    if aw == "gen_qty":
        try:
            n = int(txt)
            assert 1 <= n <= 500
        except Exception:
            await update.message.reply_text("❌ Quantidade inválida (1 a 500).")
            return
        ctx.user_data["gen"]["qty"] = n
        ctx.user_data["await"] = "gen_days"
        await update.message.reply_text("📆 Validade em dias (0 = sem expiração):")
        return
    if aw == "gen_days":
        try:
            d = int(txt)
            assert d >= 0
        except Exception:
            await update.message.reply_text("❌ Dias inválidos.")
            return
        ctx.user_data["gen"]["valid_days"] = d
        ctx.user_data["await"] = "gen_value"
        await update.message.reply_text("💰 Valor (opcional, ex: 8.50 ou '-'):")
        return
    if aw == "gen_value":
        ctx.user_data["gen"]["value"] = None if txt == "-" else float(txt.replace(",", "."))
        ctx.user_data["await"] = "gen_desc"
        await update.message.reply_text("📝 Descrição do voucher (ou '-'):")
        return
    if aw == "gen_desc":
        ctx.user_data["gen"]["description"] = None if txt == "-" else txt
        g = ctx.user_data["gen"]
        # confirmar
        unit = db.get_unit(g["unit_id"])
        camp = next(c for c in db.list_campaigns() if c["id"] == g["campaign_id"])
        validade = (dt.date.today() + dt.timedelta(days=g["valid_days"])).isoformat() if g["valid_days"] else "sem expiração"
        msg = (f"📋 *Confirmar geração*\n\n"
               f"Campanha: {camp['name']}\nUnidade: {unit['name']}\n"
               f"Quantidade: {g['qty']}\nValidade: {validade}\n"
               f"Valor: {g['value'] or 'n/i'}\nDescrição: {g['description'] or 'n/i'}")
        ctx.user_data["await"] = "gen_confirm"
        await update.message.reply_text(msg, parse_mode="Markdown",
                                         reply_markup=kb([("✅ Gerar", "gen_do"), ("🔙 Cancelar", "cancel_await")], cols=2))
        return

    # fallback
    await update.message.reply_text("Comando não reconhecido. Use /menu.", reply_markup=main_menu(ctx))

# ---------- callback queries (menu) ----------
async def cb_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    mgr = user_manager(ctx)
    if not mgr:
        await q.edit_message_text("Sessão expirada. Use /start.")
        return

    # logout
    if data == "logout":
        ctx.user_data.clear()
        await q.edit_message_text("👋 Logout. Use /start.")
        return
    if data == "cancel_await":
        ctx.user_data["await"] = None
        await q.edit_message_text("❌ Cancelado.", reply_markup=main_menu(ctx))
        return
    if data == "menu":
        await q.edit_message_text("📋 Menu:", reply_markup=main_menu(ctx))
        return

    # validar por foto
    if data == "validate_photo":
        ctx.user_data["await"] = "validate_photo"
        await q.edit_message_text("📷 Envie a FOTO do QR Code do voucher para validar.")
        return
    if data == "validate_type":
        ctx.user_data["await"] = "validate_type"
        await q.edit_message_text("⌨️ Digite o código do voucher (ex: BAU...):")
        return

    # confirmar / cancelar validacao (pergunta antes de validar)
    if data.startswith("val_confirm:"):
        code = data[len("val_confirm:"):].strip().upper()
        await process_validation(q, ctx, code, True, True)
        return
    if data == "val_cancel":
        await q.edit_message_text("🔍 Consulta finalizada. Nenhuma validação foi feita.",
                                 reply_markup=main_menu(ctx))
        return

    if is_admin(ctx):
        await admin_cb(q, ctx, data, mgr)
    else:
        await manager_cb(q, ctx, data, mgr)

# ---------- admin callbacks ----------
async def admin_cb(q, ctx, data, mgr):
    if data == "vchr_menu":
        await q.edit_message_text(
            "🎟️ *Vouchers*\n\nEscolha uma ação:",
            parse_mode="Markdown",
            reply_markup=kb([
                ("📥 Importar TXT", "imp_start"),
                ("📤 Exportar TXT", "exp_start"),
                ("👁️ Ver Vouchers", "vchr_list"),
                ("🔙 Menu", "menu"),
            ], cols=2))
        return
    if data == "imp_start":
        await q.edit_message_text(
            "📥 *Importar vouchers via TXT*\n\n"
            "Envie um arquivo `.txt` (gerado pela planilha, encoding ANSI/CP1252) com os vouchers.\n"
            "O bot respeita o CÓDIGO de cada voucher e o status já existente.\n\n"
            "Formato (separado por `|`):\n"
            "`CODIGO|EMPRESA|VALOR|DATA_ENTREGA|UNIDADE_DESTINO|STATUS|DATA_UTILIZACAO|UNIDADE_UTILIZADA|CLIENTE|TELEFONE|RESPONSAVEL`\n"
            "• STATUS pode vir como Disponível/Utilizado/Cancelado\n"
            "• datas em DD/MM/AAAA\n\n"
            "Exemplo de linha:\n`MIX0001|Radio Mix|100,00|10/08/2026|Barra da Tijuca|Disponivel|||Joao|21999990000|Robson`",
            parse_mode="Markdown",
            reply_markup=kb([("📄 Baixar modelo", "imp_template"),
                             ("🔙 Menu", "menu")], cols=2))
        ctx.user_data["await"] = "imp_file"
        return
    if data == "vchr_list":
        rows = db.list_all_vouchers(limit=30)
        if not rows:
            await q.edit_message_text("📭 Nenhum voucher cadastrado ainda.",
                                     reply_markup=kb([("🔙 Vouchers", "vchr_menu")], cols=1))
            return
        lines = ["👁️ *Vouchers cadastrados* (últimos 30):"]
        for v in rows:
            unit = db.get_unit(v["unit_id"])
            st = {"active": "🟢 Disponível", "used": "✅ Utilizado",
                  "cancelled": "⛔ Cancelado", "lost": "❓ Perdido"}.get(v["status"], v["status"])
            lines.append(f"• `{v['code']}` — {md_escape(v.get('description') or '-')} — R$ {v.get('value') or '0'} — {md_escape(unit['name'] if unit else '-')} — {st}")
        lines.append("\n_Digite um código para ver detalhes._")
        await q.edit_message_text("\n".join(lines), parse_mode="Markdown",
                                  reply_markup=kb([
                                      ("📥 Importar", "imp_start"),
                                      ("📤 Exportar", "exp_start"),
                                      ("🔙 Vouchers", "vchr_menu"),
                                  ], cols=2))
        ctx.user_data["await"] = "vchr_lookup"
        return
    if data == "imp_template":
        path = os.path.join(vch.EXPORT_DIR, "modelo_vouchers.txt")
        db.export_template_txt(path)
        await q.edit_message_text("📄 Modelo gerado. Enviando...")
        with open(path, "rb") as fh:
            await ctx.bot.send_document(chat_id=q.message.chat_id,
                                       document=InputFile(fh, filename="modelo_vouchers.txt"),
                                       caption="📄 Modelo de vouchers (formato da planilha). Preencha e envie de volta em 📥 Importar TXT.")
        await q.edit_message_text("✅ Use esse modelo para preencher e enviar de volta.",
                                 reply_markup=main_menu(ctx))
        return
    if data == "exp_start":
        await q.edit_message_text(
            "📤 *Exportar TXT atualizado*\n\n"
            "O arquivo trará o status capturado pelo bot (usado/cancelado, data e unidade).",
            parse_mode="Markdown",
            reply_markup=kb([
                ("📋 Todos os vouchers", "exp_all"),
                ("🟢 Só os ativos", "exp_active"),
                ("🔙 Menu", "menu"),
            ], cols=2))
        return
    if data == "exp_all":
        path = os.path.join(vch.EXPORT_DIR, f"vouchers_atualizado_{dt.datetime.now():%Y%m%d_%H%M%S}.txt")
        n = db.export_vouchers_txt(path, only_active=False)
        await q.edit_message_text(f"✅ {n} vouchers exportados. Enviando...")
        with open(path, "rb") as fh:
            await ctx.bot.send_document(chat_id=q.message.chat_id,
                                       document=InputFile(fh, filename="vouchers_atualizado.txt"),
                                       caption="📤 Vouchers atualizados (status capturado pelo bot). Importe na planilha.")
        await q.edit_message_text("📤 TXT atualizado enviado.", reply_markup=main_menu(ctx))
        return
    if data == "exp_active":
        path = os.path.join(vch.EXPORT_DIR, f"vouchers_ativos_{dt.datetime.now():%Y%m%d_%H%M%S}.txt")
        n = db.export_vouchers_txt(path, only_active=True)
        await q.edit_message_text(f"✅ {n} vouchers ativos exportados. Enviando...")
        with open(path, "rb") as fh:
            await ctx.bot.send_document(chat_id=q.message.chat_id,
                                       document=InputFile(fh, filename="vouchers_ativos.txt"),
                                       caption="📤 Vouchers ainda ativos (não usados/cancelados).")
        await q.edit_message_text("📤 TXT de ativos enviado.", reply_markup=main_menu(ctx))
        return

    if data == "gen_start":
        camps = db.list_campaigns()
        if not camps:
            await q.edit_message_text("⚠️ Crie uma campanha primeiro (menu 🎯 Campanhas).", reply_markup=main_menu(ctx))
            return
        btns = [(f"{c['name']}", f"gen_camp:{c['id']}") for c in camps]
        btns.append(("📄 Sem campanha", "gen_camp:0"))
        btns.append(("🔙 Voltar", "menu"))
        await q.edit_message_text("🎯 Selecione a campanha:", reply_markup=kb(btns, cols=1))
        return
    if data.startswith("gen_camp:"):
        cid = int(data.split(":")[1])
        ctx.user_data["gen"] = {"campaign_id": cid if cid else None}
        units = db.list_units()
        btns = [(u["name"], f"gen_unit:{u['id']}") for u in units]
        btns.append(("🔙 Voltar", "gen_start"))
        await q.edit_message_text("🏢 Selecione a unidade:", reply_markup=kb(btns, cols=1))
        return
    if data.startswith("gen_unit:"):
        uid = int(data.split(":")[1])
        ctx.user_data["gen"]["unit_id"] = uid
        ctx.user_data["await"] = "gen_qty"
        await q.edit_message_text("🔢 Quantos vouchers gerar? (1-500)")
        return
    if data == "gen_do":
        g = ctx.user_data.get("gen", {})
        try:
            codes = db.bulk_generate(g["qty"], g["campaign_id"], g["unit_id"],
                                     g.get("description"), g.get("value"),
                                     g.get("valid_days"), created_by=mgr["id"])
            camp = next((c for c in db.list_campaigns() if c["id"] == g["campaign_id"]), None) if g.get("campaign_id") else None
            camp_name = camp["name"] if camp else "Sem campanha"
            unit = db.get_unit(g["unit_id"])
            validade = (dt.date.today() + dt.timedelta(days=g["valid_days"])).isoformat() if g["valid_days"] else "sem expiração"
            meta = {"title": f"{camp_name} - {unit['name']}", "validade": validade}
            pdf = vch.export_vouchers_pdf(codes, meta, os.path.join(vch.EXPORT_DIR, f"vouchers_{dt.datetime.now():%Y%m%d_%H%M%S}.pdf"))
            csvp = vch.export_vouchers_csv(codes, meta, os.path.join(vch.EXPORT_DIR, f"vouchers_{dt.datetime.now():%Y%m%d_%H%M%S}.csv"))
            ctx.user_data["await"] = None
            await q.edit_message_text(
                f"✅ *{len(codes)} vouchers gerados!* (Campanha: {camp_name})\n\nEnvando PDF com os QR Codes...",
                parse_mode="Markdown")
            await ctx.bot.send_document(chat_id=q.message.chat_id, document=InputFile(pdf))
            await ctx.bot.send_document(chat_id=q.message.chat_id, document=InputFile(csvp))
        except Exception as e:
            log.exception("erro gerar")
            await q.edit_message_text(f"❌ Erro ao gerar: {e}", reply_markup=main_menu(ctx))
        return

    if data == "export_menu":
        # exporta todos os vouchers ainda ativos (nao usados) como QR? 
        # Aqui exportamos um lote especifico: reusa gen_start p/ escolher campanha/unidade
        # Simplificacao: gera QR de vouchers ativos da campanha escolhida.
        camps = db.list_campaigns()
        btns = [(f"{c['name']}", f"exp_camp:{c['id']}") for c in camps]
        btns.append(("🔙 Voltar", "menu"))
        await q.edit_message_text("📤 Exportar QR de vouchers ATIVOS da campanha:", reply_markup=kb(btns, cols=1))
        return
    if data.startswith("exp_camp:"):
        cid = int(data.split(":")[1])
        conn = db.get_conn()
        rows = conn.execute("SELECT code FROM vouchers WHERE campaign_id=? AND status='active'", (cid,)).fetchall()
        conn.close()
        codes = [r["code"] for r in rows]
        if not codes:
            await q.edit_message_text("Nenhum voucher ativo para exportar.", reply_markup=main_menu(ctx))
            return
        camp = next(c for c in db.list_campaigns() if c["id"] == cid)
        meta = {"title": f"QR ativos - {camp['name']}", "validade": "ver banco"}
        pdf = vch.export_vouchers_pdf(codes, meta, os.path.join(vch.EXPORT_DIR, f"export_{camp['name']}_{dt.datetime.now():%Y%m%d_%H%M%S}.pdf"))
        await q.edit_message_text(f"✅ {len(codes)} QR codes ativos exportados. Enviando...")
        await ctx.bot.send_document(chat_id=q.message.chat_id, document=InputFile(pdf))
        return

    if data == "camp_list":
        camps = db.list_campaigns()
        txt = "🎯 *Campanhas:*\n" + ("\n".join(f"• {c['name']} ({c['partner'] or 'sem parceiro'})" for c in camps) or "— nenhuma —")
        await q.edit_message_text(txt, parse_mode="Markdown",
                                  reply_markup=kb([("➕ Nova campanha", "camp_new"), ("🔙 Menu", "menu")], cols=2))
        return
    if data == "camp_new":
        ctx.user_data["await"] = "add_camp_name"
        await q.edit_message_text("🎯 Nome da campanha:")
        return

    if data == "unit_list":
        units = db.list_units()
        txt = "👥 *Unidades:*\n" + ("\n".join(f"• {u['name']} ({u['city'] or '-'})" for u in units) or "— nenhuma —")
        await q.edit_message_text(txt, parse_mode="Markdown",
                                  reply_markup=kb([("➕ Nova unidade", "unit_new"), ("🔙 Menu", "menu")], cols=2))
        return
    if data == "unit_new":
        ctx.user_data["await"] = "add_unit_name"
        await q.edit_message_text("👥 Nome da unidade:")
        return

    if data == "mgr_list":
        mgrs = db.list_managers()
        lines = []
        btns = []
        for m in mgrs:
            role = "👑 ADMIN" if m["role"] == "admin" else "🧑‍💼 Gerente"
            un = m["unit_name"] or "—"
            st = "✅" if m["active"] else "⛔"
            lines.append(f"{st} {role} {m['full_name']} (@{m['username']}) → {un}")
            # soh gerentes ativos podem ser editados/removidos; admin mostra soh ver
            if m["role"] == "admin":
                btns.append((f"👁 {m['full_name']}", f"mgr_view:{m['id']}"))
            else:
                btns.append((f"✏️ {m['full_name']}", f"mgr_view:{m['id']}"))
        txt = "👤 *Gerentes:*\n" + ("\n".join(lines) or "— nenhum —")
        btns.append(("➕ Novo gerente", "mgr_new"))
        btns.append(("🔙 Menu", "menu"))
        await q.edit_message_text(txt, parse_mode="Markdown",
                                  reply_markup=kb(btns, cols=2))
        return
    if data.startswith("mgr_view:"):
        mid = int(data.split(":")[1])
        m = db.get_manager(mid)
        if not m:
            await q.edit_message_text("Gerente não encontrado.", reply_markup=main_menu(ctx))
            return
        role = "👑 ADMIN" if m["role"] == "admin" else "🧑‍💼 Gerente"
        unit = db.get_unit(m["unit_id"]) if m["unit_id"] else None
        info = (f"👤 *{m['full_name']}*\n"
                f"Usuário: `{m['username']}`\n"
                f"Perfil: {role}\n"
                f"Unidade: {unit['name'] if unit else '—'}\n"
                f"Status: {'✅ ativo' if m['active'] else '⛔ inativo'}")
        if m["role"] == "admin":
            await q.edit_message_text(info, parse_mode="Markdown",
                                      reply_markup=kb([("🔙 Gerentes", "mgr_list")], cols=1))
        else:
            await q.edit_message_text(info, parse_mode="Markdown",
                                      reply_markup=kb([
                                          ("🔑 Resetar senha", f"mgr_resetpw:{mid}"),
                                          ("✏️ Editar dados", f"mgr_edit:{mid}"),
                                          ("🗑️ Remover", f"mgr_remove:{mid}"),
                                          ("🔙 Gerentes", "mgr_list"),
                                      ], cols=2))
        return
    if data.startswith("mgr_resetpw:"):
        mid = int(data.split(":")[1])
        ctx.user_data["gen"] = {"mgr_id": mid, "op": "resetpw"}
        ctx.user_data["await"] = "mgr_resetpw_new"
        await q.edit_message_text("🔑 Digite a NOVA senha temporária para este gerente:")
        return
    if data.startswith("mgr_edit:"):
        mid = int(data.split(":")[1])
        ctx.user_data["gen"] = {"mgr_id": mid}
        m = db.get_manager(mid)
        await q.edit_message_text(
            f"✏️ Editando *{m['full_name']}*.\nO que deseja alterar?",
            parse_mode="Markdown",
            reply_markup=kb([
                ("👤 Nome completo", f"mgr_edit_field:{mid}:full_name"),
                ("🔑 Usuário", f"mgr_edit_field:{mid}:username"),
                ("🏢 Unidade", f"mgr_edit_field:{mid}:unit_id"),
                ("🔙 Voltar", f"mgr_view:{mid}"),
            ], cols=2))
        return
    if data.startswith("mgr_edit_field:"):
        _, mid, field = data.split(":")
        mid = int(mid)
        if field == "unit_id":
            units = db.list_units()
            btns = [(u["name"], f"mgr_set_unit:{mid}:{u['id']}") for u in units]
            btns.append(("🔙 Cancelar", f"mgr_view:{mid}"))
            await q.edit_message_text("🏢 Nova unidade:", reply_markup=kb(btns, cols=1))
        else:
            ctx.user_data["gen"] = {"mgr_id": mid, "field": field}
            ctx.user_data["await"] = "mgr_edit_value"
            label = "nome completo" if field == "full_name" else "usuário"
            await q.edit_message_text(f"Digite o novo {label}:")
        return
    if data.startswith("mgr_set_unit:"):
        _, mid, uid = data.split(":")
        mid, uid = int(mid), int(uid)
        db.update_manager(mid, unit_id=uid)
        db.log_audit("edit_manager", manager_id=mgr["id"], unit_id=uid, detail=f"#{mid} unit->{uid}")
        await q.edit_message_text("✅ Unidade atualizada.", reply_markup=kb([("🔙 Ver gerente", f"mgr_view:{mid}")], cols=1))
        return
    if data.startswith("mgr_remove:"):
        mid = int(data.split(":")[1])
        m = db.get_manager(mid)
        await q.edit_message_text(
            f"⚠️ Remover *{m['full_name']}* (@{m['username']})?\n"
            f"Ele perderá o acesso. Esta ação pode ser desfeita reativando depois.",
            parse_mode="Markdown",
            reply_markup=kb([
                ("🗑️ Sim, remover", f"mgr_remove_confirm:{mid}"),
                ("🔙 Cancelar", f"mgr_view:{mid}"),
            ], cols=2))
        return
    if data.startswith("mgr_remove_confirm:"):
        mid = int(data.split(":")[1])
        res = db.deactivate_manager(mid, by_manager_id=mgr["id"])
        await q.edit_message_text(("✅ " + res["message"]) if res["ok"] else ("❌ " + res["message"]),
                                  reply_markup=kb([("🔙 Gerentes", "mgr_list")], cols=1))
        return
    if data == "mgr_new":
        ctx.user_data["await"] = "add_mgr_user"
        await q.edit_message_text("👤 Username do novo gerente:")
        return
    if data.startswith("sel_unit:"):
        uid = int(data.split(":")[1])
        g = ctx.user_data["gen"]
        g["unit_id"] = uid
        db.add_manager(g["username"], g["full_name"], g["password"], role="manager", unit_id=uid)
        db.log_audit("add_manager", manager_id=mgr["id"], unit_id=uid, detail=g["username"])
        ctx.user_data["await"] = None
        unit = db.get_unit(uid)
        await q.edit_message_text(f"✅ Gerente *{g['full_name']}* criado para {unit['name']}.",
                                 parse_mode="Markdown", reply_markup=main_menu(ctx))
        return

    if data == "report_menu":
        await q.edit_message_text("📊 Relatórios:", reply_markup=kb([
            ("📈 Visão geral", "rep_overview"),
            ("🎯 Por campanha", "rep_camp"),
            ("🏢 Por unidade", "rep_unit"),
            ("📋 Validações", "rep_valid"),
            ("🕓 Auditoria recente", "rep_audit"),
            ("🔙 Menu", "menu"),
        ], cols=2))
        return
    if data == "rep_valid":
        rows = db.report_validations(limit=20)
        if not rows:
            await q.edit_message_text("📋 Nenhuma validação registrada ainda.",
                                     reply_markup=kb([("🔙 Relatórios", "report_menu")], cols=1))
            return
        lines = ["📋 *Validações recentes* (quem / unidade / quando):"]
        for v in rows:
            quando = (v["used_at"] or "")[:16].replace("T", " ")
            cli = v["beneficiary"] or v["description"] or "-"
            cpf = f" (CPF {v['cpf']})" if v["cpf"] else ""
            lines.append(f"• `{v['code']}` — {md_escape(cli)}{cpf}\n  ✓ {md_escape(v['gerente'] or '-')} @ {md_escape(v['unidade_uso'] or '-')} — {quando}")
        await q.edit_message_text("\n".join(lines), parse_mode="Markdown",
                                 reply_markup=kb([("🔙 Relatórios", "report_menu")], cols=1))
        return
    if data == "rep_overview":
        r = db.report_overview()
        bs = r["by_status"]
        msg = (f"📈 *Visão Geral*\n\nTotal de vouchers: *{r['total']}*\n"
               f"🟢 Ativos: {bs.get('active',0)}\n"
               f"✅ Usados: {bs.get('used',0)}\n"
               f"⌛ Expirados: {bs.get('expired',0)}\n"
               f"⛔ Cancelados: {bs.get('cancelled',0)}\n"
               f"\n📉 *Não utilizados (ativos+expirados):* {bs.get('active',0)+bs.get('expired',0)}")
        await q.edit_message_text(msg, parse_mode="Markdown", reply_markup=kb([("🔙 Relatórios", "report_menu")], cols=1))
        return
    if data == "rep_camp":
        r = db.report_overview()
        lines = [f"🎯 *Por Campanha*"]
        for c in r["by_campaign"]:
            lines.append(f"• {md_escape(c['name'])}: {c['used']}/{c['total']} usados")
        await q.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=kb([("🔙 Relatórios", "report_menu")], cols=1))
        return
    if data == "rep_unit":
        r = db.report_overview()
        lines = [f"🏢 *Validações por Unidade*"]
        for u in r["by_unit"]:
            lines.append(f"• {md_escape(u['name'])}: {u['validated']} validados")
        await q.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=kb([("🔙 Relatórios", "report_menu")], cols=1))
        return
    if data == "rep_audit":
        rows = db.get_audit(limit=15)
        lines = ["🕓 *Auditoria (15 últimos)*"]
        for a in rows:
            t = a["created_at"][:16].replace("T", " ")
            mgr_name = db.get_manager_name(a["manager_id"]) if a["manager_id"] else "sistema"
            lines.append(f"• [{t}] {md_escape(a['action'])} por {md_escape(mgr_name)}: {md_escape(a['detail'])}")
        await q.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=kb([("🔙 Relatórios", "report_menu")], cols=1))
        return

    # default
    await q.edit_message_text("Ação desconhecida.", reply_markup=main_menu(ctx))

# ---------- manager callbacks ----------
async def manager_cb(q, ctx, data, mgr):
    if data == "my_stats":
        conn = db.get_conn()
        total = conn.execute("SELECT COUNT(*) c FROM vouchers WHERE used_by=?", (mgr["id"],)).fetchone()["c"]
        unit = db.get_unit(mgr["unit_id"])
        # vouchers da unidade
        u_total = conn.execute("SELECT COUNT(*) c FROM vouchers WHERE unit_id=?", (mgr["unit_id"],)).fetchone()["c"]
        u_used = conn.execute("SELECT COUNT(*) c FROM vouchers WHERE used_unit_id=?", (mgr["unit_id"],)).fetchone()["c"]
        conn.close()
        msg = (f"📊 *Meu Desempenho*\n\nGerente: {mgr['full_name']}\nUnidade: {unit['name'] if unit else '-'}\n"
               f"✅ Validei: *{total}* vouchers\n"
               f"🏢 Na minha unidade: {u_used}/{u_total} usados")
        await q.edit_message_text(msg, parse_mode="Markdown", reply_markup=main_menu(ctx))
        return
    await q.edit_message_text("Ação desconhecida.", reply_markup=main_menu(ctx))

# ---------- validação (foto QR ou código) ----------
async def photo_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    mgr = user_manager(ctx)
    if not mgr:
        await update.message.reply_text("Use /start para login.")
        return
    if ctx.user_data.get("await") != "validate_photo":
        # permite validar a qualquer momento se logado? soh se em modo foto
        await update.message.reply_text("Envie uma foto apenas no modo de validação (menu 📷).")
        return
    ctx.user_data["await"] = None
    try:
        f = await update.message.photo[-1].get_file()
        data = await f.download_as_bytearray()
        arr = np.frombuffer(bytes(data), np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
        det = cv2.QRCodeDetector()
        val, pts, _ = det.detectAndDecode(img)
        if not val:
            # tenta binarizar
            _, img2 = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            val, _, _ = det.detectAndDecode(img2)
        if not val:
            await update.message.reply_text(
                "❌ Não consegui ler o QR. Tente uma foto mais nitidez ou use '⌨️ Digitar código'.",
                reply_markup=main_menu(ctx))
            return
        code, signed = vch.verify_payload(val)
        await show_voucher_info(update, ctx, code, signed)
    except Exception as e:
        log.exception("erro qr")
        await update.message.reply_text(f"❌ Erro ao processar imagem: {e}", reply_markup=main_menu(ctx))

async def show_voucher_info(update, ctx, code, signed):
    """Mostra os dados do voucher e PERGUNTA se o gerente quer validar.
    So valida de fato quando ele confirma (callback val_confirm:<code>)."""
    mgr = user_manager(ctx)
    v = db.get_voucher_by_code(code)
    if not v:
        await update.message.reply_text(
            f"❌ Voucher `{code}` não encontrado no sistema.",
            parse_mode="Markdown", reply_markup=main_menu(ctx))
        return
    if not signed:
        # assinatura invalida -> possivel falsificacao, nem oferece validar
        await update.message.reply_text(
            "⚠️ *QR Code com assinatura INVÁLIDA.* Possível falsificação. Voucher não validado.",
            parse_mode="Markdown", reply_markup=main_menu(ctx))
        db.log_audit("validate_suspect", manager_id=mgr["id"], unit_id=mgr["unit_id"], detail=f"code {code}")
        return
    unit = db.get_unit(v["unit_id"])
    status = {"active": "🟢 Disponível", "used": "✅ Utilizado",
              "cancelled": "⛔ Cancelado", "lost": "❓ Perdido",
              "expired": "⌛ Expirado"}.get(v["status"], v["status"])
    info = (f"🔎 *Consulta de Voucher*\n\n"
            f"🎫 Código: `{v['code']}`\n"
            f"📝 {md_escape(v.get('description') or v.get('beneficiary') or 'voucher')}\n"
            f"💰 Valor: {v.get('value') or 'n/i'}\n"
            f"🏢 Unidade: {md_escape(unit['name'] if unit else '-')}\n"
            f"📌 Status: {status}")
    if v.get("used_at"):
        used = db.get_unit(v["used_unit_id"]) if v.get("used_unit_id") else None
        info += f"\n🕓 Utilizado em: {v['used_at'][:16].replace('T',' ')}"
        info += f"\n🏢 Usado em: {md_escape(used['name'] if used else '-')}"
    # Se ja usado/cancelado/expirado, nao oferece validar de novo
    if v["status"] != "active":
        await update.message.reply_text(info + "\n\n⚠️ Este voucher não está disponível para validação.",
                                        parse_mode="Markdown", reply_markup=main_menu(ctx))
        return
    await update.message.reply_text(
        info + "\n\n✅ Deseja *VALIDAR* este voucher agora?",
        parse_mode="Markdown",
        reply_markup=kb([
            ("✅ Sim, validar", f"val_confirm:{v['code']}"),
            ("🔍 Só consultar", "val_cancel"),
        ], cols=2))

async def process_validation(update, ctx, code, signed_ok, signed):
    """Valida efetivamente o voucher (chamado APENAS ao confirmar)."""
    mgr = user_manager(ctx)
    unit_id = mgr["unit_id"]
    res = db.validate_voucher(code, mgr["id"], unit_id)
    if not signed:
        await update.message.reply_text(
            "⚠️ *QR Code com assinatura INVÁLIDA.* Possível falsificação. Voucher não validado.",
            parse_mode="Markdown", reply_markup=main_menu(ctx))
        db.log_audit("validate_suspect", manager_id=mgr["id"], unit_id=unit_id, detail=f"code {code}")
        return
    if res["ok"]:
        v = res["voucher"]
        await update.message.reply_text(
            f"{res['message']}\n\n"
            f"🎫 Código: `{v['code']}`\n"
            f"📝 {v['description'] or 'voucher'}\n"
            f"💰 Valor: {v['value'] or 'n/i'}\n"
            f"🏢 Unidade: {db.get_unit(v['used_unit_id'])['name'] if v['used_unit_id'] else '-'}\n"
            f"🕓 Em: {v['used_at'][:16].replace('T',' ')}",
            parse_mode="Markdown", reply_markup=main_menu(ctx))
    else:
        v = res.get("voucher")
        extra = ""
        if v and v.get("used_at"):
            unit = db.get_unit(v["used_unit_id"]) if v["used_unit_id"] else None
            extra = f"\n🏢 Usado em: {unit['name'] if unit else '-'}\n🕓 Quando: {v['used_at'][:16].replace('T',' ')}"
        await update.message.reply_text(res["message"] + extra, reply_markup=main_menu(ctx))

# ---------- upload de TXT (importar vouchers) ----------
async def doc_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    mgr = user_manager(ctx)
    if not mgr:
        await update.message.reply_text("Use /start para login.")
        return
    if ctx.user_data.get("await") != "imp_file":
        await update.message.reply_text("Envie o TXT apenas no modo de importação (menu 📥 Importar TXT).")
        return
    ctx.user_data["await"] = None
    doc = update.message.document
    name = (doc.file_name or "").lower()
    mime = (doc.mime_type or "").lower()
    # aceita .txt OU arquivos sem extensao com mime texto/octet (Telegram as vezes manda assim)
    ok_name = name.endswith(".txt") or name == ""
    ok_mime = mime in ("", "text/plain", "application/octet-stream", "text/csv")
    if not (ok_name and ok_mime):
        await update.message.reply_text("❌ Envie um arquivo .txt válido.", reply_markup=main_menu(ctx))
        return
    try:
        f = await doc.get_file()
        data = await f.download_as_bytearray()
        tmp = os.path.join(vch.EXPORT_DIR, f"_imp_{dt.datetime.now():%Y%m%d_%H%M%S}.txt")
        os.makedirs(vch.EXPORT_DIR, exist_ok=True)
        with open(tmp, "wb") as fp:
            fp.write(bytes(data))
        res = db.import_vouchers_txt(tmp)
        os.remove(tmp)
        msg = (f"✅ *Importação concluída*\\n\\n"
               f"📥 Importados: {len(res['imported'])}\\n"
               f"⏭️ Já existiam (ignorados): {len(res['skipped'])}\\n"
               f"❌ Erros: {len(res['errors'])}")
        if res["imported"]:
            msg += "\\n• " + ", ".join(res["imported"][:10]) + ("…" if len(res["imported"]) > 10 else "")
        if res["errors"]:
            msg += "\\n\\n⚠️ Erros:\\n" + "\\n".join(
                f"  linha {e['line']}: {e['reason']}" for e in res["errors"][:8])
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=main_menu(ctx))
    except Exception as e:
        log.exception("erro importar txt")
        await update.message.reply_text(f"❌ Erro ao importar: {e}", reply_markup=main_menu(ctx))

# ---------- main ----------
def main():
    if not TOKEN:
        print("❌ Defina TELEGRAM_BOT_TOKEN (env) ou crie config.json com {'token': '...'}")
        sys.exit(1)
    db.seed_demo()  # init_db + seed idempotente (recria unidades/admin se o disco foi resetado)
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("logout", logout))
    app.add_handler(CallbackQueryHandler(cb_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, doc_handler))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    # ignora erro inofensivo "Message is not modified" (clicar em botao que nao muda a msg)
    async def _ignore_not_modified(update, context):
        err = context.error
        if isinstance(err, BadRequest) and "not modified" in str(err).lower():
            return
        logging.getLogger("bauru").error("Erro nao tratado: %s", err)
    app.add_error_handler(_ignore_not_modified)
    print("🤖 Bot Bauru iniciado. Polling...")
    app.run_polling(allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    main()
