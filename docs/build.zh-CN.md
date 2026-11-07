# 构建指南

目标系统为 Ubuntu 26.04 amd64 / GNOME 50。构建需要 Python 3.12+、Rust 1.88+ 和 C/C++ 工具链，运行时需要 Python 3.10+。源码不包含模型、上游 Facelock 检出或预编译依赖。

## 主程序与硬件模块

```sh
sudo apt install git build-essential cargo rustc pkg-config libsystemd-dev libtss2-dev dpkg-dev python3-gi python3-gi-cairo python3-evdev gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-nm-1.0
export CARGO_BUILD_JOBS=2
(umask 077; cd vendor/letsnote-wheelpad; cargo test --locked)
git clone https://github.com/tyvsmith/facelock.git ../facelock
python3 tools/build_pose_helper.py ../facelock
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 tools/check_release.py
python3 tools/build_deb.py
python3 tools/build_firmware_deb.py
```

Facelock 固定于提交 `352bbbcb0f8f61cc21e36a1f038442e94fa6e26b`，构建器从该提交的干净归档编译。主包构建器自动编译 WheelPad。Cargo 使用锁文件；首次构建需要下载依赖。构建器自动收集实际使用的 Rust 依赖许可证，缺少授权文本时停止，并将编译机的私人路径替换为通用构建路径。

输出位于 `dist/`：

| 文件 | 用途 |
| --- | --- |
| `oh-my-panasonic_0.20.2_amd64.deb` | 主程序 |
| `panasonic-on-ubuntu_0.20.2_all.deb` | 保留旧包升级和可选组件依赖的空兼容包 |
| `panasonic-on-ubuntu-firmware-dkms_0.7.2_all.deb` | CF-SV7 专用模块源码；安装时由 DKMS 编译 |

固件包的名称为历史兼容标识，不包含 Panasonic 专有固件。其模块版本仍为 0.7.1。需要匹配当前内核的头文件，Secure Boot 环境还需要模块签名。

## 可选人脸与核显组件

主程序可在未安装 Facelock 时使用其余功能。要启用人脸功能，需要另外安装与上述固定提交兼容的 Facelock 0.2.0，并根据其文档配置红外摄像头、TPM 和合法获得的模型。不要将个人录入数据纳入软件包。

`tools/build_face_gpu_deb.py --help` 提供 UHD 620 加速组件的构建入口。需要固定 Facelock 提交、OpenVINO 2025.4.0 Linux x86_64 wheel、OpenSSL 开发头文件和兼容的 Intel OpenCL 驱动。构建脚本校验 wheel 的 SHA-256：

`6b581cf1c3fa759495eb051d1ec5be4b5cb61a4e15265db7b9d56260ea5925d0`

GPU 包要求 `facelock (= 0.2.0-1~ubuntu26.04.1)`。它不是主包的必需依赖，也不包含在本次预构建发布物中。使用源码构建入口并确认所有依赖授权后再自行分发；本机模型编译由 `pou-face-gpu-prepare` 完成，不上传模型。

## 验证

Python 单元测试和 WheelPad Rust 测试不操作实际硬件。图形测试需要 GNOME / GTK 环境；协议测试使用隔离总线：

```sh
PYTHONPATH=src dbus-run-session -- python3 tools/check_power_profiles_bus.py
PYTHONPATH=src GSK_RENDERER=cairo GDK_BACKEND=x11 OMP_PRIVATE_BUS_TEST=1 xvfb-run -a dbus-run-session -- python3 tools/gui_bluetooth_flow.py
```

`tools/validate_*.py` 和 `tools/verify_display_preview.py` 等实机工具默认显示帮助，显式传入 `--run` 才执行。运行前保存工作；显示测试会暂时改变屏幕配置，电源测试会实际改变设备设置。

安装后可逐文件比较发布包：

```sh
python3 tools/verify_installed.py dist/oh-my-panasonic_0.20.2_amd64.deb
```

## 发布

只发布通过检查的 Git 提交及 `dist/` 中本版本的指定包；不要归档整个工作区。用 `git archive` 导出源码，附带 SHA-256 校验文件。检查源码归档和 deb 内不存在日志、研究文件、个人设置、模型或凭据。

首次公开发布定位为 CF-SV7 优先的测试版。发布到托管平台后应启用私有漏洞报告，并在仓库说明中设置真实的支持入口。
