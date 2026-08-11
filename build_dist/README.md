# 🍔 Bot de Controle de Vouchers — Restaurante Bauru

Sistema completo de controle de vouchers via Telegram. Cada gerente tem login/senha
e unidade próprios; o administrador gerencia tudo e gera os QR Codes. Toda validação
é auditada (quem, quando, onde).

---

## ⚠️ Arquitetura (leia 1x)

O **Telegram NÃO é banco de dados**. Ele só entrega mensagens. O "cérebro" roda neste
programa Python + um banco SQLite (arquivo `data/bauru.db`, leve e ilimitado).

```
Gerente manda foto do QR  ──►  Bot lê QR  ──►  Valida no SQLite  ──►  Registra auditoria
```

Para ficar **online 24/7**, o bot precisa rodar num servidor ligado (sua máquina ou host
gratuito — veja "Hospedagem" no fim).

---

## 🚀 Setup rápido (Windows)

1. **Criar o bot no Telegram** — fale com [@BotFather](https://t.me/BotFather):
   `/newbot` → dê um nome → copie o **TOKEN**.

2. **Colar o token** no arquivo `.env`:
   ```
   TELEGRAM_BOT_TOKEN=SEU_TOKEN_AQUI
   ```
   (ou crie `config.json` com `{"token": "SEU_TOKEN_AQUI"}`.)

3. **Instalar dependências** (já feito neste PC, mas para replicar):
   ```bash
   python -m venv venv
   venv\Scripts\activate
   pip install -r requirements.txt
   ```

4. **Criar o banco + dados de exemplo**:
   ```bash
   python db.py
   ```
   Isso cria unidades, gerentes e vouchers de demonstração.

5. **Rodar o bot**:
   ```bash
   python bot.py
   ```
   Deixe a janela aberta. No Telegram, mande `/start` para o seu bot.

---

## 👤 Usuários de demonstração (criados pelo `db.py`)

| Perfil | Usuário | Senha |
|--------|---------|-------|
| 👑 Admin | `admin` | `admin123` |
| 🧑‍💼 Gerente Centro | `gerente1` | `gerente123` |
| 🧑‍💼 Gerente Shopping | `gerente2` | `gerente123` |

> ⚠️ Troque essas senhas no primeiro uso! (no código `db.seed_demo()`, ou pelo menu
> futuro — por enquanto edite em `db.py` e rode `python db.py` novamente).

---

## 📋 Como usar no dia a dia

### Administrador
- **Gerar vouchers**: escolhe Campanha → Unidade → Quantidade → Validade (dias) → Valor → Descrição.
  O bot envia um **PDF com os QR Codes** (+ CSV) pronto para imprimir e distribuir nas rádios/parcerias.
- **Exportar QR**: reenvia PDF dos vouchers ainda ativos de uma campanha.
- **Campanhas / Unidades / Gerentes**: cadastra e lista.
- **Relatórios**: visão geral (gerados × usados × não usados), por campanha, por unidade, e auditoria.

### Gerente (por unidade)
- **Validar via QR**: manda a FOTO do voucher impresso → bot diz se está ativo/usado/expirado.
- **Digitar código**: alternativa se o QR não ler bem.
- **Meu desempenho**: quantos vouchers ele validou e o total da unidade.

Toda validação grava: código, gerente, unidade, data/hora → rastreabilidade total.

---

## 🔐 Segurança

- Senhas: **hash PBKDF2** (nunca em texto puro no banco).
- QR Codes levam **assinatura HMAC** (`code|assinatura`). Um voucher forjado (QR falsificado)
  é detectado como "assinatura inválida" e **não é validado**.
- Cada validação é registrada em `audit_log` — você sempre sabe quem ativou o quê.

---

## 🗂️ Estrutura

```
bauru_voucher_bot/
├── bot.py              # Lógica do bot Telegram (menus, login, validação)
├── db.py               # Banco SQLite + helpers (unidades, gerentes, vouchers, auditoria)
├── vouchers.py         # Geração de QR codes + export PDF/CSV + assinatura
├── data/bauru.db       # Banco (criado automaticamente)
├── data/secret.key     # Chave da assinatura dos QR (NÃO compartilhe)
├── vouchers_export/    # PDFs/CSV gerados
├── .env                # Seu token (NÃO commite)
└── requirements.txt
```

---

## ☁️ Hospedagem 24/7 (opções gratuitas)

O bot precisa de um processo Python sempre ligado. Opções:

1. **Seu PC / servidor interno** — rode `python bot.py` numa tela/serviço. Simples, mas
   precisa ficar ligado.
2. **Render.com (grátis)** — crie um Web Service, aponte o repo, Build: `pip install -r
   requirements.txt`, Start: `python bot.py`. Use o plano gratuito (pode "dormir" após
   inatividade — para sempre-on use Fly.io ou Railway).
3. **Railway / Fly.io** — planos pequenos sempre ativos, bom para produção real.

Para produção, também recomendo agendar backup do arquivo `data/bauru.db` (é só 1 arquivo).

---

## 🧩 Próximos passos sugeridos (posso implementar)

- [ ] Troca de senha pelo próprio bot (admin redefine).
- [ ] Bloquear validação de voucher de outra unidade (regra de escopo).
- [ ] Relatórios em Excel/PDF periódicos via cron.
- [ ] Webhook em vez de polling (melhor para escala).
- [ ] Painel web (dashboard) além do Telegram.
- [ ] Notificação quando um voucher expirar ou for usado.
