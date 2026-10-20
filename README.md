# pcl-version-homepage

给 PCL 用的自定义主页。打开 PCL 就能看到 Minecraft 版本列表和加载器下载按钮。

## 启动

```bash
python server.py
```

零依赖，Python 3.9 以上即可。默认只监听本机：

```
http://127.0.0.1:8087/homepage.xaml
```

要让别的机器访问，加 `--host 0.0.0.0`。

## 使用

1. 打开 PCL，进 `设置 → 个性化 → 主页`
2. 选「联网更新」，填上面的地址
3. 回到启动页，右侧就是版本列表

点版本行下载原版 `client.jar`。下面四个按钮下载 Forge / NeoForge / Fabric 安装器，OptiFine 按钮打开官网。

## 可选参数

```
?releases=30    正式版列出多少条
?snapshots=10   预览版列出多少条
?loaders=0      关掉加载器按钮
```
