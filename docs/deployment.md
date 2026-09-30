# BeamChat 部署维护手册

本手册记录 2026-09-30 配置的生产入口与维护流程。公网客户端连接 `wss://medguide.lemon-travelhokkaido.com/beamchat/`，本机开发可连接 `ws://127.0.0.1:8765`。

## 当前部署

| 项目 | 配置 |
|---|---|
| 当前 VPS | `106.55.8.122`，本地 SSH 别名 `pikastairs` |
| 公网 WSS 入口 | `wss://medguide.lemon-travelhokkaido.com/beamchat/` |
| WS 后端 | `127.0.0.1:8765`，由 Nginx 在本机访问 |
| 服务与工作目录 | `p2pchat.service`、`/opt/p2pchat` |
| 服务启动命令 | `/opt/p2pchat/venv/bin/python3 server.py --host 0.0.0.0 --port 8765` |
| Nginx 站点实际文件 | `/etc/nginx/sites-available/MedGuide` |
| Nginx 启用入口 | `/etc/nginx/sites-enabled/MedGuide` |
| 原 MedGuide 路由 | 根路径继续转发到原 `9001` 服务 |

`medguide.lemon-travelhokkaido.com` 指向当前 VPS。`pikastairs.cc` 指向旧地址 `202.78.167.177`，与 SSH 别名 `pikastairs` 不同，维护时应使用表中的域名、IP 或已确认的 SSH 别名。

`server.py`、`deploy.py` 和 `start.py` 提供普通 WS 后端；将 URL 的 `ws://` 改写为 `wss://` 不会为该端口启用 TLS。Nginx 使用有效域名证书提供公网 WSS，客户端通过正常证书校验后连接。

从 `v1.2.2` 起，握手要求客户端声明 `persistent_room_membership`，避免旧单群客户端错误处理后台群消息和同步游标。协议版本仍为 `5`，`v1.2.1` 客户端已支持该能力；不支持的旧客户端会收到明确升级提示，应安装最新发布的 EXE。

## Nginx 反代

在现有 MedGuide HTTPS `server` 块中增加独立的 BeamChat 路由，保留原根路径代理：

```nginx
location = /beamchat/ {
    proxy_pass http://127.0.0.1:8765;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
    proxy_buffering off;
}
```

HTTPS `server` 块使用以下证书路径：

```nginx
ssl_certificate /etc/letsencrypt/live/medguide.lemon-travelhokkaido.com/fullchain.pem;
ssl_certificate_key /etc/letsencrypt/live/medguide.lemon-travelhokkaido.com/privkey.pem;
```

修改后先验证配置，再平滑重载：

```bash
sudo nginx -t
sudo systemctl reload nginx
```

客户端地址须包含完整的 `/beamchat/` 路径及末尾斜杠。公网使用 `443`；证书 HTTP 验证需要 `80`。`8765` 仍是 WS 后端端口。

## 证书与自动续期

2026-09-30 使用以下命令申请并注册 Let's Encrypt 证书：

```bash
sudo certbot certonly --nginx --non-interactive --agree-tos \
  --register-unsafely-without-email \
  --cert-name medguide.lemon-travelhokkaido.com \
  -d medguide.lemon-travelhokkaido.com
```

本次签发证书到期日为 **2026-12-29**。续期后以证书的实际日期为准：

```bash
sudo certbot certificates
sudo openssl x509 \
  -in /etc/letsencrypt/live/medguide.lemon-travelhokkaido.com/fullchain.pem \
  -noout -subject -issuer -dates
```

旧证书来自 `TrustAsia DV TLS RSA CA2025`，存放于 `/etc/nginx/ssl/medguide/medguide.lemon-travelhokkaido.com_bundle.crt`，在 2026-09-11 到期。当时 `certbot certificates` 返回 `No certificates found`，`/etc/letsencrypt/renewal` 为空；已有 `certbot.timer` 没有可续期的证书对象，因此不会维护这份静态证书。

新证书已由 Certbot 管理。`certbot.timer` 每日安排两次检查，并添加随机延迟；检查时只有符合续期条件才会重新签发。确保定时器启用并检查下次运行时间：

```bash
sudo systemctl enable --now certbot.timer
systemctl is-enabled certbot.timer
systemctl is-active certbot.timer
systemctl list-timers certbot.timer --all
```

续期成功后执行 `/etc/letsencrypt/renewal-hooks/deploy/beamchat-nginx-reload`，验证 Nginx 配置并重载，让新连接使用新证书。钩子的核心命令为：

```sh
nginx -t && systemctl reload nginx
```

使用测试环境检查续期流程与部署钩子，并查看日常任务日志：

```bash
sudo certbot renew --dry-run \
  --cert-name medguide.lemon-travelhokkaido.com \
  --run-deploy-hooks --non-interactive --no-random-sleep-on-renew
sudo journalctl -u certbot.service --since "30 days ago" --no-pager
```

2026-10-01 的模拟续期与部署钩子验证已通过，新证书继续由生产环境使用。

## 运行验证

以下命令分别检查后端、TLS 域名校验和原站点：

```bash
ssh pikastairs 'systemctl is-active p2pchat'
ssh pikastairs 'sudo journalctl -u p2pchat -n 50 --no-pager'
openssl s_client -connect medguide.lemon-travelhokkaido.com:443 \
  -servername medguide.lemon-travelhokkaido.com \
  -verify_hostname medguide.lemon-travelhokkaido.com \
  -verify_return_error </dev/null
curl --fail --show-error https://medguide.lemon-travelhokkaido.com/api/ping
python client.py --server wss://medguide.lemon-travelhokkaido.com/beamchat/
```

检查 `p2pchat` 为 `active`、TLS 验证成功、原站点 `/api/ping` 正常，以及客户端通过完整 WSS 地址连接。单独访问 HTTPS 不能代替 WebSocket 握手验证。

2026-09-30 已验证公网 WSS 普通 `HELLO` 握手返回服务版本 `1.2.1`、协议版本 `5`；原站点 `/api/ping` 返回 HTTP `200` 和 `{"ok":true}`。

## 常规发布顺序

1. 确认发布提交、版本号及服务端文件清单，完成与改动相关的测试。
2. 备份现有服务源码、Nginx 配置和持久化数据。上传源码后核对清单与 SHA-256，保留既有服务目录和数据配置。
3. 更新服务端并重启 `p2pchat`，核对服务状态、实际源码校验值与日志。客户端和服务端应使用配套协议版本。
4. 需要修改反代时，先执行 `nginx -t` 再重载。检查证书、完整公网 WSS 握手和原站点健康状态。
5. 使用实际 WSS 地址构建 Windows 客户端，核对 EXE 内置配置；每次生成 `dist/BeamChat.exe` 后都复制到 `F:\beam-build`（WSL 为 `/mnt/f/beam-build`）。
6. 验证打包客户端连接，生成 EXE 的 SHA-256 后发布同版本 tag、Release 和构建产物。

构建命令：

```bash
python build.py --server-url wss://medguide.lemon-travelhokkaido.com/beamchat/
```

维护已有服务时，`deploy.py` 会重写 systemd 服务并配置后端防火墙；应先确认这些操作符合当前配置。它的 WS 输出地址不能作为公网 WSS 验证结果。

## 当前备份与回滚

| 备份 | 路径 |
|---|---|
| 服务端源码 | `/opt/p2pchat/.backups/20260930-v1.2.1-8139eab` |
| Nginx 配置 | `/opt/p2pchat/.backups/tls-20260930` |

源码备份目录中的 `deployment.json` 记录本次部署文件清单。回滚服务端时，停止 `p2pchat`，按清单恢复备份中的同名源码和依赖文件，再启动服务并重复运行验证。仅恢复部署涉及的文件，保留房间数据和消息数据库；先核对本次新增文件是否存在于旧版本。

TLS 配置备份包含 `MedGuide.conf` 及 Nginx 配置快照，不含私钥目录。单独恢复原站点配置可执行：

```bash
sudo cp /opt/p2pchat/.backups/tls-20260930/MedGuide.conf \
  /etc/nginx/sites-available/MedGuide
sudo nginx -t
sudo systemctl reload nginx
```

该旧配置引用已过期的 TrustAsia 证书，恢复后会失去当前有效的 WSS 入口。只需回滚应用代码时，保留新证书、续期定时器和 Nginx 的 `/beamchat/` 路由；恢复旧站点配置用于配置故障回退，并需重新核验 TLS 与原站点状态。
