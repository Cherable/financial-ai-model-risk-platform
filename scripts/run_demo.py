"""一键启动演示：建库、启动 API、监测 Worker 与页面。

默认使用 data/runtime/demo.db（已被 .gitignore 排除）。首次运行会创建
四个演示账号并打印统一密码；JWT 密钥保存在 data/runtime/jwt_secret.txt，
同一次演示期间保持不变。

用法：
    python -m scripts.run_demo
    python -m scripts.run_demo --password demo12345 --api-port 8000 --ui-port 8501
按 Ctrl+C 结束全部进程。
"""

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "data" / "runtime"
ACCOUNTS = ["admin（管理员）", "developer（模型开发人员）", "risk（风险管理人员）", "auditor（审计人员）"]


def load_secret(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    value = secrets.token_urlsafe(48)
    path.write_text(value, encoding="utf-8")
    return value


def seed(db_url: str, password: str) -> None:
    env = {**os.environ, "DATABASE_URL": db_url, "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run([sys.executable, "-m", "scripts.seed_users", "--password", password],
                            cwd=ROOT, env=env, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
        raise SystemExit("初始化演示数据失败")


def wait_for_api(port: int, timeout: int = 60) -> bool:
    """模型包加载需要十几秒，等 API 真正可用再提示用户。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
                if response.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


def main() -> None:
    # 子进程与父进程共用标准输出；保证横幅在任何重定向下都能立即出现。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--password", default="", help="演示账号统一密码；留空则随机生成")
    parser.add_argument("--api-port", type=int, default=8000)
    parser.add_argument("--ui-port", type=int, default=8501)
    args = parser.parse_args()

    RUNTIME.mkdir(parents=True, exist_ok=True)
    db_path = RUNTIME / "demo.db"
    db_url = "sqlite:///" + db_path.as_posix()
    secret = load_secret(RUNTIME / "jwt_secret.txt")

    fresh = not db_path.exists()
    password = args.password or secrets.token_urlsafe(9)
    if fresh:
        print("首次运行，正在创建演示数据库与账号…")
        seed(db_url, password)
    else:
        password = args.password or "（数据库已存在，沿用此前设置的密码）"

    env = {**os.environ, "DATABASE_URL": db_url, "JWT_SECRET": secret,
           "API_BASE_URL": f"http://127.0.0.1:{args.api_port}"}
    commands = [
        ("API", [sys.executable, "-m", "uvicorn", "app.main:create_app", "--factory",
                 "--host", "127.0.0.1", "--port", str(args.api_port)]),
        ("Worker", [sys.executable, "-m", "app.worker"]),
        ("页面", [sys.executable, "-m", "streamlit", "run", "streamlit_app.py",
                "--server.address", "127.0.0.1", "--server.port", str(args.ui_port),
                "--server.headless", "true", "--browser.gatherUsageStats", "false"]),
    ]
    processes = []
    print("\n演示账号：" + "、".join(ACCOUNTS))
    print(f"统一密码：{password}")
    print(f"页面地址：http://127.0.0.1:{args.ui_port}    API 文档：http://127.0.0.1:{args.api_port}/docs")
    print("按 Ctrl+C 结束全部进程。\n")
    try:
        for name, command in commands:
            print(f"启动{name}…")
            processes.append(subprocess.Popen(command, cwd=ROOT, env=env))
        if wait_for_api(args.api_port):
            print("API 已就绪，可在浏览器打开上面的页面地址。\n")
        else:
            print("API 在 60 秒内未就绪，请查看上面的日志。\n")
        for process in processes:
            process.wait()
            if process.returncode not in (0, None):
                print(f"进程退出，代码 {process.returncode}")
                break
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == "__main__":
    main()
