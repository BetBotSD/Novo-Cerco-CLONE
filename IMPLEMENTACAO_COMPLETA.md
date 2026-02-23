# ✅ IMPLEMENTAÇÃO CONCLUÍDA - Canal Mobile CERCO

## 📦 O que foi implementado

### ✅ Novos Arquivos Criados

#### Módulos Python:
- `src/cerco/db.py` - Acesso ao PostgreSQL (conexão, queries, migrations)
- `src/cerco/push.py` - Push notifications via Expo Push API
- `src/cerco/mobile_cerco.py` - Novo canal Mobile (paralelo ao Telegram)
- `src/api.py` - API FastAPI completa para o app mobile

#### Migrations:
- `migrations/001_initial_schema.sql` - Schema PostgreSQL (users, alerts, device_tokens)

#### Configuração:
- `.env.example` - Template de variáveis de ambiente
- `Procfile` - Configuração para Railway (2 serviços)
- `RAILWAY_DEPLOY.md` - Instruções detalhadas de deploy
- `README_MOBILE.md` - Documentação completa da API
- `.gitignore` - Arquivos a ignorar no Git
- `test_api.py` - Script de testes de integração

### ✅ Arquivos Modificados

#### `src/main.py`:
- **Linha 48-63**: Adicionado import de `mobile_cerco`
- **Linha 122-160**: Modificado `run_cycle()` para chamar `mobile_cerco.publish_match_result()` após `telegram_cerco.send_match_result()`

**TELEGRAM PERMANECE 100% INTACTO** ✅

#### `requirements.txt`:
- Adicionadas dependências: fastapi, uvicorn, psycopg2-binary, pyjwt, bcrypt, python-multipart

---

## 🚀 Como Fazer Deploy no Railway

### Passo 1: Adicionar PostgreSQL

1. No projeto Railway, clique em **"+ New"**
2. Selecione **"Database" → "PostgreSQL"**
3. O Railway criará automaticamente a variável `DATABASE_URL`

### Passo 2: Criar Serviço Worker (Detecção)

1. Clique em **"+ New" → "GitHub Repo"** (se ainda não conectou)
2. No serviço, configure:
   - **Nome**: `cerco-worker`
   - **Build Command**: (deixe padrão)
   - **Start Command**: `python src/main.py`
   - **Conecte o PostgreSQL** ao serviço

3. Adicione as variáveis de ambiente:
```env
DATABASE_URL=<fornecido-automaticamente-pelo-railway>
RAPIDAPI_KEY=sua-chave
RAPIDAPI_HOST=pinnacle-odds.p.rapidapi.com
CERCO_TELEGRAM_BOT_TOKEN=seu-token
CERCO_TELEGRAM_CHAT_ID=seu-chat-id
CERCO_TIME_WINDOW_HOURS=2.0
CERCO_POLL_EVERY_SEC=90
CERCO_STAKE_BASE_BRL=1000.0
APP_TZ=America/Bahia
EXPO_ACCESS_TOKEN=<opcional>
```

### Passo 3: Criar Serviço API (REST)

1. Clique em **"+ New" → "GitHub Repo"** (mesmo repo)
2. No serviço, configure:
   - **Nome**: `cerco-api`
   - **Build Command**: (deixe padrão)
   - **Start Command**: `uvicorn src.api:app --host 0.0.0.0 --port $PORT`
   - **Conecte o MESMO PostgreSQL** ao serviço

3. Adicione as variáveis de ambiente:
```env
DATABASE_URL=<fornecido-automaticamente-pelo-railway>
JWT_SECRET_KEY=<gere-com-openssl-rand-hex-32>
PORT=<fornecido-automaticamente-pelo-railway>
```

4. O Railway fornecerá uma URL pública para a API (ex: `cerco-api.up.railway.app`)

### Passo 4: Verificar

1. **Worker**: Verifique logs para ver detecção de oportunidades
2. **API**: Acesse `https://sua-api.up.railway.app/health`
3. **Docs da API**: Acesse `https://sua-api.up.railway.app/docs`

---

## 📱 Endpoints da API

### Base URL (Produção)
```
https://sua-api.up.railway.app
```

### Autenticação

**POST /api/auth/register**
```bash
curl -X POST https://sua-api.up.railway.app/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"user@example.com","password":"senha123"}'
```

**POST /api/auth/login**
```bash
curl -X POST https://sua-api.up.railway.app/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"user@example.com","password":"senha123"}'
```

### Alerts

**GET /api/alerts** (requer autenticação)
```bash
curl https://sua-api.up.railway.app/api/alerts \
  -H "Authorization: Bearer <token>"
```

**GET /api/alerts/{id}** (requer autenticação)
```bash
curl https://sua-api.up.railway.app/api/alerts/1 \
  -H "Authorization: Bearer <token>"
```

### Device Tokens

**POST /api/devices/register** (requer autenticação)
```bash
curl -X POST https://sua-api.up.railway.app/api/devices/register \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"token":"ExponentPushToken[xxx]","platform":"expo"}'
```

---

## 🧪 Testar Localmente

### 1. Instalar Dependências
```bash
cd /app/cerco-project
pip install -r requirements.txt
```

### 2. Configurar Banco de Dados
```bash
# Com Docker
docker run -d \
  --name cerco-postgres \
  -e POSTGRES_USER=cerco \
  -e POSTGRES_PASSWORD=cerco123 \
  -e POSTGRES_DB=cerco_db \
  -p 5432:5432 \
  postgres:15

# Atualizar .env
echo "DATABASE_URL=postgresql://cerco:cerco123@localhost:5432/cerco_db" > .env
```

### 3. Rodar Testes
```bash
python test_api.py
```

### 4. Rodar Worker
```bash
python src/main.py
```

### 5. Rodar API (terminal separado)
```bash
uvicorn src.api:app --reload --port 8000
```

Acesse: http://localhost:8000/docs

---

## 🔍 Verificar Integração

### Verificar se Telegram ainda funciona:
```bash
# Nos logs do Worker, você deve ver:
[CERCO] Mensagens enviadas ao Telegram neste ciclo: X
```

### Verificar se Mobile está salvando:
```bash
# Nos logs do Worker, você deve ver:
[mobile_cerco] Alert salvo | id=X | ...
[mobile_cerco] Push notifications enviadas | dispositivos=X | sucesso=X
```

### Verificar banco de dados:
```bash
# Conecte ao PostgreSQL e verifique:
SELECT COUNT(*) FROM alerts;
SELECT COUNT(*) FROM users;
SELECT COUNT(*) FROM device_tokens;
```

---

## 📊 Estrutura do Banco

### Tabela: users
```sql
id              SERIAL PRIMARY KEY
email           VARCHAR(255) UNIQUE NOT NULL
password_hash   VARCHAR(255) NOT NULL
created_at      TIMESTAMP WITH TIME ZONE
```

### Tabela: alerts
```sql
id              SERIAL PRIMARY KEY
match_data      JSONB NOT NULL
opportunities   JSONB NOT NULL
created_at      TIMESTAMP WITH TIME ZONE
```

### Tabela: device_tokens
```sql
id              SERIAL PRIMARY KEY
user_id         INTEGER REFERENCES users(id)
token           VARCHAR(512) UNIQUE NOT NULL
platform        VARCHAR(50) DEFAULT 'expo'
created_at      TIMESTAMP WITH TIME ZONE
```

---

## ✅ Checklist de Implementação

- [x] Criar módulo `db.py` para PostgreSQL
- [x] Criar módulo `push.py` para Expo Push
- [x] Criar módulo `mobile_cerco.py` (novo canal)
- [x] Criar API FastAPI em `api.py`
- [x] Criar migrations SQL (`001_initial_schema.sql`)
- [x] Modificar `main.py` para chamar `mobile_cerco`
- [x] Manter Telegram 100% intacto
- [x] Atualizar `requirements.txt`
- [x] Criar `.env.example`
- [x] Criar documentação (`README_MOBILE.md`)
- [x] Criar instruções de deploy (`RAILWAY_DEPLOY.md`)
- [x] Criar `Procfile` para Railway
- [x] Criar script de testes (`test_api.py`)
- [x] Testar todos os módulos (✅ todos passaram)

---

## 🎯 Status: PRONTO PARA DEPLOY

Todos os arquivos foram criados e testados com sucesso. O sistema está pronto para deploy no Railway seguindo as instruções em `RAILWAY_DEPLOY.md`.

### Resumo de Funcionalidades:

1. **Worker** detecta oportunidades e envia para:
   - ✅ Telegram (intacto)
   - ✅ Mobile (PostgreSQL + Push)

2. **API** fornece endpoints para:
   - ✅ Registro e login de usuários
   - ✅ Listagem de alerts
   - ✅ Registro de device tokens
   - ✅ Autenticação JWT

3. **Push Notifications**:
   - ✅ Notifica usuários via Expo Push API
   - ✅ Inclui dados da oportunidade

---

Para dúvidas ou problemas, consulte:
- `README_MOBILE.md` - Documentação completa
- `RAILWAY_DEPLOY.md` - Instruções de deploy
- `/docs` endpoint da API - Documentação interativa
