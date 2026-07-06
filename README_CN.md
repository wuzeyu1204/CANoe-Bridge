# ZLG-CANoe Python CANFD Bridge

这是一个 **Python 版本的周立功 CANFD 硬件 ↔ Vector CANoe 虚拟通道桥接程序模板**。

目标链路：

```text
CANoe / CANalyzer
  CANFD Channel = Vector Virtual CAN
        ↓
Vector XL Driver / vxlapi64.dll
        ↓
Python Bridge
        ↓
ZLG ZCAN / zlgcan.dll
        ↓
周立功 CANFD 硬件
        ↓
真实 ECU / ACU
```

## 重要说明

1. 这不是把周立功硬件伪装成 Vector VN 硬件。
2. CANoe 侧仍然使用 Vector Virtual CAN。
3. Python 程序负责在 Vector Virtual CAN 和 ZLG CANFD 硬件之间透明转发 CAN/CANFD 帧。
4. 本模板使用 `ctypes` 调用 `vxlapi64.dll` 和 `zlgcan.dll`。
5. 不同版本的 Vector XL API / ZLG ZCAN SDK 结构体可能略有差异。如果运行时报结构体或 API 错误，需要对照你本机 SDK demo 微调 `vector_xl.py` 和 `zlg_zcan.py`。

---

## 目录结构

```text
ZLG_CANoe_Bridge_Python_CANFD/
├─ README_CN.md
├─ requirements.txt
├─ config/
│  ├─ bridge_config.json          # 真机配置
│  └─ bridge_config_mock.json     # 离线模拟配置
├─ docs/
│  └─ CANoe配置说明.md
├─ zlg_canoe_bridge/
│  ├─ __main__.py                 # 程序入口
│  ├─ bridge.py                   # 双向桥接核心
│  ├─ frame.py                    # CANFD统一帧和DLC映射
│  ├─ logger.py
│  └─ adapters/
│     ├─ vector_xl.py             # Vector XL Driver适配
│     ├─ zlg_zcan.py              # 周立功 ZCAN 适配
│     └─ mock.py                  # 离线模拟适配
└─ tools/
```

---

## 环境要求

建议环境：

```text
Windows 10 / Windows 11
Python 3.10+，64 位
CANoe / CANalyzer 已安装
Vector Driver 已安装
周立功 ZCAN 驱动和二次开发 DLL 已安装
```

检查 Python 是否为 64 位：

```bat
python -c "import platform; print(platform.architecture())"
```

输出应为：

```text
('64bit', 'WindowsPE')
```

---

## 需要准备的 DLL

### Vector 侧

通常 `vxlapi64.dll` 已经在系统 PATH 中。也可以在 CANoe / Vector Driver 安装目录中找到。

配置位置：

```json
"vector": {
  "dllPath": "vxlapi64.dll"
}
```

如果 DLL 不在 PATH，可以写绝对路径，例如：

```json
"dllPath": "C:/Windows/System32/vxlapi64.dll"
```

### 周立功侧

需要 `zlgcan.dll` 或你设备 SDK 对应的 DLL。

配置位置：

```json
"zlg": {
  "dllPath": "zlgcan.dll"
}
```

如果你的 SDK 叫 `zcan.dll`，就改成：

```json
"dllPath": "zcan.dll"
```

---

## CANoe 侧配置

你之前遇到的报错：

```text
Measurement start was aborted after check of VN800/VN890 channel assignment.
```

说明当前 CANoe 工程仍然绑定了 VN800/VN890 真实 Vector 硬件。

桥接方案下，CANoe 侧要改成：

```text
CANoe CAN1 → Vector Virtual CAN 1
```

不要是：

```text
CANoe CAN1 → VN800 / VN890 / VN1630 / VN1640
```

然后在 **Vector Hardware Config** 里配置一个应用：

```text
Application Name: ZLG_CANOE_BRIDGE
Application Channel: 0
Hardware Channel: Virtual CAN 1
```

`config/bridge_config.json` 中对应：

```json
"vector": {
  "applicationName": "ZLG_CANOE_BRIDGE",
  "applicationChannel": 0
}
```

---

## 运行离线模拟

先验证 Python 工程本身能启动：

```bat
cd ZLG_CANoe_Bridge_Python_CANFD
python -m zlg_canoe_bridge config\bridge_config_mock.json --mock-inject
```

预期日志类似：

```text
ZLG-CANoe Python CANFD Bridge
Opening Vector side...
Opening ZLG side...
Bridge started. Press Ctrl+C to stop.
[CANoe -> ZLG] CANFD/STD/BRS ID=0x7F1 DLC=8 LEN=8 DATA=[02 10 03 00 00 00 00 00]
[ZLG -> CANoe] CANFD/STD/BRS ID=0x7F9 DLC=8 LEN=8 DATA=[02 50 03 00 00 00 00 00]
```

---

## 运行真机桥接

1. CANoe 工程设置为 Vector Virtual CAN。
2. Vector Hardware Config 中配置 `ZLG_CANOE_BRIDGE`。
3. 插上周立功 CANFD 硬件。
4. 修改 `config/bridge_config.json` 里的设备类型、通道和 DLL 路径。
5. 运行：

```bat
python -m zlg_canoe_bridge config\bridge_config.json
```

CANFD 常用配置：

```json
"canfd": {
  "arbitrationBitrate": 500000,
  "dataBitrate": 2000000,
  "brs": true,
  "isoCanFd": true
}
```

---

## 第一阶段验证目标

CANoe Diagnostic Console / CAPL 发送：

```text
ID: 0x7F1
DATA: 02 10 03 00 00 00 00 00
```

真实 ECU 回应：

```text
ID: 0x7F9
DATA: 02 50 03 00 00 00 00 00
```

Python Bridge 日志应显示：

```text
[CANoe -> ZLG] CANFD/STD/BRS ID=0x7F1 DLC=8 LEN=8 DATA=[02 10 03 00 00 00 00 00]
[ZLG -> CANoe] CANFD/STD/BRS ID=0x7F9 DLC=8 LEN=8 DATA=[02 50 03 00 00 00 00 00]
```

CANoe Trace 里能看到 ECU 响应，说明链路打通。

---

## 关键配置说明

### Vector 配置

```json
"vector": {
  "dllPath": "vxlapi64.dll",
  "applicationName": "ZLG_CANOE_BRIDGE",
  "applicationChannel": 0,
  "receiveTxOk": false
}
```

`receiveTxOk=false` 表示不接收 Vector 侧 TX Confirmation，减少回环风险。

### ZLG 配置

```json
"zlg": {
  "dllPath": "zlgcan.dll",
  "deviceType": 41,
  "deviceIndex": 0,
  "channelIndex": 0,
  "enableTermination": false,
  "useSetValue": true
}
```

`deviceType` 必须根据你的 ZLG SDK 头文件修改。不同设备值不同。

常见要查的位置：

```text
zcan.h
controlcan.h
官方 demo 的 OpenDevice 示例
```

---

## 常见问题

### 1. CANoe 还是报 VN800/VN890

说明工程里还有真实硬件绑定。先不要启动 Python Bridge，先让 CANoe 单独使用 Virtual CAN 能 Start Measurement。

### 2. Python 报找不到 vxlapi64.dll

把 `vxlapi64.dll` 放到系统 PATH，或者在配置中写绝对路径。

### 3. Python 报找不到 zlgcan.dll

把 ZLG DLL 放到当前目录、系统 PATH，或者在配置中写绝对路径。

### 4. ZCAN_InitCAN 失败

通常是 `ZCAN_CHANNEL_INIT_CONFIG` 结构体和你的 SDK 不一致，或者 `deviceType` 不对。

处理方式：

```text
1. 打开周立功官方 Python/C/C++ demo
2. 找到 CANFD 初始化结构体
3. 对照修改 zlg_canoe_bridge/adapters/zlg_zcan.py
```

### 5. CANFD 报文发不通

重点检查三边配置是否一致：

```text
CANoe：仲裁段 500K、数据段 2M、BRS、ISO CAN FD
Bridge：bridge_config.json
ZLG：ZCAN 初始化参数
ECU：实际 CANFD 配置
```

---

## 建议开发顺序

第一步只做普通 CANFD 单通道透明转发：

```text
CANoe 0x7F1 → ZLG → ECU
ECU 0x7F9 → ZLG → CANoe
```

第二步再加：

```text
扩展帧
多通道
过滤规则
日志保存 ASC/CSV
GUI 界面
自动重连
BusOff 状态检测
```
