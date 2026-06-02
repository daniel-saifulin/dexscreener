"""Pre-swap safety checks. Вызывается ПЕРЕД покупкой токена в live.

Цель: отсеять honeypot'ы, токены с большой sell-tax, концентрированные у
одного владельца, или просто слишком молодые / нестабильные.

Источник: RugCheck.xyz (бесплатно, без ключа). Solana-specific.

Возвращает SafetyResult — если safe=True, можно swap'ать.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import requests

log = logging.getLogger("dexbot.safety")

RUGCHECK_TIMEOUT = 10

# Допустимый уровень риска по RugCheck. Что-то жёстче — отказ.
ALLOWED_RISK_LEVELS = {"info", "warn"}   # "danger" блокируется

# Минимальная ликвидность пула для входа в live
MIN_LIQUIDITY_USD_LIVE = 30_000.0


@dataclass
class SafetyResult:
    safe: bool
    reason: str = "ok"
    raw: dict = field(default_factory=dict)
    rugcheck_score: Optional[int] = None
    risks: list = field(default_factory=list)


def check_token(token_mint: str, *,
                liquidity_usd: Optional[float] = None) -> SafetyResult:
    """Главная функция safety_runtime.

    Параметры:
      token_mint: Solana mint address
      liquidity_usd: ликвидность пула на момент проверки (из DexScreener)

    Возвращает SafetyResult с safe=True если всё ok.
    Любая ошибка сети → safe=False (fail-closed для live).
    """
    # 1. Жёсткий лимит по ликвидности — независимо от RugCheck
    if liquidity_usd is not None and liquidity_usd < MIN_LIQUIDITY_USD_LIVE:
        return SafetyResult(
            False,
            f"liquidity ${liquidity_usd:.0f} < ${MIN_LIQUIDITY_USD_LIVE:.0f}",
        )

    # 2. RugCheck — структурный анализ контракта
    try:
        r = requests.get(
            f"https://api.rugcheck.xyz/v1/tokens/{token_mint}/report/summary",
            timeout=RUGCHECK_TIMEOUT,
        )
    except requests.RequestException as e:
        log.warning("rugcheck network error for %s: %s", token_mint[:8], e)
        return SafetyResult(False, f"rugcheck network error: {e}")

    if r.status_code == 404:
        # Незарегистрированный токен. На свежих memecoin часто бывает.
        # Для live это пока fail-closed.
        return SafetyResult(False, "rugcheck 404 (token not indexed)")

    if not r.ok:
        return SafetyResult(False, f"rugcheck HTTP {r.status_code}")

    try:
        data = r.json()
    except ValueError:
        return SafetyResult(False, "rugcheck bad JSON")

    risks = data.get("risks") or []
    score = data.get("score")

    # 3. Проверяем уровень рисков
    danger_flags = []
    for risk in risks:
        level = (risk.get("level") or "").lower()
        name = risk.get("name") or ""
        if level == "danger":
            danger_flags.append(name)

    if danger_flags:
        return SafetyResult(
            False,
            f"rugcheck danger flags: {','.join(danger_flags[:3])}",
            raw=data,
            rugcheck_score=score,
            risks=danger_flags,
        )

    # 4. Score sanity: если RugCheck дал жёстко плохую оценку — отказ
    # (Score у них = "лучше меньше". Конкретные пороги уточним по данным.)
    if score is not None and score > 50_000:  # консервативный порог
        return SafetyResult(
            False,
            f"rugcheck score {score} too high (risky)",
            raw=data,
            rugcheck_score=score,
        )

    return SafetyResult(
        True, "ok",
        raw=data,
        rugcheck_score=score,
        risks=[],
    )
