# -*- coding: utf-8 -*-
"""
config_cerco.py
----------------
Configurações **exclusivas** do novo sistema CERCO (arbitragem entre casas).

- Tudo aqui pode ser sobrescrito via variável de ambiente.
- Nada de lógica de negócio; apenas parâmetros e helpers simples.

Uso:
    from .config_cerco import (
        RAPIDAPI_KEY, RAPIDAPI_HOST, PINNACLE_BASE_URL,
        HTTP_TIMEOUT_SEC, APP_TZ,
        CERCO_TIME_WINDOW_HOURS, CERCO_POLL_EVERY_SEC,
        CERCO_TODAY_LEAGUES_REFRESH_HOURS,
        CERCO_STAKE_BASE_BRL,
        CERCO_SPORTS,
    )
"""

from __future__ import annotations

import os
from typing import Dict, Any

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9 (não é o seu caso, mas deixo defensivo)
    ZoneInfo = None  # type: ignore


# ---------------------------------------------------------------------------
# Helpers de ENV
# ---------------------------------------------------------------------------

def _env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Config da API (RapidAPI → Pinnacle KIT)
# ---------------------------------------------------------------------------

RAPIDAPI_KEY: str = _env_str("RAPIDAPI_KEY")
RAPIDAPI_HOST: str = _env_str("RAPIDAPI_HOST", "pinnacle-odds.p.rapidapi.com")

PINNACLE_BASE_URL: str = _env_str(
    "PINNACLE_BASE_URL",
    "https://pinnacle-odds.p.rapidapi.com",
)

HTTP_TIMEOUT_SEC: float = _env_float("CERCO_HTTP_TIMEOUT_SEC", 10.0)


# ---------------------------------------------------------------------------
# Fuso horário da aplicação (para logs / comparações humanas)
# Internamente continuamos usando UTC, mas é bom ter um TZ “oficial”.
# ---------------------------------------------------------------------------

APP_TZ_NAME: str = _env_str("APP_TZ", "America/Bahia")
APP_TZ = ZoneInfo(APP_TZ_NAME) if ZoneInfo is not None else None  # type: ignore


# ---------------------------------------------------------------------------
# Janela de tempo e frequências dos loops
# ---------------------------------------------------------------------------

# Janela que o CERCO vai varrer (em horas) a partir de "agora".
# Em produção você vai querer **2.0**, mas podemos sobrescrever por env.
CERCO_TIME_WINDOW_HOURS: float = _env_float("CERCO_TIME_WINDOW_HOURS", 2.0)

# Com qual frequência (segundos) o loop vai rodar no Railway.
# Ex: 120 → a cada 2 minutos.
CERCO_POLL_EVERY_SEC: int = _env_int("CERCO_POLL_EVERY_SEC", 90)

# Com que frequência (em horas) vamos recalcular o snapshot
# de "ligas com eventos hoje" a partir do catálogo grande de ligas.
# Default: 6h (4x ao dia).
CERCO_TODAY_LEAGUES_REFRESH_HOURS: float = _env_float(
    "CERCO_TODAY_LEAGUES_REFRESH_HOURS",
    6.0,
)

# Stake base em BRL para cálculo de sugestões de stakes nas oportunidades
# de arbitragem (ex.: 1000.00).
CERCO_STAKE_BASE_BRL: float = _env_float("CERCO_STAKE_BASE_BRL", 1000.0)

# Mínimo de vantagem (edge) para aceitar arbitragem
CERCO_MIN_EDGE = 0.0   # 0.0%

# Faixa de ROI para classificação visual
CERCO_ROI_SOBRA_MIN = 0.005   # 0.5% → a partir daqui é "Cerco com Sobra"

# ---------------------------------------------------------------------------
# Esportes suportados
# ---------------------------------------------------------------------------
# A chave é o sport_id do KIT.
# O "profile" é o perfil de ligas a usar do leagues_cerco:
#   - "brazil" → somente ligas do Brasil (quando fizer sentido)
#   - "core"   → ligas principais que você definiu
#   - "all"    → lista completona para aquele esporte
#
# Você pode ajustar isso à vontade depois.
# ---------------------------------------------------------------------------

CERCO_SPORTS: Dict[int, Dict[str, Any]] = {
    1: {  # Soccer
        "name": "soccer",
        "profile": _env_str("CERCO_PROFILE_SOCCER", "all"),
    },
    3: {  # Basketball
        "name": "basketball",
        "profile": _env_str("CERCO_PROFILE_BASKETBALL", "core"),
    },
    7: {  # American Football
        "name": "american_football",
        "profile": _env_str("CERCO_PROFILE_AMERICAN_FOOTBALL", "core"),
    },
    2: {  # Tennis
        "name": "tennis",
        "profile": _env_str("CERCO_PROFILE_TENNIS", "core"),
    },
     5: {  # Volleyball
        "name": "volleyball",
        "profile": _env_str("CERCO_PROFILE_VOLLEYBALL", "core"),
    },
}


# ---------------------------------------------------------------------------
# Função helper para logar configs no boot
# ---------------------------------------------------------------------------

def dump_config_for_logging() -> dict:
    """Retorna um dicionário resumido para aparecer no log inicial."""
    return {
        "RAPIDAPI_HOST": RAPIDAPI_HOST,
        "PINNACLE_BASE_URL": PINNACLE_BASE_URL,
        "HTTP_TIMEOUT_SEC": HTTP_TIMEOUT_SEC,
        "APP_TZ": APP_TZ_NAME,
        "CERCO_TIME_WINDOW_HOURS": CERCO_TIME_WINDOW_HOURS,
        "CERCO_POLL_EVERY_SEC": CERCO_POLL_EVERY_SEC,
        "CERCO_TODAY_LEAGUES_REFRESH_HOURS": CERCO_TODAY_LEAGUES_REFRESH_HOURS,
        "CERCO_STAKE_BASE_BRL": CERCO_STAKE_BASE_BRL,
        "CERCO_SPORTS": CERCO_SPORTS,
    }
