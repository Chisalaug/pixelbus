# pixelbus
把没有 API 的图形界面变成可订阅的语义事件流。YAML 定义区域与像素判据 → appear/disappear/change 事件 → SSE / webhook / 按键 / MCP。Windows 实测 1080p 三区域 15fps 占单核 &lt; 5%，内存约 50MB。给你的老系统装个门铃：盯住屏幕上几个小区域，状态一变就发 SSE 事件。不录屏、不做全量 OCR、不注入页面、不挂调试器。YAML 写判据，probe 实时打分调阈值，动作/按键/MCP 都能接。
