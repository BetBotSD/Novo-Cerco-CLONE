# -*- coding: utf-8 -*-
# src/main.py
"""
Entry point principal do sistema CERCO para execução no Railway.

Fluxo:
- Carrega configs e inicializa logging.
- Cria um CercoOrchestrator (min_edge configurável).
- Entra em loop infinito:
    - roda orchestrator.run_once()
    - envia oportunidades ao Telegram (texto + imagem opcional)
    - aguarda CERCO_POLL_EVERY_SEC (com pequeno jitter)

Observação:
- O refresh de ligas "do dia" (janela de 6h) já é tratado dentro do CercoEngine/
  leagues_cerco.

AJUSTE NESTA AUDITORIA:
- Logging split:
    * INFO/WARNING -> stdout (não vermelho no Railway)
    * ERROR/EXCEPTION -> stderr (vermelho só pra erro real)
"""

from __future__ import annotations

import os
import sys
import logging
import random
import time
from typing import List


# -----------------------------------------------------------------------------
# Bootstrap de sys.path para tornar "cerco" importável no Railway.
# -----------------------------------------------------------------------------
_HERE = os.path.dirname(__file__)                  # /app/src
_ROOT = os.path.abspath(os.path.join(_HERE, "..")) # /app

if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# -----------------------------------------------------------------------------
# Imports do CERCO com fallback:
# -----------------------------------------------------------------------------
try:
    from cerco.config_cerco import (
        CERCO_POLL_EVERY_SEC,
        CERCO_STAKE_BASE_BRL,
        dump_config_for_logging,
    )
    from cerco.cerco_orchestrator import CercoOrchestrator, CercoMatchResult
    from cerco import telegram_cerco
except ModuleNotFoundError:
    from src.cerco.config_cerco import (
        CERCO_POLL_EVERY_SEC,
        CERCO_STAKE_BASE_BRL,
        dump_config_for_logging,
    )
    from src.cerco.cerco_orchestrator import CercoOrchestrator, CercoMatchResult
    from src.cerco import telegram_cerco


logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.replace(",", "."))
    except Exception:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    raw = raw.strip().lower()
    return raw in ("1", "true", "yes", "y", "on")


def _setup_logging() -> None:
    """
    Configura logging com dois handlers:
      - stdout para INFO/WARNING
      - stderr para ERROR/EXCEPTION

    Assim o Railway só pinta de vermelho o que for erro real.
    """
    level_name = os.getenv("LOG_LEVEL", "DEBUG").upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    root.setLevel(level)

    # Remove handlers antigos (evita duplicar)
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    # stdout handler: até WARNING
    h_out = logging.StreamHandler(sys.stdout)
    h_out.setLevel(level)
    h_out.addFilter(lambda r: r.levelno < logging.ERROR)
    h_out.setFormatter(fmt)

    # stderr handler: ERROR+
    h_err = logging.StreamHandler(sys.stderr)
    h_err.setLevel(logging.ERROR)
    h_err.setFormatter(fmt)

    root.addHandler(h_out)
    root.addHandler(h_err)


def run_cycle(orchestrator: CercoOrchestrator) -> None:
    """
    Executa um ciclo completo:
    - Busca snapshots + comparações via CercoOrchestrator.
    - Envia oportunidades de arbitragem ao Telegram.
    """
    stake_base = _env_float("CERCO_STAKE_BASE_BRL", CERCO_STAKE_BASE_BRL)
    with_image = _env_bool("CERCO_TELEGRAM_WITH_IMAGE", default=True)

    match_results: List[CercoMatchResult] = orchestrator.run_once()
    logger.info("[CERCO] run_once retornou %d confrontos analisados.", len(match_results))

    if not telegram_cerco.is_enabled():
        logger.info(
            "[CERCO] Telegram desabilitado (sem token/chat_id); "
            "nenhuma mensagem será enviada neste ciclo."
        )
        return

    sent = 0
    for match in match_results:
        opps = getattr(match, "opportunities", None) or []
        if not opps:
            continue

        telegram_cerco.send_match_result(
            match,
            stake_base=stake_base,
            with_image=with_image,
        )
        sent += 1

    logger.info("[CERCO] Mensagens enviadas ao Telegram neste ciclo: %d", sent)


def main_loop() -> None:
    """
    Loop principal para rodar continuamente no Railway.
    """
    _setup_logging()

    logger.info("Boot CERCO main_loop | config=%s", dump_config_for_logging())

    min_edge = _env_float("CERCO_MIN_EDGE", 0.01)
    orchestrator = CercoOrchestrator(min_edge=min_edge)

    try:
        while True:
            try:
                run_cycle(orchestrator)
            except Exception:
                # stderr (vermelho) só aqui
                logger.exception("[CERCO] Erro inesperado no ciclo principal.")

            base = CERCO_POLL_EVERY_SEC
            jitter_factor = random.uniform(0.9, 1.1)
            sleep_sec = int(base * jitter_factor)
            logger.info(
                "[CERCO] Aguardando %ds (base=%ds, jitter=%.2f) até o próximo ciclo...",
                sleep_sec,
                base,
                jitter_factor,
            )
            time.sleep(sleep_sec)
    finally:
        try:
            orchestrator.engine.close()
        except Exception:
            logger.exception("[CERCO] Falha ao fechar engine no shutdown.")


if __name__ == "__main__":
    main_loop()
