# Railway Configuration
# Este arquivo configura 2 serviços separados no Railway

# INSTRUÇÕES DE DEPLOY:
# 1. Crie 2 serviços no Railway a partir deste repositório
# 2. Configure as variáveis de ambiente em cada serviço
# 3. Defina o comando de start para cada serviço:

# ============================================================================
# SERVIÇO 1: CERCO WORKER (Detecção de Oportunidades)
# ============================================================================
# Comando: python src/main.py
# 
# Variáveis de ambiente necessárias:
# - DATABASE_URL (fornecido pelo Railway se adicionar PostgreSQL)
# - RAPIDAPI_KEY
# - RAPIDAPI_HOST
# - CERCO_TELEGRAM_BOT_TOKEN
# - CERCO_TELEGRAM_CHAT_ID
# - CERCO_TIME_WINDOW_HOURS
# - CERCO_POLL_EVERY_SEC
# - CERCO_STAKE_BASE_BRL
# - APP_TZ
# - EXPO_ACCESS_TOKEN (opcional)

# ============================================================================
# SERVIÇO 2: CERCO API (API REST para Mobile)
# ============================================================================
# Comando: uvicorn src.api:app --host 0.0.0.0 --port $PORT
# 
# Variáveis de ambiente necessárias:
# - DATABASE_URL (o mesmo do Worker - compartilhado)
# - JWT_SECRET_KEY
# - PORT (fornecido automaticamente pelo Railway)

# ============================================================================
# BANCO DE DADOS
# ============================================================================
# Adicione um PostgreSQL no Railway e conecte aos 2 serviços
# A variável DATABASE_URL será automaticamente injetada

# ============================================================================
# PROCFILE (ALTERNATIVA)
# ============================================================================
# Se preferir usar Procfile ao invés de configurar manualmente:
# 
# worker: python src/main.py
# api: uvicorn src.api:app --host 0.0.0.0 --port $PORT
