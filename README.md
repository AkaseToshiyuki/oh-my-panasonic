# Oh My Panasonic

Panasonic Let's Note 的 Ubuntu 控制中心，提供电池保养、圆形触控板、电源策略、硬件监控和 GNOME 常驻菜单。

**0.20.6 · 测试版 · Ubuntu 26.04 amd64 / GNOME 50**

主要支持 **CF-SV7 / CFSV7-3L、BIOS V3.11L22**。其他机型可使用系统提供的通用控制；机型专用硬件功能仅在白名单设备上启用。本项目由社区独立开发，与 Panasonic 官方无隶属关系。

## 功能

- 卡片式控制面板与常驻菜单，管理 Wi-Fi、蓝牙、音量、显示亮度、分辨率和缩放。
- 充电上限、立刻充满、自动电池保养和续航估计。
- 圆形触控板滚动，保留多指手势。
- CPU 功率预设、温度保护、风扇曲线与 SSD 电源状态联动。
- 光驱省电，以及驱动器、光盘类型和容量信息。
- 可选红外人脸登录、锁屏解锁、主动确认的本地 sudo 和桌面授权。

## 安装

下载同一版本的主包和兼容包，在下载目录运行：

```sh
sudo apt install ./oh-my-panasonic_0.20.6_amd64.deb ./panasonic-on-ubuntu_0.20.6_all.deb
pou gui
```

CF-SV7 的 EC 电池、风扇和光驱控制还需要可选硬件模块包：

```sh
sudo apt install dkms linux-headers-$(uname -r)
sudo apt install ./panasonic-on-ubuntu-firmware-dkms_0.7.2_all.deb
```

启用 Secure Boot 时，请按 Ubuntu 提示完成模块签名密钥注册。模块只在匹配的设备上提供控制。状态栏扩展安装后需要重新登录 GNOME。

人脸识别及核显加速为独立可选组件，需要兼容的 Facelock、红外摄像头和自行获得使用许可的模型。发布包不包含模型、录入数据或已启用的认证配置。

## 使用与维护

- [使用指南：设置、升级、卸载和排障](docs/usage.zh-CN.md)
- [硬件兼容性](docs/hardware.zh-CN.md)
- [构建指南](docs/build.zh-CN.md)
- [版本变化](CHANGELOG.md)
- [安全与隐私](SECURITY.md)
- [参与开发](CONTRIBUTING.md)

## 许可证

原创应用代码与图标采用 [MIT](LICENSE)，内核模块采用 [GPL-2.0-only](kernel/COPYING)。第三方源码、版本和授权见 [依赖说明](vendor/README.md)。

人脸模型与程序代码的许可不同。InsightFace 公布的预训练模型限非商业研究使用，其他用途需取得相应授权；本项目的代码许可证不授予模型使用权。[上游许可说明](https://github.com/deepinsight/insightface#license)
