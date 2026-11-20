import streamlit as st

from ui.api_client import ApiError
from ui.common import can_audit, client, error, heading, mutate

STATUS_LABELS = {"pending": "已排队", "running": "执行中", "passed": "通过",
                 "mismatch": "结果不一致", "failed": "执行失败"}
CHECK_LABELS = {"pending": "待核验", "verified": "核验通过",
                "package_missing": "模型包缺失", "digest_mismatch": "包摘要不符",
                "environment_mismatch": "运行环境不符", "inference_failed": "推理失败",
                "internal_error": "内部错误"}


def show_task(task):
    status = task["status"]
    st.subheader(f"任务状态：{STATUS_LABELS.get(status, status)}")
    st.caption(f"任务 {task['replay_id']} · 被复现评分 {task['prediction_id']}")
    st.caption(f"环境核验：{CHECK_LABELS.get(task['environment_check'], task['environment_check'])} · "
               f"发起人 {task['requested_by']} · 发起时间 {task['created_at']}")
    if status in {"pending", "running"}:
        st.info("任务由监测 Worker 在下一轮领取执行。稍后刷新本页查看结果。")
        return
    if status == "failed":
        st.error(f"复现无法完成：{task['failure_reason'] or '原因未记录'}")
        st.caption("失败表示没有取得可信计算，不会退回到当前激活版本给出近似结果。")
        return
    columns = st.columns(4)
    columns[0].metric("原概率", task["original_probability"])
    columns[1].metric("复现概率", task["replay_probability"])
    columns[2].metric("绝对误差", task["absolute_error"])
    columns[3].metric("判定一致", "是" if task["decision_match"] else "否")
    if status == "passed":
        st.success("使用原模型包与原输入复现成功，误差在 1e-8 以内且阈值判断一致。")
    else:
        st.warning("计算已完成但与原始记录不一致。请核对模型包版本与运行环境。")


def render():
    heading("复现任务", "用原评分固定的模型版本与包摘要重新计算；不会调用当前激活版本。")
    if not can_audit():
        st.info("复现需要 audit 或 admin 角色。请使用审计账号登录。")
        return

    prediction_id = st.text_input("评分编号", value=st.session_state.get("replay_prediction", ""),
                                  help="从审计查询页复制评分编号")
    if st.button("发起复现", type="primary"):
        if not prediction_id.strip():
            st.warning("请先填写评分编号。")
        else:
            created = mutate("/api/v1/replay-runs", {"prediction_id": prediction_id.strip()})
            if created:
                st.session_state["replay_id"] = created["replay_id"]

    replay_id = st.text_input("复现任务编号", value=st.session_state.get("replay_id", ""))
    if not replay_id.strip():
        st.caption("同一评分已有排队或执行中的任务时，服务端返回 409 并保留原任务。")
        return

    try:
        task = client().request("GET", f"/api/v1/replay-runs/{replay_id.strip()}")
    except ApiError as exc:
        error(exc)
        return
    show_task(task)
    with st.expander("任务原始记录"):
        st.json(task)
