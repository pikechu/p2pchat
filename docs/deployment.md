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

## Windows 本地打包：工具与缓存固定到 F 盘

从实际项目目录 `F:\claude projects\p2pchat` 执行：

```powershell
.\build-local.ps1
# 需要调试控制台时：
.\build-local.ps1 -DebugBuild
```

该入口使用独立的 F 盘 Python 3.11 工具链，环境变量只对本次构建生效。构建 `PATH` 仅包含该 Python、其 DLL 目录与 Windows 系统目录，防止桌面应用附加的 DLL 路径污染打包结果。

| 内容 | 路径 |
|---|---|
| Python 与 PyInstaller、项目依赖 | `F:\beam-build\toolchain\python311` |
| 工具链版本清单 | `F:\beam-build\toolchain\packages.json` |
| pip 缓存 | `F:\beam-build\cache\pip` |
| PyInstaller 缓存 | `F:\beam-build\cache\pyinstaller` |
| 临时目录 | `F:\beam-build\tmp` |
| 中间文件与配置 | `F:\claude projects\p2pchat\build`、`BeamChat.spec` |
| EXE 组装目录 | `F:\beam-build\local-build\dist` |
| 最终产物与额外副本 | 项目 `dist\BeamChat.exe`、`F:\beam-build\BeamChat.exe` |
| 完整构建日志 | `F:\beam-build\build-local.log` |

EXE 组装完成后，先以 `--help` 验证冻结程序可以启动；非零退出或超时会让构建失败，停止复制正式产物。复制失败也会明确报错。构建成功后核对组装目录与两份最终 EXE 的 SHA-256，并为最终产物生成相邻 `.sha256` 文件。构建失败会返回错误，不能将组装目录中的未完成 EXE 当作发布包。现有 GitHub Release 的版本归档保留在 `F:\beam-build\release-v版本号`。

切换执行环境后，如果显示 `C:\mnt\f\...`，这是 WSL 路径被错误转换得到的目录；本地执行应使用真实的 `F:\...` 项目路径。
2026-10-01 已修复 Windows 本地打包并完成正常版构建，工具链为 Python 3.11.3、PyInstaller 6.20.0。排查确认了两个问题：

- EXE 写头时存在短暂文件共享冲突。独立大文件副本的 Windows 原始错误为 `ERROR_SHARING_VIOLATION (32)`，等待后写入恢复。`build_windows.py` 保留文件头及已有调试时间戳、系统算法计算的完整 PE 校验和，仅写入需要修改的等长字段；最多等待 30 秒，超时明确失败且不被 PyInstaller 外层重试放大。未确认占用者，不以此推断具体防护软件。
- 构建继承了 Codex 的 Poppler DLL 搜索路径，错误打包其 `icuuc.dll`。该库提供带 `_78` 后缀的符号，而 Qt 6.10 需要 Windows 系统 ICU 的无后缀符号，因此冻结程序导入 QtWidgets 时失败。Windows 入口隔离 `PATH` 后，新包不再包含这些外部 ICU DLL，启动检查通过。兼容处理仅作用于本次构建进程，结束时恢复原环境与函数，不修改已安装的 PyInstaller。

瘦身前的构建验证结果：内嵌版本 `1.2.2` 与默认 WSS 地址正确，PE 校验和匹配，附加 PKG 与组装前文件逐字节一致，319 个归档条目可完整读取，`--help` 启动退出码为 `0`。组装目录、项目 `dist` 与 `F:\beam-build` 的 EXE 均为 86,696,276 字节，SHA-256 为 `0bf3db71dca99742fa95f84678a0f487c27189d0bde150ab9b56bf0dd29046cf`。这是本地重新构建的产物；已有 GitHub `v1.2.2` 发布归档仍保留在 `F:\beam-build\release-v1.2.2`。

### Windows 包瘦身（2026-10-01）

使用同一 F 盘工具链重建，EXE 从 **86,696,276 字节（82.68 MiB）缩小到 72,934,836 字节（69.56 MiB）**，减少 **15.87%**。该本地 `1.2.2` 瘦身试包的 SHA-256 为 `6482986fe1167db6ad90f80904cf3e04000c7dc220890acbd042c267f71b2133`；下方 `1.2.3` 发布验收记录为最终包信息。瘦身前的本地包保留在 `F:\beam-build\local-build\before-slimming`。

`build.py` 默认启用 `build_hooks` 内的 Qt 收集规则，仍使用 PyInstaller 的 Qt 依赖分析，收集后只裁剪本项目没有使用的资源：

- 软件 OpenGL 库 `opengl32sw.dll`。界面只使用普通 Qt Widgets、QPainter 和 QPixmap，没有 QOpenGL 或 Qt Quick；Qt Widgets 使用软件光栅绘制，见 [Qt 图形说明](https://doc.qt.io/qt-6/topics-graphics.html)。
- PDF 图片解码插件和未使用的 TUIO 网络输入插件，避免连带打包 Qt6Pdf、Qt6Network。PDF 仍作为文件发送，并由系统默认程序打开；普通 Windows 鼠标、键盘与触屏平台插件保留。
- 中英文以外的 Qt 翻译，以及 NumPy 测试和 Fortran 构建模块。Qt 核心、常用图片格式、语音所需 NumPy 和 PortAudio 均保留。

FFmpeg、PyAV、aiortc 及 NumPy 的 OpenBLAS 未直接删减：原生 DLL 存在直接链接依赖，删除所谓“未使用”的编码器 DLL 会使整套媒体库加载失败。聊天、语音、图片、视频文件与 WebRTC 文件传输的应用代码没有改动，91 个关键原生依赖与瘦身前包逐字节相同。新包的 203 个归档条目全部可读取，PE 校验和及 `--help` 启动检查通过。


瘦身后使用 `verify_package_runtime.py` 提取本次可信构建的依赖，在同一 F 盘 Python 的 `-I -S` 子进程中验证，禁止第三方包回退到工具链的 site-packages：

```powershell
& 'F:\beam-build\toolchain\python311\python.exe' -B .\verify_package_runtime.py 'F:\beam-build\BeamChat.exe'
```

2026-10-01 的隔离验证退出码为 `0`：QWidget/QPainter/QPixmap 光栅绘制，PNG/JPEG/GIF/WebP/TIFF 实际解析，NumPy 语音 PCM 转换、PyAV 音频帧及 Fernet 加密均通过。两端只使用 `127.0.0.1`、不配置 STUN/TURN 的 WebRTC DataChannel 完成消息传输。它验证包内依赖，不替代 EXE 引导器检查；引导器由构建时的 `--help` 检查覆盖。未启动实际麦克风或连接生产服务。

验证临时文件位于 `F:\beam-build\tmp`，成功后清理本次提取目录，JSON 结果保留在 `F:\beam-build\tmp\beam-package-runtime-report.json`。体积与保留依赖对照位于 `F:\beam-build\local-build\slimming-report.json`。构建相关的 32 项回归测试已通过。
### v1.2.3 发布包与在线更新验收（2026-10-01）

最终包内嵌 `version.__version__` 与 `protocol.CLIENT_VERSION` 均为 `1.2.3`，默认 WSS 地址保持不变，协议版本仍为 `5`。本次只发布客户端；服务端不依据客户端补丁版本拒绝连接，无需重启 VPS。

| 项目 | 验收结果 |
|---|---|
| EXE 大小 | 72,934,583 字节（69.56 MiB） |
| SHA-256 | `a19a8551eab9e8f9be5aaa44ba83981d6a263af18dae8ca5c6ecbaf5c146ad52` |
| 本地发布归档 | `F:\beam-build\release-v1.2.3\BeamChat.exe` 与相邻 `.sha256` |
| 构建与版本、更新器、发布流程、协议测试 | 71 项通过 |
| EXE 启动与内嵌版本、WSS 地址、摘要检查 | 通过 |
| 包内 Qt、图片、语音 PCM、PyAV、Fernet、本机 WebRTC 验证 | 通过 |

与 GitHub 已发布的 `v1.2.2`（84,455,656 字节）相比，最终包减少约 **13.6%**。瘦身前后本地 `1.2.2` 的 15.87% 对比仅用于构建优化记录。

启动时的更新检测改为通过 Qt 信号把结果派发到界面线程，解决新版本自动更新横幅的回调问题。该修复随 `1.2.3` 生效；现有 `1.2.2` 用户通过 **设置 → 检查更新 → 立即更新** 升级。

更新器读取 `https://api.github.com/repos/pikechu/p2pchat/releases/latest`，只接受版本号更高且同时包含 `BeamChat.exe` 和 `BeamChat.exe.sha256` 的正式 Release，下载后验证长度和 SHA-256，再允许安装。检查结果缓存 10 分钟。

发布步骤为：合并已评审的源码，按合并提交创建 `v1.2.3` 草稿 Release，上传本地验收过的 EXE 和摘要，核对资产后发布并设为 latest。标签构建仍保存 Actions 产物；工作流在发布前检查已有正式或草稿 Release，发现同标签时跳过上传，避免覆盖本地验收包。API 查询异常会使流程失败，不能当作“不存在”继续发布。

验证范围：隔离依赖验证未使用麦克风或生产 WebRTC；冻结引导器由 `--help` 启动检查覆盖。远程发布后还需使用旧版本更新器验证公开 latest 检测、下载及摘要，不在维护环境替换用户正在运行的客户端。
