# Contributing

Project author and maintainer: [AkaseToshiyuki](https://github.com/AkaseToshiyuki).

Please include the model family, BIOS version, kernel version and the affected component. Remove hostnames, IP/MAC addresses, serial numbers, Wi-Fi names, credentials and biometric data from reports.

Run `PYTHONPATH=src python3 -m unittest discover -s tests -v`. WheelPad changes also require `(umask 077; cd vendor/letsnote-wheelpad; cargo test --locked)`. UI changes should include renders at laptop and wider window sizes.

Keep privileged operations narrowly scoped and reversible. Do not enable EC writes on a new model based solely on a similar port layout. Require documented commands, bounded operations, readback and restoration. Preserve stricter external limits, user settings and authentication fallbacks.

Record genuine hardware limitations in documentation; do not substitute percentages, negotiated limits or model-based estimates for unavailable sensors. UI text should focus on values, controls and actionable errors.

The default for hardware validators is help; actual actions require `--run`. Never include local credentials, firmware dumps, compiled models or enrollment data in a contribution.

## 工程原则

- 产品显示名称统一为 Oh My Panasonic；旧包名、配置路径和协议标识仅用于兼容。
- 界面只展示状态、数值、单位、操作和必要错误。不要加入开发过程、技术路线、重复定义或强调性说明。
- 控件随可用宽度布局；系统电源按钮只在状态栏首页显示，隐藏图标入口保留在主面板。
- 高权限操作限定设备和协议，保留热保护、密码回退、用户设置及可恢复路径。
- 节能使用真实硬件电源接口；请求值、规格和测量值必须区分，不以带宽限制代替电源管理。
- 硬件验证工具默认显示帮助，显式 --run 后才操作，并在结束时恢复设置。
- 临时构建和诊断使用独立目录。不得在用户主目录堆放副本，或将个人数据、专有文件、模型及开发记录纳入发布物。
- 修改后完成相关回归与打包检查。卸载与清除配置分开，升级保留已有选择。
