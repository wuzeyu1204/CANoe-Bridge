# Bridge Project Rules

## Vector Virtual CAN ownership

- 默认由CANoe拥有Vector Virtual CAN通道的init access。
- 桥接工具默认只对Vector侧进行收发，不配置波特率或CAN FD参数。
- 共享模式下xlOpenPort的permissionMask输入必须为0。
- 不得直接修改site-packages中的python-can。
- ZLG物理通道始终由桥接工具初始化。
- 必须保留CAN FD、BRS、ESI、DLC和0～64字节数据。
- 必须使用驱动方向信息避免双向转发回环。

## Validation

- 修改后运行单元测试。
- 输出修改文件和测试结果。
- 无真实硬件验证时必须明确说明。
