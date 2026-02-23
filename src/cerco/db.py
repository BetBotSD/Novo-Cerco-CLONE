# -*- coding: utf-8 -*-
# src/cerco/db.py
"""
Módulo de acesso ao banco de dados PostgreSQL para o canal Mobile.

Funções principais:
- init_db(): Inicializa conexão e cria tabelas se necessário
- save_alert(): Salva uma oportunidade de arbitragem
- get_alerts(): Lista alerts com paginação
- get_alert_by_id(): Busca alert específico
- create_user(): Cria novo usuário
- get_user_by_email(): Busca usuário por email
- register_device_token(): Registra token de push
- get_all_device_tokens(): Retorna todos os tokens ativos
"""

from __future__ import annotations

import os
import logging
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone
import json

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor, Json
    from psycopg2.pool import SimpleConnectionPool
except ImportError:
    psycopg2 = None  # type: ignore
    RealDictCursor = None  # type: ignore
    Json = None  # type: ignore
    SimpleConnectionPool = None  # type: ignore

logger = logging.getLogger(__name__)

# Pool de conexões (inicializado sob demanda)
_pool: Optional[Any] = None


def get_database_url() -> str:
    """Retorna a URL do banco de dados PostgreSQL."""
    return os.getenv("DATABASE_URL", "")


def init_db() -> None:
    """Inicializa o pool de conexões PostgreSQL."""
    global _pool
    
    if psycopg2 is None:
        logger.warning("[db] psycopg2 não instalado; funcionalidades de DB desabilitadas.")
        return
    
    db_url = get_database_url()
    if not db_url:
        logger.warning("[db] DATABASE_URL não configurada; funcionalidades de DB desabilitadas.")
        return
    
    try:
        _pool = SimpleConnectionPool(
            minconn=1,
            maxconn=10,
            dsn=db_url
        )
        logger.info("[db] Pool de conexões PostgreSQL inicializado.")
        
        # Executa migrations se necessário
        _run_migrations()
    except Exception as e:
        logger.exception(f"[db] Falha ao inicializar pool de conexões: {e}")
        _pool = None


def _run_migrations() -> None:
    """Executa migrations SQL se necessário."""
    if _pool is None:
        return
    
    migrations_path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "migrations",
        "001_initial_schema.sql"
    )
    
    if not os.path.exists(migrations_path):
        logger.warning(f"[db] Migration file não encontrado: {migrations_path}")
        return
    
    try:
        conn = _pool.getconn()
        try:
            with conn.cursor() as cur:
                with open(migrations_path, "r", encoding="utf-8") as f:
                    sql = f.read()
                cur.execute(sql)
            conn.commit()
            logger.info("[db] Migrations executadas com sucesso.")
        finally:
            _pool.putconn(conn)
    except Exception as e:
        logger.exception(f"[db] Falha ao executar migrations: {e}")


def get_connection():
    """Obtém uma conexão do pool."""
    if _pool is None:
        raise RuntimeError("Pool de conexões não inicializado. Chame init_db() primeiro.")
    return _pool.getconn()


def return_connection(conn) -> None:
    """Retorna uma conexão ao pool."""
    if _pool is not None:
        _pool.putconn(conn)


def is_enabled() -> bool:
    """Retorna True se o banco de dados está configurado e disponível."""
    return _pool is not None and psycopg2 is not None


# ---------------------------------------------------------------------------
# ALERTS
# ---------------------------------------------------------------------------

def save_alert(match_data: Dict[str, Any], opportunities: List[Dict[str, Any]]) -> Optional[int]:
    """
    Salva uma nova oportunidade de arbitragem no banco.
    
    Args:
        match_data: Dados do confronto (serializado do CercoMatchResult.snapshot)
        opportunities: Lista de oportunidades (serializado de ArbitrageOpportunity)
    
    Returns:
        ID do alert criado ou None em caso de erro
    """
    if not is_enabled():
        logger.debug("[db] save_alert: banco desabilitado")
        return None
    
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO alerts (match_data, opportunities)
                    VALUES (%s, %s)
                    RETURNING id
                    """,
                    (Json(match_data), Json(opportunities))
                )
                alert_id = cur.fetchone()[0]
            conn.commit()
            logger.info(f"[db] Alert salvo | id={alert_id}")
            return alert_id
        finally:
            return_connection(conn)
    except Exception as e:
        logger.exception(f"[db] Falha ao salvar alert: {e}")
        return None


def get_alerts(limit: int = 50, offset: int = 0) -> List[Dict[str, Any]]:
    """
    Lista alerts com paginação.
    
    Args:
        limit: Número máximo de resultados
        offset: Offset para paginação
    
    Returns:
        Lista de alerts
    """
    if not is_enabled():
        return []
    
    try:
        conn = get_connection()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT id, match_data, opportunities, created_at
                    FROM alerts
                    ORDER BY created_at DESC
                    LIMIT %s OFFSET %s
                    """,
                    (limit, offset)
                )
                rows = cur.fetchall()
                return [dict(row) for row in rows]
        finally:
            return_connection(conn)
    except Exception as e:
        logger.exception(f"[db] Falha ao buscar alerts: {e}")
        return []


def get_alert_by_id(alert_id: int) -> Optional[Dict[str, Any]]:
    """
    Busca um alert específico por ID.
    
    Args:
        alert_id: ID do alert
    
    Returns:
        Alert ou None se não encontrado
    """
    if not is_enabled():
        return None
    
    try:
        conn = get_connection()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT id, match_data, opportunities, created_at
                    FROM alerts
                    WHERE id = %s
                    """,
                    (alert_id,)
                )
                row = cur.fetchone()
                return dict(row) if row else None
        finally:
            return_connection(conn)
    except Exception as e:
        logger.exception(f"[db] Falha ao buscar alert {alert_id}: {e}")
        return None


# ---------------------------------------------------------------------------
# USERS
# ---------------------------------------------------------------------------

def create_user(email: str, password_hash: str) -> Optional[int]:
    """
    Cria um novo usuário.
    
    Args:
        email: Email do usuário
        password_hash: Hash da senha (bcrypt)
    
    Returns:
        ID do usuário criado ou None em caso de erro
    """
    if not is_enabled():
        logger.debug("[db] create_user: banco desabilitado")
        return None
    
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO users (email, password_hash)
                    VALUES (%s, %s)
                    RETURNING id
                    """,
                    (email, password_hash)
                )
                user_id = cur.fetchone()[0]
            conn.commit()
            logger.info(f"[db] Usuário criado | id={user_id} | email={email}")
            return user_id
        finally:
            return_connection(conn)
    except Exception as e:
        logger.exception(f"[db] Falha ao criar usuário: {e}")
        return None


def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    """
    Busca um usuário por email.
    
    Args:
        email: Email do usuário
    
    Returns:
        Dados do usuário ou None se não encontrado
    """
    if not is_enabled():
        return None
    
    try:
        conn = get_connection()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT id, email, password_hash, created_at
                    FROM users
                    WHERE email = %s
                    """,
                    (email,)
                )
                row = cur.fetchone()
                return dict(row) if row else None
        finally:
            return_connection(conn)
    except Exception as e:
        logger.exception(f"[db] Falha ao buscar usuário: {e}")
        return None


# ---------------------------------------------------------------------------
# DEVICE TOKENS
# ---------------------------------------------------------------------------

def register_device_token(user_id: int, token: str, platform: str = "expo") -> bool:
    """
    Registra ou atualiza um token de dispositivo.
    
    Args:
        user_id: ID do usuário
        token: Expo Push Token
        platform: Plataforma (expo, ios, android)
    
    Returns:
        True se sucesso, False caso contrário
    """
    if not is_enabled():
        logger.debug("[db] register_device_token: banco desabilitado")
        return False
    
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                # Upsert: insere ou atualiza se já existir
                cur.execute(
                    """
                    INSERT INTO device_tokens (user_id, token, platform, updated_at)
                    VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                    ON CONFLICT (token)
                    DO UPDATE SET
                        user_id = EXCLUDED.user_id,
                        platform = EXCLUDED.platform,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (user_id, token, platform)
                )
            conn.commit()
            logger.info(f"[db] Device token registrado | user_id={user_id} | platform={platform}")
            return True
        finally:
            return_connection(conn)
    except Exception as e:
        logger.exception(f"[db] Falha ao registrar device token: {e}")
        return False


def get_all_device_tokens() -> List[str]:
    """
    Retorna todos os tokens de dispositivos ativos.
    
    Returns:
        Lista de tokens
    """
    if not is_enabled():
        return []
    
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT token
                    FROM device_tokens
                    ORDER BY created_at DESC
                    """
                )
                rows = cur.fetchall()
                return [row[0] for row in rows]
        finally:
            return_connection(conn)
    except Exception as e:
        logger.exception(f"[db] Falha ao buscar device tokens: {e}")
        return []


def get_user_device_tokens(user_id: int) -> List[str]:
    """
    Retorna os tokens de dispositivos de um usuário específico.
    
    Args:
        user_id: ID do usuário
    
    Returns:
        Lista de tokens
    """
    if not is_enabled():
        return []
    
    try:
        conn = get_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT token
                    FROM device_tokens
                    WHERE user_id = %s
                    ORDER BY created_at DESC
                    """,
                    (user_id,)
                )
                rows = cur.fetchall()
                return [row[0] for row in rows]
        finally:
            return_connection(conn)
    except Exception as e:
        logger.exception(f"[db] Falha ao buscar device tokens do usuário: {e}")
        return []
