# bypass 源码来源与审计说明

## 这是什么

本目录收录的是 [Morpheus799/pmhq-bypass](https://github.com/Morpheus799/pmhq-bypass) 的**源代码**（不含预编译产物），用于让 PMHQ 在**本机离线**完成它对 Luckylillia 服务端的账号认证绑定。

**版本锁死**：这套补丁只对 **pmhq 8.1.1** 生效（上游 README 原话："本项目只对pmhq8.1.1生效，其他版本请用户自行逆向"）。
因此 `Dockerfile.pmhq` 里基础镜像固定为 `linyuchen/pmhq:8.1.1`，**不允许改成 `latest`**。升级 PMHQ 会直接让补丁失效。

## 上游文件清单与哈希

收录时间：2026-09-30，来源 `main` 分支（`codeload.github.com/Morpheus799/pmhq-bypass/tar.gz/refs/heads/main`）。

| 文件 | 大小 | SHA256（前 16 位） |
|---|---|---|
| `Makefile` | 385 B | `D4BAEC1F5BF409A1` |
| `patch_libpmhq.c` | 970 B | `E7A0D838B50A0E77` |
| `launcher.c` | 2579 B | `717E5B955C4DC4E8` |
| `libpmhqwrap.c` | 2927 B | `2ABB838504230320` |
| `tlsserver.c` | 3410 B | `0659DB8562BED162` |

上游另有两个预编译产物 `libpmhq.so.orig` / `libpmhq.so.patched`（各 1,078,352 B），本仓库**不收录**，只留哈希备查：

- `libpmhq.so.orig`　‎`5DE40933E7EA791C09FB6C8D122B83B44FE31526D00AB7DBC674BCBB4752806E`
- `libpmhq.so.patched` `89B4F90A1E65FC23E68F989BAFBDF236571F5FC15E72D11C9316454199328B60`

## 为什么不直接用上游的预编译产物

镜像里**只从本目录的源码编译**（`Makefile` 全程 `gcc`），不下载、不拷贝任何上游二进制。理由是可审计性：源码只有不到 10 KB，能逐行读完；预编译的 `.so` 与可执行文件不能。

作为交叉验证，我对上游的 `libpmhq.so.orig` 与 `libpmhq.so.patched` 做了逐字节比对：

```
大小相同（1,078,352 B）
不同的字节：5 个，偏移 0x76326 0x76327 0x76328 0x76329 0x7632B
原  字节：0F 84 1F 01 00      （jz rel32，条件跳转）
补丁后　：E9 20 01 00 90      （jmp rel32 + nop，无条件跳转）
```

这与 `patch_libpmhq.c` 里写死的替换（第 5-6 行的 `Z0`/`Z1`）**完全一致**，说明上游发布的 `.patched` 文件里没有夹带补丁之外的内容。第 6 个字节 `0x7632A` 原值本就是 `00`，新旧写法相同，故不计入差异。

## 它究竟改了什么（三个独立动作）

1. **`patch_libpmhq.c`**：在 `libpmhq.so` 文件偏移 `0x76326` 处，把一个 `jz`（条件跳转）改成 `jmp`（无条件跳转），即**跳过认证失败的落点**。写入前会校验原字节序列，不匹配就拒绝执行（`unexpected bytes`），不会误伤别的版本。
2. **`libpmhqwrap.so`**（通过 `LD_PRELOAD` 注入）：
   - 劫持 `getaddrinfo`，把 `api-auth.luckylillia.com` 解析到 `127.0.0.1`；
   - 劫持 `connect`，把所有指向 `127.0.0.1:443` 的连接重定向到本机 `PMHQ_TLS_PORT`（默认 8443）；
   - 通过 `dl_iterate_phdr` 定位 `libpmhq.so` 的加载基址，把内存里另一处 7 字节序列改成 `c6 07 16 48 89 f8 c3`。
3. **`tlsserver`**：在 `127.0.0.1:8443` 起一个最小 HTTPS 服务，**现场自签**一张 `CN=api-auth.luckylillia.com` 的证书，对任何请求都回 `{"code":0}`。这就是被伪造的那个认证端点。
4. **`launcher`（编译产物名 `pmhq`）**：先 fork `tlsserver`，再以 `LD_PRELOAD=libpmhqwrap.so` 启动真正的 `pmhq.real`，并在未显式传 `--auth-token` 时补一个占位值 `--auth-token=pmhq_bypass`。转发 SIGINT/SIGTERM。

**网络行为**：`tlsserver` 只 `bind` 到 `127.0.0.1`；`getaddrinfo` 劫持把认证域名解析到回环。整套动作**不需要也无法访问 Luckylillia 服务端**——它把外发认证请求就地截断成本地应答。

## 许可证状态（需要知情）

上游仓库**没有 LICENSE 文件**，因此默认保留所有权利。本目录是对其源码的本地副本，仅用于本机个人部署；如需再分发，请自行联系上游作者确认授权。

## 维护提醒

- **PMHQ 一升级就失效**。升级前先确认新版本的 `libpmhq.so` 在 `0x76326` 处是否仍是 `0F 84 1F 01 00 00`；不是就得重新逆向（`patch_libpmhq` 会直接拒绝，不会静默损坏文件）。
- 构建时会强制校验补丁结果（见 `Dockerfile.pmhq`），补丁没打上就让构建失败，避免"看起来起来了其实还在走联网认证"。
