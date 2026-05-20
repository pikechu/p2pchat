# BeamChat

NAT 穿透 P2P 聊天应用。客户端只需出站连接到中继服务器，无需公网 IP。服务端不解密消息，端到端加密全部在客户端完成。

---

## 功能特性

- **聊天室**：创建/加入多个聊天室，支持密码保护（端到端加密）
- **持久聊天室**：房间永久保留，服务器重启后仍可访问
- **房间管理**：创建者可修改聊天室名称、图标，或永久删除房间
- **直接消息（DM）**：在线用户之间点对点私信
- **文件传输**：支持发送图片、视频、任意文件（P2P 中继，500 MB 限制）
- **表情符号**：内置 Emoji 选择面板
- **消息回复**：引用回复特定消息
- **已读回执**：发送/送达/已读状态标识
- **深色/浅色主题**：启动时选择，或通过设置切换
- **跨平台**：Windows 提供打包好的 `.exe`，Linux/macOS 运行 Python 源码

---

## 快速开始

### 使用打包版（Windows）

1. 从 [Releases](../../releases) 下载 `BeamChat.exe`
2. 双击运行，填入服务器地址即可连接

### 从源码运行

**环境要求：** Python 3.10+

```bash
git clone https://github.com/pikechu/p2pchat.git
cd p2pchat
pip install -r requirements.txt
```

**启动服务端（本机测试）：**

```bash
python server.py
```

**启动 GUI 客户端：**

```bash
python gui_client.py
```

---

## 客户端使用说明

### 首次连接

启动后在设置中填入：
- **服务器地址**：`ws://服务器IP:8765`
- **用户名**：任意名称（同一服务器上唯一）
- **主题**：浅色 / 深色

### 聊天室

| 操作 | 方法 |
|------|------|
| 创建聊天室 | 左侧面板点击 **+** 按钮 |
| 搜索/加入聊天室 | 左侧面板点击 **🔍** 按钮，按名称搜索 |
| 加入密码房间 | 搜索找到后点击「加入」，输入密码 |
| 查看房间信息 | 聊天界面右上角点击 **⋯** |
| 修改房间名称 | 房间信息面板 → ✏️（仅创建者） |
| 修改房间图标 | 房间信息面板 → 更换图标（仅创建者） |
| 删除聊天室 | 左侧房间列表右键 → 删除聊天室（仅创建者） |
| 离开聊天室 | 聊天界面左下角离开按钮 |

### 消息功能

- **发送消息**：输入框输入后按 Enter 或点击 ↑
- **发送表情**：点击 😊 打开表情面板，选择后自动关闭
- **回复消息**：右键消息气泡 → 回复
- **发送文件/图片/视频**：点击 📎 / 🖼 / 🎬 选择文件
- **粘贴图片**：在输入框按 Ctrl+V 直接发送剪贴板图片

### 直接消息（DM）

在左侧「Peers」栏点击在线用户名即可发起私信。

---

## 服务端部署

### 方式一：一键部署到 VPS（推荐）

**前提条件：**
- 本机已安装 `ssh` / `scp`（Windows 10/11 内置，或 Git for Windows 自带）
- 已配置 SSH 密钥免密登录，或准备好输入 VPS 密码
- VPS 为 Ubuntu / Debian 系统

```bash
# 部署并启动服务
python deploy.py ubuntu@your-vps-ip

# 自定义端口
python deploy.py ubuntu@your-vps-ip --port 9000

# 自定义 SSH 端口
python deploy.py ubuntu@your-vps-ip:2222

# 查看运行日志
python deploy.py ubuntu@your-vps-ip --logs

# 重启服务
python deploy.py ubuntu@your-vps-ip --restart

# 停止服务
python deploy.py ubuntu@your-vps-ip --stop
```

部署脚本会自动完成：上传服务文件 → 安装 Python 依赖 → 创建 systemd 服务 → 开放防火墙端口 → 输出连接地址。

### 方式二：手动部署

```bash
# 在 VPS 上
git clone https://github.com/pikechu/p2pchat.git
cd p2pchat
pip3 install -r requirements-server.txt

# 直接运行（前台）
python3 server.py --host 0.0.0.0 --port 8765

# 或使用 systemd 后台运行
sudo tee /etc/systemd/system/p2pchat.service > /dev/null <<EOF
[Unit]
Description=BeamChat Relay Server
After=network.target

[Service]
ExecStart=/usr/bin/python3 /opt/p2pchat/server.py --host 0.0.0.0 --port 8765
WorkingDirectory=/opt/p2pchat
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable --now p2pchat
```

### 方式三：本机 + SSH 隧道（无公网 IP）

```bash
# 通过 SSH 反向隧道将本地服务暴露到公网
python start.py --ssh user@your-vps-ip
python start.py --ssh user@your-vps-ip:2222 --port 9000
```

客户端连接地址为 `ws://your-vps-ip:8765`。

### 防火墙配置

```bash
# Ubuntu/Debian (ufw)
sudo ufw allow 8765/tcp

# CentOS/RHEL (firewalld)
sudo firewall-cmd --permanent --add-port=8765/tcp
sudo firewall-cmd --reload
```

---

## 服务端命令行参数

```
python server.py [--host HOST] [--port PORT]

  --host  监听地址，默认 0.0.0.0（所有网卡）
  --port  监听端口，默认 8765
```

房间数据持久化保存在 `~/.p2pchat_rooms.json`，服务重启后自动加载。

---

## 客户端命令行参数

```
python gui_client.py [--server URL] [--name NAME] [--theme THEME]

  --server  服务器 WebSocket 地址，默认 ws://localhost:8765
  --name    预填用户名
  --theme   界面主题：light（默认）或 dark
```

---

## 从源码构建 Windows exe

```bash
pip install pyinstaller pillow
python build.py
# 输出：dist/BeamChat.exe
```

---

## 终端客户端（可选）

轻量级命令行版本，无需 PyQt6：

```bash
pip install websockets cryptography
python client.py --server ws://HOST:8765

# 可用命令
/name <用户名>       设置用户名
/create <房间名>     创建聊天室
/create <名> <密码>  创建加密聊天室
/join <房间ID>       加入聊天室
/join <ID> <密码>    加入加密聊天室
/leave               离开当前聊天室
/rooms               列出所有聊天室
/dm <用户名> <消息>  发送私信
/quit                退出
```

---

## 技术架构

```
客户端 A ──┐
           ├── WebSocket ──► 中继服务器（server.py）── WebSocket ──┬── 客户端 B
客户端 C ──┘                                                      └── 客户端 D
```

- **中继服务器**：仅做消息路由，不存储消息，不解密内容
- **E2E 加密**：使用 Fernet（AES-128-CBC + HMAC-SHA256），密钥由 PBKDF2-HMAC-SHA256（20 万次迭代）从房间密码派生
- **无密码房间**：仍会派生隔离密钥（基于 room_id），防止跨房间混淆
- **WebSocket**：基于 `websockets` 库，协议帧为 JSON `{type, payload, ts, mid}`

---

## 依赖

| 包 | 用途 |
|----|------|
| `websockets` | WebSocket 通信 |
| `cryptography` | Fernet 加密 / PBKDF2 |
| `PyQt6` | GUI 界面（仅客户端） |
| `rich` | 部署脚本终端输出（仅 deploy.py） |
