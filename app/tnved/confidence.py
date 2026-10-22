"""
Уверенность считается кодом по сигналам, а не берётся из ответа LLM.

Сигналы (все вычисляются из детерминированных данных конвейера):
  rank      — место выбранной позиции в результатах текстового поиска по
              позициям (независимый от LLM метод 1): 1-е, до 3-го, до 10-го, иначе.
  hint      — совпала ли выбранная позиция с позицией, названной LLM по её
              знаниям до просмотра кандидатов (метод 2), и существует ли она в
              справочнике.
  gap       — относительный разрыв bm25 между лучшей и второй позицией поиска.
  precedent — выбранный код совпал с кодом одного из двух ближайших подтверждённых
              решений по похожим товарам (tnved_synonyms).
  missing   — число недостающих атрибутов, названных LLM (штраф).
Веса и пороги — в tnved/config.json.
"""
import json
import os

_CFG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def load_config(path: str = _CFG_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _rank_score(rank, table: dict) -> float:
    if not rank:
        return 0.0
    for limit in sorted(int(k) for k in table):
        if rank <= limit:
            return float(table[str(limit)])
    return 0.0


def compute(signals: dict, cfg: dict) -> dict:
    """signals: rank (int|None), hint (bool), gap (0..1), precedent (bool), missing (int).
    Возвращает {score, level, abstain}."""
    w = cfg["weights"]
    score = (w["rank"] * _rank_score(signals.get("rank"), cfg["rank_scores"])
             + w["hint"] * (1.0 if signals.get("hint") else 0.0)
             + w["gap"] * max(0.0, min(1.0, signals.get("gap") or 0.0))
             + w.get("precedent", 0.0) * (1.0 if signals.get("precedent") else 0.0)
             - w["missing"] * min(signals.get("missing") or 0, 3) / 3.0)
    t = cfg["thresholds"]
    level = "high" if score >= t["high"] else "medium" if score >= t["medium"] else "low"
    abstain = score < t["abstain"] or (signals.get("missing") or 0) > cfg["max_missing_to_answer"]
    return {"score": round(score, 3), "level": level, "abstain": abstain}
