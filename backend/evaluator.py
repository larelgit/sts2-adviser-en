"""
backend/evaluator.py
卡牌评估器（CardEvaluator）

职责：
  - 接收 RunState + 卡库
  - 检测当前 run 匹配的套路
  - 对每张候选卡评估并打分
  - 输出 EvaluationResult 列表（已排序）

依赖：
  - archetypes.ArchetypeLibrary
  - scoring.*
  - models.*
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

from utils.paths import get_app_root

_LOGS_DIR = get_app_root() / "logs"
_LOGS_DIR.mkdir(exist_ok=True)

log = logging.getLogger(__name__)

from .archetypes import ArchetypeLibrary, archetype_library
from .archetype_inference import infer_weight
from .models import (
    Card, Archetype, CardRole, GamePhase, Rarity,
    RunState, EvaluationResult, ScoreBreakdown,
)
from .scoring import (
    score_base_dimension,
    score_rarity_dimension,
    score_archetype_dimension,
    score_completion_dimension,
    score_phase_dimension,
    score_synergy_bonus,
    pollution_penalty,
    deck_bloat_penalty,
    combine_scores,
    cross_validate,
    ascension_modifier,
    Alignment,
    CrossValidationResult,
    # V2 additions
    calculate_skip_score,
    calculate_skip_score_v2,
    calculate_pick_delta,
    format_pick_recommendation,
    determine_role_v2,
    soft_role_confidence,
)
from .deck_profiler import analyze_deck, get_card_functions
from .threat_model import assess_threats


def score_to_grade(score: float) -> str:
    """将 0~100 的数字分转换为字母等级（仅展示用，不参与计算）。"""
    if score >= 90: return "S"
    if score >= 80: return "A+"
    if score >= 72: return "A"
    if score >= 65: return "A-"
    if score >= 58: return "B+"
    if score >= 50: return "B"
    if score >= 43: return "B-"
    if score >= 35: return "C+"
    if score >= 25: return "C"
    return "D"


class CardEvaluator:
    """
    评估器主类。

    使用方式：
        evaluator = CardEvaluator(card_db)
        results, verdict = evaluator.rank_cards(run_state)
    """

    def __init__(
        self,
        card_db: dict[str, Card],
        library: Optional[ArchetypeLibrary] = None,
        raw_card_db: Optional[dict[str, dict]] = None,
        community_db: Optional[dict] = None,
    ) -> None:
        """
        card_db:      card_id -> Card（全卡库）
        library:      套路库（默认使用模块级单例）
        raw_card_db:  card_id -> 原始 JSON dict（含 powers_applied / keywords_key）
        community_db: card_id -> CommunityStats（社区统计数据，可选）
        """
        self.card_db = card_db
        self.library = library or archetype_library
        self.raw_card_db: dict[str, dict] = raw_card_db or {}
        self.community_db: dict = community_db or {}

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def rank_cards(self, run_state: RunState) -> tuple[list[EvaluationResult], dict]:
        """
        V2: 对 run_state.card_choices 中的所有候选卡进行评估。
        
        Returns:
            Tuple of (card_results, verdict):
            - card_results: 按 total_score 降序排列的卡牌 EvaluationResult 列表（不含 Skip）
            - verdict: 最终判定 dict with keys:
                - best_action: "pick" 或 "skip"
                - best_card: 最佳卡名 (如果 pick)
                - skip_score: Skip 的分数
                - pick_delta: 最佳卡相对 Skip 的优势
                - recommendation: 完整推荐文本
        """
        log.debug(f"card_choices: {run_state.card_choices}")
        detected = self.detect_archetypes(run_state)
        relic_tags = self._extract_relic_tags(run_state)
        relic_boosts = self._build_relic_synergy(run_state, detected)

        # V2-lite: analyze deck and threats first
        target_deck_size = 12 if not detected else detected[0].target_card_count
        deck_profile = analyze_deck(run_state, self.card_db, target_size=target_deck_size)
        threat_profile = assess_threats(run_state, deck_profile)

        # Keep old function as fallback safety
        legacy_skip = calculate_skip_score(
            deck_size=len(run_state.deck),
            target_deck_size=target_deck_size,
            phase=run_state.phase,
            hp_ratio=run_state.hp_ratio,
            deck_consistency=deck_profile.consistency_score,
            has_critical_gaps=bool(deck_profile.critical_gaps),
        )
        skip_score = calculate_skip_score_v2(deck_profile, threat_profile, run_state.phase)
        skip_score = round((skip_score * 0.75) + (legacy_skip * 0.25), 1)

        results: list[EvaluationResult] = []
        for card_id in run_state.card_choices:
            card = self._resolve_card(card_id)
            if card is None:
                log.warning(f"Card not found in DB: {card_id}")
                continue
            log.debug(f"Evaluating card: {card_id} -> {card.name}")
            result = self.evaluate_card(
                card,
                run_state,
                detected,
                relic_tags,
                relic_boosts,
                skip_score=skip_score,
                deck_profile=deck_profile,
                threat_profile=threat_profile,
            )
            results.append(result)

        log.debug(f"Evaluation results: {[r.card_name for r in results]}")
        results.sort(key=lambda r: r.total_score, reverse=True)
        
        # V2: Build verdict (final recommendation)
        verdict = self._build_verdict(results, skip_score)
        verdict["confidence"] = self._estimate_confidence(results, deck_profile, threat_profile)
        verdict["deck_profile"] = {
            "consistency": round(deck_profile.consistency_score, 3),
            "dead_draw_rate": round(deck_profile.dead_draw_rate, 3),
            "critical_gaps": deck_profile.critical_gaps,
        }
        verdict["threat_profile"] = {
            "survival_urgency": round(threat_profile.survival_urgency, 3),
            "elite_readiness": round(threat_profile.elite_readiness, 3),
            "boss_plan": round(threat_profile.boss_plan_completeness, 3),
        }
        
        self._save_score_log(results, run_state, detected, verdict)
        return results, verdict
    
    def _build_verdict(self, results: list[EvaluationResult], skip_score: float) -> dict:
        """
        V2: Build final verdict comparing best card vs Skip.
        """
        if not results:
            return {
                "best_action": "skip",
                "best_card": None,
                "skip_score": skip_score,
                "pick_delta": 0,
                "recommendation": "No valid cards — Skip",
            }
        
        best_card = results[0]
        pick_delta = best_card.total_score - skip_score
        
        if pick_delta > 5:
            # Clear pick
            action = "pick"
            if pick_delta > 20:
                rec_text = f"🎯 Pick {best_card.card_name} (+{pick_delta:.0f} vs Skip)"
            else:
                rec_text = f"✓ Pick {best_card.card_name} (+{pick_delta:.0f} vs Skip)"
        elif pick_delta > 0:
            # Marginal pick
            action = "pick"
            rec_text = f"⚖ Slight edge: {best_card.card_name} (+{pick_delta:.0f})"
        elif pick_delta > -5:
            # Borderline - could go either way
            action = "skip"
            rec_text = f"⚖ Borderline — Skip preferred ({-pick_delta:.0f})"
        else:
            # Clear skip
            action = "skip"
            rec_text = f"⏭ Skip is better ({-pick_delta:.0f})"
        
        return {
            "best_action": action,
            "best_card": best_card.card_name if action == "pick" else None,
            "best_card_id": best_card.card_id if action == "pick" else None,
            "skip_score": round(skip_score, 1),
            "pick_delta": round(pick_delta, 1),
            "recommendation": rec_text,
        }

    def detect_archetypes(self, run_state: RunState) -> list[Archetype]:
        """
        根据当前牌组，检测玩家正在走的套路。

        检测条件（同时满足）：
          1. 必须持有至少 1 张该套路定义的 CORE 牌（精确层）
          2. 整体完成度 >= _DETECT_THRESHOLD（防止只靠通用 filler 触发）

        过滤逻辑：
          - 按完成度降序排列
          - 只返回不超过 _MAX_ARCHETYPES 个套路
          - 第 2 个以后的套路，其完成度必须 >= 领先套路的 _SECONDARY_RATIO 倍
            （避免只有 1 张 filler 牌就并列检测出多个套路）
        """
        _DETECT_THRESHOLD   = 0.04   # 整体完成度最低门槛（主要靠 CORE 门槛过滤，此值仅排除极低噪音）
        _MAX_ARCHETYPES     = 2      # 最多同时检测几个套路
        _SECONDARY_RATIO    = 0.55   # 次选套路至少是领先套路完成度的 55%

        candidate_archetypes = self.library.get_by_character(run_state.character)
        deck_set = set(self._normalize_card_id(cid) for cid in run_state.deck)

        scored: list[tuple[float, Archetype]] = []
        for archetype in candidate_archetypes:
            # 门槛1：整体完成度
            completion = self._calc_completion(archetype, deck_set)
            if completion < _DETECT_THRESHOLD:
                continue

            # 门槛2：必须持有至少 1 张精确定义的 CORE 牌 (Soft posterior - removed hard restriction)
            has_core = any(
                w.role.value == "core" and w.card_id.lower() in deck_set
                for w in archetype.card_weights
            )
            # if not has_core:
            #     continue

            scored.append((completion, archetype))

        scored.sort(key=lambda x: x[0], reverse=True)

        # 过滤：次选套路完成度不能太低于领先套路
        if not scored:
            return []

        top_score = scored[0][0]
        result: list[Archetype] = []
        for completion, archetype in scored[:_MAX_ARCHETYPES]:
            if completion >= top_score * _SECONDARY_RATIO:
                result.append(archetype)

        return result

    def evaluate_card(
        self,
        card: Card,
        run_state: RunState,
        detected_archetypes: list[Archetype],
        relic_synergy_tags: list[str],
        relic_boosts: dict[str, float] | None = None,
        skip_score: float = 50.0,  # V2: Skip score for delta calculation
        deck_profile=None,
        threat_profile=None,
    ) -> EvaluationResult:
        """
        对单张卡进行全维度评估，返回 EvaluationResult。
        """
        deck_set = set(self._normalize_card_id(cid) for cid in run_state.deck)

        # 1. 收集该卡在各套路中的权重
        # 精确层：手动 card_weights 定义（权重 0.40~0.98）
        # 推断层：基于 powers_applied / keywords / desc 自动推断（上限 0.35）
        archetype_weights: list[float] = []
        matched_archetype_ids: list[str] = []
        inferred_archetype_ids: list[str] = []   # 仅推断层命中（用于日志区分）
        is_exact_match = False  # 是否有精确层命中

        raw = self.raw_card_db.get(card.id)      # 原始 JSON dict（可能为 None）

        for archetype in detected_archetypes:
            weight_info = self.library.get_card_weight(archetype.id, card.id)
            if weight_info:
                # 精确层命中
                archetype_weights.append(weight_info.weight)
                matched_archetype_ids.append(archetype.id)
                is_exact_match = True
            elif raw is not None:
                # 推断层兜底
                inferred_w = infer_weight(raw, archetype.id)
                if inferred_w > 0.0:
                    archetype_weights.append(inferred_w)
                    matched_archetype_ids.append(archetype.id)
                    inferred_archetype_ids.append(archetype.id)
                    log.debug(
                        f"推断权重 {card.id} → {archetype.id}: {inferred_w:.2f}"
                    )

        # 2. 确定卡牌角色
        # 推断层命中的卡最低为 FILLER，不判定为 POLLUTION（推断层设计上限 0.35 < 精确层最低 0.40）
        role = self._determine_role(card, detected_archetypes, archetype_weights,
                                    inferred_only=not is_exact_match and bool(inferred_archetype_ids))

        # 3. 计算套路完成度贡献
        comp_before = 0.0
        comp_after = 0.0
        all_comp_before = {}
        all_comp_after = {}
        
        if detected_archetypes:
            primary = detected_archetypes[0]
            comp_before = self._calc_completion(primary, deck_set)
            new_deck = deck_set | {card.id}
            comp_after = self._calc_completion(primary, new_deck)
            
            for arc in detected_archetypes:
                all_comp_before[arc.id] = self._calc_completion(arc, deck_set)
                all_comp_after[arc.id] = self._calc_completion(arc, new_deck)

        # 4. 各维度评分
        # v0.7: bloat_penalty 显式计算；rarity_score 字段改存 community_score
        bloat_pen = deck_bloat_penalty(card, len(run_state.deck), role)

        # 查社区数据
        community_stats = self.community_db.get(card.id)
        community_norm: Optional[float] = (
            community_stats.community_score if community_stats is not None else None
        )

        breakdown = ScoreBreakdown(
            base_score=score_base_dimension(card, run_state.phase),
            rarity_score=community_norm if community_norm is not None else 0.0,  # community_score
            archetype_score=score_archetype_dimension(card, archetype_weights),
            completion_score=score_completion_dimension(
                comp_before,
                comp_after,
                all_completions_before=all_comp_before if detected_archetypes else None,
                all_completions_after=all_comp_after if detected_archetypes else None
            ),
            phase_score=score_phase_dimension(card, run_state.phase, role, hp_ratio=run_state.hp_ratio),
            synergy_bonus=score_synergy_bonus(
                card, run_state, relic_synergy_tags,
                relic_boosts=relic_boosts or {},
                matched_archetype_ids=matched_archetype_ids,
            ),
            pollution_penalty=pollution_penalty(card, len(run_state.deck), role),
        )

        # algo_score（原有流程）
        algo_score_100 = combine_scores(breakdown, bloat_penalty=bloat_pen)

        # Ascension 修正（在社区交叉验证之前，基于算法分施加）
        asc_delta = ascension_modifier(role, run_state.ascension, breakdown.archetype_score)
        algo_score_100 = round(max(0.0, min(100.0, algo_score_100 + asc_delta)), 1)

        # 社区交叉验证（post-processing）
        algo_norm = algo_score_100 / 100.0
        cv_result = cross_validate(algo_norm, community_norm)
        total = round(cv_result.blended_norm * 100, 1)

        # 5. 生成解释
        reasons_for, reasons_against = self._build_reasons(
            card, role, breakdown, matched_archetype_ids, run_state,
            inferred_ids=inferred_archetype_ids,
            community_stats=community_stats,
            cv_result=cv_result,
            algo_score=algo_score_100,
        )

        # V2-lite: deck/threat aware delta adjustment
        if deck_profile is not None and threat_profile is not None:
            gap_bonus = 0.0
            card_text = (card.description or "").lower()
            tags = {t.lower() for t in card.tags}
            if "block" in deck_profile.critical_gaps and (card.base_block or 0) >= 6:
                gap_bonus += 1.5
            if "frontload" in deck_profile.critical_gaps and card.card_type.value == "attack":
                gap_bonus += 1.0
            if "draw" in deck_profile.critical_gaps and ("draw" in card_text or "draw" in tags):
                gap_bonus += 1.0
            if "aoe" in deck_profile.critical_gaps and ("all enemies" in card_text or "aoe" in tags):
                gap_bonus += 1.2
            if "scaling" in deck_profile.critical_gaps and any(k in card_text for k in ("strength", "focus", "poison", "star", "doom")):
                gap_bonus += 1.2
            urgency_scale = 1.0 + (threat_profile.survival_urgency * 0.15)
            
            # Apply gap_bonus directly to total_score before final ranking
            total = round(min(100.0, total + gap_bonus * urgency_scale), 1)

        pick_delta = calculate_pick_delta(total, skip_score)
        
        recommendation = self._make_recommendation_v2(total, role, pick_delta)
        
        # V2: Add skip comparison to reasons
        if pick_delta > 10:
            reasons_for.append(f"Strong pick (+{pick_delta:.1f} vs Skip)")
        elif pick_delta > 3:
            reasons_for.append(f"Good pick (+{pick_delta:.1f} vs Skip)")
        elif pick_delta < -5:
            reasons_against.append(f"Consider skipping (Skip is {-pick_delta:.1f} better)")
        elif pick_delta < 0:
            reasons_against.append(f"Marginal pick (Skip is slightly better)")

        return EvaluationResult(
            card_id=card.id,
            card_name=card.name,
            rarity=card.rarity.value,
            total_score=total,
            role=role,
            breakdown=breakdown,
            matched_archetypes=matched_archetype_ids,
            reasons_for=reasons_for,
            reasons_against=reasons_against,
            recommendation=recommendation,
            grade=score_to_grade(total),
        )

    @staticmethod
    def _estimate_confidence(results: list[EvaluationResult], deck_profile, threat_profile) -> dict:
        """
        V2-lite confidence estimate for recommendation quality.
        """
        if not results:
            return {"level": "low", "score": 0.25, "reason": "No valid card matches"}

        top = results[0].total_score
        second = results[1].total_score if len(results) > 1 else top - 1
        gap = abs(top - second)

        model_conf = 0.45
        model_conf += min(0.20, gap / 30.0)
        model_conf += max(0.0, (deck_profile.consistency_score - 0.45) * 0.25)
        model_conf += max(0.0, (0.70 - deck_profile.dead_draw_rate) * 0.15)
        model_conf -= threat_profile.survival_urgency * 0.12
        model_conf = max(0.05, min(0.95, model_conf))

        if model_conf >= 0.72:
            level = "high"
            reason = "Clear score separation and stable deck context"
        elif model_conf >= 0.48:
            level = "medium"
            reason = "Usable signal with moderate uncertainty"
        else:
            level = "low"
            reason = "Close scores or unstable context"

        return {"level": level, "score": round(model_conf, 3), "reason": reason}

    # ------------------------------------------------------------------
    # 内部辅助方法
    # ------------------------------------------------------------------

    def _resolve_card(self, card_id: str) -> Optional[Card]:
        """将 card_id（含升级标记）解析为 Card 对象"""
        normalized = self._normalize_card_id(card_id)
        return self.card_db.get(normalized)

    @staticmethod
    def _normalize_card_id(card_id: str) -> str:
        """去除升级后缀并统一小写（e.g. 'Shiv+' -> 'shiv', 'DEMON_FORM' -> 'demon_form'）"""
        return card_id.rstrip("+").lower()

    def _calc_completion(self, archetype: Archetype, deck_set: set[str]) -> float:
        """
        计算套路完成度 (0.0 ~ 1.0)。
        加权：核心卡权重更高，filler 权重更低。
        """
        if not archetype.card_weights:
            return 0.0

        total_weight = sum(w.weight for w in archetype.card_weights)
        if total_weight == 0:
            return 0.0

        owned_weight = sum(
            w.weight for w in archetype.card_weights
            if w.card_id.lower() in deck_set
        )
        return owned_weight / total_weight

    def _determine_role(
        self,
        card: Card,
        detected_archetypes: list[Archetype],
        archetype_weights: list[float],
        inferred_only: bool = False,
    ) -> CardRole:
        """
        V2: 根据套路匹配结果推断卡牌角色，使用 soft thresholds。

        inferred_only: 若为 True，表示所有权重来自推断层（非手动定义），
                       最低角色保底为 FILLER，不判定为 POLLUTION。
        """
        if not detected_archetypes or not archetype_weights:
            # 未匹配任何套路 → 按稀有度做保守判断
            from .models import Rarity
            if card.rarity in (Rarity.RARE, Rarity.ANCIENT):
                return CardRole.FILLER
            elif card.rarity in (Rarity.UNCOMMON, Rarity.COMMON):
                return CardRole.FILLER
            else:
                return CardRole.UNKNOWN

        max_weight = max(archetype_weights)
        
        # V2: Use soft role determination with smoother boundaries
        return determine_role_v2(max_weight, inferred_only)

    def _build_reasons(
        self,
        card: Card,
        role: CardRole,
        breakdown: ScoreBreakdown,
        matched_archetypes: list[str],
        run_state: RunState,
        inferred_ids: Optional[list[str]] = None,
        community_stats=None,
        cv_result: Optional[CrossValidationResult] = None,
        algo_score: float = 0.0,
    ) -> tuple[list[str], list[str]]:
        """
        生成中文可解释理由。
        返回 (reasons_for, reasons_against)。
        """
        inferred_ids = inferred_ids or []
        reasons_for: list[str] = []
        reasons_against: list[str] = []

# 套路契合：区分精确层和推断层
        exact_ids = [aid for aid in matched_archetypes if aid not in inferred_ids]
        if exact_ids:
            archetype_names = [
                a.name
                for a in [self.library.get_archetype(aid) for aid in exact_ids]
                if a is not None
            ]
            reasons_for.append(f"Fits Archetypes: {', '.join(archetype_names)}")
        if inferred_ids:
            inferred_names = [
                a.name
                for a in [self.library.get_archetype(aid) for aid in inferred_ids]
                if a is not None
            ]
            reasons_for.append(f"Inferred Synergy (Keywords): {', '.join(inferred_names)}")

        # 稀有度（直接读 card.rarity，不依赖 breakdown.rarity_score）
        if card.rarity in (Rarity.RARE, Rarity.ANCIENT):
            reasons_for.append(f"High rarity ({card.rarity.value}), high baseline value")

        # 套路完成度贡献
        if breakdown.completion_score > 0.05:
            pct = round(breakdown.completion_score * 100, 1)
            reasons_for.append(f"Boosts primary archetype completion +{pct}%")

        # 协同
        if breakdown.synergy_bonus > 0.0:
            reasons_for.append("Has synergy with current relics or deck")

        # 阶段适配 (Transition removed)
        
        # 污染
        if role == CardRole.POLLUTION:
            reasons_against.append("No synergy with current archetypes, will dilute the deck")

        # 仅推断匹配时，补充置信度说明
        if matched_archetypes and not exact_ids and inferred_ids:
            reasons_against.append("Only matched via keyword inference (not a core card), actual value may vary")

        # 无任何匹配
        if not matched_archetypes:
            reasons_against.append("Does not match any detected archetypes, value in current run is unclear")

        # 社区数据理由 (V2: sanity-check layer only, not a decision maker)
        if cv_result is not None:
            if cv_result.has_community_data and community_stats is not None:
                wr_pct = community_stats.win_rate_pct
                wr = f"{wr_pct:.1f}%"
                pr = f"{community_stats.pick_rate_pct:.1f}%"
                
                # V2: Sanity check warnings for extreme cases
                if wr_pct < 40.0 and algo_score > 60:
                    # Low community win rate but algorithm says it's good - warn
                    reasons_against.append(
                        f"⚠ Community Caution: Win Rate {wr} is low. Algorithm may be overvaluing this card."
                    )
                elif wr_pct < 30.0:
                    # Very low win rate - strong warning
                    reasons_against.append(
                        f"⚠ Low Win Rate Warning: {wr} historically. Consider skipping."
                    )
                elif wr_pct > 70.0 and algo_score < 50:
                    # High community win rate but algorithm says it's bad - note potential
                    reasons_for.append(
                        f"Community says {wr} win rate, algorithm may be undervaluing this."
                    )

        return reasons_for, reasons_against

    @staticmethod
    def _make_recommendation(total_score: float, role: CardRole) -> str:
        """根据分数和角色生成推荐语（与 scoring.py 分档对应）- 旧接口保留"""
        if role == CardRole.POLLUTION:
            return "Skip"
        if total_score >= 80:
            return "Highly Recommended"
        elif total_score >= 65:
            return "Recommended"
        elif total_score >= 50:
            return "Optional"
        elif total_score >= 30:
            return "Caution"
        else:
            return "Skip"

    @staticmethod
    def _make_recommendation_v2(total_score: float, role: CardRole, pick_delta: float) -> str:
        """
        V2: Generate recommendation considering pick delta vs Skip.
        
        Includes the delta in the recommendation when relevant.
        """
        if role == CardRole.POLLUTION:
            return "Skip (Pollution)"
        if role == CardRole.SKIP:
            return "Skip"
        
        # V2: Consider delta vs Skip
        if pick_delta < -5:
            return "Skip Recommended"
        elif pick_delta < 0:
            return "Consider Skip"
        
        # Standard recommendations with delta
        if total_score >= 80:
            return f"Highly Recommended (+{pick_delta:.0f})"
        elif total_score >= 65:
            return f"Recommended (+{pick_delta:.0f})"
        elif total_score >= 50:
            return f"Optional (+{pick_delta:.0f})" if pick_delta > 0 else "Optional"
        elif total_score >= 30:
            return "Caution"
        else:
            return "Skip"

    def _create_skip_result(self, skip_score: float, run_state: RunState) -> EvaluationResult:
        """
        V2: Create an EvaluationResult for the Skip option.
        """
        reasons_for = []
        reasons_against = []
        
        deck_size = len(run_state.deck)
        
        if deck_size >= 15:
            reasons_for.append(f"Deck has {deck_size} cards, avoiding dilution")
        if deck_size >= 20:
            reasons_for.append("Large deck - skip helps maintain consistency")
        
        if run_state.phase == GamePhase.LATE:
            reasons_for.append("Late game - be selective with picks")
        elif run_state.phase == GamePhase.EARLY:
            reasons_against.append("Early game - usually want to build deck")
        
        if deck_size < 10:
            reasons_against.append(f"Small deck ({deck_size} cards) - need more options")
        
        if run_state.hp_ratio < 0.3:
            reasons_against.append("Low HP - may need survival cards")
        
        return EvaluationResult(
            card_id="__SKIP__",
            card_name="Skip",
            rarity="",
            total_score=skip_score,
            role=CardRole.SKIP,
            breakdown=ScoreBreakdown(
                base_score=skip_score / 100.0,
                rarity_score=0.0,
                archetype_score=0.0,
                completion_score=0.0,
                phase_score=0.0,
                synergy_bonus=0.0,
                pollution_penalty=0.0,
            ),
            matched_archetypes=[],
            reasons_for=reasons_for,
            reasons_against=reasons_against,
            recommendation="Skip" if skip_score >= 50 else "Pick a card",
            grade=score_to_grade(skip_score),
        )

    @staticmethod
    def _build_relic_synergy(
        run_state: RunState,
        detected_archetypes: list,
    ) -> dict[str, float]:
        """
        根据当前持有遗物和已检测套路，构建遗物→套路 boost 映射。
        返回 {archetype_id: max_boost_score}。
        只返回已检测到的套路的 boost，避免未走的套路被误激活。
        """
        from .relic_archetype_map import RELIC_ARCHETYPE_MAP
        detected_ids = {a.id for a in detected_archetypes}
        boosts: dict[str, float] = {}
        for relic in run_state.relics:
            relic_key = relic.id.upper()
            for archetype_id, score in RELIC_ARCHETYPE_MAP.get(relic_key, []):
                if archetype_id in detected_ids:
                    boosts[archetype_id] = max(boosts.get(archetype_id, 0.0), score)
        return boosts

    @staticmethod
    def _extract_relic_tags(run_state: RunState) -> list[str]:
        """旧接口保留：返回 tags 字段（当前始终为空列表）。"""
        tags: list[str] = []
        for relic in run_state.relics:
            tags.extend(relic.tags)
        return tags

    @staticmethod
    def _save_score_log(
        results: list[EvaluationResult],
        run_state: RunState,
        detected_archetypes: list | None = None,
        verdict: dict | None = None,
    ) -> None:
        """
        将评分细节写入 logs/score_YYYYMMDD_HHMMSS.json。
        每次调用 rank_cards 时生成一份。保留最近 30 份。
        """
        try:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_path = _LOGS_DIR / f"score_{ts}.json"

            payload = {
                "timestamp": ts,
                "character": run_state.character.value if hasattr(run_state.character, "value") else str(run_state.character),
                "phase": run_state.phase.value if hasattr(run_state.phase, "value") else str(run_state.phase),
                "ascension": run_state.ascension,
                "floor": run_state.floor,
                "deck_size": len(run_state.deck),
                "deck": run_state.deck,
                "detected_archetypes": [a.id for a in (detected_archetypes or [])],
                "relics": [r.id for r in run_state.relics],
                "verdict": verdict,  # V2: final recommendation
                "results": [
                    {
                        "card_id": r.card_id,
                        "card_name": r.card_name,
                        "rarity": r.rarity,
                        "total_score": r.total_score,
                        "grade": r.grade,
                        "recommendation": r.recommendation,
                        "role": r.role.value if hasattr(r.role, "value") else str(r.role),
                        "matched_archetypes": r.matched_archetypes,
                        "breakdown": {
                            "value_score":        round(r.breakdown.base_score, 4),
                            "archetype_score":    round(r.breakdown.archetype_score, 4),
                            "phase_score":        round(r.breakdown.phase_score, 4),
                            "completion_score":   round(r.breakdown.completion_score, 4),
                            "synergy_bonus":      round(r.breakdown.synergy_bonus, 4),
                            "pollution_penalty":  round(r.breakdown.pollution_penalty, 4),
                            "community_score":     round(r.breakdown.rarity_score, 4),
                        },
                        "reasons_for": r.reasons_for,
                        "reasons_against": r.reasons_against,
                    }
                    for r in results
                ],
            }

            log_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            log.info(f"评分日志已保存: {log_path.name}")

            # 只保留最新 30 份
            old_logs = sorted(_LOGS_DIR.glob("score_*.json"))
            for old in old_logs[:-30]:
                old.unlink(missing_ok=True)

        except Exception as e:
            log.warning(f"保存评分日志失败: {e}")
