# 各版本客户端 / 加载器 / API 的下载来源

原则一句话：**不要存死链，全部从官方元数据接口现查。** 版本号会变，接口不会。

---

## 原版（Vanilla）

### 第一步：拉版本清单

```
https://piston-meta.mojang.com/mc/game/version_manifest_v2.json
```

| 字段 | 含义 |
|---|---|
| `latest.release` | 最新正式版，例如 `26.3` |
| `latest.snapshot` | 最新快照 |
| `versions[].id` | 版本号 |
| `versions[].type` | `release` / `snapshot` / `old_beta` / `old_alpha` |
| `versions[].url` | 该版本的 JSON 地址 |

### 第二步：拉该版本的 JSON

`versions[].url` 指到的文件里有：

| 字段 | 内容 |
|---|---|
| `downloads.client.url` | 客户端 `client.jar` |
| `downloads.server.url` | 服务端 `server.jar` |
| `assetIndex.url` | 资源索引（贴图和音效的哈希表） |
| `libraries[].downloads.artifact.url` | 启动要用的库 |
| `javaVersion.majorVersion` | 该版本要求的 Java 大版本 |
| `mainClass` | 启动主类，例如 `net.minecraft.client.main.Main` |

26.3 实测：`client.jar` 41,483,720 字节，`libraries` 114 个，要求 Java 25。

### 注意

只下 `client.jar` **跑不起来**。一个能启动的客户端 = `client.jar` + `libraries` + `assets` + 正确的启动参数（`arguments.jvm` / `arguments.game`）。要么用启动器，要么自己按版本 JSON 拼。

---

## Forge

### 查最新版本号

```
https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json
```

里面是 `<mc>-latest` 和 `<mc>-recommended` 两个键，例如 `"26.3-latest": "66.0.13"`。

### 拼安装器地址

```
https://maven.minecraftforge.net/net/minecraftforge/forge/<mc>-<forge>/forge-<mc>-<forge>-installer.jar
```

实测：

| MC | Forge | 大小 |
|---|---|---|
| 26.3 | 66.0.13 | 7,981,966 |
| 1.21.8 | 58.1.26 | 9,397,647 |
| 1.20.1 | 47.4.28 | 8,859,760 |

### 人看的页面

```
https://files.minecraftforge.net/net/minecraftforge/forge/index_<mc>.html
```

---

## NeoForge

### 查版本列表

```
https://maven.neoforged.net/releases/net/neoforged/neoforge/maven-metadata.xml
```

### 拼安装器地址

```
https://maven.neoforged.net/releases/net/neoforged/neoforge/<ver>/neoforge-<ver>-installer.jar
```

### 版本号怎么对上

NeoForge 的版本号 = MC 版本去掉开头的 `1.`，再加构建号：

| MC | NeoForge | 规则 |
|---|---|---|
| 26.3 | `26.3.0.95` | 前缀 `26.3.` |
| 1.21.8 | `21.8.x` | 去掉 `1.` → 前缀 `21.8.` |
| 1.20.1 | 不在这个 artifact 里 | 见下 |

1.20.1 用的是另一个 artifact：

```
https://maven.neoforged.net/releases/net/neoforged/forge/maven-metadata.xml
```

版本号形如 `1.20.1-47.1.7`。

---

## Fabric

### 安装器

```
https://meta.fabricmc.net/v2/versions/installer
```

取第一条的 `url`。安装器**与游戏版本无关**，装的时候才选游戏版本，所以不用按版本找。

### 某个游戏版本能用哪些 loader

```
https://meta.fabricmc.net/v2/versions/loader/<mc>
```

26.3 实测有 255 条 loader 记录，最新 loader `0.20.1`。

### Fabric API

Fabric API 是一个普通模组，走 maven：

```
https://maven.fabricmc.net/net/fabricmc/fabric-api/fabric-api/maven-metadata.xml
https://maven.fabricmc.net/net/fabricmc/fabric-api/fabric-api/<ver>/fabric-api-<ver>.jar
```

版本号带游戏版本后缀，例如 `0.164.0+26.3`、`0.164.0+26.4`。**按后缀筛出对应游戏版本的那一条**，别拿错。

实测 `0.164.0+26.4` → 1,775,999 字节。

---

## Quilt

```
https://meta.quiltmc.org/v3/versions/installer
```

返回的 `url` 指向 `maven.quiltmc.org` 上的 `quilt-installer`。Quilt 的 API（QSL）也在这个 maven 上。

---

## OptiFine

**没有公开接口。** 只能走网页：

```
https://optifine.net/downloads
```

按游戏版本点进去，直链是 `adloadx` 跳转，中间夹广告页。不要批量抓，会被拦。

---

## 通用模组站（API 类模组大多在这）

| 站点 | 接口 |
|---|---|
| Modrinth | `https://api.modrinth.com/v2/project/<slug>/version?game_versions=["<mc>"]&loaders=["fabric"]` |
| CurseForge | 需要申请 API key |

例：Fabric API 在 Modrinth 的 slug 是 `fabric-api`。

---

## 国内网络：BMCLAPI

```
https://bmclapi2.bangbang93.com/
```

| 内容 | 能不能用 |
|---|---|
| 原版 | 有完整镜像 |
| NeoForge 元数据 | 能拉 |
| NeoForge 的 jar | **不镜像**，实测 404，下载链接还得指官方 maven |

镜像会滞后。实测 NeoForge 官方已有 `26.3.0.95`，镜像停在 `26.3.0.94`。

---

## 踩过的坑

1. **清单里 `type` 混着快照和远古版本**。只想要正式版就筛 `release`。
2. **面板那台机器没有 IPv6 路由**。`getaddrinfo` 先返回 AAAA，每个请求都要先白等一次超时。代码里应该强制 IPv4。
3. **Fabric 安装器与游戏版本无关**，别按版本去找安装器；要按版本找的是 loader 列表。
4. **Forge 的 `promotions_slim.json` 里老版本可能只有 `recommended`**，没有 `latest`，两个都要兜。
5. **NeoForge 的版本前缀规则**不是简单的字符串包含。`1.21.8` 对应 `21.8.x`，不是 `1.21.8.x`。
6. **`client.jar` 不等于能玩的客户端**，见上面原版那节。
