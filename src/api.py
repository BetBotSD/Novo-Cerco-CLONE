# -*- coding: utf-8 -*-
# src/api.py
"""
API FastAPI para o aplicativo mobile do CERCO.

Endpoints:
- POST /api/auth/register - Cadastro de usuário
- POST /api/auth/login - Login (retorna JWT)
- GET /api/alerts - Feed paginado de oportunidades
- GET /api/alerts/{id} - Detalhes de uma oportunidade
- POST /api/devices/register - Registra token de push

Deploy no Railway:
    uvicorn src.api:app --host 0.0.0.0 --port $PORT
"""

from __future__ import annotations

import os
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field

try:
    import bcrypt
except ImportError:
    bcrypt = None  # type: ignore

try:
    import jwt
except ImportError:
    jwt = None  # type: ignore

from cerco import db

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

logger = logging.getLogger(__name__)

# JWT
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "change-this-secret-key-in-production")
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 24 * 7  # 7 dias

# FastAPI app
app = FastAPI(
    title="CERCO Mobile API",
    description="API para o aplicativo mobile do sistema CERCO de arbitragem",
    version="1.0.0",
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Em produção, especificar domínios
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Security
security = HTTPBearer()


# ---------------------------------------------------------------------------
# Modelos Pydantic
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=6)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: int
    email: str


class DeviceTokenRequest(BaseModel):
    token: str
    platform: str = "expo"


class AlertResponse(BaseModel):
    id: int
    match_data: Dict[str, Any]
    opportunities: List[Dict[str, Any]]
    created_at: datetime


class AlertListResponse(BaseModel):
    alerts: List[AlertResponse]
    total: int
    page: int
    per_page: int


# ---------------------------------------------------------------------------
# Helpers de autenticação
# ---------------------------------------------------------------------------

def hash_password(password: str) -> str:
    """Gera hash bcrypt da senha."""
    if bcrypt is None:
        raise RuntimeError("bcrypt não instalado")
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    """Verifica se a senha bate com o hash."""
    if bcrypt is None:
        raise RuntimeError("bcrypt não instalado")
    return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))


def create_access_token(user_id: int, email: str) -> str:
    """Cria um JWT token."""
    if jwt is None:
        raise RuntimeError("jwt não instalado")
    
    payload = {
        "sub": str(user_id),
        "email": email,
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRATION_HOURS),
        "iat": datetime.now(timezone.utc),
    }
    
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> Dict[str, Any]:
    """Decodifica e valida um JWT token."""
    if jwt is None:
        raise RuntimeError("jwt não instalado")
    
    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expirado",
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido",
        )


async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> Dict[str, Any]:
    """Dependency para obter usuário autenticado."""
    token = credentials.credentials
    payload = decode_access_token(token)
    
    user_id = int(payload.get("sub", 0))
    if user_id <= 0:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Usuário inválido",
        )
    
    return {
        "user_id": user_id,
        "email": payload.get("email"),
    }


# ---------------------------------------------------------------------------
# Evento de startup
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup_event():
    """Inicializa banco de dados na startup da API."""
    logger.info("[api] Inicializando banco de dados...")
    db.init_db()
    
    if not db.is_enabled():
        logger.warning(
            "[api] Banco de dados NÃO configurado. "
            "Configure DATABASE_URL para habilitar funcionalidades."
        )
    else:
        logger.info("[api] Banco de dados inicializado com sucesso.")


# ---------------------------------------------------------------------------
# Endpoints - Health Check
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    """Health check endpoint."""
    return {
        "service": "CERCO Mobile API",
        "status": "running",
        "database": "connected" if db.is_enabled() else "disconnected",
    }


@app.get("/health")
async def health():
    """Health check detalhado."""
    return {
        "status": "healthy",
        "database": db.is_enabled(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Endpoints - Autenticação
# ---------------------------------------------------------------------------

@app.post("/api/auth/register", response_model=LoginResponse, status_code=status.HTTP_201_CREATED)
async def register(request: RegisterRequest):
    """
    Cadastra um novo usuário.
    
    Retorna JWT token para login automático.
    """
    if not db.is_enabled():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Banco de dados não configurado",
        )
    
    if bcrypt is None or jwt is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Dependências de autenticação não instaladas",
        )
    
    # Verifica se email já existe
    existing_user = db.get_user_by_email(request.email)
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email já cadastrado",
        )
    
    # Cria usuário
    password_hash = hash_password(request.password)
    user_id = db.create_user(request.email, password_hash)
    
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Falha ao criar usuário",
        )
    
    # Gera token
    token = create_access_token(user_id, request.email)
    
    return LoginResponse(
        access_token=token,
        user_id=user_id,
        email=request.email,
    )


@app.post("/api/auth/login", response_model=LoginResponse)
async def login(request: LoginRequest):
    """
    Login de usuário.
    
    Retorna JWT token.
    """
    if not db.is_enabled():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Banco de dados não configurado",
        )
    
    if bcrypt is None or jwt is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Dependências de autenticação não instaladas",
        )
    
    # Busca usuário
    user = db.get_user_by_email(request.email)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email ou senha inválidos",
        )
    
    # Verifica senha
    if not verify_password(request.password, user["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email ou senha inválidos",
        )
    
    # Gera token
    token = create_access_token(user["id"], user["email"])
    
    return LoginResponse(
        access_token=token,
        user_id=user["id"],
        email=user["email"],
    )


# ---------------------------------------------------------------------------
# Endpoints - Alerts
# ---------------------------------------------------------------------------

@app.get("/api/alerts", response_model=AlertListResponse)
async def get_alerts(
    page: int = 1,
    per_page: int = 20,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Lista alerts (oportunidades de arbitragem) com paginação.
    
    Requer autenticação.
    """
    if not db.is_enabled():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Banco de dados não configurado",
        )
    
    # Validação
    if page < 1:
        page = 1
    if per_page < 1 or per_page > 100:
        per_page = 20
    
    offset = (page - 1) * per_page
    
    # Busca alerts
    alerts = db.get_alerts(limit=per_page, offset=offset)
    
    # Formata resposta
    alerts_response = [
        AlertResponse(
            id=alert["id"],
            match_data=alert["match_data"],
            opportunities=alert["opportunities"],
            created_at=alert["created_at"],
        )
        for alert in alerts
    ]
    
    return AlertListResponse(
        alerts=alerts_response,
        total=len(alerts),  # TODO: adicionar count total no db.py se necessário
        page=page,
        per_page=per_page,
    )


@app.get("/api/alerts/{alert_id}", response_model=AlertResponse)
async def get_alert_by_id(
    alert_id: int,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Retorna detalhes de um alert específico.
    
    Requer autenticação.
    """
    if not db.is_enabled():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Banco de dados não configurado",
        )
    
    alert = db.get_alert_by_id(alert_id)
    
    if not alert:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Alert não encontrado",
        )
    
    return AlertResponse(
        id=alert["id"],
        match_data=alert["match_data"],
        opportunities=alert["opportunities"],
        created_at=alert["created_at"],
    )


# ---------------------------------------------------------------------------
# Endpoints - Device Tokens
# ---------------------------------------------------------------------------

@app.post("/api/devices/register", status_code=status.HTTP_201_CREATED)
async def register_device(
    request: DeviceTokenRequest,
    current_user: Dict[str, Any] = Depends(get_current_user),
):
    """
    Registra token de push notification do dispositivo.
    
    Requer autenticação.
    """
    if not db.is_enabled():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Banco de dados não configurado",
        )
    
    user_id = current_user["user_id"]
    
    success = db.register_device_token(
        user_id=user_id,
        token=request.token,
        platform=request.platform,
    )
    
    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Falha ao registrar device token",
        )
    
    return {
        "success": True,
        "message": "Device token registrado com sucesso",
    }


# ---------------------------------------------------------------------------
# Entrypoint para testes locais
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    
    port = int(os.getenv("PORT", "8000"))
    
    uvicorn.run(
        "api:app",
        host="0.0.0.0",
        port=port,
        reload=True,
    )
