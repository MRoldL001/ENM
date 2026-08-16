<div align="center">
  <img src="enm.png" alt="ENM Logo" width="325">
</div>

<br/>

<div align="center">
  <strong>ENM - 更轻松地安装 EUI-NEO SDK、创建应用并打包</strong>
</div>

<br/>

<div align="center">
  <a href="https://github.com/sudoevolve/EUI-NEO"><img src="https://img.shields.io/badge/EUI--NEO-Repo-4867da?style=for-the-badge" alt="EUI-NEO Repo"></a>
  <a href="https://sudoevolve.github.io/EUI-NEO/"><img src="https://img.shields.io/badge/EUI--NEO-Website-4867da?style=for-the-badge" alt="EUI-NEO Website"></a>
</div>

<div align="center">
  <img src="https://img.shields.io/badge/ENM-0.2.1-2F80ED?style=flat-square" alt="ENM 0.2.1">
  <img src="https://img.shields.io/badge/Python-%E2%89%A53.9-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/License-MIT-green?style=flat-square" alt="MIT License">
  <img src="https://img.shields.io/badge/Status-Experimental-orange?style=flat-square" alt="Experimental">
</div>

---

ENM 是一个非官方的 EUI-NEO 命令行工具。它帮你完成 SDK 下载、环境检查、应用创建、构建和打包，不需要把自己的应用放进 EUI-NEO 源码目录。

> [!IMPORTANT]
> ENM 会直接读取 EUI-NEO 的 [GitHub Releases](https://github.com/sudoevolve/EUI-NEO/releases)。版本列表、最新版和下载地址都来自上游，不需要等待 ENM 更新版本表。

## ✨ 能做什么

- 查看可用的 EUI-NEO 版本
- 下载并切换不同版本的 SDK
- 检查电脑是否具备构建条件
- 一条命令创建新的 EUI-NEO 应用
- 配置、构建和简单测试应用
- 整理运行文件并生成 ZIP 或 tar.gz
- 按需创建 GitHub Actions

## 🚀 快速开始

### 准备环境

需要提前安装：

- Python 3.9 或更高版本
- CMake 3.14 或更高版本
- Windows：Visual Studio 2022 Build Tools，并勾选 C++ 桌面开发工具
- 能正常访问 GitHub

Vulkan SDK 不是默认必需项。普通项目默认使用 OpenGL；只有你主动选择 Vulkan 时才需要安装它。

### 安装 ENM

ENM 不提供独立 EXE。你可以下载完整源码，也可以只保留两个安装器文件。

#### 方式一：下载完整源码

适合需要离线保存源码、查看实现或之后重新安装的用户。

1. 打开 [ENM Releases](https://github.com/MRoldL001/ENM/releases)，选择需要的版本。
2. 展开 `Assets`，下载 GitHub 自动生成的 `Source code (zip)`。
3. 完整解压 ZIP。
4. 进入解压后的目录，双击 `install.cmd`。
5. 选择 `Install ENM from this folder`。

安装器会确认当前目录包含 `pyproject.toml` 和 `src/enm` 等必要文件，然后从本地源码安装。安装完成后，下载和解压的源码都不会被删除。

#### 方式二：只下载安装器

适合只想安装和使用 ENM、不想长期保存完整源码的用户。

1. 下载 [`install.cmd`](https://raw.githubusercontent.com/MRoldL001/ENM/main/install.cmd)。
2. 下载 [`install.ps1`](https://raw.githubusercontent.com/MRoldL001/ENM/main/install.ps1)。
3. 保持文件名不变，并把两个文件放进同一个文件夹。
4. 双击 `install.cmd`。
5. 选择 `Download ENM from GitHub Releases`，再用方向键选择版本并按 Enter。

两个安装器文件必须同时存在，因为 `install.cmd` 会启动同目录下的 `install.ps1`。联网安装会下载所选 Release 的 `Source code (zip)`，在系统临时目录中解压并安装；无论成功还是失败，临时下载和解压文件都会被清理。你手动下载的 `install.cmd` 和 `install.ps1` 不会被删除。

#### 从 PowerShell 启动

无论采用哪种下载方式，也可以在安装器所在目录打开 PowerShell：

```powershell
.\install.ps1
```

安装界面的两个选项分别表示：

- `Install ENM from this folder`：检查安装器所在目录是否包含完整 ENM 源码，然后从本地安装；不会删除本地源码。
- `Download ENM from GitHub Releases`：读取 [ENM Releases](https://github.com/MRoldL001/ENM/releases)，继续使用方向键选择版本，再下载该 Release 自动生成的 `Source code (zip)`。源码会被临时解压，并在安装结束后删除。

也可以跳过交互选择，直接指定网络版本：

```powershell
.\install.ps1 -Source GitHub -Version 0.2.1
```

完成后请打开一个新终端并确认：

```powershell
enm --version
enm doctor
```

安装器会比较源码包与本机已安装的 ENM 版本。源码包版本更低时会拒绝降级；相同版本允许重新安装。这个限制只针对 ENM 自身，不影响安装或切换旧版 EUI-NEO SDK。

> [!WARNING]
> 只有完整解压 Release 源码后才能选择本地安装。只下载两个安装器文件时必须选择 GitHub Releases，并保持网络可用。

卸载：

```powershell
.\uninstall.ps1
```

也可以直接从源码安装：

```powershell
python -m pip install -e .
```

## 📦 安装 EUI-NEO SDK

先查看版本：

```powershell
enm sdk list
```

只查看某个版本：

```powershell
enm sdk list v0.5.6
```

示例输出：

```text
VERSION            PUBLISHED    HOST SDK INSTALLED ACTIVE
v0.5.6             2026-08-10   yes      yes       *
v0.5.5             2026-08-02   yes      no
```

- `HOST SDK`：该版本是否提供适合当前电脑的 SDK
- `INSTALLED`：是否已经安装到本机
- `ACTIVE`：星号表示当前使用的版本

只查看本机已经安装的 SDK（不访问网络）：

```powershell
enm sdk installed
```

示例输出：

```text
VERSION            ACTIVE  PATH
v0.5.6             *       C:\Users\name\AppData\Local\enm\sdks\v0.5.6\windows-x64
v0.5.5                     C:\Users\name\AppData\Local\enm\sdks\v0.5.5\windows-x64
```

安装最新版：

```powershell
enm sdk install latest
```

安装或切换指定版本：

```powershell
enm sdk install v0.5.5
enm sdk use v0.5.5
```

查看当前 SDK 的位置：

```powershell
enm sdk path
```

删除不再需要的版本时，必须明确写出版本号：

```powershell
enm sdk uninstall v0.5.5
```

命令不接受空版本。当前激活的 SDK 默认不能删除；请先用 `enm sdk use` 切换，或在确认后添加 `--force`。强制删除当前版本后将不再有激活版本。

下载完成后，ENM 会核对文件摘要。不同版本可以同时保留，切换版本不会覆盖其他 SDK。

## 🪄 创建第一个应用

```powershell
enm init "My App" --path my-app
cd my-app
enm configure
enm build
```

程序通常位于：

```text
my-app/build/default/Release/My_App.exe
```

生成的项目会记住创建时选择的 EUI-NEO 版本。以后即使你切换了全局 SDK，这个项目仍会继续使用原来的版本，除非手动修改 `enm-project.json`。

ENM 会按 SDK 的实际内容选择接入方式。提供 `eui_neo_configure_app()` 的新版 SDK 直接使用上游统一入口；只导出 `eui::neo` 的旧版 SDK 会自动下载并缓存同标签的上游源码，用其中匹配的 GLFW/SDL2 入口补齐构建。首次配置旧 SDK 需要联网，之后可以使用缓存离线构建；判断依据不是写死的版本号。

`enm-project.json` 是项目名称、项目版本、构建目标和 EUI-NEO 版本的唯一配置源。`enm configure` 会在构建目录生成临时的 `enm-config.cmake` 供 CMake 使用，不需要在 `CMakeLists.txt` 中重复维护这些值。

项目使用 EUI-NEO 官方外部应用接口，并将源码单独放在 `src/`：

```text
my-app/
├─ src/
│  └─ app.cpp
├─ tests/
│  └─ app_config_test.cpp
├─ CMakeLists.txt
├─ enm-project.json
└─ .gitignore
```

默认测试通过应用实际导出的 `dslAppConfig()` 检查标题、页面 ID 和窗口尺寸。项目变大后，可以按上游应用的组织方式自行加入 `assets/`、`components/` 和 `pages/`。

### 后续操作

```powershell
enm test
enm deploy
enm package --format zip
```

- `test`：单独构建测试程序，再通过 CTest 运行登记的测试；普通 `build` 不构建测试
- `deploy`：把程序、资源和需要随程序提供的文件整理到 `dist/`
- `package`：无论成功失败都会删除临时部署目录；成功时只保留压缩包与 `.sha256`，需要展开目录请使用 `deploy`
- `package`：把整理后的目录压缩，并附带 `.sha256` 校验文件

## 🩺 关于环境检查

运行：

```powershell
enm doctor
```

`enm doctor` 会按上游实际需求做能力探测，而不只是匹配版本号：编译器是否真正支持 C++17、OpenGL 开发文件、平台系统依赖、所选窗口/渲染后端、SDK 头文件与库完整性等。

常用形式：

```powershell
enm doctor                       # 常规检查
enm doctor --deep                # 额外做一次真实 CMake configure，验证 SDK 与本机工具链是否兼容
enm doctor --project ./my-app    # 根据项目推断后端依赖（如 SDL2 / Vulkan）
enm doctor fix                   # 列出缺失依赖并询问安装
enm doctor fix --yes             # 自动安装必需依赖，可选依赖仍会询问
```

输出标记：

- `OK`：已找到并可使用
- `--`：可选项目未找到，通常不影响当前操作
- `!!`：需要处理的问题
- `++`：本次 `fix` 已安装，随后会重跑检查确认
- `??`：暂时无法确认，例如 Visual Studio 正在安装或更新

如果提示 `vs-version` 或 `c++17` 不兼容，请先通过 Visual Studio Installer 更新 Visual Studio 2022 Build Tools，然后删除项目的 `build` 目录并重新构建：

```powershell
Remove-Item -Recurse -Force .\build
enm configure
enm build
```

## 🤖 可选：GitHub Actions

`enm init` 默认不会创建 `.github/`。如果你确实需要 GitHub Actions，可以在项目目录中运行：

```powershell
enm ci init github `
  --install-spec "git+https://github.com/OWNER/enm.git@TAG"
```

也可以在创建项目时一起生成：

```powershell
enm init "My App" --path my-app --ci github `
  --install-spec "git+https://github.com/OWNER/enm.git@TAG"
```

请把 `OWNER` 和 `TAG` 换成真实的仓库所有者和版本标签。由于 ENM 尚未发布到 Python 包索引，这里必须提供 GitHub Actions 能访问的 ENM 仓库或 wheel 地址。

## 🔧 命令列表

### 全局参数

| 参数          | 说明                        |
| ----------- | ------------------------- |
| `--help`    | 显示帮助信息                    |
| `--version` | 显示 ENM 版本号                |
| `--home`    | 覆盖 ENM 状态目录（默认在各自系统用户目录下） |

### `enm doctor` — 环境检查

| 参数/子命令               | 说明                                        |
| -------------------- | ----------------------------------------- |
| `--project <路径>`     | 指定项目目录，根据 `enm-project.json` 推断窗口/渲染后端依赖  |
| `--deep`             | 额外用真实 CMake configure 验证当前 SDK 与本机工具链是否兼容 |
| `--json`             | 以 JSON 格式输出检查结果                           |
| `fix`                | 进入依赖补全模式                                  |
| `fix --yes`          | 自动确认安装必需依赖；不能与 `--force` 同时使用            |
| `fix --force`        | 同时询问安装可选依赖；不能与 `--yes` 同时使用              |
| `fix --project <路径>` | 与 `doctor --project` 相同，用于 fix 模式         |
| `fix --deep`         | 与 `doctor --deep` 相同，用于 fix 模式            |

### `enm sdk` — SDK 管理

| 参数/子命令                         | 说明                               |
| ------------------------------ | -------------------------------- |
| `list [VERSION]`               | 从 GitHub Releases 查看版本列表；可指定单个版本 |
| `list --include-prerelease`    | 包含预发布版本                          |
| `list --json`                  | 以 JSON 格式输出                      |
| `installed`                    | 查看本机已安装的 SDK，不访问网络               |
| `installed --json`             | 以 JSON 格式输出                      |
| `install [VERSION]`            | 下载、校验并激活 SDK；省略版本时默认 `latest`    |
| `install --include-prerelease` | 允许安装预发布版本                        |
| `install --force`              | 允许重新下载已安装的版本                     |
| `install --allow-unverified`   | 跳过文件摘要校验（不推荐）                    |
| `use VERSION`                  | 切换到已安装的某个 SDK 版本                 |
| `path`                         | 输出当前激活 SDK 的本地路径                 |
| `uninstall VERSION`            | 删除指定版本的本地 SDK                    |
| `uninstall --force`            | 允许删除当前激活的 SDK                    |

### `enm init` — 创建应用

| 参数               | 说明                                           |
| ---------------- | -------------------------------------------- |
| `NAME`           | 应用名称，会用于目录和项目配置                              |
| `--path <路径>`    | 指定项目创建目录，默认使用应用名                             |
| `--version <标签>` | 固定使用的 EUI-NEO 版本，默认使用当前激活或最新版                |
| `--force`        | 目录已存在时覆盖                                     |
| `--ci github`    | 同时创建 GitHub Actions 工作流                      |
| `--install-spec` | 与 `--ci github` 一起使用，指定 CI 中安装 ENM 的 pip 源地址 |

### `enm configure` / `enm build` / `enm test`

| 参数               | 说明                                                   |
| ---------------- | ---------------------------------------------------- |
| `--project <路径>` | 指定项目目录，默认在当前目录查找                                     |
| `-- <额外参数>`      | `--` 之后的所有参数原样传给 CMake（configure/build）或 CTest（test） |

### `enm deploy` — 整理运行文件

| 参数                   | 说明                 |
| -------------------- | ------------------ |
| `--project <路径>`     | 指定项目目录             |
| `--destination <路径>` | 自定义输出目录，默认 `dist/` |
| `--binary <文件名>`     | 显式指定要部署的主程序名       |
| `--force`            | 输出目录已存在时覆盖         |

### `enm package` — 打包

| 参数                | 说明            |
| ----------------- | ------------- |
| `--project <路径>`  | 指定项目目录        |
| `--binary <文件名>`  | 显式指定要打包的主程序名  |
| `--format zip`    | 输出 ZIP 格式（默认） |
| `--format tar.gz` | 输出 tar.gz 格式  |

### `enm ci` — CI 配置

| 参数/子命令                | 说明                                                                    |
| --------------------- | --------------------------------------------------------------------- |
| `init github`         | 创建 GitHub Actions 工作流                                                 |
| `--project <路径>`      | 指定项目目录                                                                |
| `--install-spec <地址>` | 必填，CI 中安装 ENM 的 pip 源地址，例如 `git+https://github.com/OWNER/enm.git@TAG` |

每个命令都可以使用 `--help` 查看完整选项，例如：

```powershell
enm sdk install --help
```

## 🧩 给开发者



从源码参与开发时可使用：

```powershell
python -m pip install -e .
python -m unittest discover -s tests -v
```

## ⚠️ 当前状态

- ENM 仍处于实验阶段，当前版本为 `0.2.1`
- 主要在 Windows x64 上进行了实际验证
- Linux 与 macOS 支持尚未经过同等程度的测试
- `enm doctor fix` 可尝试一键安装部分依赖（CMake、Ninja、Linux 系统库等），但编译器、Visual Studio、Vulkan SDK 等仍可能需要手动安装；加 `--yes` 自动确认必需依赖
- 不会为发布包签名，也不会制作 MSI 等系统安装包
- 上游预编译 SDK 仍可能与本机编译器不兼容，ENM 会尽量提前提示

## 📄 非官方声明

ENM 是由独立开发者制作的第三方工具，与 EUI-NEO 作者没有隶属、授权或官方合作关系。EUI-NEO 的名称、代码和发布文件归各自权利人所有。

## 📜 许可证

ENM 使用 [MIT License](LICENSE)。EUI-NEO 及其依赖仍使用各自的许可证。
