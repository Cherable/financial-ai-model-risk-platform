import streamlit as st

from ui.common import client, heading, identity, mutate, version_picker


def render():
    heading("模型版本", "核对受信任模型包与当前服务版本；激活操作由服务端验证并留痕。")
    version, versions = version_picker()
    a, b, c = st.columns(3)
    a.metric("已登记版本", len(versions))
    b.metric("当前版本", version["version"])
    c.metric("服务状态", "已激活" if version["active"] else "未激活")
    st.subheader("版本登记簿")
    st.dataframe(versions, hide_index=True, width="stretch",
                 column_order=["version", "active", "status", "model_version_id", "model_id", "manifest_sha256"])
    with st.expander("所选版本完整标识"):
        st.json(version)
    if "admin" in identity()["roles"]:
        with st.form("activate"):
            confirmed = st.checkbox("确认将所选版本设为当前评分版本")
            if st.form_submit_button("激活所选版本", disabled=version["active"]):
                if not confirmed:
                    st.warning("请先勾选确认。")
                else:
                    mutate(f"/api/v1/model-versions/{version['model_version_id']}/activate", {})
    else:
        st.info("当前角色可查看版本；激活需要管理员权限。")
    st.caption("评分仍由 A3 API 提供；本页不上传或反序列化任意模型文件。")
