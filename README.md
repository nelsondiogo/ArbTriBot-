# nelsondiogo-bot v6.0

Bot de trading automatizado com Dashboard Web — deploy no Render via GitHub.

## Estrutura

```
nelsondiogo-bot/
├── app.py            # Flask app + rotas
├── bot_engine.py     # Loop de trading (thread de background)
├── strategy.py       # Indicadores e sinais (RSI/EMA/ADX)
├── models.py         # SQLAlchemy (SQLite)
├── config.py         # Configurações e env vars
├── crypto_utils.py   # Criptografia AES-256 para API keys
├── requirements.txt
├── Procfile
├── render.yaml
└── templates/
    ├── base.html
    ├── login.html
    ├── dashboard.html
    └── settings.html
```

## Deploy no Render

### 1. Variáveis de ambiente (Environment Variables)

| Variável            | Obrigatória | Descrição                                                |
|---------------------|-------------|----------------------------------------------------------|
| `FLASK_SECRET`      | ✅ Sim       | Chave de criptografia das API keys. Use "Generate Value" |
| `DASHBOARD_PASSWORD`| ✅ Sim       | Senha de acesso ao Dashboard web                         |
| `PORT`              | Automático  | Render define automaticamente (não altere)               |
| `DB_DIR`            | ✅ Sim       | Caminho do disco persistente. Defina como `/data`        |

### 2. Disco Persistente

No painel do Render → seu serviço → **Disks** → Add Disk:
- Mount Path: `/data`
- Size: 1 GB

### 3. Configurar API keys

1. Acesse `https://seu-bot.onrender.com`
2. Faça login com `DASHBOARD_PASSWORD`
3. Vá em **Configurações → Chaves de API**
4. Cole API Key e API Secret → Salvar

As chaves são criptografadas com AES-256 e persistidas no SQLite.

## Estratégia

- **LONG:** RSI 35-65 + EMA9 > EMA21 + ADX > 25 + DI+ > DI- + RSI subindo
- **SHORT:** RSI 35-65 + EMA9 < EMA21 + ADX > 25 + DI- > DI+ + RSI caindo
- **Trailing Stop:** fecha ao recuar X% do pico (configurável no Dashboard, sem parar o bot)
- **Reversão:** fecha na inversão de EMA ou mudança brusca de RSI (com lucro)
- **Capital Shield:** jamais fecha operação que resulte em saldo < 99% do inicial da sessão

## Erro retCode 10006

Corrigido via `enableRateLimit: True` no CCXT — respeita automaticamente os rate limits da exchange.
