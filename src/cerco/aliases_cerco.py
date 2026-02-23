# -*- coding: utf-8 -*-
"""
aliases_cerco.py
----------------
Helpers para normalização e aliases de país/ligas com base no esnet_league_map.json.

Objetivos:
- Extrair, a partir do esnet_league_map.json, mapeamentos:
    * país Pinnacle (inglês)  → país EsporteNet (português)
    * liga Pinnacle (inglês)  → liga EsporteNet (português)
- Fornecer um helper para transformar o "league_name" da Pinnacle
  em uma league_norm mais próxima do formato EsporteNet, usando:
    * Country alias (ex.: "brazil" -> "brasil")
    * League alias (quando houver entrada explícita no JSON)
- Unificar normalização simples (lower, remover acentos, etc.)
  para ser compartilhada entre Orchestrator, SnapshotCache, etc.
"""

from __future__ import annotations

import json
import logging
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, Tuple, Optional, List

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Normalização simples (compartilhada)
# ---------------------------------------------------------------------------

def norm_simple(s: str) -> str:
    """
    Normalização simples e estável:
    - strip
    - lower
    - troca alguns separadores
    - remove acentos (NFKD → ASCII)
    """
    if not s:
        return ""

    # Remove acentos mantendo apenas ASCII
    s = unicodedata.normalize("NFKD", s)
    s = s.encode("ascii", "ignore").decode("ascii")

    return (
        s.strip()
         .lower()
         .replace("–", "-")
         .replace("—", "-")
         .replace("|", " ")
         .replace("/", " ")
    )


def split_country_league(full_name: str) -> Tuple[str, str]:
    """
    Divide "Pais - Liga" em (pais, liga).
    Se não houver "-", devolve (full_name, "").
    """
    if not full_name:
        return "", ""
    parts = full_name.split("-", 1)
    if len(parts) == 1:
        return parts[0].strip(), ""
    return parts[0].strip(), parts[1].strip()


# ---------------------------------------------------------------------------
# Estruturas de aliases
# ---------------------------------------------------------------------------

@dataclass
class LeagueCountryAliases:
    """
    Estrutura para guardar aliases de país e de liga.

    As chaves e valores já vêm normalizados via norm_simple().
    """
    country_alias: Dict[str, str] = field(default_factory=dict)
    league_alias: Dict[str, str] = field(default_factory=dict)

    def transform_pinnacle_league(self, raw_league_name: str) -> str:
        """
        Recebe o league_name "bruto" da Pinnacle (ex.: "Brazil - Serie A")
        e tenta aproximar ao padrão de escrita da EsporteNet, usando:

        - country_alias: ex. "brazil" -> "brasil"
        - league_alias:  ex. "serie a" -> "serie a" (ou outro texto em PT)

        Estratégia:
        1) Se conseguir separar "Pais - Liga", aplica:
            pais_norm  → country_alias[pais_norm] (se existir)
            liga_norm  → league_alias[liga_norm]  (se existir)
        2) Recombina "pais_pt - liga_pt" (ou somente liga se não houver país).
        3) Normaliza de novo com norm_simple para ficar consistente
           com a EsnetSnapshotCache.
        4) Se nada der certo, cai de volta em norm_simple(raw_league_name).
        """
        if not raw_league_name:
            return ""

        country_raw, league_raw = split_country_league(raw_league_name)
        c_norm = norm_simple(country_raw)
        l_norm = norm_simple(league_raw)

        # Se não conseguimos nem separar direito, normaliza direto
        if not c_norm and not l_norm:
            return norm_simple(raw_league_name)

        mapped_country = self.country_alias.get(c_norm, c_norm) if c_norm else ""
        mapped_league = self.league_alias.get(l_norm, l_norm) if l_norm else ""

        # Se não houver alias específico para a liga, reaproveita o nome original
        # já normalizado (l_norm).
        if not mapped_league and l_norm:
            mapped_league = l_norm

        # Se não houver alias específico para o país, reaproveita c_norm.
        if not mapped_country and c_norm:
            mapped_country = c_norm

        if mapped_country and mapped_league:
            combo = f"{mapped_country} - {mapped_league}"
        elif mapped_league:
            combo = mapped_league
        else:
            combo = mapped_country or raw_league_name

        return norm_simple(combo)


# ---------------------------------------------------------------------------
# Construção de aliases a partir do esnet_league_map.json (já carregado)
# ---------------------------------------------------------------------------

def build_aliases_from_league_map(raw_map: Dict[str, Any]) -> LeagueCountryAliases:
    """
    Recebe o dicionário já carregado do esnet_league_map.json e
    constrói os aliases de país e liga.

    Suporta dois formatos:

    1) Formato antigo:
        {
            "leagues": [
                { "pinnacle_name": "...", "esnet_name": "..." },
                ...
            ]
        }

    2) Formato novo (multi-esporte):
        {
            "sports": {
                "soccer": {
                    "pinnacle_sport_id": 1,
                    "esnet_sport_id": 102,
                    "leagues": [
                        { "pinnacle_name": "...", "esnet_name": "..." },
                        ...
                    ]
                },
                "basketball": { ... },
                ...
            }
        }
    """
    aliases = LeagueCountryAliases()

    def _iter_leagues() -> List[Dict[str, Any]]:
        # Formato 1: topo
        leagues_top = raw_map.get("leagues")
        if isinstance(leagues_top, list):
            for it in leagues_top:
                if isinstance(it, dict):
                    yield it

        # Formato 2: sports.<sport>.leagues
        sports = raw_map.get("sports") or {}
        if isinstance(sports, dict):
            for _sport_key, spec in sports.items():
                if not isinstance(spec, dict):
                    continue
                leagues = spec.get("leagues") or []
                if not isinstance(leagues, list):
                    continue
                for it in leagues:
                    if isinstance(it, dict):
                        yield it

    for it in _iter_leagues():
        pinn_name = (it.get("pinnacle_name") or "").strip()
        esnet_name = (it.get("esnet_name") or "").strip()

        # se não temos os dois lados, não usamos para alias
        if not pinn_name or not esnet_name or str(esnet_name).lower() == "null":
            continue

        pinn_country, pinn_league = split_country_league(pinn_name)
        esnet_country, esnet_league = split_country_league(esnet_name)

        pc_norm = norm_simple(pinn_country)
        ec_norm = norm_simple(esnet_country)
        pl_norm = norm_simple(pinn_league)
        el_norm = norm_simple(esnet_league)

        # País
        if pc_norm and ec_norm:
            prev = aliases.country_alias.get(pc_norm)
            if prev and prev != ec_norm:
                logger.debug(
                    "[aliases] conflito country_alias para %r: %r -> %r (ignorado novo=%r)",
                    pc_norm,
                    prev,
                    prev,
                    ec_norm,
                )
            else:
                aliases.country_alias[pc_norm] = ec_norm

        # Liga
        if pl_norm and el_norm:
            prev_l = aliases.league_alias.get(pl_norm)
            if prev_l and prev_l != el_norm:
                logger.debug(
                    "[aliases] conflito league_alias para %r: %r -> %r (ignorado novo=%r)",
                    pl_norm,
                    prev_l,
                    prev_l,
                    el_norm,
                )
            else:
                aliases.league_alias[pl_norm] = el_norm

    logger.info(
        "[aliases] construídos | paises=%d | ligas=%d",
        len(aliases.country_alias),
        len(aliases.league_alias),
    )

    return aliases

