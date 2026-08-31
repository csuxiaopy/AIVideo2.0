# AI 巡检系统前端科技化重构报告

- 项目：无锡广电 AI 巡检系统（YOLO + VLM 智能视频监控）
- 范围：**仅前端 UI/UX 重构，业务逻辑零破坏**
- 完成日期：2026-08-31
- Git 基线：`f672296`（checkpoint，重构前稳定快照）→ `3180830`（重构提交）
- 重构提交：`3180830 feat(ui): 前端科技化重构 - 深蓝 AI 监控 HUD 主题体系`（9 files，+1333 / −123）

---

## 1. 技术栈

| 项 | 版本 / 说明 |
| --- | --- |
| 前端框架 | Vue 3.5.13 + Vite 6.0.5 + TypeScript 5.7.2 |
| 包管理器 | pnpm 10.12.1 |
| 路由方案 | 无 vue-router，沿用 hash + Tab 切换（未改动） |
| UI 组件库 | 无（全部自研，未引入） |
| 图标库 | 无第三方，新建统一 SVG 线性图标库 `icons.ts` |
| 后端 | FastAPI + SQLAlchemy + Alembic（零改动） |
| 存储 | SQLite（开发）/ PostgreSQL 17（生产），由 `DATABASE_URL` 切换（零改动） |

---

## 2. 重构前问题分析

| # | 问题 | 影响 |
| --- | --- | --- |
| 1 | 传统 SaaS 后台风格，浅色蓝白配色 | 与"AI 智慧监控中心"定位不匹配，缺少科技感与专业性 |
| 2 | 全局硬编码颜色分散，且存在"暗色基础层 + 浅蓝覆盖层"两层样式冲突 | 主题维护困难，改色易出错 |
| 3 | 图标使用 Unicode 字符（◷ ▣ ♨ 等） | 渲染不一致、不专业、无统一风格 |
| 4 | 使用原生 `confirm` / `prompt` 弹窗 | 与深色主题割裂，体验差 |
| 5 | 页面存在 `body { min-width: 1180px }` | 1366×768 分辨率下产生横向滚动条 |
| 6 | 无统一 Design Token 体系 | 无法快速换肤 / 对齐品牌色 |
| 7 | 无 favicon（404） | 浏览器标签页报错且无品牌标识 |
| 8 | 摄像头卡片信息密度低 | 96 路场景下列表冗长，性能与浏览效率差 |

---

## 3. 总体设计方案

**视觉定位**：现代智慧安防 + AI 视频分析 + 数字孪生监控中心 + HUD 科技大屏。

**明确排除**：游戏 UI、赛博朋克、满屏霓虹、无意义动效。

核心设计语言：

- **深蓝底色 + 科技青蓝点缀**：`#020814` 深空底 + `#008CFF` 主蓝 + `#00E5FF` 青色高亮，克制使用高亮色
- **HUD 取景框**：摄像头面板四角取景框（多重 linear-gradient 拼角），营造"监控镜头"语义
- **网格底纹**：全局背景细网格 + 渐隐遮罩（mask-image），增加科技纵深
- **字体**：Rajdhani（HUD 数字/英文标签）+ DM Sans + Noto Sans SC 兜底
- **动效克制**：仅 hover 过渡、状态脉冲点、扫描线，时长 ≤200ms，禁止大范围动画

---

## 4. 颜色 Design Token 规格

定义于 `frontend/src/style.css` `:root`，全部颜色统一走 Token，**硬编码颜色已清除**：

| Token | 值 | 用途 |
| --- | --- | --- |
| `--bg-base` | `#020814` | 页面最深底色 |
| `--bg-primary` | `#031326` | 一级面板底 |
| `--bg-panel` | `rgba(5,27,51,.88)` | 浮层/卡片 |
| `--bg-input` | `rgba(0,30,60,.6)` | 输入框 |
| `--primary` | `#008CFF` | 主操作/强调 |
| `--cyan` | `#00E5FF` | 高亮/取景框/焦点 |
| `--success` | `#00E6A8` | 在线/成功 |
| `--warning` | `#FFB020` | 警告 |
| `--danger` | `#FF4D5A` | 严重/错误 |
| `--text-primary` | `#EAF6FF` | 主文字 |
| `--text-secondary` | `#7A9DBF` | 次级文字 |
| `--text-muted` | `#4A6A8A` | 弱化文字 |
| `--border-highlight` | `rgba(0,229,255,.75)` | HUD 高亮描边 |
| `--font-hud` | `'Rajdhani','DM Sans','Noto Sans SC',sans-serif` | HUD 字体族 |

---

## 5. 统一组件体系

| 组件 | 实现 | 说明 |
| --- | --- | --- |
| `TechIcon.vue`（新增） | 统一 SVG 线性图标组件 | 24×24 viewBox，currentColor 描边，30+ 图标 |
| `icons.ts`（新增） | 图标库数据源 | dashboard/video/flame/alert/bell/settings 等，Lucide 风格 |
| 统一确认对话框 | `dialogConfirm()`（App.vue 内 Promise 化） | 替换全部原生 `confirm`/`prompt`，含 input 模式（清理告警天数输入） |
| Toast | 全局深色 Toast 体系 | 成功/警告/错误三态 |
| Switch | 科技风 Toggle Switch | cyan 高亮 + 轨道过渡 |
| 徽章 | `sys-badge` / `severity-badge` / `status-dot` | 在线状态、级别、系统标签 |
| Data Table | 深色数据表 | 表头分隔线 + hover 高亮 |
| 空/加载/错误态 | 深色占位 + 脉冲指示器 + 重试按钮 | 全局统一 |

---

## 6. 文件清单

### 新增

| 文件 | 说明 |
| --- | --- |
| `frontend/src/icons.ts` | 统一 SVG 线性图标库 |
| `frontend/src/components/TechIcon.vue` | 图标渲染组件 |
| `frontend/public/favicon.svg` | 科技风 favicon（修复 404） |

### 重写

| 文件 | 说明 |
| --- | --- |
| `frontend/src/style.css` | Design Token + 全局主题 + HUD 视觉语言（消除双主题层冲突） |
| `frontend/src/App.vue` | 布局/Header/Sidebar/各页面/确认对话框系统（**业务逻辑保留**） |
| `frontend/index.html` | `color-scheme: dark`、`theme-color`、favicon 引用 |

### 修改

| 文件 | 说明 |
| --- | --- |
| `frontend/src/components/BatchCameraModal.vue` | 深色化 + BATCH IMPORT 英文标识 + TOTAL/VALID/INVALID 统计 |
| `frontend/src/components/EvidencePreview.vue` | 缩略图/lightbox 深色科技化 + HUD 角标 |

> 其余 `backend/`、`alembic/`、`run.py`、`Dockerfile*`、`compose*.yml`、`requirements*.txt`、`pyproject.toml` **零改动**。

---

## 7. 页面级改动明细

### 7.1 整体 Layout

- **Header（64~76px）**：左侧品牌区（江苏有线/无锡广电 AI 巡检系统 + 深蓝标识），右侧**实时时钟**（HH:MM:SS）、**系统状态点**（WebSocket 在线/离线）、全屏按钮（`toggleFullscreen`）、实时预览计数
- **Sidebar**：科技化导航项（SVG 图标 + 选中高亮条），tab 英文小标签（MONITORING CENTER 等）
- **Content**：网格底纹背景，卡片圆角 10px + 1px 蓝边框

### 7.2 Dashboard（视觉最强页面）

- **Metric HUD Card**：总摄像头/在线/告警/今日事件指标卡，大号 Rajdhani 数字 + 脉冲状态点 + 趋势角标
- **AI MONITORING GRID**：摄像头面板网格
  - HUD 四角取景框 + 扫描线
  - 左上：ONLINE/OFFLINE 状态徽章 + 实时时间戳
  - 右上：摄像头 ID（CAM-xx）
  - 底部：名称 + 场景标签 + AI ACTIVE 标签
  - hover 显示操作浮层（实时预览 / 分析 / 编辑）
- **告警实时流**：最近告警列表（级别徽章 + 时间 + 摄像头）

### 7.3 摄像头配置页

- 左：配置表单（基础字段 + 场景几何区）；右：**Compact Camera Row 列表**（快照缩略图 + ID + 名称 + 状态 + 场景徽章 + 操作）
- 列表使用 `content-visibility:auto` 优化 96 路渲染性能
- RTSP 地址以 `rtsp://user:****@host` 形式脱敏展示

### 7.4 批量导入 Modal

- 深色化 + 文件拖拽区 + 解析预览表格（行级有效性标记）
- 底部统计：TOTAL / VALID / INVALID

### 7.5 告警中心

- **Dark Data Table**：告警时间/摄像头/类型/级别/处理状态
- 级别徽章：NORMAL（青）/ WARNING（黄）/ CRITICAL（红），附状态点
- 删除/清空均走统一 `dialogConfirm`，清理天数走 input 模式

### 7.6 系统配置页

- Switch 科技化（VLM 启用、Webhook、Shadow 模式等）
- API Key 输入保持**掩码脱敏**（`sk-****xxxx`），提交不泄露明文
- 检测器状态卡（YOLO / 烟火 / VLM）带就绪状态点
- VLM 测试按钮带加载反馈（`testing` 状态）

---

## 8. 响应式适配

| 分辨率 | 适配策略 |
| --- | --- |
| 1920×1080 | 满配 4 列网格 |
| 1600×900 | 3~4 列自适应 |
| 1440×900 | 3 列 |
| 1366×768 | 2~3 列 + 紧凑间距，**无横向滚动** |

断点：`1350px / 1100px / 860px`。原 `body { min-width:1180px }` 已移除，自动化滚动检测 **7 项全部 OK**（见 §19）。

---

## 9. 性能优化

| 措施 | 说明 |
| --- | --- |
| 不并发播放 RTSP | 沿用快照策略（后端周期抓帧），仅"实时预览"按租约单路播放（preview/start/heartbeat/stop） |
| `content-visibility:auto` + `contain-intrinsic-size` | 96 路摄像头列表跳过屏外渲染 |
| 图标内联 SVG | 无额外网络请求 |
| 动效克制 | 仅 transform/opacity，无重排动画 |

---

## 10. 安全处理

- **RTSP 密码脱敏**：正则 `source.replace(/(\/\/[^/:@]+:)[^@]*(?=@)/, '$1****')`，列表/面板永不展示明文密码
- **API Key 掩码**：系统配置页仅显示掩码值
- 未引入任何新的第三方依赖（无供应链新增面）

---

## 11. 业务逻辑零破坏确认

| 检查项 | 结果 |
| --- | --- |
| 后端 API | **未修改**（No） |
| 后端 Python 代码 | **未修改**（No） |
| 数据库结构 / 数据 | **未修改**（No） |
| Alembic Migration | **未生成、未修改**（No） |
| Docker Compose / Dockerfile | **未修改**（No） |
| WebSocket 协议 | **未修改**（No） |
| YOLO/VLM/任务调度/FFmpeg 逻辑 | **未修改**（No） |
| 业务数据结构与接口契约 | **未修改**（No） |

前端重构仅为表现层替换：所有 API 调用、数据结构、事件订阅、交互流程保持原样。

---

## 12. 测试与验收结果

### 构建与类型

| 项 | 结果 |
| --- | --- |
| `vue-tsc` 类型检查 | ✅ 通过 |
| `vite build` 生产构建 | ✅ 通过（先清理 sandbox 拦截的旧 dist 后成功） |

### Playwright 自动化验收（playwright-core + Chromium）

| 检查项 | 结果 |
| --- | --- |
| 页面遍历（Dashboard/摄像头/告警/人流/系统配置） | ✅ 5/5 |
| 编辑弹窗打开 | ✅ |
| 批量导入弹窗打开 | ✅ |
| 横向滚动检测（1920×1366 × 多页面，7 项） | ✅ 全部 OK |
| `pageerror` | ✅ 0 |
| `failedrequest` | ✅ 0 |
| `console` 错误 | ⚠️ 残留 1 条瞬时 503（见 §13） |

### 验收截图

9 张截图已生成：`data/ui_shots/01_dashboard_1920.png` ~ `09_cameras_1366.png`（覆盖 5 页面 × 2 分辨率 + 2 弹窗）。截图按规划存放于 gitignore 的 `data/` 目录，**建议人工打开复核视觉效果**（本模型不具备读图能力，未做像素级复核）。

---

## 13. Console 状态

- 修复前：`favicon.ico 404`（新建 `favicon.svg` 后复测消失，已解决）
- 残留 1 条：`503 (Service Unavailable)`，来源为 **Vite dev server 的 ws 代理偶发 ECONNRESET**——基础设施瞬时问题，非本次重构引入，生产（Nginx 直接服务构建产物）不受影响

---

## 14. 已知问题（Existing Issues，本次未触碰）

| # | 问题 | 说明 |
| --- | --- | --- |
| 1 | **dev SQLite 库 Alembic revision mismatch** | 现有 `data/yolo_vlm.db` 的 `alembic_version` 指向不存在的 `20260828_04`，启动报 "Can't locate revision"。**按约定未修改任何数据库**，改用全新临时库完成测试（迁移链 `20260805_01→20260826_02→20260828_03→20260829_04` 完整跑通，临时库已删除）。建议后续由有权限者核对 dev 库迁移链或直接使用 PostgreSQL |
| 2 | Vite ws 代理偶发 503 / ECONNRESET | 瞬时性基础设施问题，非 UI 引入 |

---

## 15. 后续建议

1. **人工视觉复核** `data/ui_shots/` 下 9 张截图，确认 HUD 细节符合预期后再对外演示
2. **处理 dev 库迁移链问题**（§14#1）后再用 `data/yolo_vlm.db` 联调
3. 如需进一步微调品牌色，只需改 `style.css` `:root` 中的 Token
4. 生产发布时重新构建前端并部署构建产物（`frontend/dist`）

---

## 16. 开发环境启动命令

### 方式 A：Docker Compose（官方推荐，无需本机环境）

```powershell
Set-Location D:\project\yolo_vlm_monitor
docker compose -f compose.cpu.yml -f compose.cpu.dev.yml up -d
```

访问：

- 前端（Vite 热更新）：http://127.0.0.1:5173
- 后端页面/API：http://127.0.0.1:8100（/docs、/health）

### 方式 B：本机直启（本次验收使用）

```bash
# 1. 后端（需 .venv，PostgreSQL 或临时 SQLite 均可）
DATABASE_URL="sqlite:///data/yolo_vlm.db" SCHEDULER_ENABLED=false .venv/Scripts/python.exe run.py

# 2. 前端（另开终端）
cd frontend && pnpm install && pnpm dev
```

> 注意：若直接使用现有 `data/yolo_vlm.db`，会触发 §14#1 的迁移链报错，需先处理该已知问题。

### 生产构建

```bash
cd frontend && pnpm build   # 产物在 frontend/dist
```

---

## 17. Git 提交建议

已按约定流程完成：

```text
f672296 checkpoint: UI 重构前的稳定快照（重构前）
3180830 feat(ui): 前端科技化重构 - 深蓝 AI 监控 HUD 主题体系（本次重构，9 files, +1333/−123）
```

建议后续操作：

1. 推送前确认远程分支策略（`git push origin main` 或按团队规范开 MR）
2. `yolo26n.pt`（根目录大模型文件）已加入 `.gitignore`，不纳入版本库
3. 验收截图与临时测试产物位于 `data/`（已被 gitignore 覆盖），如需留档请自行另存

---

*报告完 · 重构范围严格限定前端表现层，业务逻辑、数据与基础设施零改动。*
