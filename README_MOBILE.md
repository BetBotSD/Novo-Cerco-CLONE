# CERCO - Canal Mobile

## Visão Geral

Este projeto adiciona um novo canal de saída **Mobile** ao sistema CERCO de detecção de oportunidades de arbitragem, mantendo o canal **Telegram** 100% intacto.

### Canais de Saída

1. **Telegram** (existente) - Envia mensagens e imagens para chat do Telegram
2. **Mobile** (novo) - Salva alerts no PostgreSQL e envia push notifications

---

## Arquitetura

### Componentes Novos

```
src/
├── cerco/
│   ├── db.py              # Acesso ao PostgreSQL
│   ├── mobile_cerco.py    # Canal Mobile (novo)
│   ├── push.py            # Push notifications (Expo)
│   └── telegram_cerco.py  # Canal Telegram (INTACTO)
├── api.py                 # API FastAPI para mobile
└── main.py               # Entry point (modificado)

migrations/
└── 001_initial_schema.sql # Schema PostgreSQL
```

### Banco de Dados (PostgreSQL)

**Tabelas:**
- `users` - Usuários do app mobile
- `alerts` - Oportunidades de arbitragem detectadas
- `device_tokens` - Tokens de push notification

---

## Deploy no Railway

### Serviço 1: Worker (Detecção de Oportunidades)

**Comando:**
```bash
python src/main.py
```

**Variáveis de Ambiente:**
```env
DATABASE_URL=postgresql://...
RAPIDAPI_KEY=...
RAPIDAPI_HOST=pinnacle-odds.p.rapidapi.com
CERCO_TELEGRAM_BOT_TOKEN=...
CERCO_TELEGRAM_CHAT_ID=...
CERCO_TIME_WINDOW_HOURS=2.0
CERCO_POLL_EVERY_SEC=90
CERCO_STAKE_BASE_BRL=1000.0
APP_TZ=America/Bahia
EXPO_ACCESS_TOKEN=...  # Opcional
```

### Serviço 2: API (REST para Mobile)

**Comando:**
```bash
uvicorn src.api:app --host 0.0.0.0 --port $PORT
```

**Variáveis de Ambiente:**
```env
DATABASE_URL=postgresql://...  # Mesmo do Worker
JWT_SECRET_KEY=seu-secret-key-aqui
PORT=$PORT  # Fornecido automaticamente pelo Railway
```

### Banco de Dados

1. Adicione um **PostgreSQL** no Railway
2. Conecte-o aos 2 serviços (Worker e API)
3. A variável `DATABASE_URL` será automaticamente injetada
4. As migrations serão executadas automaticamente no startup

---

## API Endpoints

### Autenticação

**POST /api/auth/register**
```json
{
  "email": "user@example.com",
  "password": "senha123"
}
```

Retorna:
```json
{
  "access_token": "eyJ...",
  "token_type": "bearer",
  "user_id": 1,
  "email": "user@example.com"
}
```

**POST /api/auth/login**
```json
{
  "email": "user@example.com",
  "password": "senha123"
}
```

Retorna JWT token (mesmo formato do register).

---

### Alerts

**GET /api/alerts**

Query params:
- `page` (default: 1)
- `per_page` (default: 20, max: 100)

Headers:
```
Authorization: Bearer <token>
```

Retorna:
```json
{
  "alerts": [
    {
      "id": 1,
      "match_data": {
        "league_name": "Brasil - Serie A",
        "home": "Flamengo",
        "away": "Palmeiras",
        "starts_at_utc": "2025-02-24T19:00:00Z",
        ...
      },
      "opportunities": [
        {
          "family": "moneyline",
          "edge": 0.025,
          "roi": 0.023,
          "profit": 23.50,
          "legs": [...]
        }
      ],
      "created_at": "2025-02-23T19:00:00Z"
    }
  ],
  "total": 50,
  "page": 1,
  "per_page": 20
}
```

**GET /api/alerts/{id}**

Retorna detalhes de um alert específico.

---

### Device Tokens

**POST /api/devices/register**
```json
{
  "token": "ExponentPushToken[xxxxxxxxxxxxxxxxxxxxxx]",
  "platform": "expo"
}
```

Headers:
```
Authorization: Bearer <token>
```

---

## Fluxo de Dados

1. **Worker** detecta oportunidade via `CercoOrchestrator`
2. **Worker** envia para **Telegram** (existente)
3. **Worker** envia para **Mobile**:
   - Salva alert no PostgreSQL (`alerts` table)
   - Busca todos os device tokens
   - Envia push notification via Expo
4. **App Mobile** recebe push e chama `GET /api/alerts/{id}`
5. **API** retorna detalhes da oportunidade

---

## Push Notifications (Expo)

### Formato da Notificação

```json
{
  "to": "ExponentPushToken[...]",
  "sound": "default",
  "title": "🚨 CERCO: 2 oportunidade(s)",
  "body": "Brasil - Serie A\nFlamengo x Palmeiras",
  "priority": "high",
  "data": {
    "type": "cerco_alert",
    "alert_id": 123,
    "league": "Brasil - Serie A",
    "home": "Flamengo",
    "away": "Palmeiras",
    "opportunities_count": 2
  }
}
```

### Expo Access Token (Opcional)

Para maior rate limit, configure:
```env
EXPO_ACCESS_TOKEN=seu-token-aqui
```

Obtém em: https://expo.dev/accounts/[username]/settings/access-tokens

---

## Desenvolvimento Local

### 1. Instalar Dependências

```bash
pip install -r requirements.txt
```

### 2. Configurar Variáveis de Ambiente

```bash
cp .env.example .env
# Edite .env com suas credenciais
```

### 3. Iniciar PostgreSQL (Docker)

```bash
docker run -d \
  --name cerco-postgres \
  -e POSTGRES_USER=cerco \
  -e POSTGRES_PASSWORD=cerco123 \
  -e POSTGRES_DB=cerco_db \
  -p 5432:5432 \
  postgres:15
```

Atualize DATABASE_URL:
```env
DATABASE_URL=postgresql://cerco:cerco123@localhost:5432/cerco_db
```

### 4. Rodar Worker

```bash
python src/main.py
```

### 5. Rodar API (Terminal separado)

```bash
uvicorn src.api:app --reload --port 8000
```

API disponível em: http://localhost:8000

Docs: http://localhost:8000/docs

---

## Testes

### Testar API

```bash
# Health check
curl http://localhost:8000/health

# Registrar usuário
curl -X POST http://localhost:8000/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"test@example.com","password":"senha123"}'

# Login
curl -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"test@example.com","password":"senha123"}'

# Listar alerts (substitua TOKEN)
curl http://localhost:8000/api/alerts \
  -H "Authorization: Bearer <TOKEN>"
```

---

## Troubleshooting

### Worker não está salvando alerts

1. Verifique se `DATABASE_URL` está configurada
2. Verifique logs: `[mobile_cerco]` e `[db]`
3. Teste conexão PostgreSQL:
   ```bash
   psql $DATABASE_URL -c "SELECT * FROM alerts LIMIT 1;"
   ```

### Push notifications não chegam

1. Verifique formato do token: deve começar com `ExponentPushToken[`
2. Verifique logs: `[push]`
3. Teste manualmente: https://expo.dev/notifications

### API retorna 503 "Banco de dados não configurado"

1. Verifique se `DATABASE_URL` está configurada no serviço da API
2. Reinicie o serviço da API no Railway

---

## Segurança

### Produção

1. **JWT_SECRET_KEY**: Use um valor aleatório forte (min 32 chars)
   ```bash
   openssl rand -hex 32
   ```

2. **CORS**: Em `src/api.py`, limite `allow_origins` aos domínios do app

3. **HTTPS**: O Railway fornece HTTPS automaticamente

4. **Rate Limiting**: Considere adicionar rate limiting na API

---

## Próximos Passos

- [ ] Adicionar filtros por liga/esporte na API
- [ ] Implementar notificações personalizadas por usuário
- [ ] Adicionar histórico de alerts lidos/não lidos
- [ ] Implementar webhook para alerts em tempo real
- [ ] Adicionar analytics de oportunidades

---

## Suporte

Em caso de dúvidas ou problemas:
1. Verifique os logs no Railway
2. Consulte a documentação da API: `/docs`
3. Revise as variáveis de ambiente
