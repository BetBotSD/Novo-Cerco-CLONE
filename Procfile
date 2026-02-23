# Procfile para Railway
# Este arquivo define os processos que podem ser executados
# 
# Para usar:
# 1. Crie 2 serviços no Railway
# 2. No primeiro serviço, defina o comando: worker
# 3. No segundo serviço, defina o comando: api

worker: python src/main.py
api: uvicorn src.api:app --host 0.0.0.0 --port $PORT
