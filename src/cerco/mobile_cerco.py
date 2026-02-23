# -*- coding: utf-8 -*-
# src/cerco/mobile_cerco.py
"""
Canal de saída MOBILE para o sistema CERCO.

Função principal: publish_match_result(match, stake_base, with_image)

Fluxo:
1. Serializa CercoMatchResult para formato JSON
2. Salva alert no PostgreSQL (tabela alerts)
3. Envia push notification para todos os dispositivos registrados
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone

from . import db
from . import push

logger = logging.getLogger(__name__)


def is_enabled() -> bool:
    """
    Retorna True se o canal Mobile está habilitado.
    Requer banco de dados configurado.
    """
    return db.is_enabled()


def _serialize_snapshot(snapshot: Any) -> Dict[str, Any]:
    """
    Serializa CercoEventSnapshot para JSON.
    
    Args:
        snapshot: Objeto CercoEventSnapshot
    
    Returns:
        Dicionário serializado
    """
    try:
        starts_at = getattr(snapshot, "starts_at_utc", None)
        if isinstance(starts_at, datetime):
            starts_at_str = starts_at.isoformat()
        else:
            starts_at_str = None
        
        return {
            "event_id": getattr(snapshot, "event_id", None),
            "sport_id": getattr(snapshot, "sport_id", None),
            "league_id": getattr(snapshot, "league_id", None),
            "league_name": getattr(snapshot, "league_name", ""),
            "home": getattr(snapshot, "home", ""),
            "away": getattr(snapshot, "away", ""),
            "starts_at_utc": starts_at_str,
            "delta_hours_from_now": getattr(snapshot, "delta_hours_from_now", None),
        }
    except Exception as e:
        logger.warning(f"[mobile_cerco] Falha ao serializar snapshot: {e}")
        return {}


def _serialize_opportunity(opp: Any) -> Dict[str, Any]:
    """
    Serializa ArbitrageOpportunity para JSON.
    
    Args:
        opp: Objeto ArbitrageOpportunity
    
    Returns:
        Dicionário serializado
    """
    try:
        legs = []
        for leg in getattr(opp, "legs", []) or []:
            leg_dict = {
                "house": getattr(leg, "house", getattr(leg, "book", getattr(leg, "source", ""))),
                "outcome": getattr(leg, "outcome", getattr(leg, "selection", "")),
                "price": float(getattr(leg, "price", getattr(leg, "odd", getattr(leg, "odds", 0.0))) or 0.0),
                "stake": float(getattr(leg, "stake", 0.0) or 0.0),
            }
            legs.append(leg_dict)
        
        return {
            "family": getattr(opp, "family", ""),
            "line": str(getattr(opp, "line", "") or ""),
            "edge": float(getattr(opp, "edge", 0.0) or 0.0),
            "roi": float(getattr(opp, "roi", 0.0) or 0.0),
            "profit": float(getattr(opp, "profit", 0.0) or 0.0),
            "implied_prob": float(getattr(opp, "implied_prob", 0.0) or 0.0),
            "legs": legs,
        }
    except Exception as e:
        logger.warning(f"[mobile_cerco] Falha ao serializar opportunity: {e}")
        return {}


def publish_match_result(
    match: Any,
    *,
    stake_base: Optional[float] = None,
    with_image: bool = True,
) -> None:
    """
    Publica resultado de match no canal Mobile.
    
    Args:
        match: Objeto CercoMatchResult
        stake_base: Stake base em BRL (não usado diretamente aqui, mas mantido para compat)
        with_image: Flag de imagem (não usado aqui, mantido para compat)
    """
    if not is_enabled():
        logger.debug(
            "[mobile_cerco] Canal Mobile desabilitado (DATABASE_URL não configurada); "
            "nada será publicado."
        )
        return
    
    try:
        # Extrai dados do match
        snapshot = getattr(match, "snapshot", None)
        if snapshot is None:
            logger.warning("[mobile_cerco] Match sem snapshot; ignorando.")
            return
        
        opportunities = getattr(match, "opportunities", []) or []
        if not opportunities:
            logger.debug(
                "[mobile_cerco] Match sem oportunidades; não será publicado. | %s x %s",
                getattr(snapshot, "home", "?"),
                getattr(snapshot, "away", "?"),
            )
            return
        
        # Serializa dados
        match_data = _serialize_snapshot(snapshot)
        match_data["esnet_found"] = getattr(match, "esnet_found", False)
        match_data["esnet_url"] = getattr(match, "esnet_url", None)
        match_data["esnet_meta"] = getattr(match, "esnet_meta", {})
        
        opportunities_data = [_serialize_opportunity(opp) for opp in opportunities]
        
        # Salva no banco
        alert_id = db.save_alert(
            match_data=match_data,
            opportunities=opportunities_data,
        )
        
        if alert_id is None:
            logger.error("[mobile_cerco] Falha ao salvar alert no banco.")
            return
        
        logger.info(
            "[mobile_cerco] Alert salvo | id=%s | %s x %s | oportunidades=%d",
            alert_id,
            getattr(snapshot, "home", "?"),
            getattr(snapshot, "away", "?"),
            len(opportunities),
        )
        
        # Envia push notifications
        tokens = db.get_all_device_tokens()
        if not tokens:
            logger.info("[mobile_cerco] Nenhum device token registrado; push notification não enviada.")
            return
        
        result = push.send_match_notification(
            tokens=tokens,
            league_name=getattr(snapshot, "league_name", ""),
            home=getattr(snapshot, "home", ""),
            away=getattr(snapshot, "away", ""),
            opportunities_count=len(opportunities),
            alert_id=alert_id,
        )
        
        if result.get("success"):
            logger.info(
                "[mobile_cerco] Push notifications enviadas | dispositivos=%d | sucesso=%d",
                len(tokens),
                result.get("sent", 0),
            )
        else:
            logger.warning(
                "[mobile_cerco] Falha ao enviar push notifications | erro=%s",
                result.get("error"),
            )
    
    except Exception:
        logger.exception("[mobile_cerco] Erro inesperado ao publicar match result.")
