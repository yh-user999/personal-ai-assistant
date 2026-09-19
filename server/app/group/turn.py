"""群聊一轮的编排：前置场景准备、计划字段写入、提示注入与收尾记账。

仅群聊（``group_id`` 非空）路径可达，由 ``app.chat.pipeline`` 在 ``ctx.is_group``
时调用；私聊主链路不经过本模块。当前没有 QQ 下游入口，重启接入时从这里复用。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from app.chat import retrieval, response_plan
from app.chat.context import ChatContext, ChatResponse, ChatRuntime
from app.group import (
    attention as attention_drift,
    context as group_context,
    heartflow,
    interjection as group_interjection,
)


@dataclass(slots=True)
class GroupTurnState:
    """群聊一轮的前置编排结果：作用域上下文、社交评分与闸门决策。"""

    scene: dict[str, Any]
    planner_history: list[dict[str, Any]]
    score: group_interjection.SocialScore | None = None
    gate: group_interjection.GateDecision | None = None
    reservation: group_interjection.GateReservation | None = None
    care: Any | None = None


def prepare_interject_config(settings: Any) -> group_interjection.InterjectionConfig:
    """装配主动插话闸门并返回本轮配置。"""
    group_interjection.configure_from_settings(settings)
    return group_interjection.config_from_settings(settings)


async def _short_circuit_group_social(
    ctx: ChatContext,
    runtime: ChatRuntime,
    state: GroupTurnState,
    *,
    gate: group_interjection.GateDecision | None,
    action: str,
) -> ChatResponse:
    """在群社交硬闸门拒绝后只记录当前群事件，不进入主 LLM。"""
    score = state.score
    settings = runtime.settings
    memory = runtime.memory
    if score is not None:
        ctx.trace.route_name = (
            "group:social_shadow"
            if gate is not None and gate.reason == "shadow_only"
            else "group:social_ignore"
        )
        ctx.trace.social_judgment = {
            **score.as_dict(),
            "action": action,
            "addressed": False,
            "atmosphere": str(state.scene.get("atmosphere") or "casual"),
            "gate": gate.as_dict() if gate is not None else {"reason": "planner_ignore"},
            "interject_enabled": bool(getattr(settings, "group_social_interject_enabled", False)),
            "care_allowed": bool(state.care and state.care.eligible),
            "care_kind": str(getattr(state.care, "kind", "") or ""),
            "care_reason": str(getattr(state.care, "reason", "") or ""),
            "heartflow": state.scene.get("heartflow", {}),
            "heartflow_decision": state.scene.get("heartflow_decision", {}),
        }
    with ctx.trace.stage("social_gate"):
        group_context.remember(ctx.group_id, "user", ctx.message, user_id=ctx.uid)
        await memory.write_message("user", ctx.message, user_id=ctx.uid, group_id=ctx.group_id)
    heartflow.record_silence(ctx.group_id)
    return ChatResponse(reply="", memories_used=0)


async def prepare_group_scene(
    ctx: ChatContext,
    runtime: ChatRuntime,
    interject_config: group_interjection.InterjectionConfig,
) -> tuple[GroupTurnState, ChatResponse | None]:
    """群聊前置编排：恢复群上下文、更新群状态、评分与闸门决策。

    返回 ``(state, early)``；``early`` 非空表示本轮在生成前短路，调用方原样返回。
    """
    settings = runtime.settings
    services = runtime.services
    msg = ctx.message

    # 重启后只从既有群记忆恢复有限上下文；不新增原始消息存储。
    if getattr(settings, "group_state_persistence_enabled", True):
        await asyncio.to_thread(group_context.hydrate, ctx.group_id)
    # planner 只读取当前群的有界内存上下文；绝不把私聊历史带进群判断。
    planner_history = group_context.recent_messages(ctx.group_id)
    scene = group_context.scene_summary(ctx.group_id)
    heartflow.observe_message(
        ctx.group_id,
        directed=ctx.group_directed,
        atmosphere=str(scene.get("atmosphere") or "casual"),
    )
    scene["heartflow"] = heartflow.snapshot(ctx.group_id)
    robot_service = getattr(services, "robot_state", None)
    robot_snapshot: dict[str, Any] = {}
    if robot_service:
        try:
            hydrate = getattr(robot_service, "hydrate", None)
            if callable(hydrate) and getattr(settings, "group_state_persistence_enabled", True):
                await asyncio.to_thread(hydrate, ctx.group_id)
            robot_service.observe_group_message(
                ctx.group_id,
                atmosphere=str(scene.get("atmosphere") or "casual"),
            )
            robot_snapshot = robot_service.snapshot(ctx.group_id)
        except Exception as exc:  # noqa: BLE001
            runtime.logger.debug("机器人群状态更新失败: %s", type(exc).__name__)
    relationship_service = getattr(services, "group_relationship", None)
    relationship_snapshot: dict[str, Any] = {}
    if relationship_service:
        try:
            await asyncio.to_thread(
                relationship_service.observe_message,
                ctx.group_id,
                ctx.uid,
                msg,
                directed=ctx.group_directed,
            )
            relationship_snapshot = await asyncio.to_thread(
                relationship_service.get_snapshot, ctx.group_id, ctx.uid
            )
            scene["relationship"] = relationship_snapshot
        except Exception as exc:  # noqa: BLE001
            runtime.logger.debug("群关系更新失败: %s", type(exc).__name__)

    care_decision = None
    care_service = getattr(services, "group_care", None)
    if care_service:
        try:
            care_decision = await asyncio.to_thread(
                care_service.assess,
                ctx.group_id,
                ctx.uid,
                msg,
                relationship_snapshot,
                scene,
                enabled=getattr(settings, "group_care_enabled", True),
                cooldown_seconds=getattr(settings, "group_care_cooldown_seconds", 21600.0),
                daily_limit=getattr(settings, "group_care_daily_limit", 2),
                max_followups=getattr(settings, "group_care_max_followups", 1),
            )
            scene["care"] = care_decision.as_dict()
        except Exception as exc:  # noqa: BLE001
            runtime.logger.debug("群聊关怀判断失败: %s", type(exc).__name__)

    state = GroupTurnState(scene=scene, planner_history=planner_history, care=care_decision)
    # 每条进入服务端的群消息推进序号；主动插话配额只在发送成功后记账。
    group_interjection.interject_gate.observe_message(ctx.group_id)
    if ctx.group_directed:
        return state, None

    score = group_interjection.score_group_interjection(
        msg,
        planner_history,
        scene,
        robot_snapshot,
        directed=False,
        threshold=interject_config.threshold,
    )
    if interject_config.enabled:
        heartflow_decision = heartflow.decide(
            ctx.group_id,
            score=score.score,
            directed=False,
            config=heartflow.config_from_settings(
                settings, interject_enabled=interject_config.enabled
            ),
        )
        scene["heartflow_decision"] = heartflow_decision.as_dict()
        if not heartflow_decision.allowed:
            score = group_interjection.SocialScore(
                score=score.score,
                eligible=False,
                action="ignore",
                factors={
                    **score.factors,
                    "heartflow_propensity": heartflow_decision.propensity,
                },
                penalties=score.penalties,
                reasons=tuple(
                    list(score.reasons) + [heartflow_decision.reason]
                ),
            )
    state.score = score

    # 主动插话关闭时严格 fail-closed，不能为了语义判断调用 planner。
    if not interject_config.enabled:
        return state, await _short_circuit_group_social(
            ctx, runtime, state, gate=None, action="ignore"
        )

    # 开启主动插话时先让 semantic planner 选择是否/如何接话，再由
    # finalize_group_social_gate() 执行评分、心流、冷却和配额硬闸门。
    return state, None


def apply_group_plan_fields(plan: response_plan.ResponsePlan, state: GroupTurnState) -> None:
    """把群聊关怀信号与插话评分写进生效计划（未触发时保持原计划）。"""
    care_decision = state.care
    if care_decision is not None and care_decision.eligible:
        plan.social_reasons = list(dict.fromkeys(
            list(plan.social_reasons) + list(care_decision.signals)
        ))[:8]
    score = state.score
    if score is None:
        return
    gate = state.gate
    # 评分和闸门只能决定是否进入生成链路；planner 已选出的 banter/tease/ask_back
    # 仍保留为表达动作，不能被旧的 interject 默认值覆盖。
    if plan.source != "llm" or plan.social_action not in response_plan.SOCIAL_ACTIONS:
        plan.social_action = "interject"
    plan.social_confidence = max(plan.social_confidence, score.score)
    plan.social_reasons = list(dict.fromkeys(
        list(plan.social_reasons) + list(score.reasons)
    ))[:8]
    plan.social_addressed = False
    if plan.source != "llm" or not plan.social_atmosphere:
        plan.social_atmosphere = str(state.scene.get("atmosphere") or "casual")[:32]
    plan.social_score = score.score
    plan.social_factors = dict(score.factors)
    plan.social_penalties = dict(score.penalties)
    plan.social_gate_reason = gate.reason if gate else ""
    plan.social_gate_allowed = bool(gate and gate.allowed)
    plan.social_gate_would_allow = bool(gate and gate.would_allow)
    plan.social_cooldown_remaining = gate.cooldown_remaining if gate else 0.0
    plan.social_hourly_count = gate.hourly_count if gate else 0
    plan.social_message_gap = gate.message_gap if gate else 0


async def finalize_group_social_gate(
    ctx: ChatContext,
    runtime: ChatRuntime,
    plan: response_plan.ResponsePlan,
    state: GroupTurnState,
    interject_config: group_interjection.InterjectionConfig,
) -> ChatResponse | None:
    """在 semantic planner 之后执行非直达群消息的硬插话闸门。"""
    if ctx.group_directed or state.score is None:
        return None
    score = state.score
    requested = str(plan.social_action or "ignore").strip()
    if requested == "ignore":
        plan.social_gate_reason = "planner_ignore"
        plan.social_gate_allowed = False
        plan.social_gate_would_allow = bool(score.eligible)
        ctx.trace.response_plan.update(plan.summary())
        return await _short_circuit_group_social(
            ctx, runtime, state, gate=None, action="ignore"
        )

    gate, reservation = group_interjection.interject_gate.reserve(
        ctx.group_id,
        score,
        config=interject_config,
    )
    state.gate = gate
    if not gate.allowed:
        plan.social_gate_reason = gate.reason
        plan.social_gate_allowed = False
        plan.social_gate_would_allow = bool(gate.would_allow)
        plan.social_cooldown_remaining = gate.cooldown_remaining
        plan.social_hourly_count = gate.hourly_count
        plan.social_message_gap = gate.message_gap
        ctx.trace.response_plan.update(plan.summary())
        return await _short_circuit_group_social(
            ctx, runtime, state, gate=gate, action="ignore"
        )

    state.reservation = reservation
    plan.social_gate_reason = gate.reason
    plan.social_gate_allowed = True
    plan.social_gate_would_allow = bool(gate.would_allow)
    plan.social_cooldown_remaining = gate.cooldown_remaining
    plan.social_hourly_count = gate.hourly_count
    plan.social_message_gap = gate.message_gap
    return None


def record_group_social_trace(
    ctx: ChatContext,
    plan: response_plan.ResponsePlan,
    state: GroupTurnState,
    runtime: ChatRuntime,
) -> None:
    """群聊社交判定落 Trace（仅在生效计划带有 social_action 时调用）。"""
    settings = runtime.settings
    care_decision = state.care
    ctx.trace.social_judgment = {
        "action": plan.social_action,
        "confidence": plan.social_confidence,
        "reasons": plan.social_reasons,
        "addressed": plan.social_addressed,
        "atmosphere": plan.social_atmosphere,
        "topic_shift": plan.social_topic_shift,
        "score": plan.social_score,
        "factors": plan.social_factors,
        "penalties": plan.social_penalties,
        "gate_reason": plan.social_gate_reason,
        "gate_allowed": plan.social_gate_allowed,
        "gate_would_allow": plan.social_gate_would_allow,
        "cooldown_remaining": plan.social_cooldown_remaining,
        "hourly_count": plan.social_hourly_count,
        "message_gap": plan.social_message_gap,
        "interject_enabled": bool(getattr(settings, "group_social_interject_enabled", False)),
        "care_allowed": bool(care_decision and care_decision.eligible),
        "care_kind": str(getattr(care_decision, "kind", "") or ""),
        "care_reason": str(getattr(care_decision, "reason", "") or ""),
    }


def apply_group_prompt_injections(
    ctx: ChatContext,
    runtime: ChatRuntime,
    bundle: retrieval.RetrievalBundle,
) -> None:
    """群聊提示注入：注意力漂移与心流提示块并入 robot_state。"""
    drift_block = attention_drift.build_prompt_block(runtime.settings)
    if drift_block:
        bundle.robot_state = "\n".join(
            item for item in (bundle.robot_state, drift_block) if item
        )
    heartflow_note = heartflow.get_injection(ctx.group_id)
    if heartflow_note:
        bundle.robot_state = "\n".join(
            item for item in (bundle.robot_state, heartflow_note) if item
        )


def release_group_reservation(state: GroupTurnState) -> None:
    """释放未提交的主动插话预留名额（生成失败或回复为空时）。"""
    if state.reservation is not None:
        group_interjection.interject_gate.release(state.reservation)
        state.reservation = None


async def finalize_group_turn(
    ctx: ChatContext,
    runtime: ChatRuntime,
    plan: response_plan.ResponsePlan,
    reply: str,
    state: GroupTurnState,
) -> None:
    """群聊收尾：回复落群作用域、插话配额记账、关系/关怀/表达学习更新。"""
    settings = runtime.settings
    memory = runtime.memory
    services = runtime.services

    heartflow.record_reply(
        ctx.group_id,
        action=str(plan.social_action or ("answer" if ctx.group_directed else "interject")),
    )
    group_context.remember(ctx.group_id, "assistant", reply)
    with ctx.trace.stage("persistence.assistant"):
        await memory.write_message(
            "assistant", reply, user_id=ctx.uid, group_id=ctx.group_id
        )
    if state.reservation is not None:
        if reply.strip():
            # 只有生成并通过审校的非空回复才提交预留名额。
            if not group_interjection.interject_gate.commit(state.reservation):
                runtime.logger.warning("主动插话 reservation 已过期或重复提交")
        else:
            group_interjection.interject_gate.release(state.reservation)
        state.reservation = None
    robot_service = getattr(services, "robot_state", None)
    if robot_service:
        try:
            robot_service.record_group_reply(ctx.group_id)
        except Exception as exc:  # noqa: BLE001
            runtime.logger.debug("机器人群状态记录失败: %s", type(exc).__name__)
    relationship_service = getattr(services, "group_relationship", None)
    if relationship_service:
        try:
            await asyncio.to_thread(
                relationship_service.record_reply,
                ctx.group_id,
                ctx.uid,
                action=plan.social_action or "answer",
                success=bool(reply.strip()),
            )
        except Exception as exc:  # noqa: BLE001
            runtime.logger.debug("群关系回复记录失败: %s", type(exc).__name__)
    care_decision = state.care
    if care_decision is not None and care_decision.eligible and reply.strip():
        care_service = getattr(services, "group_care", None)
        if care_service:
            try:
                await asyncio.to_thread(
                    care_service.record_sent,
                    ctx.group_id,
                    ctx.uid,
                    care_decision.kind or "initial",
                )
            except Exception as exc:  # noqa: BLE001
                runtime.logger.debug("群聊关怀记账失败: %s", type(exc).__name__)
    # 群级表达/黑话学习放后台：不能拖慢群回复，也不写入个人画像。
    if getattr(settings, "group_expression_learning_enabled", True):
        from app.group import profile as group_profile_extract

        retrieval.track_background(
            runtime,
            group_profile_extract.maybe_extract(
                ctx.group_id,
                ctx.uid,
                ctx.message,
                request_id=ctx.request_id,
                write_profile=False,
            ),
        )
