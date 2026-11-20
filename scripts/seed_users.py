"""初始化正式身份数据：建表、写入四类角色、创建演示账号与模型授权。

不加载模型包，只读取 trusted_registry.json 中的版本与摘要，因此在未安装
CPython 3.12.14 的机器上也能执行。密码随机生成并打印一次，不写入仓库。

用法：
    python -m scripts.seed_users
    python -m scripts.seed_users --password 自定义密码
"""

import argparse
import json
import secrets
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from app.auth.store import AuthStore
from app.config import Settings
from app.demo_auth import DEMO_MODEL_ID
from app.storage.sqlite_store import SqlitePredictionStore

DEMO_ACCOUNTS = [
    ("admin", "管理员", ["admin"]),
    ("developer", "模型开发人员", ["model_developer"]),
    ("risk", "风险管理人员", ["risk_manager"]),
    ("auditor", "审计人员", ["auditor"]),
]


def descriptors_from_registry(root: Path) -> list[dict]:
    registry = json.loads((root / "trusted_registry.json").read_text(encoding="utf-8"))["versions"]
    return [{"model_version_id": str(uuid5(NAMESPACE_URL, "financial-ai:model-version:" + name)),
             "model_id": DEMO_MODEL_ID, "version": name,
             "manifest_sha256": entry["manifest_sha256"]}
            for name, entry in sorted(registry.items())]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--password", default="", help="所有演示账号统一使用的密码；留空则随机生成")
    args = parser.parse_args()
    password = args.password or secrets.token_urlsafe(12)

    settings = Settings.from_env()
    base = SqlitePredictionStore(settings.database_url)
    try:
        base.initialize(descriptors_from_registry(settings.artifact_root))
        auth = AuthStore(base.engine)
        auth.seed_roles()
        created = []
        for username, display, roles in DEMO_ACCOUNTS:
            if auth.user_by_username(username) is not None:
                created.append((username, "已存在，跳过"))
                continue
            auth.create_user(username, password, display, roles, [DEMO_MODEL_ID])
            created.append((username, "已创建"))
        print(json.dumps({"password": password,
                          "accounts": [{"username": u, "state": s} for u, s in created]},
                         ensure_ascii=False, indent=2))
    finally:
        base.close()


if __name__ == "__main__":
    main()
