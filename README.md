# PCL 版本下载主页

给 PCL 用的自定义主页：自动从官方数据源取版本列表，生成 PCL 能渲染的 XAML。
布局照抄 PCL 自带的 `下载 → 原版游戏`：**最新版本** + **正式版** + **预览版** 三张卡。
每个版本下面有一排加载器按钮。

## 为什么要服务端

PCL 的自定义主页是 XAML，**没有循环、没有网络请求**。所以「自动获取版本列表」只能在服务端做：
每次 PCL 拉取主页时，服务端现取清单、解析加载器、展开成一串控件，再把整段 XAML 回给 PCL。

## 已部署

面板实例 `PCL-Version-Homepage`，UUID `b5443735df2b4fba9bd1d61e7a028cf6`，内网 `0.0.0.0:32936`。

| 项 | 值 |
|---|---|
| PCL 主页地址 | `http://110.42.14.233:32936/homepage.xaml` |
| HTML 预览 | `http://110.42.14.233:32936/` |
| 外网端口 | `32936`（handbook NAT 表里内外同号，原本空闲） |

部署时踩到的坑：

- `31437` 在 handbook 里看着空闲，实际被 `docker-proxy` 占了，起服务直接 `Address already in use`。
- 面板主机**没有 IPv6 路由**，`getaddrinfo` 先返回 AAAA，每个上游都要先失败一次。代码里已强制 IPv4。
- `maven.neoforged.net` 从面板主机连不上（超时），改用 `bmclapi2.bangbang93.com` 取版本列表。
  BMCLAPI 只镜像元数据、不镜像 jar，所以安装器链接仍指向官方 maven。
- 冷启动原本 40 秒（40 个 `version.json` + 加载器元数据）。现在版本信息与加载器元数据都落盘缓存，
  再加一层 5 分钟的渲染缓存和启动时后台预热，实测请求 **106 ms**。

## 跑起来

```text
python server.py
```

默认 `127.0.0.1:8087`。给外网用：

```text
python server.py --host 0.0.0.0 --port 8087
```

## 接到 PCL

PCL → `设置 → 个性化 → 主页` → 选「联网更新」，填：

```text
http://127.0.0.1:8087/homepage.xaml
```

| 参数 | 含义 | 默认 |
|---|---|---|
| `releases` | 正式版列出多少条 | 30 |
| `snapshots` | 预览版列出多少条 | 10 |
| `tz` | 「发布于」用的 UTC 偏移，单位小时 | 8 |
| `loaders` | `0` 关闭加载器按钮行 | 开 |

```text
http://127.0.0.1:8087/homepage.xaml?releases=50&snapshots=0
http://127.0.0.1:8087/homepage.xaml?loaders=0
```

时区默认 +8，和 PCL 自己显示的时间对得上（26.3 显示为 `2026/09/15 19:23`）。

## 每行的四个按钮

| 按钮 | 来源 | 说明 |
|---|---|---|
| 版本行本身 | Mojang `version.json` | 下载该版本的原版 `client.jar` |
| Forge | `promotions_slim.json` | 取 `<mc>-latest`，拼成 `forge-<mc>-<ver>-installer.jar` |
| NeoForge | NeoForge maven 的 `maven-metadata.xml` | 版本号去掉开头的 `1.`（`1.21.8`→`21.8`、`26.3`→`26.3`），取前缀匹配的最大构建 |
| Fabric | `meta.fabricmc.net/v2/versions/installer` | 安装器与游戏版本无关，永远是最新安装器 |
| OptiFine | — | 没有公开直链，按钮改为打开 optifine.net |

拿不到构建的加载器（例如 1.20.1 的 NeoForge）会渲染成一个按钮，点了弹提示说明该版本没有构建，
不会给出一个 404 的链接。

实测 26.3：Forge `66.0.13`、NeoForge `26.3.0.95`、Fabric `1.1.2`，三个安装器 HEAD 都是 200。

## 为什么按钮，不是下拉框

试过 `local:MyComboBox`，PCL 触发自定义事件的入口是这样的（`Modules/ModEvent.vb`）：

```vb
Dim EventType = CustomEventService.GetEventType(Control)
If EventType <> CustomEvent.EventType.None Then
    Events.Add(New CustomEvent(EventType, CustomEventService.GetEventData(Control)))
```

它只读控件上挂死的 `EventType` / `EventData`，**从不读 `SelectedItem`**。所以一个下拉框不管选哪一项，
触发的都是同一个事件，「选 Forge 装 Forge」用下拉框做不到。`MyButton`、`MyListItem` 这类是逐控件挂事件的，
一个控件一个动作，所以改成按钮。

## 接口

| 路径 | 内容 |
|---|---|
| `/homepage.xaml` | PCL 自定义主页 XAML |
| `/homepage.xaml.ini` | 版本校验串。PCL 每次先取它，值没变就不重新下载 |
| `/` | 浏览器能看的 HTML 预览，方便不开 PCL 时核对 |
| `/api/versions` | 同样的数据，JSON，便于脚本用 |
| `/healthz` | 存活检查 |

## 导出静态快照

不想跑服务时，也可以定时生成一份静态 XAML：

```text
python server.py --export out.xaml --releases 30
```

配合 cron 或 GitHub Actions 定时重跑即可。

## 缓存

| 数据 | 缓存 |
|---|---|
| Mojang 版本清单 | 10 分钟 |
| 每个版本的 `version.json` | 7 天（发布后不再变） |
| Forge / NeoForge / Fabric 元数据 | 1 小时 |

冷启动第一屏会拉 1 + N 次 HTTP（N = 当前页条数 + 3 次加载器元数据），之后都走内存。

## PCL 的缓存行为（踩过的坑）

PCL 加载联网主页时（`PageLaunchRight.xaml.vb`）：

1. 如果 URL 和上次一致，就直接渲染 `%TEMP%\PCL\Cache\Custom.xaml` 里的缓存。
2. 然后**后台**重新下载一份，但 `ShouldRefresh=False`，也就是只更新缓存、不重新渲染。

所以改了页面之后，用户必须按一次 **F5**（触发 `ForceRefresh`，会清缓存并重下），否则屏幕上还是旧内容。

`.xaml.ini` 是第 2 步的版本校验：值不变时 PCL 直接跳过下载。服务端返回一个基于清单内容和加载器版本的短哈希。

## XAML 格式依据

PCL 内置帮助「个性化 / 自定义主页与帮助 XAML 格式」，以及 PCL wiki 的自定义事件与替换标记页面。

几条硬约束：

- 可用的命名空间只有默认 presentation、`x`、`sys`、`local`，不要自己加。
- 顶层直接平铺多个 `local:MyCard`，不需要根元素包裹。
- `TextWrapping` 是 WPF 枚举，只能写 `Wrap` / `NoWrap` / `WrapWithOverflow`，**不能写布尔值**。
- 换行用 `&#xA;`，`&` 要写成 `&amp;`。
- `MyComboBox` 不在内置帮助的控件清单里，只在事件支持列表里出现。
