# BeamChat 端到端加密、持久化与 TTL 实施计划

> **供自动化执行者使用：** 必须使用 `subagent-driven-development`（推荐）或 `executing-plans`，逐任务执行本计划。所有步骤使用复选框跟踪。

**目标：** 完成 issue #3、#5、#7，使消息、文件和语音均使用经过身份认证的端到端加密，并提供房间/私聊五档 TTL 配置与严格协议握手。

**架构：** 新增独立的身份存储与加密信封模块，GUI 和终端客户端共享同一套密钥、TOFU、消息、文件及语音加密接口。服务端只负责严格握手、密钥包转发、密文路由、TTL 授权和密文持久化，不参与解密。

**技术栈：** Python 3.10+、cryptography、argon2-cffi、websockets、PyQt6、pytest。

## 全局约束

- 所有新增交流、文档、文档字符串、代码注释和用户可见说明使用中文。
- 协议字段、代码标识符、命令和第三方库名称保留英文。
- 不允许消息、文件、语音、房间密码或密钥在任何网络路径中回退为明文。
- 服务端日志和数据库不得出现明文、密码、私钥、完整密文或解密后的文件元数据。
- 协议版本 3 使用严格握手，旧客户端必须收到明确升级错误。
- 用户要求只在全部实现和完整测试通过后统一提交，不做中间提交。

---

### 任务 1：身份密钥、指纹和 TOFU 存储

**文件：**
- 新建：`identity.py`
- 修改：`requirements.txt`
- 测试：`tests/test_identity.py`

**接口：**
- 产出：`IdentityStore.load_or_create() -> DeviceIdentity`
- 产出：`DeviceIdentity.public_bundle(ephemeral_public, signature) -> dict`
- 产出：`fingerprint(public_key: bytes) -> str`
- 产出：`TrustStore.observe(peer_name, identity_public) -> TrustDecision`
- 产出：`TrustStore.accept(peer_name, identity_public) -> None`

- [ ] 写测试：首次创建后重载得到相同 Ed25519 身份和持久 X25519 预密钥，文件权限为 `0600`。
- [ ] 运行 `pytest -q tests/test_identity.py`，确认因模块不存在而失败。
- [ ] 实现 PEM/原始字节序列化、原子写入和中文文档字符串；加入 `argon2-cffi>=23.1.0`。
- [ ] 写测试：指纹格式稳定，密钥包签名可验证，篡改预密钥或临时密钥后验证失败。
- [ ] 实现 `sign_key_bundle()` 与 `verify_key_bundle()`，签名内容固定为协议版本、身份公钥、预密钥和临时密钥的长度前缀编码。
- [ ] 写测试：TOFU 首次返回 `NEW`，相同密钥返回 `TRUSTED`，变化返回 `CHANGED`，显式接受后返回 `TRUSTED`。
- [ ] 实现 JSON 信任库原子更新，不以展示名之外的数据推断信任。
- [ ] 运行 `pytest -q tests/test_identity.py`，确认全部通过。

### 任务 2：统一 AEAD 信封和密钥派生

**文件：**
- 新建：`e2e_crypto.py`
- 修改：`crypto.py`
- 测试：`tests/test_e2e_crypto.py`、`tests/test_crypto.py`

**接口：**
- 消费：任务 1 的 `DeviceIdentity` 和密钥包验证函数。
- 产出：`derive_scope_keys(root_key, scope_type, scope_id) -> ScopeKeys`
- 产出：`encrypt_envelope(key, plaintext, context) -> dict`
- 产出：`decrypt_envelope(key, envelope, context) -> bytes`
- 产出：`encrypt_dm_for_participants(...) -> dict`
- 产出：`decrypt_dm_envelope(...) -> bytes`
- 产出：`derive_room_root(room_id, password, salt) -> bytes`

- [ ] 写测试：相同 X25519 组合得到相同根密钥，不同作用域派生出的消息、文件和语音子密钥均不同。
- [ ] 运行 `pytest -q tests/test_e2e_crypto.py`，确认接口缺失导致失败。
- [ ] 使用 X25519、HKDF-SHA256 和显式上下文实现根密钥与子密钥派生。
- [ ] 写测试：ChaCha20-Poly1305 信封可往返，密文、nonce、附加认证数据或算法版本被篡改时均失败。
- [ ] 实现版本化 JSON 信封，随机 96 位 nonce，异常统一为 `CryptoError`，不得返回未经认证的数据。
- [ ] 写测试：DM 内容密钥分别为发送方和接收方预密钥包装，接收方重启后仍可解密，第三方不能解密。
- [ ] 实现双参与者密钥信封，并将双方身份公钥、作用域和消息 ID 加入附加认证数据。
- [ ] 写测试：Argon2id 房间根密钥可复现，错误密码无法解密访问令牌，旧 Fernet 房间消息仅可读取不可新写。
- [ ] 修改 `crypto.py`，保留旧解码入口并把所有新加密转发到统一 AEAD 实现。
- [ ] 运行 `pytest -q tests/test_e2e_crypto.py tests/test_crypto.py`，确认全部通过。

### 任务 3：严格握手、密钥目录和能力协商

**文件：**
- 修改：`protocol.py`、`server.py`、`client.py`、`gui/bridge.py`、`gui/window.py`
- 测试：`tests/test_protocol.py`、`tests/test_chat_integration.py`、`tests/test_bridge_integration.py`、`tests/test_strict_handshake.py`

**接口：**
- 消费：任务 1 的签名密钥包。
- 产出：`CLIENT_HELLO`、`SERVER_HELLO`、`READY`、`PEER_KEY_BUNDLE`、`GET_PEER_KEY`。
- 产出：服务端连接状态 `HELLO`、`IDENTIFYING`、`READY`。

- [ ] 写测试：服务端连接后不主动发送 `WELCOME`；首帧不是 `CLIENT_HELLO` 时返回不可恢复的 `PROTOCOL_INCOMPATIBLE`。
- [ ] 运行 `pytest -q tests/test_strict_handshake.py`，确认旧欢迎帧行为导致失败。
- [ ] 在 `protocol.py` 增加必需能力 `authenticated_key_exchange`、`encrypted_files`、`encrypted_voice`、`ttl_policy`，并增加上述帧类型。
- [ ] 在 `server.py` 实现显式状态机；HELLO 只校验格式、版本和能力，身份签名由客户端对端校验。
- [ ] 写测试：缺少任一必需能力、版本不匹配和无效 base64 均返回结构化错误；成功路径严格按 HELLO、SET_NAME、READY 排序。
- [ ] 客户端和 GUI bridge 在连接线程内等待 `SERVER_HELLO`，成功后才报告 connected 并发送 `SET_NAME`；收到 `READY` 后才恢复房间。
- [ ] 写测试：已就绪用户可请求对端公开密钥包，未就绪用户不可请求；重连后临时 X25519 公钥变化且签名有效。
- [ ] 服务端维护仅在线的公开密钥目录，不写入私钥或共享密钥，不在日志输出完整密钥。
- [ ] 更新所有集成测试连接辅助函数为严格握手。
- [ ] 运行 `pytest -q tests/test_protocol.py tests/test_strict_handshake.py tests/test_chat_integration.py tests/test_bridge_integration.py`。

### 任务 4：加密私聊、TOFU 界面和离线同步

**文件：**
- 新建：`secure_session.py`
- 修改：`client.py`、`gui/window.py`、`server.py`
- 测试：`tests/test_secure_session.py`、`tests/test_message_persistence.py`、`tests/test_dm_e2e.py`、`tests/test_reconnect_identity.py`

**接口：**
- 消费：任务 1、2、3 的身份、信封和密钥目录。
- 产出：`SecureSessionManager.ensure_peer(peer) -> SessionState`
- 产出：`SecureSessionManager.encrypt_dm(peer, text, client_msg_id) -> dict`
- 产出：`SecureSessionManager.decrypt_dm(message) -> str`

- [ ] 写测试：首次对端密钥建立 TOFU，密钥变化冻结发送，接受后恢复，拒绝后继续冻结。
- [ ] 运行 `pytest -q tests/test_secure_session.py`，确认会话管理器不存在而失败。
- [ ] 实现会话管理器并缓存已验证密钥包；所有异常使用明确状态，不允许调用者回退 `SEND_DM` 明文路径。
- [ ] 写集成测试：A 发给 B 的服务端帧和数据库不含已知明文，B 在线和重启后均能解密，C 不能同步或解密。
- [ ] 将 GUI 和终端 DM 发送改为 `SEND_ENCRYPTED_MSG`；接收和同步统一走 `decrypt_dm()`，发送 ACK 更新本地偏移。
- [ ] 写 GUI 测试：DM 解密失败显示“加密私聊密钥不可用”，不出现房间密码提示；身份变化对话框显示新旧指纹及接受/拒绝操作。
- [ ] 移除新协议下的 `SEND_DM`/`RECV_DM` 明文使用，服务端收到后返回 `PLAINTEXT_FORBIDDEN`。
- [ ] 运行 `pytest -q tests/test_secure_session.py tests/test_dm_e2e.py tests/test_message_persistence.py tests/test_reconnect_identity.py`。

### 任务 5：房间密码客户端化和房间 AEAD 消息

**文件：**
- 修改：`server.py`、`client.py`、`gui/window.py`、`crypto.py`
- 测试：`tests/test_room_e2e.py`、`tests/test_chat_integration.py`、`tests/test_message_persistence.py`

**接口：**
- 消费：任务 2 的 `derive_room_root()`、作用域密钥和 AEAD 信封。
- 产出：`CREATE_ROOM` 的 `salt`、`encrypted_access_token`、`access_token_hash` 元数据。
- 产出：`JOIN_ROOM` 的 `access_token` 授权值，协议中不出现 password。

- [ ] 写测试：创建和加入帧中不包含密码或等价 Argon2id 验证值，错误密码无法生成有效访问令牌。
- [ ] 运行 `pytest -q tests/test_room_e2e.py`，确认现有密码验证路径导致失败。
- [ ] 创建者本地生成 salt、房间根密钥和随机访问令牌；服务端只保存令牌哈希，客户端元数据保存加密令牌。
- [ ] 服务端使用常量时间比较验证令牌哈希，移除密码接收、保存和日志路径。
- [ ] 写测试：新房间消息只使用 AEAD 密文持久化与中继，篡改时显示房间专用错误；旧 Fernet 历史只读。
- [ ] GUI 和终端房间发送、实时接收及同步统一使用 `ScopeKeys.message_key`。
- [ ] 运行 `pytest -q tests/test_room_e2e.py tests/test_chat_integration.py tests/test_message_persistence.py`。

### 任务 6：加密文件元数据和分块

**文件：**
- 修改：`file_transfer.py`、`webrtc_transfer.py`、`gui/window.py`、`protocol.py`、`server.py`
- 测试：`tests/test_file_transfer.py`、`tests/test_direct_file_sender.py`、`tests/test_webrtc_transfer.py`、`tests/test_server_file_routing.py`、`tests/test_file_e2e.py`

**接口：**
- 消费：任务 2 的文件包装密钥和 AEAD 信封。
- 产出：`EncryptedFileSender`、`EncryptedFileReceiver`。
- 产出：加密后的 `FILE_OFFER` 元数据、`FILE_CHUNK` 数据和 `FILE_DONE` 摘要确认。

- [ ] 写测试：加密文件往返后字节一致，中继载荷不包含文件名、MIME、原始分块或明文摘要。
- [ ] 运行 `pytest -q tests/test_file_e2e.py`，确认现有 base64 明文路径导致失败。
- [ ] 实现随机文件密钥、加密元数据、文件密钥包装和按索引 nonce 的独立分块 AEAD。
- [ ] 写测试：错序、重复、篡改、截断及错误作用域分块被拒绝，失败时 `.part` 文件被删除。
- [ ] 将房间 relay、DM relay 和 WebRTC DataChannel 接到同一加密发送器/接收器；服务端只验证路由字段和密文大小。
- [ ] 禁止旧明文文件帧，返回 `PLAINTEXT_FORBIDDEN`。
- [ ] 运行 `pytest -q tests/test_file_e2e.py tests/test_file_transfer.py tests/test_direct_file_sender.py tests/test_webrtc_transfer.py tests/test_server_file_routing.py`。

### 任务 7：加密语音包与重放保护

**文件：**
- 新建：`voice_crypto.py`
- 修改：`voice_call.py`、`protocol.py`、`server.py`、`gui/window.py`
- 测试：`tests/test_voice_crypto.py`、`tests/test_voice_call_state.py`、`tests/test_call_routing.py`

**接口：**
- 消费：任务 2 的语音子密钥和任务 3 的已验证临时密钥包。
- 产出：`VoiceCipher.encrypt(pcm) -> bytes`、`VoiceCipher.decrypt(packet) -> bytes`。

- [ ] 写测试：语音包往返一致，包体不含 PCM，篡改、重复序列和超出窗口的旧序列被拒绝。
- [ ] 运行 `pytest -q tests/test_voice_crypto.py`，确认模块不存在而失败。
- [ ] 实现通话标识、方向性密钥、单调序列 nonce 和 128 包有界重放窗口。
- [ ] relay 与直连 UDP 在发送前调用同一 `encrypt()`，接收后先 `decrypt()` 再交给 numpy；握手探测包保持无音频的固定控制帧。
- [ ] 写集成测试：服务端转发的 `VOICE_CHUNK` 和 UDP 音频数据均不包含已知 PCM；无密钥时通话失败关闭。
- [ ] 禁止旧明文语音帧并返回 `PLAINTEXT_FORBIDDEN`。
- [ ] 运行 `pytest -q tests/test_voice_crypto.py tests/test_voice_call_state.py tests/test_call_routing.py`。

### 任务 8：五档 TTL 协议、权限和聊天窗口控件

**文件：**
- 修改：`protocol.py`、`server.py`、`client.py`、`gui/window.py`、`gui/widgets.py`、`gui/theme.py`
- 测试：`tests/test_message_persistence.py`、`tests/test_ttl_policy.py`、`tests/test_gui_ttl.py`

**接口：**
- 产出：`SET_MESSAGE_TTL`、`MESSAGE_TTL_UPDATED`、`GET_MESSAGE_TTL`。
- 产出：`TTL_VALUES = {day, week, month, year, permanent}`。
- 产出：`TTLMenuButton.set_policy(scope_type, scope_id, ttl_seconds, enabled)`。

- [ ] 写服务端测试：五个值均可设置，其他值返回 `INVALID_TTL`；房间仅创建者可改。
- [ ] 运行 `pytest -q tests/test_ttl_policy.py`，确认协议未实现而失败。
- [ ] 实现设置、查询和广播；DM 任一方可缩短，延长时取双方请求的较短值；永久仅在双方都请求永久时生效。
- [ ] 写测试：设置立即影响新消息 `expires_at`，缩短策略会删除超期旧消息，永久为 null，关闭持久化时返回 `PERSISTENCE_DISABLED`。
- [ ] 写 GUI 测试：聊天标题区存在时钟图标，菜单显示一天/一周/一个月/一年/永久，当前项有勾选，切换会发送当前作用域。
- [ ] 实现 `TTLMenuButton`，使用现有图标库或 Qt 标准图标，并加入中文工具提示；无持久化能力时禁用。
- [ ] 终端客户端增加 `/ttl day|week|month|year|permanent`，查询当前会话策略。
- [ ] 运行 `pytest -q tests/test_ttl_policy.py tests/test_gui_ttl.py tests/test_message_persistence.py`。

### 任务 9：安全回归、迁移、打包和最终提交

**文件：**
- 修改：`build.py`、`README.md`（若存在）、相关测试 fixture。
- 测试：全部 `tests/`。

**接口：**
- 消费：任务 1 至 8 的所有行为。
- 产出：可构建的单 exe 依赖配置和一次最终 Git 提交。

- [ ] 增加迁移测试：旧数据库原地增加身份路由和 TTL 字段，不导入明文；旧本地状态首次启动生成身份且保留偏移。
- [ ] 增加安全扫描测试：捕获服务端帧、SQLite、日志和临时文件，断言已知消息、文件名、文件字节和 PCM 样本均不存在。
- [ ] 运行 `python -m py_compile protocol.py identity.py e2e_crypto.py secure_session.py file_transfer.py voice_crypto.py voice_call.py server.py client.py gui/bridge.py gui/window.py build.py`。
- [ ] 运行 `pytest -q`，修复全部失败并重新完整运行直到退出码为 0。
- [ ] 运行 `python build.py --debug --no-clean` 或仓库支持的等价单 exe 冒烟构建，确认 `argon2-cffi` 和 cryptography 原语被打包。
- [ ] 运行 `git diff --check`，检查 `git status --short`，排除运行时 `.pyc` 和日志文件，不删除用户已有修改。
- [ ] 请求独立代码审查，修复全部严重和重要问题后重新运行完整测试、编译及安全扫描。
- [ ] 仅暂存本次源代码、测试、中文规格、中文计划和 `AGENTS.md`，创建一次描述 issue #3/#5/#7 的最终提交。
