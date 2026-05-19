"""
一键启动：Chat Server + SSH 反向隧道

用法：
  python start.py --ssh user@1.2.3.4          # 通过自己的 VPS 暴露到公网
  python start.py --ssh user@1.2.3.4 --port 9000   # 自定义端口

原理：
  本地 server 监听 127.0.0.1:PORT
  SSH 在 VPS 上打开 0.0.0.0:PORT，把流量转回本地
  外网用户连 ws://VPS_IP:PORT 即可

前提：
  - VPS 已安装 SSH 服务，且你能 ssh 进去
  - VPS 防火墙放行 PORT（默认 8765）
  - VPS 的 sshd_config 中 GatewayPorts 设为 yes（见下方说明）
"""

import argparse
import asyncio
import subprocess
import sys
import threading

from rich.console import Console
from rich.panel import Panel

console = Console()


def _monitor_ssh(proc: subprocess.Popen, ready: threading.Event):
    """后台线程：等待 SSH 建立完成或报错。"""
    output_lines = []
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            output_lines.append(line)
            console.print(f"[dim]  ssh: {line}[/dim]")
            # SSH 反向端口转发成功后不会有特殊输出，只要进程没退出就算成功
    except Exception:
        pass
    # 进程结束说明连接断开
    if not ready.is_set():
        ready.set()   # 解除主线程等待，让它检测到进程已退出


def start_ssh_tunnel(local_port: int, ssh_target: str) -> subprocess.Popen | None:
    """
    启动 SSH 反向隧道。
    ssh_target 格式：user@host 或 user@host:ssh_port
    """
    # 解析可选的 SSH 端口（user@host:2222）
    ssh_args = []
    if ":" in ssh_target.split("@")[-1]:
        host_part, ssh_port = ssh_target.rsplit(":", 1)
        ssh_args += ["-p", ssh_port]
        ssh_target = host_part

    vps_host = ssh_target.split("@")[-1]

    cmd = [
        "ssh",
        *ssh_args,
        "-N",                                       # 不执行远程命令，只做端口转发
        "-o", "StrictHostKeyChecking=no",
        "-o", "ServerAliveInterval=30",
        "-o", "ExitOnForwardFailure=yes",
        "-R", f"0.0.0.0:{local_port}:localhost:{local_port}",
        ssh_target,
    ]

    console.print(f"[dim]执行：{' '.join(cmd)}[/dim]\n")

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        console.print("[red]找不到 ssh 命令。[/red]")
        return None

    ready = threading.Event()
    t = threading.Thread(target=_monitor_ssh, args=(proc, ready), daemon=True)
    t.start()

    # 等 3 秒——如果 SSH 这么快就退出，说明连接失败
    ready.wait(timeout=3)
    if proc.poll() is not None:
        console.print(Panel(
            "SSH 连接失败，常见原因：\n\n"
            "1. VPS 地址 / 用户名错误\n"
            "2. 防火墙未放行 SSH 端口\n"
            "3. VPS 的 sshd 未开启 GatewayPorts\n\n"
            "修复 GatewayPorts（在 VPS 上执行）：\n"
            "  [bold]echo 'GatewayPorts yes' >> /etc/ssh/sshd_config[/bold]\n"
            "  [bold]systemctl restart sshd[/bold]",
            title="[red]SSH 连接失败[/red]",
            border_style="red",
        ))
        return None

    return proc, vps_host


async def run_server(port: int):
    from server import _main
    await _main("127.0.0.1", port)


async def main(port: int, ssh_target: str):
    console.print(f"[dim]正在建立 SSH 反向隧道 → {ssh_target} ...[/dim]\n")

    result = start_ssh_tunnel(port, ssh_target)
    if result is None:
        sys.exit(1)
    tunnel_proc, vps_host = result

    ws_url = f"ws://{vps_host}:{port}"
    console.print(Panel(
        f"[bold green]隧道已建立！[/bold green]\n\n"
        f"把以下命令发给其他用户（复制整行）：\n\n"
        f"  [bold cyan]python client.py --server {ws_url}[/bold cyan]\n\n"
        f"[dim]本机直连：ws://localhost:{port}[/dim]\n"
        f"[dim]Ctrl+C 退出，退出后外网连接断开[/dim]",
        title="P2P Chat — SSH 隧道模式",
        border_style="cyan",
    ))

    try:
        await run_server(port)
    except asyncio.CancelledError:
        pass
    finally:
        tunnel_proc.terminate()
        console.print("[dim]SSH 隧道已关闭[/dim]")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="P2P Chat 一键启动")
    parser.add_argument(
        "--ssh",
        required=True,
        metavar="user@host[:port]",
        help="VPS SSH 地址，例如：root@1.2.3.4 或 ubuntu@1.2.3.4:2222",
    )
    parser.add_argument("--port", default=8765, type=int, help="聊天端口（默认 8765）")
    args = parser.parse_args()

    try:
        asyncio.run(main(args.port, args.ssh))
    except KeyboardInterrupt:
        console.print("\n[dim]已退出[/dim]")
