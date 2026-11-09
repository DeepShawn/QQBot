# 机器人控制台 · 液态玻璃（Liquid Glass）UI 设计规范

> 适用：`apk-miuix`（Jetpack Compose + MIUIX 0.9.3，Compose UI 1.11，minSdk 26）
> 目标：在**不加新依赖、不改架构**的前提下，借用 iOS 26「液态玻璃」的**材质语言**（不是 iOS 组件外观），把整个 App 统一成深色优先的玻璃风格。
> 现状依据（只读）：`MainActivity.kt`（Scaffold + TopAppBar + 5 Tab NavigationBar + Card Toast）、`ui/Common.kt`（`PageList` / `group` / `StatusRow` / `EditPreference` / `ConfirmDialog` / `BigStatus` / `Hint`）、各页面全部由 `group{}` + MIUIX Preference 组成，弹窗统一用 `OverlayDialog`，二级页用「← 返回更多」行做返回。

---

## 0. 本次迭代范围（一次落地）

| 优先级 | 范围 | 说明 |
|---|---|---|
| **P0** | 全局主题色板、背景光斑层、玻璃 Modifier、顶栏、浮动胶囊底栏、`group` 卡片、列表行、按钮、输入框、弹窗、Toast、概览页状态卡 | 全部集中改在 `MainActivity.kt` + `Common.kt` + 新增 `ui/Glass.kt`（仅 token 与 Modifier），各页面**零改动**也能生效 |
| **P1** | 二级页改为顶栏返回（去掉「← 返回更多」行）、长内容容器（日志/终端/文件/技能编辑）、空状态/骨架/错误组件、记录页长列表改底部 Sheet、更多页工具宫格、概览页日志摘要、弹窗背后内容模糊（API 31+） | 每页小改，可逐页提交 |
| **P2** | 背景光斑缓慢漂移动画、底栏/顶栏真实背景模糊、浅色主题精调、「外观」设置（主题/降低透明度/动效开关）、记录页分段控件 | 体验增强，可延后 |

---

## 1. 设计原则

1. **材质，不是皮肤**：玻璃 = 半透明填充 + 1dp 渐变边缘高光 + 顶部光泽 + 景深阴影。不照搬 iOS 的控件形状（不做 iOS 风开关、不做 iOS 返回文字按钮），控件仍用 MIUIX 的行为与交互，只换「材质与层次」。
2. **三层景深**（自下而上）：
   - **L0 环境层**：深色底 + 柔和 mesh 光晕（全屏唯一的「色彩来源」）。
   - **L1 内容层**：卡片 / 列表容器，薄玻璃（Thin），**不加阴影、不做实时模糊**。
   - **L2 浮层**：顶栏（折叠态）、浮动底栏、Toast、FAB、下拉，厚玻璃（Thick）+ 阴影。
   - **L3 模态层**：Dialog / Bottom Sheet，最厚玻璃 + 背后内容压暗（API 31+ 再加模糊）。
   层级越高：填充越不透明、边缘高光越亮、阴影越重。**同一屏同一层级只用一种玻璃**。
3. **光感来自左上**：所有边缘高光统一「左上亮、中段暗、右下回光」，顶部光泽统一从上往下衰减，光斑统一偏左上冷色、右下暖色，保持光源一致。
4. **克制用色**：彩色只出现在三处——背景光斑、强调色（选中/主按钮/链接）、语义状态（成功/警告/危险/信息）。卡片本身不着色；只有「状态卡」允许带一团语义色辉光。
5. **暗色优先**：默认深色（`ColorSchemeMode.Dark`），浅色作为第二套 token（P2 精调）。深色下阴影几乎不可见，层次主要靠**填充亮度差 + 边缘高光**区分。
6. **可读性优先于通透**：长文本（日志、终端输出、技能 Markdown、文件预览、聊天记录）一律放在**实底「墨」容器**里，不用玻璃；输入框使用「内凹」样式而非玻璃。
7. **Android 合理落地**：Android 无系统级背景模糊，Compose 的 `Modifier.blur` 只模糊自身。因此本方案默认「**伪玻璃**」（背景本身已经柔焦，半透明叠加即视觉等效于模糊），只在两处使用真实模糊：弹窗背后的页面内容（API 31+，P1）与可选的顶/底栏背景模糊（P2）。iOS 26 本身在滚动边缘用的就是「边缘淡出」，我们同样用**滚动边缘渐隐**代替昂贵的全屏实时模糊。

---

## 2. Design Tokens

### 2.0 色值书写约定

- 本文所有色值为 **Android / Compose 顺序 `#AARRGGBB`**，可直接写成 `Color(0xAARRGGBB)`。
  例：白色 8% = `#14FFFFFF` → `Color(0x14FFFFFF)`。（CSS 写法是 `#FFFFFF14`，注意顺序相反。）
- 不透明度 → Alpha 十六进制速查：

| % | 4 | 6 | 8 | 10 | 12 | 14 | 16 | 18 | 20 | 24 | 28 | 30 | 35 | 40 | 50 | 60 | 64 | 72 | 78 | 85 | 90 | 92 | 96 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| hex | 0A | 0F | 14 | 1A | 1F | 24 | 29 | 2E | 33 | 3D | 47 | 4D | 59 | 66 | 80 | 99 | A3 | B8 | C7 | D9 | E6 | EB | F5 |

### 2.1 背景（L0 环境层）

全屏一个 `GlassBackdrop()`，画在 `Scaffold` 后面，`Scaffold.containerColor = Transparent`。由 **底色渐变 + 4 团径向光斑 + 暗角** 三层组成，叠加顺序如下（从下到上，全部 `BlendMode.SrcOver`）：

**深色（默认）**

| 层 | 类型 | 参数 |
|---|---|---|
| ① 底色 | 竖向线性渐变 | `0%  #FF0A0D14` → `55% #FF0B0F1A` → `100% #FF07090F` |
| ② 光斑 A「冰蓝」 | 径向渐变 | 中心 (0.12W, 0.06H)，半径 0.95W，`#475B6CFF`(28%) → `#005B6CFF` |
| ③ 光斑 B「青」 | 径向渐变 | 中心 (0.98W, 0.38H)，半径 0.80W，`#292BD4C8`(16%) → 透明 |
| ④ 光斑 C「紫」 | 径向渐变 | 中心 (0.18W, 0.80H)，半径 1.00W，`#2EA15BFF`(18%) → 透明 |
| ⑤ 光斑 D「暖」 | 径向渐变 | 中心 (0.88W, 1.02H)，半径 0.70W，`#14FF7A59`(8%) → 透明 |
| ⑥ 暗角 | 径向渐变 | 中心 (0.5W, 0.45H)，半径 1.1×max(W,H)，`0.55 透明` → `1.0 #59000000`(35%) |

**浅色（P2）**

| 层 | 参数 |
|---|---|
| ① 底色 | `#FFF3F5F9` → `#FFEAEEF5` → `#FFE6EAF2` |
| ② 光斑 A | `#387C8CFF`(22%)，同位置 |
| ③ 光斑 B | `#2E5FE3D6`(18%) |
| ④ 光斑 C | `#2EC79BFF`(18%) |
| ⑤ 光斑 D | `#24FFB59A`(14%) |
| ⑥ 暗角 | 不用（浅色下改为顶部 `#14FFFFFF` 提亮） |

叠加规则：
- 光斑**只在背景层出现**，卡片 / 页面内部禁止再画光斑（状态卡的语义辉光除外，见 3.3）。
- 半径按屏幕宽 W 计算，横屏 / 平板用 `min(W,H)` 防止光斑过大变成平涂。
- 背景整页静态绘制（`drawWithCache` 缓存 Brush），滚动时不重绘。
- P2 动效：光斑中心在 ±3% 范围内做 24s 周期的缓慢漂移；仅前台、非省电模式、系统动画缩放 ≠ 0 时启用，帧率不限但只改 2 个 Offset。

### 2.2 玻璃材质

#### 2.2.1 材质等级

| 等级 | 用于 | 填充（深色） | 填充（浅色） | 顶部光泽 sheen | 真实模糊目标* | 无模糊降级填充（深色） | 阴影 |
|---|---|---|---|---|---|---|---|
| **Thin** 薄 | `group` 卡片、列表容器、二级页卡片 | `#0FFFFFFF`（6%） | `#9EFFFFFF`（62%） | 白 5% → 0，覆盖上 40% 高度 | 不模糊 | 同左（无需降级） | 无 |
| **Regular** 常规 | 状态卡、可点击卡片、分段控件选中块、顶栏圆形按钮 | `#17FFFFFF`（9%） | `#B8FFFFFF`（72%） | 白 8% → 0，上 45% | 不模糊 | 同左 | 无 |
| **Thick** 厚 | 顶栏（折叠态）、浮动底栏、Toast、FAB、下拉菜单 | `#C71A1F2B`（78% 深蓝灰）+ 白 6% | `#D9F7F8FB`（85%） | 白 10% → 0，上 50% | 24–28dp | `#E6151924`（90%） | 有（见 2.2.4） |
| **Overlay** 模态 | Dialog、Bottom Sheet | `#E0141824`（88%）+ 白 4% | `#EBF8F9FC`（92%） | 白 8% → 0，上 35% | 32dp | `#F2121621`（95%） | 有 |
| **Ink** 墨（实底） | 日志、终端输出、文件/技能预览、聊天记录列表 | `#F00A0D14`（94%） | `#F5FFFFFF` | 无 | 不模糊 | — | 无 |
| **Inset** 内凹 | 输入框、搜索框、分段控件底槽 | `#38000000`（22% 黑） | `#0F0B0F1A`（6% 黑） | 无；改为**顶部内阴影** 1dp `#33000000` | 不模糊 | — | 无 |

\* 「真实模糊目标」仅在 P2 启用背景模糊时使用；未启用时一律用「降级填充」。Thin/Regular 永远不做实时模糊（背景本身是柔焦光斑，半透明叠加在视觉上已等效模糊）。

#### 2.2.2 边缘高光（1dp 渐变描边）

统一为 **135°**（左上 → 右下，`Brush.linearGradient` 默认 start=(0,0)、end=(∞,∞) 即可）三段式：

| 等级 | 起点 0% | 中段 55% | 终点 100% | 宽度 |
|---|---|---|---|---|
| Thin（深） | `#3DFFFFFF`（24%） | `#0AFFFFFF`（4%） | `#1FFFFFFF`（12%） | 1dp |
| Regular（深） | `#4DFFFFFF`（30%） | `#0FFFFFFF`（6%） | `#24FFFFFF`（14%） | 1dp |
| Thick（深） | `#66FFFFFF`（40%） | `#14FFFFFF`（8%） | `#33FFFFFF`（20%） | 1dp |
| Overlay（深） | `#59FFFFFF`（35%） | `#0FFFFFFF`（6%） | `#29FFFFFF`（16%） | 1dp |
| Ink（深） | 纯色 `#14FFFFFF`（8%） | — | — | 1dp |
| Inset（深） | 纯色 `#14FFFFFF`；聚焦时 `#CC5B8CFF`（强调 80%） | — | — | 1dp / 聚焦 1.5dp |
| 浅色所有等级 | `#E6FFFFFF`（90%） | `#4DFFFFFF`（30%） | `#99FFFFFF`（60%） | 1dp，外加 0.5dp `#14000000` 外轮廓线以免在浅底上「消失」 |

语义/强调着色的玻璃（状态卡、危险按钮、选中 Tab）：起点色换成该语义色 50% 与白 30% 的叠加，例如成功状态卡起点 `#8030D158`。

#### 2.2.3 高光层（叠加顺序）

在玻璃填充之上、内容之下依次绘制：
1. **顶部光泽 sheen**：竖向渐变（见 2.2.1 表），模拟玻璃上沿受光。
2. **镜面光点 specular**（仅 Thick / Overlay / 选中 Tab 胶囊 / 主按钮）：径向渐变，中心 (0.22W, 0)，半径 0.6W，`#1AFFFFFF`（10%）→ 透明。
3. **语义辉光**（仅状态卡）：径向渐变，中心 (0.15W, 0.10H)，半径 0.9W，语义色 22% → 透明。
4. 内容。
5. **边缘高光描边**（2.2.2）画在内容**之上**，保证内容滚动到边缘时描边仍清晰。

#### 2.2.4 阴影（只给浮层与模态）

Android 的 elevation 阴影会透过半透明填充显出来，因此 **Thin / Regular 卡片一律不加阴影**。浮层用 `Modifier.shadow(elevation, shape, ambientColor, spotColor)` 近似：

| 元素 | 视觉目标（颜色 / 模糊 / Y 偏移） | Compose 近似 |
|---|---|---|
| 浮动底栏 | `#80000000` / 32dp / y 12dp | `elevation = 16.dp, ambient = #33000000, spot = #99000000` |
| Toast | `#66000000` / 24dp / y 8dp | `elevation = 12.dp, ambient = #26000000, spot = #80000000` |
| FAB | `#80000000` / 24dp / y 8dp + 强调色辉光 | `elevation = 12.dp, spot = #995B8CFF`（深色） |
| Dialog / Sheet | `#99000000` / 48dp / y 16dp | `elevation = 24.dp, ambient = #40000000, spot = #B3000000` |
| 主按钮 | 强调色辉光 `#595B8CFF` / 16dp / y 4dp | `elevation = 6.dp, spot = #8C5B8CFF, ambient = 透明` |
| 顶栏 | 无阴影，只用底部 0.5dp 分隔线 `#14FFFFFF`（随折叠渐显） | — |

浅色主题阴影色统一换成 `#1F1B2340`（spot）/ `#0F1B2340`（ambient）。

### 2.3 圆角体系

| Token | 值 | 用途 |
|---|---|---|
| `radius.xs` | 8dp | 标签内小图标底、骨架条 |
| `radius.sm` | 12dp | 列表行左侧图标底座、Chip、小按钮 |
| `radius.md` | 16dp | 输入框、Ink 长内容容器、下拉菜单、Toast（多行时） |
| `radius.lg` | 24dp | `group` 卡片、状态卡、可点击卡片 |
| `radius.xl` | 32dp | Dialog、Bottom Sheet 顶部、浮动底栏 |
| `radius.pill` | 高度 / 2 | 按钮、分段控件、Tab 选中胶囊、单行 Toast、顶栏圆形按钮 |

- 嵌套规则：内层圆角 = 外层圆角 − 内边距（如 24dp 卡片内 12dp 边距的子块用 12dp）。
- 统一用 `RoundedCornerShape`，并在根部 `CompositionLocalProvider(LocalSquircleEnabled provides false)`，让 MIUIX 自带控件与我们的渐变描边圆角一致（MIUIX 的 `squircleBorder` 不支持 Brush，混用会在角部错位）。

### 2.4 间距体系

基准 4dp，只用 `4 / 8 / 12 / 16 / 24`（大留白可用 32）。

| 场景 | 值 |
|---|---|
| 页面左右边距（卡片外） | 16dp（现状 12dp → 改 16dp） |
| 卡片之间 | 12dp |
| 分组小标题 | 左 28dp（16 外 + 12 内对齐行文字），上 20dp，下 8dp |
| 卡片内边距（非列表内容） | 16dp；状态卡 20dp |
| 列表行 | 左右 16dp，上下 12dp；单行最小高 56dp，双行最小高 68dp |
| 行内图标与文字间距 | 12dp |
| 按钮组间距 | 12dp（横排），8dp（竖排） |
| 弹窗内边距 | 24dp（左右），标题上 24dp，按钮区下 20dp |
| 列表底部留白 | 底栏高度 64 + 底栏下边距 10 + 系统导航栏 inset + 16dp |
| 顶部留白 | `Scaffold` 给的 top padding（已含大标题），不额外加 |

### 2.5 字体（Compose sp）

系统字体（MiSans / Roboto），不引入字体文件。

| Token | 字号 | 字重 | 行高 | 用途 |
|---|---|---|---|---|
| `display` | 30sp | Bold | 36sp | 顶栏大标题（展开态） |
| `title1` | 24sp | Bold | 30sp | 概览状态卡主标题（现 22 → 24） |
| `title2` | 20sp | SemiBold | 26sp | Dialog 标题、空状态大字 |
| `headline` | 17sp | SemiBold | 22sp | 顶栏小标题（折叠态）、卡片标题 |
| `body` | 16sp | Medium | 22sp | 列表行标题、按钮文字 |
| `callout` | 15sp | Regular | 20sp | 行右侧值、正文 |
| `subhead` | 14sp | Regular | 20sp | 状态卡副标题、Toast、对话内容 |
| `footnote` | 13sp | Regular | 18sp | 行副标题 summary、Hint |
| `section` | 13sp | Medium（字距 +0.2sp） | 18sp | 分组小标题 |
| `caption` | 12sp | Medium | 16sp | Chip、时间戳、徽标 |
| `tab` | 11sp | Medium | 13sp | 底栏标签 |
| `mono` | 12sp | Regular（Monospace） | 17sp | 日志、终端、代码、技能编辑 |
| `metric` | 28sp | SemiBold（tabular 数字） | 32sp | 状态卡里的数字（运行时长等） |

MIUIX `TextStyles` 映射（通过 `defaultTextStyles(...)` 覆盖）：`title1 → title1`、`title2 → title2`、`headline1 → headline`、`main/body1 → body`、`body2/paragraph → subhead`、`footnote1 → footnote`、`footnote2 → caption`、`button → body`。

### 2.6 图标

- 来源：仅用 `MiuixIcons.extended`（已含 Home / Messages / Community / File / More / Settings / Refresh / Tasks / Timer / Lock / Notes / Search / Copy / Delete / Edit / Add / ChevronBackward / ChevronForward / Info / Report / Update / Translate / Image / Link / Play / Pause / Tune / Layers 等），**不引入新图标库**。
- 尺寸：底栏 24dp；顶栏按钮 22dp（容器 40dp 圆）；列表行图标 18dp 放在 **32dp × 32dp、圆角 12dp 的着色底座**（底座填充 = 该行主题色 18%，图标 = 该色 100%）；Chip 内 14dp。
- 颜色：默认 `text.secondary`；选中 / 可交互 = 强调色；状态 = 语义色。不使用多色图标。
- 列表行主题色分配（只用于图标底座，保持全局一致）：服务控制 = 强调蓝；AI/人格 = 紫 `#A78BFA`；群管 = 青 `#2BD4C8`；记录/文件 = 橙 `#FF9F0A`；安全/防封 = 红 `#FF6B60`；更新/后台 = 绿 `#30D158`；工具 = 灰蓝 `#8E9AAF`。

### 2.7 文字色与语义色

**文字 / 基础（深色 · 浅色）**

| Token | 深色 | 浅色 |
|---|---|---|
| `text.primary` | `#F5F2F5FA` | `#FF0B0F1A` |
| `text.secondary` | `#A3FFFFFF`（64%） | `#B30B0F1A`（70%） |
| `text.tertiary` | `#66FFFFFF`（40%） | `#800B0F1A`（50%） |
| `text.disabled` | `#47FFFFFF`（28%） | `#470B0F1A` |
| `divider` | `#14FFFFFF`（8%），0.5dp，左缩进 16dp（有图标时 60dp） | `#140B0F1A` |
| `dim`（弹窗背后压暗） | `#8C05070C`（55%） | `#4D1B2340`（30%） |

**强调色与语义色**

| Token | 深色 | 浅色 | 着色玻璃填充（深/浅） | 着色描边 |
|---|---|---|---|---|
| `accent` 强调（冰川蓝） | `#FF5B8CFF` | `#FF2F6BFF` | `#295B8CFF` / `#1F2F6BFF` | 50% |
| `accent2`（主按钮渐变终点） | `#FF8A6CFF` | `#FF6A4CF0` | — | — |
| `accent.onGlass`（底栏选中文字/图标） | `#FF8FB0FF` | `#FF2F6BFF` | — | — |
| `success` | `#FF30D158` | `#FF1E9E4A` | `#2930D158` / `#1F1E9E4A` | 50% |
| `warning` | `#FFFF9F0A` | `#FFC77700` | `#29FF9F0A` / `#1FC77700` | 50% |
| `danger` | `#FFFF453A` | `#FFE5352B` | `#29FF453A` / `#1FE5352B` | 50% |
| `info` | `#FF64D2FF` | `#FF0A84C6` | `#2964D2FF` / `#1F0A84C6` | 50% |
| `neutral`（未知状态） | `#FF8E8E93` | `#FF6E6E73` | `#298E8E93` | 50% |

- 现有 `Good / Warn / Bad`（`Common.kt`）分别指向 `success / warning / danger` 的当前主题值。
- 语义色只用于：状态点、状态文字、Chip、Banner、危险按钮。不要把语义色铺满整个卡片。
- 状态点（`Dot`）：9dp 实心 + 外圈 3dp 同色 25% 光晕（运行中状态的光晕做 1.6s 呼吸，alpha 25%↔0%）。

### 2.8 动效

| Token | 时长 | 曲线 | 用途 |
|---|---|---|---|
| `motion.press` | 按下 90ms / 抬起弹簧 | 按下 `LinearOutSlowIn`；抬起 `spring(dampingRatio = 0.6f, stiffness = 700f)` | 按压缩放 |
| `motion.short` | 160ms | `CubicBezierEasing(0.2f, 0f, 0f, 1f)` | 颜色、透明度、开关、描边聚焦 |
| `motion.medium` | 260ms | 同上 | 卡片展开、Toast 进出、顶栏玻璃渐显 |
| `motion.long` | 340ms | 进入 `(0.05, 0.7, 0.1, 1)`，退出 `(0.3, 0, 0.8, 0.15)` 用 220ms | 页面转场、Dialog、Sheet |
| `motion.liquid` | 弹簧 | 前沿 `spring(0.72f, 600f)`，后沿 `spring(0.72f, 300f)` | 底栏选中胶囊「液体拉伸」 |

- **按压反馈**：可点击卡片 / 行：缩放 0.98 + 叠加 `#0FFFFFFF` 高亮（MIUIX `PressFeedbackType.Sink` 可直接用于 Card）；按钮：缩放 0.96 + 填充提亮 6%（主按钮叠黑 10% 反而更像按下，二选一：**深色用提亮，浅色用压暗**）；底栏项：0.92。全部用 `graphicsLayer { scaleX/scaleY }`，不触发重新布局。
- **Tab 切换**：内容 `Crossfade` 180ms + 轻微缩放 0.985→1；底栏胶囊用 `motion.liquid`，前后沿不同刚度形成「水滴拉伸再回弹」。
- **二级页**：进入 = 从右侧 25% 宽度滑入 + 淡入 300ms；返回 = 反向 240ms；顶栏标题交叉淡入。
- **Dialog**：缩放 0.92→1 + 淡入 + （API 31+）背后内容模糊 0→10dp，全部 280ms；退出 200ms。Sheet：从底部滑入 340ms。
- **Toast**：上移 16dp + 缩放 0.96→1 + 淡入 220ms；停留 2200ms（保持现状）；退出淡出下移 8dp 180ms。
- **减少动效**：`Settings.Global.ANIMATOR_DURATION_SCALE == 0` 或省电模式时：禁用背景漂移、呼吸光晕、液体拉伸（改直接位移），转场只保留淡入淡出。

---

## 3. 组件规范

> 每个组件给出：结构 / 尺寸 / 状态（默认 · 按下 · 选中 · 禁用 · 加载）。未特别说明的颜色均为深色值。

### 3.1 顶部栏 `GlassTopBar`

- **结构**：沿用 MIUIX `TopAppBar`（大标题 + 折叠），外包一层玻璃背景；左侧可选返回按钮，右侧最多 2 个操作按钮。
- **尺寸**：折叠高度沿用 MIUIX 默认（含状态栏 inset）；大标题 `display` 30sp，左边距 28dp；折叠标题 `headline` 17sp 居中。
- **背景**：`color = Transparent`；背景层 alpha = `scrollBehavior.state.collapsedFraction`：
  - 0（展开、未滚动）：完全透明，直接透出背景光斑。
  - 1（折叠）：Thick 降级填充 `#B80B0E16`（72%）+ 底部 0.5dp `#14FFFFFF` 分隔线。
- **滚动边缘渐隐**：列表顶部 24dp 区域内容从 0→100% 不透明（见 5.6），让内容「溶进」顶栏而不是被硬切。
- **返回 / 操作按钮**：40dp 圆形 Regular 玻璃，图标 22dp（`ChevronBackward` / `Refresh` 等），图标色 `text.primary`。
  - 按下：缩放 0.92，填充 → `#29FFFFFF`。
  - 禁用：图标 `text.disabled`，无描边高光。
  - 加载：图标换成 18dp `InfiniteProgressIndicator`（强调色），按钮不可点。
- **二级页**：标题 = 二级页名称（如「关键词管理」），左侧显示返回按钮；**删除页面内的「← 返回更多」行**（P1）。

### 3.2 底部导航 `GlassTabBar`（5 Tab：概览 / AI / 群管 / 记录 / 更多）

- **结构**：浮动胶囊，内部 5 等分 Item；选中项后面有一块会滑动的「玻璃胶囊」。
- **尺寸**：
  - 容器：高 64dp，左右外边距 16dp，距底部 = 系统导航栏 inset + 10dp，圆角 32dp，内边距 6dp。
  - Item：等分宽度（360dp 屏约 65dp），图标 24dp + 2dp + 标签 `tab` 11sp，垂直居中；最小触控 48dp。
  - 选中胶囊：高 52dp，宽 = Item 宽 − 4dp，圆角 26dp。
- **材质**：容器 Thick（`#C71A1F2B` + 白 6%，2.2.2 Thick 描边，阴影见 2.2.4）；选中胶囊 = `#1F5B8CFF`（强调 12%）+ `#1AFFFFFF`（白 10%）叠加 + 竖向描边 `#59FFFFFF → #0FFFFFFF` + 镜面光点。
- **状态**：
  - 默认（未选中）：图标 / 标签 `#99FFFFFF`（60%）。
  - 选中：图标 / 标签 `accent.onGlass` `#8FB0FF`，标签字重 SemiBold。
  - 按下：Item 缩放 0.92；按住时胶囊预先向手指位置偏移 6dp（可选）。
  - 切换：胶囊左右沿用 `motion.liquid`，产生拉伸回弹；图标颜色 160ms 过渡。
  - 禁用：不适用（全局连接失败时仍可切换）。
  - 加载：不适用；全局「连接管理服务…」状态下底栏照常显示。
- **徽标（可选 P2）**：右上 8dp 红点 `danger`，用于「需要登录」时的概览 Tab。
- **实现**：自绘（约 80 行），不用 MIUIX `NavigationBar`；若时间紧，可先用 MIUIX `FloatingNavigationBar` + `FloatingNavigationBarItem`，把 `color` 设为 Thick 降级填充、外层加描边 Modifier 作为过渡方案。

### 3.3 卡片

| 类型 | 结构 | 材质 / 尺寸 | 状态 |
|---|---|---|---|
| **普通卡（group）** | 可选小标题（卡外）+ 卡片 + 内部列表行 | Thin；圆角 24dp；外边距左右 16dp、下 12dp；内容贴边，由行自带 16dp 内边距 | 无交互状态 |
| **可点击卡** | 卡片整体可点，右上或右侧箭头 | Regular；圆角 24dp；内边距 16dp | 按下：`Sink` 缩放 0.98 + 白 6% 叠加；禁用：内容 alpha 0.4、描边改纯色 `#0AFFFFFF`；加载：右侧 18dp 转圈替代箭头 |
| **状态卡（Hero）** | 状态光球 + 主标题 + 副标题 + 2×2 指标格 | Regular + 语义辉光；圆角 24dp；内边距 20dp | 语义色随状态切换时辉光颜色 260ms 渐变；运行中光球呼吸 |

- 状态卡光球：14dp 实心语义色 + 36dp 径向光晕（语义色 40% → 0）。主标题 `title1` 24sp 语义色；副标题 `subhead` `text.secondary`。
- 2×2 指标格：每格 Inset 小底槽（圆角 12dp，高 56dp，内边距 12dp），上行 `caption` 标签（`text.tertiary`），下行 `callout` 值 + 状态点。
- 卡片之间不出现阴影；卡片内部不允许再套卡片（最多一层 Inset 子块）。

### 3.4 列表项 `GlassRow`

结构：`[左图标底座 32dp]? 12dp [标题 / 副标题] 弹性空间 [右侧：值 | 箭头 | 开关 | 状态]`

- 尺寸：左右 16dp、上下 12dp；单行最小 56dp、双行 68dp；标题 `body` 16sp Medium `text.primary`；副标题 `footnote` 13sp `text.secondary`，最多 2 行；右值 `callout` 15sp `text.secondary`，最多 16 字后省略。
- 右侧箭头：`ChevronForward` 16dp `text.tertiary`。
- 分隔线：行之间 0.5dp `divider`，左缩进 16dp（有图标 60dp），首尾行无线。
- 状态：
  - 按下：整行背景 `#0FFFFFFF`（白 6%），按卡片圆角裁切（首行带上圆角、末行带下圆角）。
  - 选中（下拉/单选列表中）：右侧 `Ok` 图标强调色 + 标题强调色。
  - 禁用：标题/副标题 alpha 0.4，不响应。
  - 加载：右侧箭头换成 16dp 转圈，副标题显示「处理中…」。
  - 危险行（「彻底关闭」「清空所有对话记忆」「重新安装运行环境」）：标题 `danger` 色，图标底座 `danger` 18%。
- 映射：MIUIX `BasicComponent` / `ArrowPreference` / `SwitchPreference` / `OverlayDropdownPreference` 保留，只通过 `BasicComponentColors`（标题/摘要颜色）与 `insideMargin` 统一；左图标通过 `startAction` 槽位传入 32dp 底座。

### 3.5 分段控件 / 标签

- **分段控件**（P2，用于记录页切换「群消息 / 文件 / AI 上下文」、技能/任务筛选）：底槽 Inset，高 36dp，圆角 18dp，内边距 3dp；选中块 Regular 玻璃，圆角 15dp，滑动 `motion.liquid`；文字 `subhead` 14sp，未选中 `text.secondary`、选中 `text.primary` SemiBold。禁用：整体 alpha 0.4。
- **Chip / 状态标签**：高 24dp，圆角 12dp，左右 10dp，`caption` 12sp Medium；状态标签 = 语义着色玻璃填充 + 语义色文字 + 可选 6dp 圆点；可选 Chip（如「包含 / 完全匹配 / 正则」）未选中 = Thin + `text.secondary`，选中 = `#295B8CFF` + 强调色文字 + 强调色 50% 描边。按下缩放 0.95。

### 3.6 开关

- 沿用 MIUIX `Switch`（尺寸、手势不变），通过 `SwitchDefaults.switchColors(...)` 改色：
  - 开：轨道 `accent`；滑块 `#FFFFFFFF`。
  - 关：轨道 `#29FFFFFF`（16%）+ 1dp `#14FFFFFF` 描边感（颜色接近即可）；滑块 `#FFE8EBF2`。
  - 禁用开：轨道 `#665B8CFF`；禁用关：轨道 `#14FFFFFF`；滑块 alpha 0.6。
- 切换动画沿用 MIUIX；行整体可点（`SwitchPreference` 已支持）。

### 3.7 输入框 `GlassTextField`

- **单行**：高 52dp，Inset 材质，圆角 16dp，左右内边距 16dp；文字 `body` 16sp `text.primary`；占位/label `text.tertiary`。
- **多行**：最少 3 行（技能编辑 / 欢迎语 5 行），内边距 14dp，最大高度 240dp 内部滚动；代码类（命令、技能、Python）用 `mono` 字体。
- **密码 / 密钥**：右侧 40dp 触控区眼睛图标（`Show` / `Hide`，20dp，`text.tertiary`）；secret 模式下保持现有「留空不修改」逻辑，占位文字「输入新的值；留空则不修改」。
- **状态**：
  - 默认：描边 `#14FFFFFF`。
  - 聚焦：描边 1.5dp `#CC5B8CFF` + 外圈 3dp `#265B8CFF` 光环（`motion.short`）。
  - 错误：描边 `danger`，下方 6dp 处 `footnote` 12sp `danger` 错误文字。
  - 禁用：填充 `#1A000000`，文字 `text.disabled`。
  - 只读：无描边，可选中复制。
- 映射：MIUIX `TextField` 支持 `TextFieldColors(backgroundColor, labelColor, borderColor …)` 与 `cornerRadius`，优先通过参数改造，不另起组件。

### 3.8 下拉选择

- 触发行：沿用 `OverlayDropdownPreference`，右侧显示当前值 + `ArrowUpDown` 图标。
- 弹出菜单：Thick 材质（降级填充 `#E6151924`），圆角 16dp，最小宽 200dp，最大高 360dp；菜单项高 48dp，左右 16dp；选中项强调色文字 + 右侧 `Ok` 18dp；按下项背景 `#14FFFFFF`。
- 实现：MIUIX 的弹出层颜色来自 `colorScheme.surfaceContainer` 等，因此**主题里的弹出层颜色必须保持高不透明度**（见 5.2），不要把它设成卡片的半透明值。

### 3.9 按钮

| 类型 | 高 / 圆角 | 填充 | 文字 | 按下 | 禁用 | 加载 |
|---|---|---|---|---|---|---|
| **主按钮** | 48dp / 胶囊 | 135° 渐变 `accent #5B8CFF → accent2 #8A6CFF` + 顶部 1dp 内高光 `#59FFFFFF→0` + 镜面光点；阴影见 2.2.4 | `body` 16sp SemiBold 白 | 缩放 0.96 + 叠 `#14FFFFFF` | 填充 `#14FFFFFF`，文字 `text.disabled`，无阴影 | 文字前 18dp 白色转圈，文字改「处理中…」/ 保留原宽度，不可点 |
| **次按钮（玻璃）** | 48dp / 胶囊 | Regular 玻璃 + 描边 | `body` 16sp Medium `text.primary` | 缩放 0.96 + 填充 → `#29FFFFFF` | 文字 `text.disabled`，描边纯色 `#0AFFFFFF` | 同上（转圈为强调色） |
| **文字按钮** | 40dp / 胶囊 | 无 | `body` 16sp Medium `accent` | 背景 `#0FFFFFFF` 胶囊 | 文字 `text.disabled` | 文字替换为转圈 |
| **危险按钮** | 48dp / 胶囊 | `#29FF453A` + 描边 `#80FF453A→#14FF453A` | `body` SemiBold `danger` | 缩放 0.96 + 填充 → `#3DFF453A` | 同次按钮 | 同上 |
| **危险确认（弹窗内）** | 48dp / 胶囊 | 实色 `danger` | 白 | 压暗 10% | — | — |
| **小按钮 / Chip 按钮** | 32dp / 胶囊，左右 14dp | Regular | `subhead` 14sp | 0.95 | 同上 | 14dp 转圈 |

- 横排双按钮（「开启服务 / 关闭机器人」、弹窗「取消 / 保存」）：等宽，间距 12dp；**左次右主**。
- 映射：MIUIX `TextButton` 只支持纯色 `TextButtonColors`，渐变主按钮需自定义 `GlassButton`（Box + `clickable` + 2.2 的 Modifier），其余可先用 `TextButton(colors = ...)` + 外层描边过渡。

### 3.10 FAB / 悬浮按钮（P1）

- 用于：技能页「新建技能」、关键词页「新增规则」（替代列表里的「＋ 新增…」行，可并存）。
- 56dp 圆形，填充 `#E65B8CFF`（强调 90%）+ 镜面光点 + Thick 描边；图标 `Add` 24dp 白；阴影见 2.2.4。
- 位置：右 20dp，底 = 底栏顶部 + 16dp（不能压住底栏）。
- 状态：按下 0.92；滚动向下时缩小为 0 并淡出（160ms），向上滚出现；禁用 alpha 0.4；加载：图标换 22dp 白色转圈。
- 映射：MIUIX `FloatingActionButton(shape, containerColor, shadowElevation …)` 可直接用，外加描边 Modifier。

### 3.11 Toast / Snackbar

- 结构：`[语义图标 18dp]? 10dp 文字 [操作文字按钮]?`
- 尺寸：最小高 44dp，单行胶囊（圆角 22dp），多行圆角 16dp；左右 20dp、上下 12dp；最大宽 360dp，水平居中，左右至少留 24dp。
- 位置：底栏上方 12dp（`padding.bottom + 12dp`，保持现有位置逻辑）。
- 材质：Thick + 阴影；文字 `subhead` 14sp `text.primary`。
- 语义：根据文案关键字可选着色图标——「失败 / 出错 / 读不到」→ `danger` `Report` 图标；「已…」成功类 → `success` `Ok`；其余无图标。（P1，P0 只换材质。）
- 动效见 2.8；同一时间只显示一条，新消息替换旧消息时做 120ms 交叉淡入。

### 3.12 Dialog 与 Bottom Sheet

**Dialog（`OverlayDialog`）**
- 宽：屏宽 − 2×16dp，最大 420dp；圆角 32dp；Overlay 材质（`backgroundColor = #E0141824`），描边与镜面光点通过外层 Modifier 加；阴影 24dp。
- 标题 `title2` 20sp SemiBold 居中；summary `subhead` 14sp `text.secondary` 居中；内容区左右 24dp；按钮区上 20dp、下 20dp。
- 背后：`dim #8C05070C`；API 31+ 额外对页面内容层做 10dp 模糊（P1，见 5.5）。
- 状态：打开 / 关闭动效见 2.8；内容加载中时按钮区主按钮显示加载态；内容最高 = 屏高 70%，超出内部滚动（现有多处 `heightIn(max = 420.dp)` 保留）。

**Bottom Sheet（`OverlayBottomSheet`，P1）**
- 用于长列表/长内容：群消息列表、对话记忆列表 + 详情、模型选择、任务详情、技能查看。
- 顶部圆角 32dp，底部贴边（含导航栏 inset）；最大高 88% 屏高；顶部抓手 36×5dp、圆角 2.5dp、`#4DFFFFFF`，距顶 8dp；标题 `headline` 17sp 居中，距抓手 12dp。
- 材质：Overlay；内容区若是长文本，内部再放 Ink 容器。

### 3.13 长内容容器（日志 / 终端 / 文件 / 技能 / 聊天记录）

- **Ink 容器**：圆角 16dp，填充 `#F00A0D14`，描边 1dp `#14FFFFFF`，内边距 12dp，`mono` 12sp / 行高 17sp，颜色 `#D9E6EAF2`；最大高 420dp（Sheet 中可到 60% 屏高），内部垂直滚动，长行**自动换行**（终端输出可加横向滚动开关，P2）。
- **头部条**（可选）：高 36dp，左侧状态点 + `caption`（如「运行中 · 12.4 秒」），右侧 32dp 圆形按钮：复制（`Copy`）、刷新（`Refresh`）、停止（`Pause`，`danger` 色）。
- **日志着色**（P2）：含 `ERROR` 行 `danger`，`WARN` 行 `warning`，时间戳 `text.tertiary`。
- **聊天记录**（群消息 / 对话记忆详情）：每条 = 上行 `caption` 时间 + 昵称（昵称强调色，机器人昵称 `accent`，用户昵称 `text.secondary`），下行 `subhead` 14sp 正文；条目间距 10dp；机器人消息可放 Thin 气泡（圆角 16dp，左右 12dp），用户消息不加底——**只允许这一种气泡**，避免一屏过多玻璃。
- 原则：长内容容器**禁止**用 Thin/Regular 玻璃，避免透出背景光斑影响阅读。

### 3.14 空状态 / 加载骨架 / 错误提示

**空状态**（如「还没有任务」「还没有生成过文件」「还没有接口」）
- 放在卡片内，垂直居中，上下内边距 32dp。
- 64dp 圆形 Regular 玻璃底 + 28dp 图标（`text.tertiary`）→ 12dp → 标题 `body` 16sp Medium → 4dp → 说明 `footnote` 13sp `text.secondary`（居中，最多 2 行）→ 16dp → 可选小按钮（32dp，Regular）。

**加载骨架**（列表首次加载：任务、技能、文件、群列表）
- 3 行占位，每行：左 32dp 圆角 12dp 方块 + 右两条条带（高 12dp / 10dp，宽 60% / 40%，圆角 8dp，间距 8dp）。
- 填充 `#0FFFFFFF`；流光：20° 斜向线性渐变 `透明 → #1AFFFFFF → 透明`，宽 40% 卡片宽，1200ms 线性无限循环；减少动效时去掉流光。
- 全页初次连接（现「连接管理服务…」）：屏幕中央 Regular 玻璃小卡（160×120dp，圆角 24dp）内放 28dp 转圈 + `subhead` 文案。

**错误提示**
- **行内 Banner**（任务启动失败、账号状态读取失败、更新失败）：语义着色玻璃（`#29FF453A` 填充，描边 `#80FF453A → #14FF453A`），圆角 16dp，内边距 12dp × 14dp，左 20dp `Report` 图标 `danger`，标题 `subhead` SemiBold `danger`，说明 `footnote` `text.secondary`，右侧可选「重试」文字按钮。外边距左右 16dp、下 12dp（与卡片对齐）。
- **警告 Banner**（「正在恢复」「需要登录」）：同上换 `warning` / `accent`。
- **字段错误**：见 3.7。
- **全页错误**（SetupScreen ERROR）：状态卡样式 + `danger` 辉光，下方主按钮「重试」+ 次按钮「重新安装运行环境」。

---

## 4. 页面布局规范

通用骨架（所有 Tab 页与二级页）：

```
Box(fillMaxSize)
 ├─ GlassBackdrop()                     ← L0，全屏一次
 └─ Scaffold(containerColor = Transparent)
     ├─ topBar    = GlassTopBar         ← 展开透明 / 折叠 Thick
     ├─ bottomBar = GlassTabBar         ← 浮动 Thick 胶囊
     └─ content   = PageList(...)       ← 内容滚到栏下，边缘渐隐
          └─ group(...) = Thin 卡片 × N
```

**玻璃化 vs 保持纯色总表**

| 玻璃化 | 保持实底 / 不加材质 |
|---|---|
| 顶栏（折叠态）、底栏、group 卡片、状态卡、Toast、Dialog、Sheet、下拉菜单、FAB、顶栏圆形按钮、分段控件选中块、按钮（主/次/危险） | 分组小标题与 Hint（直接在背景上，无底）、日志/终端/文件/技能/聊天记录（Ink）、输入框（Inset）、指标小格（Inset）、列表行本身（只在按下时出现白 6%）、行内分隔线 |

### 4.1 概览（HomePage）· P0

自上而下：
1. **状态卡（Hero）**：替换 `BigStatus`。光球 + 「运行中 / 需要登录 / 连接中 / 已关闭 / 已停止」+ 副标题；下方 2×2 指标格：机器人、QQ 连接、QQ 进程、OneBot 通道（即现「状态」组前 4 行，从列表移到这里）。右上角 Chip 显示运行时长（`fmtUptime`）。
2. **恢复中 Banner**（`recovering` 时）：`warning` Banner，取代现「正在恢复」group。
3. **需要登录 Banner**（`needLogin` 时）：`accent` 着色可点击卡，右侧「去登录」小主按钮。
4. **服务控制卡**：上部按钮行「关闭机器人（次）｜开启服务（主）」，内边距 16dp；下部列表行：重启机器人（`Refresh` 图标）、重启 QQ 协议层、彻底关闭（危险行）。
5. **详情卡**（原「状态」组剩余）：QQ 号、运行时长、存储剩余（`InfoRow`）。
6. **自动守护卡**：两个开关行。
7. **最近日志卡**（P1，新增）：Ink 容器显示最后 5 行（`/api/logs?lines=5`，与现有 3 秒刷新同频或更慢，建议 10 秒），右上「查看全部」文字按钮 → 跳更多页日志。
8. Hint（背景上直接排字）。

### 4.2 AI 页（AiPage）· P0 样式 / P1 结构

1. **AI 账号卡**：每个 provider 一行，左侧 32dp 底座放首字母（「G / C / G / D」，紫色底座），右侧状态 Chip（已连接 = `success` Chip 显示账号名截断；未连接 = `neutral` Chip；不支持 = `text.tertiary` 文本）。
2. **当前接口卡**：下拉行；无接口时空状态「还没有接口 · 在网页控制台添加」。
3. **接口设置卡**：认证方式 → 账号状态内嵌 Banner（已连接 `success` / 未连接 `warning`，替换现 Hint）→ 协议 → 接口地址 → 模型（可点击行 → 模型选择改为 Bottom Sheet，P1）→ API Key（密钥行，右值显示掩码）→ 温度 / tokens。
4. **人格卡**、**什么时候回复卡**、**能力卡**、**记忆卡**：标准列表；「清空所有对话记忆」为危险行并加二次确认（现直接执行，建议复用 `ConfirmDialog`）。

### 4.3 群管（GroupPage）· P0

1. **总开关卡**（可点击卡样式）：「群管理总开关」用大一号行（标题 `headline` 17sp），开启时卡片描边起点混入 `success` 50%；关闭时下方所有卡片内容 alpha 0.5（仍可编辑，只是视觉提示「不生效」）。
2. Hint「机器人必须是群管理员…」改为 `info` Banner。
3. 入群欢迎、违禁词、防刷屏三张卡：标准列表；数值类编辑行右侧值用 `callout` 等宽数字；「处理方式」下拉。

### 4.4 记录（RecordsPage）· P0 样式 / P1 Sheet / P2 分段

1. P2：顶部分段控件「群消息 ｜ 文件 ｜ AI 上下文」，一次只显示一块（当前三块纵向排列也可保留）。
2. **群内消息卡**：选择群 / 读取条数下拉 → 按钮行「刷新群列表（次）｜读取消息（主，加载态）」。消息结果改为 **Bottom Sheet**，内部按 3.13 聊天记录样式。
3. **机器人写的文件卡**：顶部小按钮「刷新」（32dp）放在卡片标题行右侧；文件行左侧 `File` 图标底座（橙），副标题 `大小 · 时间`；空状态与骨架按 3.14；预览用 Sheet + Ink。
4. **AI 上下文卡**：「查看最近的对话」可点击行 → 会话列表 Sheet → 详情（同一 Sheet 内推进，顶部带返回）；「清空所有对话记忆」危险行。

### 4.5 更多（MorePage）· P0 样式 / P1 宫格

1. **工具宫格**（P1，替代「工具」列表并移到页面最上方）：2 列，Regular 可点击卡，单格高 96dp，圆角 24dp，内边距 16dp；左上 32dp 图标底座，下方标题 `body` + 单行副标题 `caption`。8 项：关键词（`Notes`）、插件（`Layers`）、任务终端（`Tasks`）、技能（`Edit`）、定时消息（`Timer`）、防封（`Lock`）、官方机器人（`Community`）、运行 Python（`Play`）。宫格间距 12dp。
2. 其余组保持列表：关键词回复（摘要）、画图、日志（展开后用 Ink 容器）、后台运行、更新（更新进度用 `LinearProgressIndicator` 强调色 + 状态 Chip）、其它（「重新安装运行环境」为危险行）。
3. 多条 Hint（后台运行下 3 条）合并为一个 `info` Banner，内容用「·」分点。
4. P2 新增「外观」组：主题（深色 / 浅色 / 跟随系统）、降低透明度（开 = 全部使用降级填充、关闭光斑漂移）、背景动效开关。

### 4.6 「更多」二级页 · P1

通用：顶栏标题 = 页面名 + 返回按钮；删除「← 返回更多」group；`BackHandler` 保留；进入/返回转场见 2.8；底栏保持显示且「更多」处于选中态。

| 页面 | 布局要点 |
|---|---|
| **关键词管理** | 启用开关卡 → 规则卡（每行：规则名 + 摘要；摘要中的「包含/完全匹配/正则」「所有会话/仅私聊/仅群聊」改为两枚 Chip）→ FAB「新增规则」（保留列表末尾新增行作为后备）→ 联网搜索触发词卡、画图触发词卡。规则编辑弹窗：名称输入、关键词多行、匹配方式 Chip 组、范围 Chip 组、回复多行；底部「删除（危险文字按钮）｜取消｜保存」 |
| **插件管理** | 插件开关卡（每行左侧图标底座）、Python 工具卡、开发者工具卡；标准列表即可 |
| **任务（终端）** | 「运行命令」卡：多行 mono 输入框（Inset，3–8 行）→ 超时行 → 按钮行「刷新列表（次）｜运行（主，加载态）」；错误用 Banner；「最近任务」卡：每行标题 = 命令摘要（`mono` 14sp），右侧状态 Chip（运行中 `warning` 带呼吸点 / 已完成 `success` / 失败、已停止 `danger` / 未知 `neutral`），副标题 = 时长；任务详情改 Sheet：头部条（状态 + 时长 + 复制 / 刷新 / 停止）+ Ink 输出 |
| **技能** | 技能列表卡（左 `Notes` 图标，副标题文件大小/时间）、FAB 新建；查看 = Sheet + Ink（Markdown 原文 mono）；编辑 = 全屏 Dialog/Sheet，文件名单行输入 + 内容多行 mono 输入（最高 60% 屏高） |
| **定时消息** | 启用开关卡 → 设置卡（间隔、目标群、语言模型、画图模型）；「立即发送一次」次按钮放在卡片底部按钮区 |
| **防封模式** | 顶部状态卡（开启 = `success` 辉光「防封已开启」，关闭 = `neutral`）→ 防封模式 / 内容过滤 / 生效范围三卡 |
| **官方机器人** | 通道卡（开关、AppID、AppSecret 密钥行、沙箱开关）→ 当前状态卡（状态行用 `StatusRow`，或做成小状态卡）→「切换前必读」改为 `warning` Banner |
| **AI 账号（AccountDialog）** | 维持 Dialog（流程短），内容：账号状态行 → 登录流程步骤（设备码 / 链接用 Ink 小块 + 右侧复制按钮）→ 输入框 → 按钮区；「退出账号」为危险按钮并二次确认（已有） |
| **运行 Python** | Dialog 改 Sheet：代码多行 mono 输入 → 运行（主）→ 输出 Ink（stdout 正常色，stderr `danger`） |
| **更新弹窗（UpdatePanel）** | Dialog：版本号 `title2`、更新日志 Ink、进度条强调色、「稍后（次）｜立即更新（主）」 |

### 4.7 安装 / 启动页（SetupScreen）· P1

- 同样铺 `GlassBackdrop`；中央一张 Regular 卡（圆角 32dp，内边距 24dp）。
- 顶部 56dp 光球（强调色径向光晕）作为品牌视觉；标题 `title1`；说明 `subhead` 居中。
- 安装中：`LinearProgressIndicator`（强调色轨道 `#295B8CFF`）+ 详情 `footnote`。
- 按钮：主「开始安装」/「重试」，次「手动选择安装包」/「重新安装运行环境」。

---

## 5. Compose / MIUIX 实现要点（指导性）

> 新增 1 个文件 `ui/Glass.kt` 放 token、`Modifier.glass()`、`GlassBackdrop`、`GlassTabBar`、`GlassButton`；`Common.kt` 中的 `group / PageList / StatusRow / BigStatus / Hint / DialogButtons / ConfirmDialog / EditPreference` 改用新 token。各页面尽量不动。

### 5.1 Token 组织

```kotlin
enum class GlassLevel { Thin, Regular, Thick, Overlay }

@Immutable data class GlassSpec(
    val fill: Color, val fallbackFill: Color, val sheen: Float,
    val rim: List<Pair<Float, Color>>, val specular: Boolean,
    val elevation: Dp, val spot: Color,
)
// 深 / 浅两套，经 CompositionLocal 提供
val LocalGlass = staticCompositionLocalOf { DarkGlass }
```

### 5.2 主题接入（MainActivity）

- `ThemeController(ColorSchemeMode.Dark, lightColors = glassLight, darkColors = glassDark)`（P2 才放开跟随系统）。
- `darkColorScheme(...)` 用**具名参数**覆盖关键角色：
  - `primary = #FF5B8CFF`，`onPrimary = #FFFFFFFF`
  - `background = #FF0A0D14`（降级兜底）
  - `onSurface / onBackground = #F5F2F5FA`，`onSurfaceVariantSummary = #A3FFFFFF`，`onSurfaceVariantActions = #80FFFFFF`
  - `dividerLine = #14FFFFFF`，`windowDimming = #8C05070C`
  - `surfaceContainer / surfaceContainerHigh`（弹出菜单、Dialog 默认底）= **高不透明** `#F0181C27` / `#F21A1F2B` —— 不要设成半透明，否则 MIUIX 弹出层没有模糊会直接透字。
  - `secondary`（开关关闭轨道等）= `#29FFFFFF`
- 卡片的半透明填充**不靠改 colorScheme**，而是在 `group()` 里显式传 `CardColors(color = Transparent)` + `Modifier.glass(...)`，或直接用自绘 `GlassCard`。
- 根部：`CompositionLocalProvider(LocalSquircleEnabled provides false, LocalGlass provides DarkGlass)`。

### 5.3 背景光斑

```kotlin
@Composable fun GlassBackdrop(m: Modifier = Modifier) = Spacer(m.fillMaxSize().drawWithCache {
    val w = size.width; val h = size.height; val r = minOf(w, h)
    fun blob(c: Color, x: Float, y: Float, k: Float) =
        Brush.radialGradient(listOf(c, c.copy(alpha = 0f)), Offset(w * x, h * y), r * k)
    val base = Brush.verticalGradient(listOf(Color(0xFF0A0D14), Color(0xFF0B0F1A), Color(0xFF07090F)))
    val blobs = listOf(
        blob(Color(0x475B6CFF), .12f, .06f, .95f), blob(Color(0x292BD4C8), .98f, .38f, .80f),
        blob(Color(0x2EA15BFF), .18f, .80f, 1.0f), blob(Color(0x14FF7A59), .88f, 1.02f, .70f))
    val vignette = Brush.radialGradient(0.55f to Color.Transparent, 1f to Color(0x59000000),
        center = Offset(w / 2, h * .45f), radius = maxOf(w, h) * 1.1f)
    onDrawBehind { drawRect(base); blobs.forEach { drawRect(it) }; drawRect(vignette) }
})
```
- 放在 `MainScreen` / `SetupScreen` 最外层 `Box` 第一个子项；`Scaffold(containerColor = Color.Transparent)`；`TopAppBar(color = Transparent)`。
- P2 漂移：`rememberInfiniteTransition` 驱动一个 0..1 的 Float，在 `onDrawBehind` 里读取（**只在 draw 阶段读**，不触发重组）。

### 5.4 玻璃 Modifier（填充 + 光泽 + 渐变描边）

```kotlin
fun Modifier.glass(shape: Shape, spec: GlassSpec, tint: Color = Color.Unspecified) = this
    .then(if (spec.elevation > 0.dp) Modifier.shadow(spec.elevation, shape,
          ambientColor = spec.spot.copy(alpha = .25f), spotColor = spec.spot) else Modifier)
    .clip(shape)
    .background(if (GlassPrefs.reduced) spec.fallbackFill else spec.fill)
    .drawWithCache {
        val sheen = Brush.verticalGradient(0f to Color.White.copy(spec.sheen), .45f to Color.Transparent)
        val spec2 = Brush.radialGradient(listOf(Color(0x1AFFFFFF), Color.Transparent),
                        Offset(size.width * .22f, 0f), size.width * .6f)
        val glow = if (tint.isSpecified) Brush.radialGradient(listOf(tint.copy(.22f), Color.Transparent),
                        Offset(size.width * .15f, size.height * .1f), size.width * .9f) else null
        onDrawBehind { glow?.let { drawRect(it) }; drawRect(sheen); if (spec.specular) drawRect(spec2) }
    }
    .border(1.dp, Brush.linearGradient(colorStops = spec.rim.toTypedArray()), shape) // 默认即 135°
```
- `border` 放在链末尾 → 描边画在内容之上（满足 2.2.3 第 5 条）。
- 按压缩放统一用 `graphicsLayer { scaleX = s; scaleY = s }`，`s` 由 `animateFloatAsState` 驱动，不要用 `Modifier.scale` 改布局。
- 描边的 Brush 对同一尺寸会被 `border` 内部缓存；不要在每帧新建 `GlassSpec`（放 `remember` / 顶层常量）。

### 5.5 毛玻璃与版本降级

| 能力 | API 26–30 | API 31+（Android 12+） |
|---|---|---|
| 卡片 Thin / Regular | 半透明填充（无模糊，视觉已足够） | 同左（**不要**给卡片加模糊） |
| 顶栏 / 底栏 Thick | 降级填充（72%–90%）+ 滚动边缘渐隐 | P0 同左；P2 可选真实背景模糊 24–28dp |
| 弹窗背后 | `dim` 压暗 | 压暗 + 页面内容层 `Modifier.blur(10.dp)`（动画 0→10dp）|
| 降低透明度 / 省电模式 | 所有等级改 `fallbackFill` | 同左，并关闭模糊与背景动效 |

- 版本判断用 `Build.VERSION.SDK_INT >= 31`（MIUIX 依赖里也有 `isRenderEffectSupported()` 可复用）；`Modifier.blur` 在 31 以下本身是 no-op，但仍建议显式判断以便使用降级填充。
- **弹窗背后模糊（P1）**：全局 `object GlassOverlay { var openCount by mutableIntStateOf(0) }`；在统一的 `GlassDialog` 包装（替换各处 `OverlayDialog` 调用，或至少 `ConfirmDialog / EditPreference / DialogButtons` 所在弹窗）里用 `DisposableEffect(show)` 加减计数；`MainScreen` 内容区 `Box(Modifier.blur(animateDpAsState(if (openCount > 0) 10.dp else 0.dp).value))`。只模糊 Scaffold 的 content 区（MIUIX 弹窗挂在 Scaffold 的 popup host 上，不会被一起模糊）。
- **真实背景模糊（P2，可选）**：用 Compose 1.7+ 的 `rememberGraphicsLayer()` 记录内容层，在栏的 `drawBehind` 中按栏的位置平移绘制该 layer 的模糊副本（`layer.renderEffect = BlurEffect(r, r)`）。代价是滚动时每帧对整块内容重新模糊，**仅在高端机 + 用户开启时使用**；不满足时回退到降级填充。不引入 Haze 等第三方库。

### 5.6 滚动边缘渐隐（代替全屏 blur）

在 `PageList` 的 `LazyColumn` 上：
```kotlin
Modifier
  .graphicsLayer { compositingStrategy = CompositingStrategy.Offscreen }
  .drawWithContent {
      drawContent()
      val top = topInsetPx;  val fade = 24.dp.toPx()          // 顶栏下沿
      val bot = size.height - bottomBarPx; val fadeB = 32.dp.toPx()
      drawRect(Brush.verticalGradient(
          0f to Color.Transparent, (top / size.height) to Color.Transparent,
          ((top + fade) / size.height) to Color.Black,
          ((bot - fadeB) / size.height) to Color.Black, (bot / size.height) to Color.Transparent,
      ), blendMode = BlendMode.DstIn)
  }
```
- `topInsetPx` 取 `padding.calculateTopPadding()` 减去大标题展开高度后的折叠栏高度（或直接用状态栏 + 折叠高度）；底部用底栏顶边位置。
- 只用 1 个离屏层，成本远低于实时模糊。顶部渐隐仅在 `collapsedFraction > 0` 时启用（展开态大标题下方不需要）。

### 5.7 与 MIUIX 组件的映射 / 改造

| 现有 | 改造方式 | 优先级 |
|---|---|---|
| `Scaffold` | `containerColor = Transparent`，外层 Box 先画 `GlassBackdrop` | P0 |
| `TopAppBar` | `color = Transparent`，在 `Modifier.drawBehind` 里按 `scrollBehavior.state.collapsedFraction` 画 Thick 降级填充与底部分隔线；二级页传 `navigationIcon` = 40dp 玻璃圆按钮 | P0 / 返回 P1 |
| `NavigationBar` + `NavigationBarItem` | 换自绘 `GlassTabBar`（过渡方案：`FloatingNavigationBar(color = Thick 降级填充, cornerRadius = 32.dp)` + 外层描边） | P0 |
| `Card`（`group()`） | `Card(colors = CardDefaults.defaultColors(color = Transparent), cornerRadius = 24.dp, insideMargin = PaddingValues(0.dp), modifier = Modifier.padding(horizontal = 16.dp).padding(bottom = 12.dp).glass(RoundedCornerShape(24.dp), Thin))`，或直接写 `GlassCard(Column)` | P0 |
| `SmallTitle`（组标题） | 保留组件，改 `textColor = text.secondary`、`section` 字样式、左内边距 28dp | P0 |
| `BasicComponent / ArrowPreference / SwitchPreference / OverlayDropdownPreference` | 保留；统一 `BasicComponentColors`（标题 `text.primary`、摘要 `text.secondary`），`startAction` 传图标底座，按下效果使用 MIUIX 默认 indication 并把颜色调到白 6% | P0 / 图标 P1 |
| `Switch` | `SwitchDefaults.switchColors(...)` 按 3.6 | P0 |
| `TextField` | `TextFieldColors` + `cornerRadius = 16.dp` 实现 Inset；密码眼睛用 `trailingIcon`；聚焦光环用外层 `Modifier.border` + `onFocusChanged` | P0 |
| `TextButton`（主/次） | 主按钮换 `GlassButton(primary)`；次按钮 `GlassButton(secondary)`；`DialogButtons` 内部同步替换 | P0 |
| `OverlayDialog` | 传 `backgroundColor = Overlay 填充`，`modifier` 加描边/光点（若 modifier 不作用于对话框面板，则仅改颜色，描边 P1 用自绘 `GlassDialog`）；统一经 `GlassDialog` 包装以便做背后模糊计数 | P0 色 / P1 包装 |
| `OverlayBottomSheet` | 长列表弹窗迁移目标（群消息、会话、模型选择、任务详情、技能查看、Python） | P1 |
| `FloatingActionButton` | 按 3.10 传 shape / color / elevation | P1 |
| `InfiniteProgressIndicator / LinearProgressIndicator` | `ProgressIndicatorColors` 改强调色，轨道 `#295B8CFF` | P0 |
| Toast（`MainActivity` 里的 Card） | 换 `Box(Modifier.glass(pill, Thick))` + `AnimatedVisibility` | P0 |
| `BigStatus` | 改为 `StatusHeroCard`（3.3） | P0 |
| `Hint` | 保留纯文字（`footnote`、`text.secondary`、左右 28dp）；需要强调的改 Banner | P0 / P1 |

### 5.8 性能建议

1. **禁止**对整页或列表项使用 `Modifier.blur` / `RenderEffect`（唯一例外：弹窗打开时一次性的内容层模糊，静态，不随滚动重算）。
2. 背景只在一个 `Spacer` 上 `drawWithCache`，Brush 在 cache 块里建；漂移动画只在 draw 阶段读值。
3. 卡片不加 elevation 阴影（省去每卡一次阴影渲染，也避免透色发脏）；阴影只给 ≤ 3 个浮层同时存在。
4. `Modifier.glass` 全部基于 `drawWithCache` + `border(Brush)`，不新建 `Layer`；列表里每个 group 卡片 1 次填充 + 2 次渐变 + 1 次描边，开销可接受。
5. 滚动边缘渐隐只用 1 个 Offscreen 层；若低端机掉帧，降级为在顶/底栏自身下沿画 24dp 的背景色渐变遮罩（不需要离屏）。
6. 3 秒轮询刷新 `Store` 时，不要让 `GlassBackdrop`、`GlassTabBar` 读取 `Store` 状态（避免整树重组）。
7. 呼吸光晕 / 骨架流光用 `rememberInfiniteTransition`，组件离开屏幕即停止；减少动效时直接不创建。

---

## 6. 验收清单

### 6.1 全局
- [ ] 默认进入深色；全屏可见柔和 mesh 光斑（左上冰蓝、右中青、左下紫、右下微暖），无色带断层、无明显条纹。
- [ ] 背景在滚动、切 Tab 时不闪烁、不移动（P2 漂移开启时除外）。
- [ ] 所有色值、圆角、间距与第 2 章 token 一致；页面左右边距 16dp，卡片间距 12dp。
- [ ] 全 App 只有 4 种玻璃等级 + Ink + Inset，同一层级材质一致。
- [ ] 所有玻璃描边都是左上亮、右下回光的 135° 渐变，宽 1dp，圆角处无错位/断裂（已关闭 squircle）。
- [ ] 卡片无阴影；只有底栏、Toast、FAB、Dialog/Sheet、主按钮有阴影。
- [ ] 正文对比度 ≥ 4.5:1（`text.primary`/`text.secondary` 在 Thin 卡片上检查）。

### 6.2 顶栏 / 底栏
- [ ] 顶栏在页面顶部完全透明，向上滚动时玻璃底与分隔线随折叠程度渐显，内容在顶栏下沿柔和淡出而非硬切。
- [ ] 底栏为浮动胶囊：高 64dp、左右 16dp、离系统导航栏 10dp、圆角 32dp，可透出其下内容的颜色。
- [ ] 选中 Tab 有玻璃胶囊高光，切换时胶囊带拉伸回弹地滑动；选中图标与文字为 `#8FB0FF`，未选中 60% 白。
- [ ] 每页列表最后一项可完整滚出底栏上方，不被遮挡；Toast 显示在底栏上方 12dp。
- [ ] 二级页顶栏显示页面名与返回按钮，系统返回手势同样生效；页面内已无「← 返回更多」行（P1）。

### 6.3 组件
- [ ] 列表行：单行 ≥ 56dp、双行 ≥ 68dp；按下有白 6% 高亮且按卡片圆角裁切；危险行为红色。
- [ ] 主按钮为蓝→紫渐变胶囊，按下缩放 0.96；禁用为灰玻璃无阴影；加载显示转圈且宽度不跳动。
- [ ] 开关开启为强调蓝轨道，关闭为 16% 白轨道。
- [ ] 输入框为内凹深底，聚焦出现 1.5dp 强调描边 + 光环；密钥输入有眼睛按钮；错误态红框 + 红字。
- [ ] 下拉菜单不透字（高不透明度），选中项有对勾。
- [ ] Dialog 圆角 32dp、玻璃底、背后压暗；API 31+ 设备背后页面内容有模糊（P1），API 26–30 只有压暗且无异常。
- [ ] Toast 为玻璃胶囊，进出有上移/淡入淡出动画，停留约 2.2 秒。
- [ ] 日志、终端输出、文件预览、技能内容、聊天记录均在实底 Ink 容器内，等宽字体清晰可读，背景光斑不透出。
- [ ] 空状态、骨架屏、错误 Banner 至少在任务、技能、文件三个列表上正确出现（P1）。

### 6.4 页面
- [ ] 概览：状态卡带语义色辉光与光球，状态切换（运行中/需要登录/连接中/已停止）颜色正确且有渐变过渡；4 个状态以 2×2 格展示；服务按钮「次 | 主」排列。
- [ ] AI：账号行显示状态 Chip；接口设置中账号未连接时出现警告 Banner。
- [ ] 群管：总开关关闭时，下方卡片视觉变淡。
- [ ] 记录：群消息/会话/文件预览以 Sheet 打开（P1），长内容可滚动。
- [ ] 更多：工具入口为 2 列玻璃宫格（P1）；8 个二级页均能进入/返回，风格一致。
- [ ] 安装/启动页与主界面使用同一背景与卡片材质（P1）。

### 6.5 动效与性能
- [ ] 所有按压反馈为缩放 + 高亮，抬起有弹簧回弹，无布局抖动。
- [ ] 系统「关闭动画」后：无呼吸、无流光、无液体拉伸，转场仅淡入淡出。
- [ ] 中端机（如 Redmi Note 系列）在概览/更多页快速滚动无明显掉帧（GPU 呈现模式柱状图大部分在 16ms 线以下）。
- [ ] 3 秒轮询刷新时背景与底栏不重组（Layout Inspector 重组计数不增长）。
- [ ] 未引入任何新的 Gradle 依赖；改动集中在 `MainActivity.kt`、`ui/Common.kt`、新增 `ui/Glass.kt`（P0），P1 起才逐页调整。
