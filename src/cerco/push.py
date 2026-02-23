# -*- coding: utf-8 -*-
# src/cerco/push.py
"""
Módulo de push notifications via Expo Push API.

Documentação: https://docs.expo.dev/push-notifications/sending-notifications/
"""

from __future__ import annotations

import os
import logging
from typing import Any, Dict, List, Optional

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"


def get_expo_access_token() -> Optional[str]:
    """Retorna o Expo Access Token (opcional, para maior rate limit)."""
    return os.getenv("EXPO_ACCESS_TOKEN")


def is_valid_expo_token(token: str) -> bool:
    """
    Valida se o token é um Expo Push Token válido.
    
    Formato esperado: ExponentPushToken[xxxxxxxxxxxxxxxxxxxxxx]
    """
    if not token:
        return False
    return token.startswith("ExponentPushToken[") or token.startswith("ExpoPushToken[")


def send_push_notification(
    tokens: List[str],
    title: str,
    body: str,
    data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Envia push notifications via Expo Push API.
    
    Args:
        tokens: Lista de Expo Push Tokens
        title: Título da notificação
        body: Corpo da notificação
        data: Dados extras (opcional)
    
    Returns:
        Dicionário com resultado do envio (success, errors, etc.)
    """
    if httpx is None:
        logger.warning("[push] httpx não instalado; push notifications desabilitadas.")
        return {"success": False, "error": "httpx_not_installed"}
    
    if not tokens:
        logger.debug("[push] Nenhum token fornecido; nada a enviar.")
        return {"success": True, "sent": 0}
    
    # Filtra tokens válidos
    valid_tokens = [t for t in tokens if is_valid_expo_token(t)]
    if not valid_tokens:
        logger.warning(f"[push] Nenhum token válido entre {len(tokens)} fornecidos.")
        return {"success": False, "error": "no_valid_tokens"}
    
    # Monta payload para Expo
    messages = []
    for token in valid_tokens:
        message = {
            "to": token,
            "sound": "default",
            "title": title,
            "body": body,
            "priority": "high",
        }
        if data:
            message["data"] = data
        messages.append(message)
    
    # Headers
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    
    access_token = get_expo_access_token()
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    
    try:
        with httpx.Client(timeout=10.0) as client:
            response = client.post(
                EXPO_PUSH_URL,
                json=messages,
                headers=headers,
            )
        
        if response.status_code != 200:
            logger.error(
                f"[push] Erro ao enviar notificações | status={response.status_code} | response={response.text[:200]}"
            )
            return {
                "success": False,
                "error": f"http_{response.status_code}",
                "details": response.text[:200],
            }
        
        result = response.json()
        data_list = result.get("data", [])
        
        # Conta sucessos e erros
        success_count = 0
        error_count = 0
        errors = []
        
        for item in data_list:
            status = item.get("status")
            if status == "ok":
                success_count += 1
            else:
                error_count += 1
                error_msg = item.get("message", "unknown_error")
                errors.append(error_msg)
        
        logger.info(
            f"[push] Notificações enviadas | sucesso={success_count} | erro={error_count}"
        )
        
        if errors:
            logger.warning(f"[push] Erros encontrados: {errors[:5]}")
        
        return {
            "success": True,
            "sent": success_count,
            "errors": error_count,
            "error_messages": errors[:5],  # Primeiros 5 erros
        }
    
    except Exception as e:
        logger.exception(f"[push] Exceção ao enviar notificações: {e}")
        return {
            "success": False,
            "error": "exception",
            "details": str(e)[:200],
        }


def send_match_notification(
    tokens: List[str],
    league_name: str,
    home: str,
    away: str,
    opportunities_count: int,
    alert_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Envia notificação de oportunidade de arbitragem detectada.
    
    Args:
        tokens: Lista de Expo Push Tokens
        league_name: Nome da liga
        home: Time da casa
        away: Time visitante
        opportunities_count: Número de oportunidades encontradas
        alert_id: ID do alert no banco (opcional)
    
    Returns:
        Resultado do envio
    """
    title = f"🚨 CERCO: {opportunities_count} oportunidade(s)"
    body = f"{league_name}\n{home} x {away}"
    
    data = {
        "type": "cerco_alert",
        "league": league_name,
        "home": home,
        "away": away,
        "opportunities_count": opportunities_count,
    }
    
    if alert_id:
        data["alert_id"] = alert_id
    
    return send_push_notification(
        tokens=tokens,
        title=title,
        body=body,
        data=data,
    )
