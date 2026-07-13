"""
P2P Chat — 一键部署服务端到 VPS

用法：
  python deploy.py ubuntu@1.2.3.4
  python deploy.py root@1.2.3.4 --port 9000
  python deploy.py ubuntu@1.2.3.4:2222
  python deploy.py ubuntu@1.2.3.4 --logs        # 查看运行日志
  python deploy.py ubuntu@1.2.3.4 --restart      # 重启服务
  python deploy.py ubuntu@1.2.3.4 --stop         # 停止服务

前提：
  - 本机已安装 ssh / scp（Windows 10/11 内置，或 Git for Windows 自带）
  - 已配置 SSH 密钥免密登录，或准备好输入 VPS 密码
  - VPS 已安装 Python 3 + pip3（Ubuntu/Debian 可自动安装）
"""

import os
import argparse
import base64
import subprocess
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

os.environ['PYTHONIOENCODING'] = 'utf-8'
os.environ['PYTHONUTF8'] = '1'

if sys.platform == "win32" and sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import io as _io
console = Console(file=_io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace') if hasattr(sys.stdout, 'buffer') else sys.stdout)

SERVICE_NAME = "p2pchat"
SERVER_FILES = [
    "server.py",
    "protocol.py",
    "crypto.py",
    "e2e_crypto.py",
    "file_transfer.py",
    "identity.py",
    "secure_session.py",
    "voice_crypto.py",
    "requirements-server.txt",
]


# ── SSH / SCP helpers ─────────────────────────────────────────────────────────

def _parse_target(target: str) -> tuple[str, str, list[str], list[str]]:
    """
    解析 'user@host' 或 'user@host:sshport'
    返回 (ssh_target, host, ssh_p_args, scp_P_args)
    """
    ssh_p, scp_P = [], []
    if ":" in target.split("@")[-1]:
        target, port = target.rsplit(":", 1)
        ssh_p = ["-p", port]
        scp_P = ["-P", port]
    host = target.split("@")[-1]
    return target, host, ssh_p, scp_P


def _ssh(target: str, port_args: list[str], cmd: str,
         desc: str, check: bool = True) -> subprocess.CompletedProcess:
    full_cmd = ["ssh", "-o", "ConnectTimeout=10", *port_args, target, cmd]
    console.print(f"  [dim]▶ {cmd[:90]}[/dim]")
    result = subprocess.run(full_cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if check and result.returncode != 0:
        console.print(f"[red]✗ {desc} 失败[/red]")
        err = (result.stderr or result.stdout).strip()
        if err:
            console.print(f"[dim red]{err}[/dim red]")
        sys.exit(1)
    return result


def _scp(files: list[str], target: str, remote_dir: str,
         port_args: list[str]) -> None:
    cmd = ["scp", "-o", "ConnectTimeout=10", *port_args,
           *files, f"{target}:{remote_dir}/"]
    console.print(f"  [dim]▶ scp {' '.join(files)} → {target}:{remote_dir}/[/dim]")
    result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if result.returncode != 0:
        console.print("[red]✗ 文件上传失败[/red]")
        err = (result.stderr or result.stdout).strip()
        if err:
            console.print(f"[dim red]{err}[/dim red]")
        sys.exit(1)


def _ssh_write_file(target: str, port_args: list[str],
                    content: str, remote_path: str) -> None:
    """通过 base64 + tee 安全写入远程文件（避免 shell 引号转义问题）。"""
    encoded = base64.b64encode(content.encode()).decode()
    _ssh(target, port_args,
         f"echo {encoded} | base64 -d | sudo tee {remote_path} > /dev/null",
         f"写入 {remote_path}")


# ── 部署步骤 ──────────────────────────────────────────────────────────────────

def _step_upload(target: str, port_args: list[str], remote_dir: str) -> None:
    console.print("\n[bold cyan]1/5  上传服务端文件[/bold cyan]")

    missing = [f for f in SERVER_FILES if not Path(f).exists()]
    if missing:
        console.print(f"[red]找不到本地文件：{', '.join(missing)}[/red]")
        console.print("[dim]请在项目根目录运行 deploy.py[/dim]")
        sys.exit(1)

    _ssh(target, port_args, f"mkdir -p {remote_dir}", "创建远程目录")
    _scp(SERVER_FILES, target, remote_dir, port_args)
    console.print("[green]  ✓ 文件已上传[/green]")


def _step_install(target: str, port_args: list[str], remote_dir: str) -> None:
    console.print("\n[bold cyan]2/5  安装 Python 依赖[/bold cyan]")

    # 确保 ensurepip 可用（python3-venv 包提供）
    venv_check = _ssh(target, port_args,
                      "python3 -c 'import ensurepip'", "检测 ensurepip", check=False)
    if venv_check.returncode != 0:
        console.print("  [yellow]python3-venv 未找到，尝试 apt 安装...[/yellow]")
        _ssh(target, port_args,
             "sudo apt-get update -qq && "
             "sudo apt-get install -y python3 python3-venv",
             "安装 python3-venv")

    # 创建 venv（若已存在则幂等跳过），然后安装依赖
    venv = f"{remote_dir}/venv"
    _ssh(target, port_args,
         f"python3 -m venv {venv} && "
         f"{venv}/bin/pip install -q -r {remote_dir}/requirements-server.txt",
         "创建 venv 并安装依赖")
    console.print("[green]  ✓ 依赖已安装[/green]")


def _step_service(target: str, port_args: list[str],
                  remote_dir: str, port: int) -> None:
    console.print("\n[bold cyan]3/5  配置 systemd 服务[/bold cyan]")

    python_bin = f"{remote_dir}/venv/bin/python3"

    service = f"""\
[Unit]
Description=P2P Chat Relay Server
After=network.target

[Service]
WorkingDirectory={remote_dir}
ExecStart={python_bin} server.py --host 0.0.0.0 --port {port}
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""
    _ssh_write_file(target, port_args, service,
                    f"/etc/systemd/system/{SERVICE_NAME}.service")

    _ssh(target, port_args,
         f"sudo systemctl daemon-reload && "
         f"sudo systemctl enable {SERVICE_NAME} && "
         f"sudo systemctl restart {SERVICE_NAME}",
         "启动服务")
    console.print("[green]  ✓ 服务已启动[/green]")


def _step_firewall(target: str, port_args: list[str], port: int) -> None:
    console.print(f"\n[bold cyan]4/5  开放端口 {port}/tcp[/bold cyan]")

    # Always allow SSH (22) first so we don't lock ourselves out
    r = _ssh(target, port_args,
             f"sudo ufw allow 22/tcp && sudo ufw allow {port}/tcp && sudo ufw --force enable",
             "UFW 开放端口", check=False)
    if r.returncode == 0:
        console.print(f"[green]  ✓ UFW 已放行 {port}/tcp[/green]")
    else:
        # 尝试 iptables 作为备选
        r2 = _ssh(target, port_args,
                  f"sudo iptables -C INPUT -p tcp --dport {port} -j ACCEPT 2>/dev/null || "
                  f"sudo iptables -I INPUT -p tcp --dport {port} -j ACCEPT",
                  "iptables 开放端口", check=False)
        if r2.returncode == 0:
            console.print(f"[green]  ✓ iptables 已放行 {port}/tcp[/green]")
        else:
            console.print(
                f"[yellow]  ⚠ 自动配置防火墙失败，请在 VPS 控制台手动放行 {port}/tcp[/yellow]"
            )


def _step_verify(target: str, port_args: list[str], host: str, port: int) -> None:
    console.print("\n[bold cyan]5/5  验证服务状态[/bold cyan]")

    status = _ssh(target, port_args,
                  f"sudo systemctl is-active {SERVICE_NAME}",
                  "获取服务状态", check=False)
    active = status.stdout.strip() == "active"

    if active:
        console.print(Panel(
            f"[bold green]部署成功！[/bold green]\n\n"
            f"WebSocket 地址：\n\n"
            f"  [bold cyan]ws://{host}:{port}[/bold cyan]\n\n"
            f"客户端连接：\n"
            f"  [bold]python gui_client.py --server ws://{host}:{port}[/bold]\n"
            f"  [bold]python client.py --server ws://{host}:{port}[/bold]\n\n"
            f"[dim]查看日志：  python deploy.py {target} --logs[/dim]\n"
            f"[dim]重新部署：  python deploy.py {target}[/dim]\n"
            f"[dim]停止服务：  python deploy.py {target} --stop[/dim]",
            title="P2P Chat — 部署完成 ✓",
            border_style="green",
        ))
    else:
        state = status.stdout.strip() or "unknown"
        console.print(Panel(
            f"服务状态：[red]{state}[/red]\n\n"
            f"查看错误日志：\n\n"
            f"  [bold]python deploy.py {target} --logs[/bold]",
            title="[yellow]服务未能正常启动[/yellow]",
            border_style="yellow",
        ))


# ── 管理子命令 ────────────────────────────────────────────────────────────────

def _cmd_logs(target: str, port_args: list[str]) -> None:
    """实时输出服务日志（直接透传，不 capture）。"""
    console.print(f"[dim]journalctl -u {SERVICE_NAME} -n 50 -f  (Ctrl+C 退出)[/dim]\n")
    subprocess.run(
        ["ssh", *port_args, target,
         f"sudo journalctl -u {SERVICE_NAME} -n 50 -f"]
    )


def _cmd_restart(target: str, port_args: list[str]) -> None:
    _ssh(target, port_args, f"sudo systemctl restart {SERVICE_NAME}", "重启服务")
    console.print("[green]✓ 服务已重启[/green]")


def _cmd_stop(target: str, port_args: list[str]) -> None:
    _ssh(target, port_args, f"sudo systemctl stop {SERVICE_NAME}", "停止服务")
    console.print("[yellow]✓ 服务已停止[/yellow]")


# ── 入口 ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="P2P Chat 一键 VPS 部署",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python deploy.py ubuntu@1.2.3.4\n"
            "  python deploy.py root@1.2.3.4 --port 9000\n"
            "  python deploy.py ubuntu@1.2.3.4:2222\n"
            "  python deploy.py ubuntu@1.2.3.4 --logs"
        ),
    )
    parser.add_argument("target", metavar="user@host[:sshport]",
                        help="VPS SSH 地址")
    parser.add_argument("--port", default=8765, type=int,
                        help="聊天服务端口（默认 8765）")
    parser.add_argument("--dir", default="/opt/p2pchat", dest="remote_dir",
                        help="VPS 部署目录（默认 /opt/p2pchat）")

    group = parser.add_mutually_exclusive_group()
    group.add_argument("--logs",    action="store_true", help="查看服务日志")
    group.add_argument("--restart", action="store_true", help="重启服务")
    group.add_argument("--stop",    action="store_true", help="停止服务")

    args = parser.parse_args()
    ssh_target, host, ssh_p, scp_P = _parse_target(args.target)

    try:
        if args.logs:
            _cmd_logs(ssh_target, ssh_p)
        elif args.restart:
            _cmd_restart(ssh_target, ssh_p)
        elif args.stop:
            _cmd_stop(ssh_target, ssh_p)
        else:
            console.print(Panel(
                f"目标：[bold]{ssh_target}[/bold]\n"
                f"端口：[bold]{args.port}[/bold]\n"
                f"目录：[bold]{args.remote_dir}[/bold]",
                title="P2P Chat — 开始部署",
                border_style="cyan",
            ))
            _step_upload(ssh_target, scp_P, args.remote_dir)
            _step_install(ssh_target, ssh_p, args.remote_dir)
            _step_service(ssh_target, ssh_p, args.remote_dir, args.port)
            _step_firewall(ssh_target, ssh_p, args.port)
            _step_verify(ssh_target, ssh_p, host, args.port)

    except KeyboardInterrupt:
        console.print("\n[dim]已取消[/dim]")


if __name__ == "__main__":
    main()
