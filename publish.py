"""
一键发布：git commit + push GitHub，同时上传文件到 VPS 并重启服务

用法：
  python publish.py ubuntu@1.2.3.4
  python publish.py ubuntu@1.2.3.4 -m "fix: room creation bug"
  python publish.py ubuntu@1.2.3.4 --port 9000
  python publish.py ubuntu@1.2.3.4 --no-vps     # 只 commit + push，不更新 VPS
  python publish.py ubuntu@1.2.3.4 --no-push    # 只更新 VPS，不 push GitHub

前提：
  - 已配置 git remote origin（指向 GitHub）
  - VPS 上已运行 deploy.py 完成过首次部署
"""

import argparse
import subprocess
import sys
import threading
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()

SERVICE_NAME  = "p2pchat"
SERVER_FILES  = ["server.py", "protocol.py", "crypto.py"]


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def _parse_target(target: str) -> tuple[str, str, list[str], list[str]]:
    """解析 'user@host' 或 'user@host:sshport' → (ssh_target, host, ssh_p, scp_P)"""
    ssh_p, scp_P = [], []
    if ":" in target.split("@")[-1]:
        target, port = target.rsplit(":", 1)
        ssh_p  = ["-p", port]
        scp_P  = ["-P", port]
    host = target.split("@")[-1]
    return target, host, ssh_p, scp_P


def _run(cmd: list[str]) -> tuple[int, str]:
    """运行本地命令，返回 (returncode, combined_output)。"""
    r = subprocess.run(cmd, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    return r.returncode, out


def _ssh(target: str, port_args: list[str], cmd: str) -> tuple[int, str]:
    full = ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes",
            *port_args, target, cmd]
    r = subprocess.run(full, capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    return r.returncode, out


def _scp(files: list[str], target: str, remote_dir: str,
         port_args: list[str]) -> tuple[int, str]:
    cmd = ["scp", "-o", "ConnectTimeout=10", *port_args,
           *files, f"{target}:{remote_dir}/"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


# ── Git 部分 ──────────────────────────────────────────────────────────────────

def _git_has_changes() -> bool:
    code, _ = _run(["git", "status", "--porcelain"])
    _, out = _run(["git", "status", "--porcelain"])
    return bool(out.strip())


def _git_changed_files() -> list[str]:
    _, out = _run(["git", "status", "--porcelain"])
    return [line[3:].strip() for line in out.splitlines() if line.strip()]


def _auto_commit_message(files: list[str]) -> str:
    """根据改动文件自动生成 commit message。"""
    if not files:
        return "chore: update"
    # 分类
    server = [f for f in files if f in ("server.py", "protocol.py", "crypto.py")]
    gui    = [f for f in files if f.startswith("gui/")]
    tests  = [f for f in files if f.startswith("tests/")]
    other  = [f for f in files if f not in server and f not in gui and f not in tests]

    parts = []
    if server:
        parts.append("server")
    if gui:
        parts.append("gui")
    if tests:
        parts.append("tests")
    if other:
        parts.append(", ".join(other[:2]))
    return "update: " + ", ".join(parts) if parts else "chore: update"


def task_git_push(message: str, result: dict) -> None:
    """后台线程：git add → commit → push。"""
    result["status"] = "running"

    # git add -A
    code, out = _run(["git", "add", "-A"])
    if code != 0:
        result.update(status="fail", detail=f"git add 失败: {out}")
        return

    # git commit
    code, out = _run(["git", "commit", "-m", message])
    if code != 0:
        # 如果 nothing to commit，视为成功
        if "nothing to commit" in out:
            result.update(status="ok", detail="没有新改动，跳过 commit")
        else:
            result.update(status="fail", detail=f"git commit 失败: {out}")
        return

    # git push
    code, out = _run(["git", "push", "origin", "HEAD"])
    if code != 0:
        result.update(status="fail", detail=f"git push 失败: {out}")
        return

    result.update(status="ok", detail="已 push 到 GitHub")


def task_vps_update(target: str, port_args: list[str], scp_args: list[str],
                    remote_dir: str, result: dict) -> None:
    """后台线程：scp 上传 → pip install（如有新依赖）→ 重启服务。"""
    result["status"] = "running"

    # 检查哪些文件存在（跳过不存在的，避免 scp 报错）
    existing = [f for f in SERVER_FILES if Path(f).exists()]
    if not existing:
        result.update(status="fail", detail="找不到服务端文件")
        return

    # scp 上传
    code, out = _scp(existing, target, remote_dir, scp_args)
    if code != 0:
        result.update(status="fail", detail=f"上传失败: {out}")
        return

    # requirements 有改动时重新安装依赖
    req = "requirements-server.txt"
    if Path(req).exists():
        scp_req_code, _ = _scp([req], target, remote_dir, scp_args)
        if scp_req_code == 0:
            _ssh(target, port_args,
                 f"cd {remote_dir} && pip3 install -q -r {req}")

    # 重启服务
    code, out = _ssh(target, port_args,
                     f"sudo systemctl restart {SERVICE_NAME}")
    if code != 0:
        result.update(status="fail", detail=f"重启失败: {out}")
        return

    # 确认状态
    _, active = _ssh(target, port_args,
                     f"sudo systemctl is-active {SERVICE_NAME}")
    if active.strip() == "active":
        result.update(status="ok", detail=f"服务已更新并运行中")
    else:
        result.update(status="warn",
                      detail=f"已重启但状态异常（{active.strip()}），请检查日志")


# ── 主流程 ────────────────────────────────────────────────────────────────────

def publish(target: str | None, message: str, do_push: bool, do_vps: bool,
            remote_dir: str, port: int) -> None:
    # ── 解析 VPS 地址 ──────────────────────────────────────────
    if do_vps:
        assert target, "需要提供 VPS 地址"
        ssh_target, host, ssh_p, scp_P = _parse_target(target)

    # ── 自动生成 commit message ────────────────────────────────
    if not message:
        changed = _git_changed_files()
        if changed:
            message = _auto_commit_message(changed)
            console.print(f"[dim]自动 commit message：{message}[/dim]")
        else:
            message = "chore: update"

    # ── 摘要 ──────────────────────────────────────────────────
    lines = [f"commit：[bold]{message}[/bold]"]
    if do_push:
        _, remote = _run(["git", "remote", "get-url", "origin"])
        lines.append(f"push →  [bold]{remote}[/bold]")
    if do_vps:
        lines.append(f"VPS  →  [bold]{target}[/bold]  (port {port})")
    console.print(Panel("\n".join(lines), title="发布确认", border_style="cyan"))

    # ── 并行执行 ──────────────────────────────────────────────
    git_result = {"status": "skip", "detail": ""}
    vps_result = {"status": "skip", "detail": ""}

    threads = []
    if do_push:
        t = threading.Thread(
            target=task_git_push,
            args=(message, git_result),
            daemon=True,
        )
        threads.append(("GitHub", t, git_result))
        t.start()

    if do_vps:
        t = threading.Thread(
            target=task_vps_update,
            args=(ssh_target, ssh_p, scp_P, remote_dir, vps_result),
            daemon=True,
        )
        threads.append(("VPS", t, vps_result))
        t.start()

    # ── 等待并实时显示进度 ─────────────────────────────────────
    for label, thread, res in threads:
        thread.join()

    # ── 结果汇总 ──────────────────────────────────────────────
    console.print()
    all_ok = True
    for label, _, res in threads:
        status = res["status"]
        detail = res.get("detail", "")
        if status == "ok":
            console.print(f"[green]  ✓ {label}[/green]  {detail}")
        elif status == "warn":
            console.print(f"[yellow]  ⚠ {label}[/yellow]  {detail}")
        elif status == "skip":
            console.print(f"[dim]  - {label}  跳过[/dim]")
        else:
            console.print(f"[red]  ✗ {label}[/red]  {detail}")
            all_ok = False

    console.print()
    if all_ok:
        if do_vps:
            console.print(Panel(
                f"[bold green]发布成功！[/bold green]\n\n"
                f"服务地址：[bold cyan]ws://{host}:{port}[/bold cyan]",
                border_style="green",
            ))
    else:
        console.print("[red]部分步骤失败，请查看上方错误信息。[/red]")
        sys.exit(1)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="P2P Chat 一键发布：commit + push GitHub + 更新 VPS",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python publish.py ubuntu@1.2.3.4\n"
            "  python publish.py ubuntu@1.2.3.4 -m 'fix: bug'\n"
            "  python publish.py ubuntu@1.2.3.4 --no-push\n"
            "  python publish.py --no-vps          # 只 push，不更新 VPS\n"
        ),
    )
    parser.add_argument("target", metavar="user@host[:sshport]", nargs="?",
                        help="VPS SSH 地址（省略时须加 --no-vps）")
    parser.add_argument("-m", "--message", default="",
                        help="commit message（留空则自动生成）")
    parser.add_argument("--port", default=8765, type=int,
                        help="聊天服务端口（默认 8765）")
    parser.add_argument("--dir", default="/opt/p2pchat", dest="remote_dir",
                        help="VPS 部署目录（默认 /opt/p2pchat）")
    parser.add_argument("--no-push", action="store_true",
                        help="跳过 git push，只更新 VPS")
    parser.add_argument("--no-vps", action="store_true",
                        help="跳过 VPS 更新，只 commit + push")

    args = parser.parse_args()

    do_vps  = not args.no_vps
    do_push = not args.no_push

    if do_vps and not args.target:
        parser.error("需要提供 user@host 地址，或使用 --no-vps 跳过 VPS 更新")

    if not do_push and not do_vps:
        parser.error("--no-push 和 --no-vps 不能同时使用")

    try:
        publish(
            target     = args.target,
            message    = args.message,
            do_push    = do_push,
            do_vps     = do_vps,
            remote_dir = args.remote_dir,
            port       = args.port,
        )
    except KeyboardInterrupt:
        console.print("\n[dim]已取消[/dim]")


if __name__ == "__main__":
    main()
