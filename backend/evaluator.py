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
import math
from datetime import datetime
from typing import Optional

from utils.paths import get_app_root

_LOGS_DIR = get_app_root() / "logs"
_LOGS_DIR.mkdir(exist_ok=True)

log = logging.getLogger(__name__)

from .archetypes import ArchetypeLibrary, archetype_library
from .archetype_inference import infer_weight
from .contextual import (
    build_deck_metrics,
    build_run_context,
    community_prior_alpha,
    estimate_confidence,
    motif_signal_support,
    need_coverage,
    profile_card,
    soft_or,
    top_need_hits,
)
from .models import (
    Card, Archetype, CardRole, CardType, GamePhase, Rarity,
    RunState, EvaluationResult, ScoreBreakdown,
)
from .scoring import (
    apply_contextual_prior,
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
)


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
        results = evaluator.rank_cards(run_state)
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

    def rank_cards(self, run_state: RunState) -> list[EvaluationResult]:
        """
        对 run_state.card_choices 中的所有候选卡进行评估并排序。
        返回按 total_score 降序排列的 EvaluationResult 列表。
        """
        log.debug(f"card_choices: {run_state.card_choices}")
        run_context, motif_profile = self._prepare_context(run_state)
        detected = [archetype for archetype, _ in motif_profile]
        relic_tags = self._extract_relic_tags(run_state)
        relic_boosts = self._build_relic_synergy(run_state, detected)

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
                motif_profile,
                run_context,
                relic_tags,
                relic_boosts,
            )
            results.append(result)

        skip_result = self._build_skip_result(run_state, run_context, motif_profile, results)
        skip_score = skip_result.total_score
        decision_threshold = run_context.skip_threshold

        best_card: Optional[EvaluationResult] = None
        best_margin = float("-inf")
        for result in results:
            delta = round(result.total_score - skip_score, 1)
            result.pick_delta_vs_skip = delta
            result.recommendation = self._make_recommendation(
                result.total_score,
                result.role,
                pick_delta=delta,
                threshold=decision_threshold,
            )
            if delta > 0:
                result.reasons_for.insert(0, f"Beats Skip by +{delta:.1f}")
            else:
                result.reasons_against.insert(0, f"Below Skip by {abs(delta):.1f}")

            margin = delta - decision_threshold
            if margin > best_margin:
                best_margin = margin
                best_card = result

        if best_card is not None and best_margin > 0:
            skip_result.reasons_against.insert(
                0,
                f"{best_card.card_name} clears the Skip threshold by +{best_margin:.1f}",
            )
        else:
            skip_result.reasons_for.insert(
                0,
                f"No reward clears the +{decision_threshold:.1f} Skip threshold",
            )

        results.append(skip_result)
        log.debug(f"Evaluation results: {[r.card_name for r in results]}")
        results.sort(
            key=lambda r: (
                0.0 if r.is_skip_option else r.pick_delta_vs_skip - decision_threshold,
                r.total_score,
            ),
            reverse=True,
        )
        self._save_score_log(results, run_state, detected)
        return results

    def detect_archetypes(self, run_state: RunState) -> list[Archetype]:
        """
        v2-lite: return soft motifs instead of only 1-2 hard archetypes.
        """
        run_context, motif_profile = self._prepare_context(run_state)
        _ = run_context
        return [archetype for archetype, _confidence in motif_profile]

    def _prepare_context(self, run_state: RunState):
        deck_profiles = []
        raw_costs: list[int] = []
        starter_flags: list[bool] = []
        power_flags: list[bool] = []
        status_flags: list[bool] = []

        for card_id in run_state.deck:
            card = self._resolve_card(card_id)
            if card is None:
                continue
            raw = self.raw_card_db.get(card.id)
            deck_profiles.append(profile_card(card, raw))
            raw_costs.append(card.cost)
            starter_flags.append(card.rarity in (Rarity.STARTER, Rarity.BASIC))
            power_flags.append(card.card_type == CardType.POWER)
            status_flags.append(card.card_type in (CardType.STATUS, CardType.CURSE))

        deck_metrics = build_deck_metrics(
            deck_profiles,
            raw_costs=raw_costs,
            starters=starter_flags,
            power_cards=power_flags,
            status_cards=status_flags,
        )
        run_context = build_run_context(run_state, deck_metrics)
        motif_profile = self._build_motif_profile(run_state, run_context)
        run_context.motif_confidences = {
            archetype.id: confidence for archetype, confidence in motif_profile
        }
        return run_context, motif_profile

    def _build_motif_profile(self, run_state: RunState, run_context) -> list[tuple[Archetype, float]]:
        deck_set = set(self._normalize_card_id(cid) for cid in run_state.deck)
        candidate_archetypes = self.library.get_by_character(run_state.character)

        scored: list[tuple[float, Archetype]] = []
        for archetype in candidate_archetypes:
            completion = self._calc_completion(archetype, deck_set)
            core_weights = [
                weight.weight
                for weight in archetype.card_weights
                if weight.role == CardRole.CORE and weight.card_id.lower() in deck_set
            ]
            support_weights = [
                min(0.85, weight.weight)
                for weight in archetype.card_weights
                if weight.role != CardRole.POLLUTION and weight.card_id.lower() in deck_set
            ]
            signal_support = motif_signal_support(archetype.key_tags, run_context.deck)
            confidence = soft_or(
                [
                    completion,
                    soft_or(core_weights) * 0.75,
                    soft_or(support_weights) * 0.55,
                    signal_support * 0.45,
                ]
            )
            if confidence >= 0.12:
                scored.append((confidence, archetype))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [(archetype, confidence) for confidence, archetype in scored[:4]]

    def evaluate_card(
        self,
        card: Card,
        run_state: RunState,
        motif_profile: list[tuple[Archetype, float]],
        run_context,
        relic_synergy_tags: list[str],
        relic_boosts: dict[str, float] | None = None,
    ) -> EvaluationResult:
        """
        对单张卡进行全维度评估，返回 EvaluationResult。
        """
        deck_set = set(self._normalize_card_id(cid) for cid in run_state.deck)
        raw = self.raw_card_db.get(card.id)
        card_profile = profile_card(card, raw)

        archetype_weights: list[float] = []
        archetype_confidences: list[float] = []
        matched_archetype_ids: list[str] = []
        negative_archetype_ids: list[str] = []
        inferred_archetype_ids: list[str] = []
        weighted_matches: list[tuple[Archetype, float, float]] = []
        is_exact_match = False
        explicit_pollution_hit = False

        for archetype, confidence in motif_profile:
            weight_info = self.library.get_card_weight(archetype.id, card.id)
            if weight_info:
                if weight_info.role == CardRole.POLLUTION:
                    explicit_pollution_hit = True
                    negative_archetype_ids.append(archetype.id)
                    continue
                archetype_weights.append(weight_info.weight)
                archetype_confidences.append(confidence)
                matched_archetype_ids.append(archetype.id)
                weighted_matches.append((archetype, weight_info.weight, confidence))
                is_exact_match = True
            elif raw is not None:
                inferred_w = infer_weight(raw, archetype.id)
                if inferred_w > 0.0:
                    archetype_weights.append(inferred_w)
                    archetype_confidences.append(confidence)
                    matched_archetype_ids.append(archetype.id)
                    inferred_archetype_ids.append(archetype.id)
                    weighted_matches.append((archetype, inferred_w, confidence))
                    log.debug(f"inferred weight {card.id} -> {archetype.id}: {inferred_w:.2f}")

        value_score = score_base_dimension(
            card,
            run_state.phase,
            card_profile=card_profile,
            run_context=run_context,
        )
        need_cover = need_coverage(card_profile, run_context)
        archetype_score = score_archetype_dimension(
            card,
            archetype_weights,
            matched_archetype_confidences=archetype_confidences,
        )
        role = self._determine_role(
            card,
            motif_profile,
            archetype_weights,
            inferred_only=not is_exact_match and bool(inferred_archetype_ids),
            need_cover=need_cover,
            intrinsic_score=value_score,
            dead_draw_risk=card_profile.dead_draw,
            dilution_pressure=run_context.dilution_pressure,
            explicit_pollution_hit=explicit_pollution_hit,
        )

        new_deck = deck_set | {card.id}
        primary_before = 0.0
        primary_after = 0.0
        secondary_deltas: list[tuple[float, float, float]] = []
        second_plan_unlock = 0.0
        weighted_matches.sort(key=lambda item: item[1] * item[2], reverse=True)
        for idx, (archetype, weight, confidence) in enumerate(weighted_matches[:3]):
            before = self._calc_completion(archetype, deck_set)
            after = self._calc_completion(archetype, new_deck)
            if idx == 0:
                primary_before = before
                primary_after = after
            else:
                secondary_deltas.append((before, after, confidence))
            if idx > 0 and confidence < 0.40:
                second_plan_unlock = max(
                    second_plan_unlock,
                    weight * (0.40 - confidence) / 0.40,
                )

        bloat_pen = deck_bloat_penalty(
            card,
            len(run_state.deck),
            role,
            dilution_pressure=run_context.dilution_pressure,
            consistency_need=run_context.needs.get("consistency", 0.0),
            dead_draw_risk=card_profile.dead_draw,
        )

        # 查社区数据
        community_stats = self.community_db.get(card.id)
        community_norm: Optional[float] = (
            community_stats.community_score if community_stats is not None else None
        )

        breakdown = ScoreBreakdown(
            base_score=value_score,
            rarity_score=community_norm if community_norm is not None else 0.0,
            archetype_score=archetype_score,
            completion_score=score_completion_dimension(
                primary_before,
                primary_after,
                secondary_deltas=secondary_deltas,
                weakness_repair=need_cover,
                second_plan_unlock=second_plan_unlock,
            ),
            phase_score=score_phase_dimension(
                card,
                run_state.phase,
                role,
                hp_ratio=run_state.hp_ratio,
                card_profile=card_profile,
                run_context=run_context,
            ),
            synergy_bonus=score_synergy_bonus(
                card,
                run_state,
                relic_synergy_tags,
                relic_boosts=relic_boosts or {},
                matched_archetype_ids=matched_archetype_ids,
                matched_archetype_confidences=archetype_confidences,
                card_profile=card_profile,
                run_context=run_context,
            ),
            pollution_penalty=pollution_penalty(
                card,
                len(run_state.deck),
                role,
                dead_draw_risk=card_profile.dead_draw,
                dilution_pressure=run_context.dilution_pressure,
            ),
        )

        algo_score_100 = combine_scores(breakdown, bloat_penalty=bloat_pen)
        asc_delta = ascension_modifier(role, run_state.ascension, breakdown.archetype_score)
        algo_score_100 = round(max(0.0, min(100.0, algo_score_100 + asc_delta)), 1)

        prior_alpha = community_prior_alpha(
            run_state,
            run_context,
            community_norm is not None,
        )
        prior_norm = apply_contextual_prior(
            algo_score_100 / 100.0,
            community_norm,
            alpha=prior_alpha,
        )
        cv_result = cross_validate(
            prior_norm,
            community_norm,
            community_weight=max(0.05, prior_alpha * 0.55),
        )
        total = round(cv_result.blended_norm * 100, 1)

        confidence, confidence_label = estimate_confidence(
            run_state=run_state,
            run_context=run_context,
            matched_strength=archetype_score,
            need_cover=need_cover,
            exact_match=is_exact_match,
            inferred_only=not is_exact_match and bool(inferred_archetype_ids),
            has_community=community_norm is not None,
        )

        reasons_for, reasons_against = self._build_reasons(
            card,
            role,
            breakdown,
            matched_archetype_ids,
            run_state,
            run_context=run_context,
            card_profile=card_profile,
            need_hits=top_need_hits(card_profile, run_context),
            confidence_label=confidence_label,
            second_plan_unlock=second_plan_unlock,
            negative_ids=negative_archetype_ids,
            inferred_ids=inferred_archetype_ids,
            community_stats=community_stats,
            cv_result=cv_result,
            algo_score=algo_score_100,
        )

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
            recommendation=self._make_recommendation(total, role),
            grade=score_to_grade(total),
            confidence=round(confidence, 3),
            confidence_label=confidence_label,
        )

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
        detected_archetypes: list[Archetype] | list[tuple[Archetype, float]],
        archetype_weights: list[float],
        inferred_only: bool = False,
        need_cover: float = 0.0,
        intrinsic_score: float = 0.0,
        dead_draw_risk: float = 0.0,
        dilution_pressure: float = 0.0,
        explicit_pollution_hit: bool = False,
    ) -> CardRole:
        """
        根据套路匹配结果推断卡牌在当前 run 中的角色。

        inferred_only: 若为 True，表示所有权重来自推断层（非手动定义），
                       最低角色保底为 FILLER，不判定为 POLLUTION。
        """
        soft_match = soft_or(archetype_weights)

        if not detected_archetypes or not archetype_weights:
            if explicit_pollution_hit and need_cover < 0.35:
                return CardRole.POLLUTION
            if dead_draw_risk * max(0.25, dilution_pressure) > 0.30:
                return CardRole.POLLUTION
            if need_cover >= 0.45 or intrinsic_score >= 0.55:
                return CardRole.FILLER
            return CardRole.UNKNOWN

        def smooth(value: float, center: float, width: float) -> float:
            safe_width = max(width, 1e-6)
            return 1.0 / (1.0 + math.exp(-(value - center) / safe_width))

        core_score = smooth(soft_match, 0.78, 0.08)
        enabler_score = smooth(soft_match, 0.48, 0.10) * (0.75 + 0.25 * need_cover)
        filler_score = max(
            smooth(soft_match, 0.20, 0.12),
            0.45 * need_cover + 0.35 * intrinsic_score,
        )
        pollution_score = smooth(
            dead_draw_risk * max(dilution_pressure, 0.25),
            0.20,
            0.06,
        ) * (1.0 - min(1.0, soft_match + need_cover * 0.6))
        if explicit_pollution_hit and need_cover < 0.35:
            pollution_score = max(pollution_score, 0.72)

        if inferred_only:
            core_score *= 0.70
            enabler_score *= 0.90

        scores = {
            CardRole.CORE: core_score,
            CardRole.ENABLER: enabler_score,
            CardRole.FILLER: filler_score,
            CardRole.POLLUTION: pollution_score,
        }
        role = max(scores.items(), key=lambda item: item[1])[0]
        if role == CardRole.POLLUTION and need_cover > 0.35:
            return CardRole.FILLER
        return role

    def _build_reasons(
        self,
        card: Card,
        role: CardRole,
        breakdown: ScoreBreakdown,
        matched_archetypes: list[str],
        run_state: RunState,
        run_context=None,
        card_profile=None,
        need_hits: Optional[list[str]] = None,
        confidence_label: str = "",
        second_plan_unlock: float = 0.0,
        negative_ids: Optional[list[str]] = None,
        inferred_ids: Optional[list[str]] = None,
        community_stats=None,
        cv_result: Optional[CrossValidationResult] = None,
        algo_score: float = 0.0,
    ) -> tuple[list[str], list[str]]:
        """
        生成中文可解释理由。
        返回 (reasons_for, reasons_against)。
        """
        negative_ids = negative_ids or []
        inferred_ids = inferred_ids or []
        need_hits = need_hits or []
        reasons_for: list[str] = []
        reasons_against: list[str] = []

        exact_ids = [aid for aid in matched_archetypes if aid not in inferred_ids]
        if exact_ids:
            archetype_names = [
                a.name
                for a in [self.library.get_archetype(aid) for aid in exact_ids]
                if a is not None
            ]
            reasons_for.append(f"Supports motifs: {', '.join(archetype_names)}")
        if inferred_ids:
            inferred_names = [
                a.name
                for a in [self.library.get_archetype(aid) for aid in inferred_ids]
                if a is not None
            ]
            reasons_for.append(f"Inferred fit: {', '.join(inferred_names)}")

        if negative_ids:
            negative_names = [
                a.name
                for a in [self.library.get_archetype(aid) for aid in negative_ids]
                if a is not None
            ]
            if negative_names:
                reasons_against.append(f"Conflicts with motifs: {', '.join(negative_names)}")

        if need_hits:
            reasons_for.append(f"Covers current gap: {', '.join(need_hits)}")

        if breakdown.base_score >= 0.60:
            reasons_for.append("Strong standalone baseline for this reward")

        if breakdown.completion_score > 0.22:
            reasons_for.append("Improves the current shell and/or future boss plan")

        if second_plan_unlock > 0.20:
            reasons_for.append("Keeps a secondary plan or pivot open")

        if breakdown.synergy_bonus > 0.22:
            reasons_for.append("Hooks into relics or deck network effects")

        if card_profile is not None and card_profile.energy > 0.35 and card_profile.consistency > 0.30:
            reasons_for.append("Improves turn economy and hand quality")

        if role == CardRole.POLLUTION:
            reasons_against.append("Adds more dilution than payoff in the current shell")

        if matched_archetypes and not exact_ids and inferred_ids:
            reasons_against.append("Mostly supported by inference instead of exact library coverage")

        if not matched_archetypes and not need_hits:
            reasons_against.append("Does not meaningfully fit current motifs or fix a major weakness")

        if card_profile is not None and run_context is not None:
            if card_profile.dead_draw > 0.50 and run_context.dilution_pressure > 0.35:
                reasons_against.append("High dead-draw risk in an already stretched deck")
            if card_profile.setup > 0.45 and run_context.greed_tolerance < 0.45:
                reasons_against.append("Setup-heavy for the current HP/path pressure")

        if confidence_label == "Low confidence":
            reasons_against.append("Low confidence: partial inference / thin context coverage")

        if cv_result is not None:
            if cv_result.has_community_data and community_stats is not None:
                wr = f"{community_stats.win_rate_pct:.1f}%"
                pr = f"{community_stats.pick_rate_pct:.1f}%"
                cs = cv_result.community_score

                if cv_result.alignment == Alignment.AGREEMENT:
                    if cs >= 0.70:
                        reasons_for.append(f"Community prior leans positive ({wr} WR / {pr} PR)")
                    elif cs <= 0.35:
                        reasons_against.append(f"Community prior is cautious ({wr} WR / {pr} PR)")
                elif cv_result.alignment == Alignment.SOFT_CONFLICT:
                    reasons_against.append(f"Community context disagrees somewhat ({cv_result.delta:.0%} delta)")
                elif cv_result.alignment == Alignment.CONFLICT:
                    if algo_score / 100.0 > cv_result.community_score:
                        reasons_against.append(f"Community data pushes back against this line ({wr} WR)")
                    else:
                        reasons_for.append(f"Community data suggests this may be undervalued ({wr} WR)")
            elif not cv_result.has_community_data and 40 <= algo_score <= 65:
                reasons_against.append("No community prior available; using local evaluation only")

        if confidence_label and confidence_label != "Low confidence":
            reasons_for.append(confidence_label)

        return reasons_for, reasons_against

    @staticmethod
    def _make_recommendation(
        total_score: float,
        role: CardRole,
        pick_delta: float = 0.0,
        threshold: float = 3.0,
    ) -> str:
        """Generate a recommendation anchored against Skip."""
        if role in (CardRole.POLLUTION, CardRole.SKIP):
            return "Skip"
        if pick_delta <= 0:
            return "Skip"
        if pick_delta < threshold:
            return "Caution"
        if total_score >= 78 or pick_delta >= threshold + 7.0:
            return "Highly Recommended"
        elif total_score >= 60 or pick_delta >= threshold + 3.5:
            return "Recommended"
        elif total_score >= 48 or pick_delta >= threshold:
            return "Optional"
        return "Caution"

    @staticmethod
    def _build_skip_result(
        run_state: RunState,
        run_context,
        motif_profile: list[tuple[Archetype, float]],
        card_results: list[EvaluationResult],
    ) -> EvaluationResult:
        top_motifs = [archetype.name for archetype, _ in motif_profile[:2]]
        reasons_for: list[str] = []
        reasons_against: list[str] = []

        if run_context.dilution_pressure > 0.40:
            reasons_for.append("Deck dilution pressure is already high")
        if run_context.deck.dead_draw > 0.35:
            reasons_for.append("Preserving draw quality has real value here")
        if top_motifs:
            reasons_for.append(f"Current shell already has direction: {', '.join(top_motifs)}")

        if run_context.short_term_survival > 0.55:
            reasons_against.append("You still need short-term help if one offer clearly provides it")
        if not card_results:
            reasons_for.append("No reliable card evaluations were available")

        return EvaluationResult(
            card_id="__skip__",
            card_name="Skip",
            rarity="action",
            total_score=round(run_context.skip_score_norm * 100, 1),
            role=CardRole.SKIP,
            breakdown=ScoreBreakdown(
                base_score=run_context.skip_score_norm,
                rarity_score=0.0,
                archetype_score=0.0,
                completion_score=0.0,
                phase_score=run_context.dilution_pressure,
                synergy_bonus=0.0,
                pollution_penalty=0.0,
            ),
            matched_archetypes=[],
            reasons_for=reasons_for,
            reasons_against=reasons_against,
            recommendation="Skip",
            grade="",
            pick_delta_vs_skip=0.0,
            confidence=round(run_context.input_confidence, 3),
            confidence_label="High confidence" if run_context.input_confidence >= 0.78 else "Medium confidence" if run_context.input_confidence >= 0.56 else "Low confidence",
            is_skip_option=True,
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
                "results": [
                    {
                        "card_id": r.card_id,
                        "card_name": r.card_name,
                        "rarity": r.rarity,
                        "total_score": r.total_score,
                        "grade": r.grade,
                        "recommendation": r.recommendation,
                        "pick_delta_vs_skip": r.pick_delta_vs_skip,
                        "confidence": r.confidence,
                        "confidence_label": r.confidence_label,
                        "is_skip_option": r.is_skip_option,
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
