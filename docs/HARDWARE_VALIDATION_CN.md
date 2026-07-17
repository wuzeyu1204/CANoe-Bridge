# CANoe + Vector Virtual CAN + ZLG + ECU 真机验证

## 前置检查

1. Vector Hardware Config 中创建应用 `ZLG_CANOE_BRIDGE`，应用通道映射到 CANoe 使用的同一 Virtual CAN 通道。
2. CANoe 使用 Real Bus 模式并拥有该虚拟通道的 init access；配置中的 `vector.channel_owner` 保持 `canoe`。
3. 确认 ZLG 设备类型、索引、通道、仲裁/数据波特率、ISO CAN FD、终端电阻与 ECU 一致；关闭 ZXDoc/ZCANPRO。
4. 启动日志必须显示 `Init access requested: No`、请求/授予掩码均为 `0x0`，且 ZLG 初始化参数正确。

## 场景 A：CANoe 先启动

1. 关闭桥接，启动 CANoe Measurement，确认无 `No init access`。
2. 启动桥接，确认 Vector 以 `Shared RX/TX` 激活，CANoe Measurement 不受影响。

## 场景 B：桥接先启动

1. 先启动桥接并确认请求权限掩码为 0。
2. 再启动 CANoe Measurement，确认 CANoe 获得 init access 且桥接仍可收发。

## 场景 C：单帧与回环

1. CANoe 发送唯一计数值的一帧，ECU/总线分析仪确认只收到一次。
2. ECU 回应一帧，CANoe Trace 确认只收到一次。
3. 连续发送内容完全相同的周期帧，确认每个周期都被保留，状态中的 `loop_filtered` 不异常增长。

## 场景 D：UDS / ISO-TP

依次执行单帧请求、多帧请求和多帧响应；检查 First Frame、Flow Control、Consecutive Frame 序号、STmin 和最终有效载荷，确认 `dropped` 与 `queue_overflow` 为 0。

## 场景 E：CAN FD + BRS

发送 DLC 9~15（12/16/20/24/32/48/64 字节）帧，覆盖标准/扩展 ID、BRS 与接收侧 ESI；在 CANoe Trace 和 ECU 侧逐项核对 ID、DLC、长度、数据及标志。

## 重启与故障恢复

在 CANoe Measurement 运行期间重启桥接；再短暂拔除/恢复 ZLG 或制造通道错误，确认日志出现指数退避重连、恢复后双向通信正常且没有残留工作线程。

> 自动化测试使用 DLL mock 和内存适配器，不能替代上述真机总线与电气层验证。
