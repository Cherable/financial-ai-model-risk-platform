"""Application frame, invoked by the root streamlit_app.py entrypoint."""

import sys
from pathlib import Path

# Streamlit also adds ui/ to sys.path; keep the repository root authoritative.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import streamlit as st

from ui.api_client import ApiError
from ui import common
from ui.pages import alerts, models, monitoring

def run():
    st.set_page_config(page_title="金融 AI · 风险观测台", page_icon="📊", layout="wide")
    st.markdown("""<style>
    [data-testid="stAppViewContainer"] {background: #f5f7fb;}
    [data-testid="stSidebar"] {background: #eaf0f7;}
    h1,h2,h3 {color: #142e48;}
    [data-testid="stMetric"] {background: white; border: 1px solid #dce5ef; border-radius: 10px; padding: 16px;}
    .block-container {padding-top: 2rem; padding-bottom: 3rem;}
    [data-testid="stMetricValue"] {font-size: 1.7rem;}
    [data-testid="stDataFrame"] {border-radius: 10px;}
    </style>""", unsafe_allow_html=True)

    with st.sidebar:
        st.header("◈ 风险观测台")
        st.caption("金融 AI 模型风险监测与审计平台")

    page = st.navigation([
        st.Page(models.render, title="模型版本", icon="📦", url_path="models", default=True),
        st.Page(monitoring.render, title="监测指标", icon="📈", url_path="monitoring"),
        st.Page(alerts.render, title="告警处置", icon="🚩", url_path="alerts"),
    ])

    if "token" not in st.session_state:
        st.title("连接风险观测台")
        st.write("输入服务端配置的演示访问令牌。正式账号登录由成员 B 后续接入。")
        with st.form("connect", clear_on_submit=True):
            token = st.text_input("访问令牌", type="password")
            submitted = st.form_submit_button("连接", type="primary")
        if submitted:
            st.session_state["token"] = token
            try:
                st.session_state["identity"] = common.client().request("GET", "/api/v1/me")
            except ApiError:
                st.session_state.clear()
                st.error("连接失败，请检查令牌及 API 服务。")
            else:
                st.rerun()
        st.stop()

    # Refresh roles on every rerun; the server still checks every mutation independently.
    try:
        st.session_state["identity"] = common.client().request("GET", "/api/v1/me")
    except ApiError:
        st.session_state.clear()
        st.error("会话不可用，请刷新后重新连接。")
        st.stop()
    with st.sidebar:
        st.caption("角色：" + " / ".join(st.session_state["identity"]["roles"]))
        if st.button("退出连接"):
            st.session_state.clear()
            st.rerun()
        st.caption("窗口 60 秒 · UTC 时间\n\n窗口结束后 10 秒开始结算")
        st.caption("仅使用教学合成数据。阈值用于实验验证。")

    try:
        page.run()
    except ApiError as exc:
        from ui.common import error
        error(exc)
