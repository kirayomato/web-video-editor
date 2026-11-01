# 网页视频剪辑器 — 社区调研与方案设计

> 状态：设计阶段（未开始编码） | 日期：2026-11-01

---

## 一、社区成熟产品调研

### 1.1 四条技术路线对比

| 路线 | 原理 | 性能（1080p H.264） | 文件限制 | 浏览器兼容 | 许可证注意 | 代表 |
|---|---|---|---|---|---|---|
| **A. ffmpeg.wasm（纯前端）** | FFmpeg 编译为 WASM，浏览器内软件编解码 | 慢，约 25fps，CPU 打满，无 GPU | 约 2GB（输入+输出都进 WASM 内存） | Chrome 92+ / FF 79+ / Safari 15.2+ | `@ffmpeg/core` 为 **GPL-2.0**（含 x264），分发到浏览器有开源义务 | ffmpeg-webCLI、tnfe/WebVideoEditor |
| **B. WebCodecs + mediabunny（纯前端，新一代）** | 浏览器硬件编解码器 + TS 容器解析/封装 | 快，约 200fps（约为 A 的 8 倍），内存 <500MB | 无硬上限（分片读文件 + OPFS 流式写） | Chrome/Edge 94+、Safari 16.4+（音频 26）、FF 130+；Firefox Android 不支持；FF 不能编码 AAC | mediabunny 为 MPL-2.0（文件级传染，可作依赖） | OpenReel、WebAV、Tuneshot、Omniclip |
| **C. Canvas + MediaRecorder（纯前端，最简）** | 播放时逐帧画到 canvas，实时录制 | 实时编码（1 分钟视频要录 1 分钟） | 受内存约束 | 兼容性最好 | 无特殊 | client_side_video_editor |
| **D. 本地/服务端原生 ffmpeg** | 后端调用 ffmpeg CLI | 最快（可硬件加速），无格式顾虑 | 无限制 | 全部 | 服务端运行不受 GPL 分发约束 | LosslessCut（桌面）、各类后端剪辑服务 |

关键事实（2026-10 验证）：

1. **ffmpeg.wasm 维护放缓**：最后一次 release 是 2025-01，多线程相关 PR（#987 等）至今未合，issue 区的多线程挂起问题未解。多线程构建还需要全站 COOP/COEP 隔离头（SharedArrayBuffer），会波及第三方嵌入。
2. **WebCodecs 路线是当前主流方向**：硬件编解码、流式输出（OPFS StreamTarget）、无 2GB 天花板。多个独立项目（Tuneshot 的 4K 编辑器、shot-tracer 的 ADR、ryusei.io 的实测）都得出了放弃 ffmpeg.wasm 的结论。但 WebCodecs 只给编解码，**滤镜/特效要自己用 Canvas/WebGPU 写**，没有 ffmpeg 的 filter graph。
3. **无损剪辑的本质是 `-c copy`**（LosslessCut 的核心）：不解码不重编码，秒级完成、零画质损失。唯一限制是**切点必须对齐关键帧**（I-frame）；`-ss` 放 `-i` 前面 = 对齐最近关键帧的快切，放在后面 = 逐帧精确 seek 但要重编码。LosslessCut 的 Smart Cut 思路：非关键帧切割时只对该 GOP 一小段重编码再缝合。
4. **大文件必须流式**：输入输出全部进内存的写法（ffmpeg.wasm 默认、naive 的 chunk 数组攒 Blob）在 4K/长视频下必崩，要走分片读 + OPFS 分块写。
5. **浏览器端"播放"没有争议**：HTML5 `<video>` 原生播放本地 Blob 即可，逐帧（`currentTime += 1/fps`）、倍速（`playbackRate`）、波形（peaks.js/wavesurfer.js）都是成熟方案。

### 1.2 代表性项目盘点

**网页端开源：**

| 项目 | Star | 技术栈 | 特点 | 协议 |
|---|---|---|---|---|
| **OpenCut**（原 AppCut） | 高活跃 | Next.js + React，客户端处理 | 开源 CapCut 替代，多轨时间线、AI 一键剪辑，社区最活跃，部署简单 | 开源 |
| **OpenReel Video**（Augani） | ~3.6k | React + TS + WebCodecs + WebGPU | 功能最全：本地 Whisper 字幕、专业调色、3D 变换，无水印 | MIT |
| **WebAV**（国产） | ~2.1k | WebCodecs 编辑 SDK | SDK 形态可嵌入产品，文档好 | MIT |
| **ffmpeg-webCLI** | ~1.4k | ffmpeg.wasm | 30+ 操作、批量处理、PWA 离线、压缩 CRF 实时预估 | GPL-3.0 |
| **tnfe/WebVideoEditor** | — | Vue3 + TS + ffmpeg.wasm | 多轨时间线、滤镜、文字、转场 | Apache-2.0 |
| **react/vue-video-editor**（openvideodev） | ~1.7k | React / Vue3 多轨 | 现成时间线组件可二开 | 开源 |
| **Omniclip 2.0** | — | WebCodecs | 最轻量，适合二开（2.0 开发中） | 开源 |
| **Tuneshot** | — | WebCodecs + mediabunny + OPFS | 4K 纯静态站、永不上传、流式导出（工程实践参考价值高） | 开源 |
| **Kimu** | — | 浏览器 + 云 | AI copilot、多人实时协作、云端工程 | 开源 |
| **client_side_video_editor** | — | Canvas + MediaRecorder | 极简 trim+crop，教学级 | Apache-2.0 |

**桌面端参照（ffmpeg 系）：**

| 项目 | Star | 特点 |
|---|---|---|
| **LosslessCut** | 30k+ | **无损剪辑标杆**：`-c copy` 秒级裁剪、关键帧跳转、Smart Cut、合并、提取音轨、批量导出。UX（入出点快捷键、时间线缩放）值得直接借鉴 |
| Shotcut / Kdenlive / OpenShot | — | 完整桌面 NLE，FFmpeg 驱动，功能对标 Premiere（超出"简单剪辑"范畴） |

**商业 Web（体验标杆，均云端处理）：** Clipchamp、Kapwing、VEED、Canva 视频、剪映网页版。

### 1.3 调研结论

- "网页播放 + 简单剪辑"这个需求，社区已有大量零件，但**"本地 ffmpeg 驱动的 LosslessCut 网页版"这个定位仍很清晰**：开源项目要么走纯前端 WASM（慢、2GB 上限），要么是功能膨胀的全功能剪辑器（OpenCut/OpenReel），要么是玩具级 demo。以原生 ffmpeg 为引擎、聚焦无损裁剪/拼接的轻量 Web 工具仍有价值。
- 引擎选择上：**服务端/本地跑原生 ffmpeg（路线 D）与需求"使用 ffmpeg 剪辑"最贴合**；纯前端可选 WebCodecs 路线作为未来演进，ffmpeg.wasm 不推荐做主引擎（性能、内存、GPL 三重问题）。

---

## 二、需求理解与 MVP 范围

**目标**：一个在浏览器里播放视频、并用 ffmpeg 完成简单剪辑的工具。

**MVP 功能：**

1. **播放**：加载本地视频（拖拽/选择），播放/暂停、逐帧步进、倍速、音量、时间显示
2. **时间线**：波形 + 缩略图预览，入点/出点标记（I/O 快捷键），缩放
3. **剪辑**（ffmpeg 驱动）：
   - 截取片段（两种模式：**无损快切**（关键帧对齐，秒级）/ **精确切**（重编码，帧精确））
   - 删除中间段（多段保留区间导出）
   - 多片段拼接（concat demuxer）
   - 提取/去除音轨
   - 导出截帧（单帧图片）
   - 转格式 / 压缩（CRF 参数）
4. **下载**：剪辑产物一键下载；当前打开的原视频也可直接下载到本地
5. **任务系统**：ffmpeg 命令预览（透明可复制）、执行进度、产物下载
5. **信息面板**：ffprobe 元数据（编码、分辨率、帧率、关键帧间隔、音轨）

**MVP 明确不做**：多轨特效、转场、调色、字幕、AI 能力、时间线多轨编辑。

---

## 三、架构方案

### 3.1 三个候选方案

| | 方案 A：纯前端 ffmpeg.wasm | **方案 B：本地服务 + Web UI（推荐）** | 方案 C：纯前端 WebCodecs |
|---|---|---|---|
| 形态 | 静态网站，零部署 | 本机起一个 HTTP 服务，浏览器访问 localhost | 静态网站 |
| ffmpeg | ffmpeg.wasm | **原生 ffmpeg CLI** | 无 ffmpeg（WebCodecs + mediabunny） |
| 性能/大小 | 慢、2GB 上限 | **快、无限制、无损 stream copy** | 快、无上限 |
| 隐私 | 最好 | 好（数据不出本机） | 最好 |
| 实现成本 | 中（WASM 内存/多线程坑多） | **低（ffmpeg 命令即产品核心，逻辑直观）** | 高（编解码/封装/流控都要自己管） |
| 缺点 | GPL、慢、2GB | 需要本机装 ffmpeg（或 ffmpeg-static 自带） | 浏览器兼容门槛、无滤镜链、FF 无 AAC 编码 |
| 适合 | 公开网站、隐私敏感 | **自用/内网工具，"用 ffmpeg 剪辑"的字面需求** | 公开网站且要极致体验 |

**推荐方案 B**，理由：

1. 用户需求明确说"使用 ffmpeg 进行简单剪辑"——原生 ffmpeg CLI 是能力、性能、兼容性的最优解，且无损 `stream copy` 只有原生 ffmpeg 能真正做到位。
2. "网页"的价值在**交互**（播放器、时间线、可视化标记），不必强求"计算也在网页里跑"。
3. 后端只做"收命令、跑 ffmpeg、回进度"，代码量小且直观；未来若要公开部署，可平滑替换执行引擎（方案 C）或改造成桌面壳（Tauri）。

### 3.2 推荐架构（方案 B）

```
浏览器（ArtPlayer + 原生 JS，无构建步骤）
├── 播放器：ArtPlayer（Blob/Range 流式播放），逐帧/倍速/音量
├── 时间线：缩略图条（sprite）+ 波形（showwavespic）+ 入出点标记 + 播放头
├── 剪辑面板：操作选择、无损/精确模式、参数、命令预览
└── 任务列表：进度（SSE）、产物下载
        │  HTTP（REST + SSE）
        ▼
本地服务（Python http.server）
├── /api/probe        ffprobe 元数据 + 关键帧列表
├── /api/tasks        创建剪辑任务（trim / concat / audio / frame / transcode）
├── /api/tasks/:id/events   SSE 推送 ffmpeg -progress
├── /api/tasks/:id/download 产物下载
└── 任务队列（串行执行，spawn ffmpeg/ffprobe 子进程）
        │
        ▼
ffmpeg / ffprobe（系统安装 或 ffmpeg-static 自带）
        │
        ▼
输出目录 → 浏览器下载
```

**核心 ffmpeg 命令设计：**

```bash
# 无损截取（关键帧对齐，-ss 在 -i 前，秒级完成）
# 注意：无损输出区间 = [对齐后的关键帧入点, 出点]，可能略长于所选区间（LosslessCut 同款语义）
ffmpeg -ss {start} -i input.mp4 -t {dur} -c copy -avoid_negative_ts make_zero out.mp4

# 精确截取（帧精确；视频重编码 + 音频 stream copy）
# 输入侧粗跳 2s（快）+ 输出侧 -ss/-t 精修（准）。
# 注意：不能只用输入侧 -ss——音频 copy 无法解码丢包，mov demuxer seek 会带约 1s
# 负时间戳前摇进输出（音频内容错位并污染后续拼接）；输出侧 -ss/-t 按时间戳丢弃才精确。
ffmpeg -ss {max(0,start-2)} -i input.mp4 -ss {start-max(0,start-2)} -t {dur} -c:v libx264 -crf 18 -preset veryfast -c:a copy out.mp4

# 拼接（concat demuxer，要求同编码参数；不满足时统一转码拼接）
ffmpeg -f concat -safe 0 -i list.txt -c copy out.mp4

# 提取音频 / 去除音轨
ffmpeg -i in.mp4 -vn -c:a copy out.m4a
ffmpeg -i in.mp4 -c:v copy -an out.mp4

# 截帧
ffmpeg -ss {t} -i in.mp4 -frames:v 1 -q:v 2 out.jpg

# 转格式/压缩
# 转格式/压缩（视频重编码；音频一律 stream copy）
ffmpeg -i in.mp4 -c:v libx264 -crf 23 -preset medium -c:a copy out.mp4
```

**关键工程点：**

- **无损/精确双模式**：默认无损快切，UI 上提示"切点将对齐最近关键帧（{time}）"，提供"精确切割"开关（借鉴 LosslessCut）。
- **进度**：spawn 时加 `-progress pipe:1`，解析 `out_time_ms` 映射百分比，经 SSE 推前端。
- **命令透明**：所有任务展示完整 ffmpeg 命令，可复制到终端复跑——既是调试利器也符合"用 ffmpeg 剪辑"的定位。
- **缩略图/波形**：一次性预处理 `ffmpeg -i in -vf fps=1,scale=160:-1 thumb_%04d.jpg` + 音频 PCM 提取喂 peaks.js，产物缓存。

### 3.3 技术栈

| 层 | 选型 | 备注 |
|---|---|---|
| 播放器 | **ArtPlayer 5.x**（MIT） | 自定义控制/进度条高亮标记/倍速/逐帧均支持，vendor 静态引入 |
| 前端 | 原生 JS + CSS（无构建步骤） | 零依赖，`node server.js` 即可运行，无需 npm install |
| 波形/缩略图 | ffmpeg 生成（showwavespic / tile sprite） | 不依赖第三方前端库 |
| 后端 | **Python 3 标准库**（http.server + subprocess，零第三方依赖） | `python server/app.py` 直接运行，无需 pip install |
| ffmpeg 调度 | 手写 `child_process.spawn`（参数数组，不经 shell） | 参数完全可控，便于命令预览 |
| ffmpeg 来源 | 系统 PATH 优先（本机 7.1.1 gyan full build），`FFMPEG_PATH`/`FFPROBE_PATH` 可覆盖 | 免安装体验 |
| 可选打包 | Tauri / Electron | 后续变桌面应用，不在 MVP |

### 3.4 里程碑

| 阶段 | 内容 |
|---|---|
| **M1 骨架** | 本地文件加载 + 播放器（逐帧/倍速）+ ffprobe 信息面板 |
| **M2 剪辑核心** | 入出点、无损截取、精确截取、任务队列 + 进度 + 下载、命令预览 |
| **M3 完整 MVP** | 删除中间段、拼接、音频提取/去除、截帧、转格式；波形 + 缩略图时间线 |
| **M4 打磨** | 快捷键体系、批量任务、ffmpeg 路径设置、错误处理与格式兼容提示 |

---

## 四、已锁定决策（2026-11-01）

1. **产品形态**：✅ 本地服务 + Web UI（方案 B）
2. **播放器**：✅ ArtPlayer 5.x
3. **网页下载**：✅ 支持剪辑产物下载 + 原视频下载
4. **ffmpeg 来源**：系统 PATH 优先（本机已有 7.1.1），环境变量可覆盖
5. **实现约束**：零第三方依赖、无构建步骤（免安装直接 `python server/app.py`）；代码开发过程中按主题自动 git 提交；后端使用 Python（用户偏好）
6. **MVP 功能**：按第二节列表执行

### 实现分期（更新）

| 阶段 | 内容 |
|---|---|
| **M1 骨架** | 本地文件加载（路径/拖拽上传）+ ArtPlayer 播放（逐帧/倍速）+ ffprobe 信息面板 |
| **M2 剪辑核心** | 入出点、无损截取、精确截取、任务队列 + SSE 进度 + 下载、命令预览 |
| **M3 完整 MVP** | 删除中间段、拼接、音频提取/去除、截帧、转格式；波形 + 缩略图时间线 |
| **M4 打磨** | 快捷键体系、批量任务、ffmpeg 路径设置、错误处理与格式兼容提示 |
