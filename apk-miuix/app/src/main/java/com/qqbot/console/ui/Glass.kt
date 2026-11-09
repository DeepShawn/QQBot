package com.qqbot.console.ui

import android.provider.Settings
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.Spring
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.spring
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectHorizontalDragGestures
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsPressedAsState
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.BoxScope
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.State
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.drawWithCache
import androidx.compose.ui.draw.drawWithContent
import androidx.compose.ui.draw.shadow
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.unit.IntOffset
import androidx.compose.ui.graphics.BlendMode
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.CompositingStrategy
import androidx.compose.ui.graphics.Shape
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.graphics.lerp
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.foundation.layout.offset
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import top.yukonga.miuix.kmp.basic.Icon
import top.yukonga.miuix.kmp.basic.Text
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.floor
import kotlin.math.roundToInt
import kotlin.math.sin
import top.yukonga.miuix.kmp.overlay.OverlayDialog

// ============================================================ Design tokens（深/浅两套 · 液态玻璃）

/** 语义色（强调色由 LocalAccent/Accent.kt 提供，可切换）。 */
val GlassSuccess: Color get() = GlassTokens.success
val GlassWarning: Color get() = GlassTokens.warning
val GlassDanger: Color get() = GlassTokens.danger
val GlassInfo: Color get() = GlassTokens.info
val GlassNeutral: Color get() = GlassTokens.neutral

/** 文字 / 分隔。随明暗主题自动切换。 */
object GlassText {
    val primary get() = if (GlassTokens.dark) Color(0xFFF5F2F5FA) else Color(0xFF0B0F1A)
    val secondary get() = if (GlassTokens.dark) Color(0xA3FFFFFF) else Color(0xB30B0F1A)
    val tertiary get() = if (GlassTokens.dark) Color(0x66FFFFFF) else Color(0x800B0F1A)
    val disabled get() = if (GlassTokens.dark) Color(0x47FFFFFF) else Color(0x470B0F1A)
    val divider get() = if (GlassTokens.dark) Color(0x14FFFFFF) else Color(0x140B0F1A)
    val dim get() = if (GlassTokens.dark) Color(0x8C05070C) else Color(0x4D1B2340)
    val mono get() = if (GlassTokens.dark) Color(0xFFD9E6EAF2) else Color(0xFF1B2340)
}

/** 圆角 / 间距。 */
object GlassRadius {
    val xs = 8.dp
    val sm = 12.dp
    val md = 16.dp
    val lg = 24.dp
    val xl = 32.dp
}

enum class GlassLevel { Thin, Regular, Thick, Overlay, Ink, Inset }

/** 一种材质等级的完整参数（填充 / 光泽 / 描边 / 阴影）。 */
data class GlassSpec(
    val fill: Color,
    val sheen: Float,
    val rim: List<Pair<Float, Color>>,
    val specular: Boolean,
    val elevation: Dp,
    val spot: Color,
)

object GlassTokens {
    /**
     * 当前是否深色主题。MainActivity 依据 config `ui.theme` 写入；
     * 所有 token 都是 getter，切换主题会立即使读取它们的 Compose 组件重组。
     */
    var dark by mutableStateOf(true)

    /**
     * 低功耗模式（config `ui.low_power`）：停背景动画、弱化玻璃高光/镜面/阴影、
     * 关闭按压 spring 与页间变换。MainActivity 写入，切换即时生效。
     */
    var lowPower by mutableStateOf(false)

    // ---------- 语义色 ----------
    val success get() = if (dark) Color(0xFF30D158) else Color(0xFF1E9E4A)
    val warning get() = if (dark) Color(0xFFFF9F0A) else Color(0xFFC77700)
    val danger get() = if (dark) Color(0xFFFF453A) else Color(0xFFE5352B)
    val info get() = if (dark) Color(0xFF64D2FF) else Color(0xFF0A84C6)
    val neutral get() = if (dark) Color(0xFF8E8E93) else Color(0xFF6E6E73)

    // ---------- 背景（L0） ----------
    val bgTop get() = if (dark) Color(0xFF0A0D14) else Color(0xFFF3F5F9)
    val bgMid get() = if (dark) Color(0xFF0B0F1A) else Color(0xFFEAEEF5)
    val bgBottom get() = if (dark) Color(0xFF07090F) else Color(0xFFE6EAF2)

    // ---------- 材质填充 ----------
    val thinFill get() = if (dark) Color(0x0FFFFFFF) else Color(0x9EFFFFFF)       // 6% / 62%
    val regularFill get() = if (dark) Color(0x17FFFFFF) else Color(0xB8FFFFFF)    // 9% / 72%
    val thickFill get() = if (dark) Color(0xC71A1F2B) else Color(0xD9F7F8FB)      // 78% / 85%
    val thickFallback get() = if (dark) Color(0xE6151924) else Color(0xE8F7F8FB)
    val overlayFill get() = if (dark) Color(0xE0141824) else Color(0xEBF8F9FC)    // 88% / 92%
    val overlayFallback get() = if (dark) Color(0xF2121621) else Color(0xF5F8F9FC)
    val inkFill get() = if (dark) Color(0xF00A0D14) else Color(0xF5FFFFFF)       // 实底
    val insetFill get() = if (dark) Color(0x38000000) else Color(0x0F0B0F1A)     // 内凹
    val inkBorder get() = if (dark) Color(0x14FFFFFF) else Color(0x140B0F1A)
    val accentFill get() = Color(0x295B8CFF)
    val accentSoft get() = Color(0x1F5B8CFF)

    // ---------- 描边（1dp，135° 三段） ----------
    val thinRim get() = if (dark) {
        listOf(0f to Color(0x3DFFFFFF), 0.55f to Color(0x0AFFFFFF), 1f to Color(0x1FFFFFFF))
    } else {
        listOf(0f to Color(0xE6FFFFFF), 0.55f to Color(0x4DFFFFFF), 1f to Color(0x99FFFFFF))
    }
    val regularRim get() = if (dark) {
        listOf(0f to Color(0x4DFFFFFF), 0.55f to Color(0x0FFFFFFF), 1f to Color(0x24FFFFFF))
    } else {
        listOf(0f to Color(0xE6FFFFFF), 0.55f to Color(0x4DFFFFFF), 1f to Color(0x99FFFFFF))
    }
    val thickRim get() = if (dark) {
        listOf(0f to Color(0x66FFFFFF), 0.55f to Color(0x14FFFFFF), 1f to Color(0x33FFFFFF))
    } else {
        listOf(0f to Color(0xE6FFFFFF), 0.55f to Color(0x4DFFFFFF), 1f to Color(0x99FFFFFF))
    }
    val overlayRim get() = if (dark) {
        listOf(0f to Color(0x59FFFFFF), 0.55f to Color(0x0FFFFFFF), 1f to Color(0x29FFFFFF))
    } else {
        listOf(0f to Color(0xE6FFFFFF), 0.55f to Color(0x4DFFFFFF), 1f to Color(0x99FFFFFF))
    }
    val inkRim get() = listOf(0f to inkBorder, 1f to inkBorder)
    val insetRim get() = listOf(0f to inkBorder, 1f to inkBorder)

    // ---------- 浮层阴影 ----------
    val shadowAmbient get() = if (dark) Color(0x33000000) else Color(0x0F1B2340)
    val shadowSpot get() = if (dark) Color(0x99000000) else Color(0x1F1B2340)

    // ---------- 背景光斑（暗 / 浅两套） ----------
    val blobB get() = if (dark) Color(0x292BD4C8) else Color(0x2E5FE3D6)
    val blobC get() = if (dark) Color(0x2EA15BFF) else Color(0x2EC79BFF)
    val blobD get() = if (dark) Color(0x14FF7A59) else Color(0x24FFB59A)
    val vignette get() = if (dark) Color(0x59000000) else Color.Transparent
    val lightTopSheen get() = if (dark) Color.Transparent else Color(0x14FFFFFF)

    fun spec(level: GlassLevel): GlassSpec {
        val spot = if (dark) Color(0x99000000) else Color(0x1F1B2340)
        // 低功耗：高光/镜面减弱、去掉浮层阴影；填充与文字不变，保证可读性
        val sheenScale = if (lowPower) 0.35f else 1f
        val shadow = !lowPower
        return when (level) {
            GlassLevel.Thin -> GlassSpec(thinFill, 0.05f * sheenScale, thinRim, false, 0.dp, Color.Transparent)
            GlassLevel.Regular -> GlassSpec(regularFill, 0.08f * sheenScale, regularRim, false, 0.dp, Color.Transparent)
            GlassLevel.Thick -> GlassSpec(thickFill, 0.10f * sheenScale, thickRim, false, if (shadow) 16.dp else 0.dp, spot)
            GlassLevel.Overlay -> GlassSpec(overlayFill, 0.08f * sheenScale, overlayRim, false, if (shadow) 24.dp else 0.dp, spot)
            GlassLevel.Ink -> GlassSpec(inkFill, 0f, inkRim, false, 0.dp, Color.Transparent)
            GlassLevel.Inset -> GlassSpec(insetFill, 0f, insetRim, false, 0.dp, Color.Transparent)
        }
    }
}

// ============================================================ 玻璃 Modifier

/**
 * 液态玻璃材质：半透明填充 + 顶部光泽 + 镜面光点 + 135° 渐变描边（内容之上）。
 * 层级越高：填充越不透明、描边越亮、阴影越重；Thin/Regular 不加阴影。
 */
fun Modifier.glass(
    level: GlassLevel = GlassLevel.Thin,
    shape: Shape = RoundedCornerShape(GlassRadius.lg),
    tint: Color = Color.Unspecified,
): Modifier {
    val spec = GlassTokens.spec(level)
    var m = this
    if (spec.elevation > 0.dp) {
        m = m.shadow(
            elevation = spec.elevation,
            shape = shape,
            ambientColor = spec.spot.copy(alpha = 0.25f),
            spotColor = spec.spot,
        )
    }
    return m
        .clip(shape)
        .background(spec.fill)
        .drawWithCache {
            val sheen = Brush.verticalGradient(
                0f to Color.White.copy(alpha = spec.sheen),
                0.45f to Color.Transparent,
            )
            val specular = if (spec.specular && !GlassTokens.lowPower) {
                Brush.radialGradient(
                    colors = listOf(Color(0x1AFFFFFF), Color.Transparent),
                    center = Offset(size.width * 0.22f, 0f),
                    radius = (size.width * 0.6f).coerceAtLeast(1f),
                )
            } else null
            val glow = if (tint != Color.Unspecified) {
                Brush.radialGradient(
                    colors = listOf(tint.copy(alpha = 0.22f), Color.Transparent),
                    center = Offset(size.width * 0.15f, size.height * 0.10f),
                    radius = (size.width * 0.9f).coerceAtLeast(1f),
                )
            } else null
            onDrawBehind {
                glow?.let { drawRect(it) }
                drawRect(sheen)
                specular?.let { drawRect(it) }
                // 更细的顶部内高光（0.5dp）；低功耗模式省略
                if (!GlassTokens.lowPower) {
                    drawRect(
                        color = Color.White.copy(alpha = if (GlassTokens.dark) 0.10f else 0.55f),
                        topLeft = Offset(0f, 0.5.dp.toPx()),
                        size = Size(size.width, 0.5.dp.toPx()),
                    )
                }
            }
        }
        // border 在链尾：描边画在内容之上，滚动到边缘也清晰
        .border(1.dp, Brush.linearGradient(colorStops = spec.rim.toTypedArray()), shape)
}

// ============================================================ 背景（L0）

/** 4 团光斑的基准位置 / 半径 / 漂移幅度 / 相位（不同相位避免同步移动）。 */
private val BackdropBlobs = listOf(
    floatArrayOf(0.12f, 0.06f, 0.95f, 0.030f, 0.00f),  // 冰蓝（强调色）
    floatArrayOf(0.98f, 0.38f, 0.80f, 0.025f, 0.33f),  // 青
    floatArrayOf(0.18f, 0.80f, 1.00f, 0.030f, 0.66f),  // 紫
    floatArrayOf(0.88f, 1.02f, 0.70f, 0.020f, 0.15f),  // 暖
)

/**
 * 深/浅色底 + 4 团径向光斑 +（暗色）暗角。
 * 光斑缓慢漂移 + 轻微呼吸（约 19–30 秒一轮）；系统关闭动画时保持静态。
 * 只在 Root 层画一次；整屏绘制，不随滚动重算。
 */
@Composable
fun GlassBackdrop(modifier: Modifier = Modifier) {
    val ctx = LocalContext.current
    val accent = LocalAccent.current
    val dark = GlassTokens.dark
    val lowPower = GlassTokens.lowPower

    // 尊重系统「动画时长缩放 = 0」（移除动画）
    val animationsEnabled = remember(ctx) {
        try {
            Settings.Global.getFloat(ctx.contentResolver, Settings.Global.ANIMATOR_DURATION_SCALE, 1f) > 0f
        } catch (_: Exception) {
            true
        }
    }
    val transition = rememberInfiniteTransition(label = "glassBackdrop")
    val animate = animationsEnabled && !lowPower
    val driftState: State<Float> = if (animate) {
        transition.animateFloat(
            initialValue = 0f,
            targetValue = 1f,
            animationSpec = infiniteRepeatable(tween(30000, easing = LinearEasing)),
            label = "glassDrift",
        )
    } else {
        remember { mutableFloatStateOf(0.25f) }
    }
    val breathState: State<Float> = if (animate) {
        transition.animateFloat(
            initialValue = 0f,
            targetValue = 1f,
            animationSpec = infiniteRepeatable(tween(19000, easing = LinearEasing), RepeatMode.Reverse),
            label = "glassBreath",
        )
    } else {
        remember { mutableFloatStateOf(0.5f) }
    }

    Spacer(
        modifier
            .fillMaxSize()
            .drawWithCache {
                val w = size.width
                val h = size.height
                val base = Brush.verticalGradient(
                    colorStops = arrayOf(
                        0f to GlassTokens.bgTop,
                        0.55f to GlassTokens.bgMid,
                        1f to GlassTokens.bgBottom,
                    ),
                )
                val vignette = if (dark) {
                    Brush.radialGradient(
                        colorStops = arrayOf(0.55f to Color.Transparent, 1f to GlassTokens.vignette),
                        center = Offset(w / 2f, h * 0.45f),
                        radius = maxOf(w, h) * 1.1f,
                    )
                } else null
                val topSheen = if (!dark) {
                    Brush.verticalGradient(0f to GlassTokens.lightTopSheen, 0.5f to Color.Transparent)
                } else null
                val accentBlob = if (dark) accent.blob else accent.primary.copy(alpha = 0.22f)
                val blobColors = listOf(accentBlob, GlassTokens.blobB, GlassTokens.blobC, GlassTokens.blobD)

                onDrawBehind {
                    drawRect(base)
                    // 动画状态只在 draw 阶段读，不触发整页重组
                    val t = driftState.value
                    val breath = 0.5f + (breathState.value - 0.5f) * 0.10f
                    val wd = size.width
                    val hd = size.height
                    val r = minOf(wd, hd).coerceAtLeast(1f)
                    BackdropBlobs.forEachIndexed { i, b ->
                        val angle = (t + b[4]) * 2f * PI.toFloat()
                        val dx = sin(angle) * b[3] * wd
                        val dy = cos(angle * 0.8f) * b[3] * 0.7f * hd
                        val color = blobColors[i]
                        val brush = Brush.radialGradient(
                            colors = listOf(color, color.copy(alpha = 0f)),
                            center = Offset(wd * b[0] + dx, hd * b[1] + dy),
                            radius = (r * b[2] * (1f + (breath - 0.5f) * 0.10f)).coerceAtLeast(1f),
                        )
                        drawRect(brush)
                    }
                    topSheen?.let { drawRect(it) }
                    vignette?.let { drawRect(it) }
                }
            },
    )
}

// ============================================================ 滚动边缘渐隐

/**
 * 列表上下边缘淡出，代替昂贵的全屏实时模糊（规格 5.6）。
 * 只用 1 个离屏层：顶部 8dp、底部 40dp 内容渐隐。
 */
fun Modifier.scrollEdgeFade(topFade: Dp = 8.dp, bottomFade: Dp = 40.dp): Modifier =
    this
        .graphicsLayer { compositingStrategy = CompositingStrategy.Offscreen }
        .drawWithContent {
            drawContent()
            val top = topFade.toPx()
            val bottom = bottomFade.toPx()
            val h = size.height
            if (h <= top + bottom + 1f) {
                drawRect(Color.Black, blendMode = BlendMode.DstIn)
                return@drawWithContent
            }
            drawRect(
                brush = Brush.verticalGradient(
                    colorStops = arrayOf(
                        0f to Color.Transparent,
                        (top / h) to Color.Black,
                        ((h - bottom) / h) to Color.Black,
                        1f to Color.Transparent,
                    ),
                ),
                blendMode = BlendMode.DstIn,
            )
        }

// ============================================================ 玻璃卡片 / 弹窗 / 按钮

/** Thin 玻璃分组卡片（替代 MIUIX Card，避免 elevation 阴影透色）。 */
@Composable
fun GlassCard(
    modifier: Modifier = Modifier,
    level: GlassLevel = GlassLevel.Thin,
    shape: Shape = RoundedCornerShape(GlassRadius.lg),
    tint: Color = Color.Unspecified,
    content: @Composable ColumnScope.() -> Unit,
) {
    Column(modifier.fillMaxWidth().glass(level, shape, tint), content = content)
}

/** Overlay 材质弹窗（统一深色玻璃底 + 明亮标题）。 */
@Composable
fun GlassDialog(
    show: Boolean,
    title: String? = null,
    summary: String? = null,
    onDismissRequest: () -> Unit,
    content: @Composable () -> Unit,
) {
    OverlayDialog(
        show = show,
        title = title,
        titleColor = GlassText.primary,
        summary = summary,
        summaryColor = GlassText.secondary,
        backgroundColor = GlassTokens.overlayFill,
        onDismissRequest = onDismissRequest,
        content = content,
    )
}

/**
 * 玻璃按钮：主按钮 = 强调色渐变胶囊 + 阴影；次按钮 = Regular 玻璃。
 * 按下缩放 0.96（graphicsLayer，不触发布局）。
 */
@Composable
fun GlassButton(
    text: String,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    primary: Boolean = true,
) {
    val interaction = remember { MutableInteractionSource() }
    val pressed by interaction.collectIsPressedAsState()
    val scale by animateFloatAsState(
        targetValue = if (pressed && !GlassTokens.lowPower) 0.96f else 1f,
        animationSpec = spring(dampingRatio = 0.55f, stiffness = 700f),
        label = "glassButtonScale",
    )
    val shape = RoundedCornerShape(50)
    val accent = LocalAccent.current

    val base = modifier.height(48.dp).graphicsLayer { scaleX = scale; scaleY = scale }
    val decorated = if (primary) {
        base.shadow(6.dp, shape, ambientColor = Color.Transparent, spotColor = accent.glow)
    } else base

    Box(
        modifier = decorated
            .clip(shape)
            .background(if (primary) Brush.linearGradient(listOf(accent.primary, accent.secondary)) else Brush.linearGradient(listOf(GlassTokens.regularFill, GlassTokens.regularFill)))
            .then(if (primary) Modifier else Modifier.border(1.dp, Brush.linearGradient(colorStops = GlassTokens.regularRim.toTypedArray()), shape))
            .clickable(interactionSource = interaction, indication = null, onClick = onClick),
        contentAlignment = Alignment.Center,
    ) {
        Text(
            text,
            color = if (primary) Color.White else GlassText.primary,
            fontSize = 16.sp,
            fontWeight = FontWeight.Medium,
            modifier = Modifier.padding(horizontal = 20.dp, vertical = 12.dp),
        )
    }
}

/** 浮动胶囊玻璃底栏项。 */
data class GlassTabItem(val label: String, val icon: ImageVector)

/**
 * 浮动胶囊玻璃底栏（5 Tab）。
 * [pageOffset] 是 pager 的连续页偏移（currentPage + offsetFraction），
 * 选中胶囊在 layout 阶段随它连续移动（不逐帧重组）；点击走动画滚页。
 */
@Composable
fun GlassTabBar(
    items: List<GlassTabItem>,
    selectedIndex: Int,
    pageOffset: () -> Float,
    onSelect: (Int) -> Unit,
    onDragToPage: (page: Int, offsetFraction: Float) -> Unit,
    modifier: Modifier = Modifier,
) {
    if (items.isEmpty()) return
    val count = items.size
    val shape = RoundedCornerShape(GlassRadius.xl)
    val accent = LocalAccent.current
    val density = androidx.compose.ui.platform.LocalDensity.current

    BoxWithConstraints(
        modifier = modifier
            .fillMaxWidth()
            .height(64.dp)
            .shadow(16.dp, shape, ambientColor = GlassTokens.shadowAmbient, spotColor = GlassTokens.shadowSpot)
            .glass(GlassLevel.Thick, shape)
            .padding(6.dp)
            // 底栏上左右滑动选择 Tab：胶囊跟手连续移动，松手吸附最近页并驱动 pager
            .pointerInput(count) {
                val itemPx = size.width.toFloat() / count
                var startOffset = 0f
                var accum = 0f
                detectHorizontalDragGestures(
                    onDragStart = {
                        startOffset = pageOffset().coerceIn(0f, (count - 1).toFloat())
                        accum = 0f
                    },
                    onHorizontalDrag = { change, dragAmount ->
                        change.consume()   // 消费横向手势，避免带动页面/列表
                        accum += dragAmount
                        val abs = (startOffset + accum / itemPx).coerceIn(0f, (count - 1).toFloat())
                        val page = floor(abs).toInt().coerceIn(0, count - 1)
                        val frac = if (page == count - 1) 0f else abs - page
                        onDragToPage(page, frac)
                    },
                    onDragEnd = {
                        val abs = (startOffset + accum / itemPx).coerceIn(0f, (count - 1).toFloat())
                        onSelect(abs.roundToInt().coerceIn(0, count - 1))
                    },
                    onDragCancel = {},
                )
            },
    ) {
        val itemWidth = maxWidth / count
        val indicatorWidth = itemWidth - 4.dp

        // 选中胶囊：连续跟随 pageOffset（layout 阶段读，不触发重组）
        Box(
            Modifier
                .offset {
                    val off = pageOffset().coerceIn(0f, (count - 1).toFloat())
                    IntOffset((off * itemWidth.toPx()).toInt(), 0)
                }
                .width(indicatorWidth)
                .fillMaxHeight()
                .clip(RoundedCornerShape(26.dp))
                .background(accent.fill)
                .border(
                    1.dp,
                    Brush.verticalGradient(listOf(accent.primary.copy(alpha = 0.55f), Color(0x0FFFFFFF))),
                    RoundedCornerShape(26.dp),
                ),
        )

        Row(Modifier.fillMaxSize(), verticalAlignment = Alignment.CenterVertically) {
            items.forEachIndexed { i, item ->
                val selected = i == selectedIndex
                val tint = if (selected) {
                    if (GlassTokens.dark) accent.onGlass else accent.primary
                } else {
                    GlassText.secondary
                }
                val interaction = remember(i) { MutableInteractionSource() }
                val pressed by interaction.collectIsPressedAsState()
                val scale by animateFloatAsState(
                    targetValue = if (pressed && !GlassTokens.lowPower) 0.92f else 1f,
                    animationSpec = spring(dampingRatio = 0.5f, stiffness = 800f),
                    label = "glassTabScale",
                )
                Box(
                    modifier = Modifier
                        .weight(1f)
                        .fillMaxHeight()
                        .graphicsLayer { scaleX = scale; scaleY = scale }
                        .clip(RoundedCornerShape(26.dp))
                        .clickable(interactionSource = interaction, indication = null) { onSelect(i) },
                    contentAlignment = Alignment.Center,
                ) {
                    Column(horizontalAlignment = Alignment.CenterHorizontally) {
                        Icon(
                            imageVector = item.icon,
                            contentDescription = item.label,
                            tint = tint,
                            modifier = Modifier.size(24.dp),
                        )
                        Spacer(Modifier.height(2.dp))
                        Text(
                            item.label,
                            color = tint,
                            fontSize = 11.sp,
                            fontWeight = if (selected) FontWeight.SemiBold else FontWeight.Medium,
                        )
                    }
                }
            }
        }
    }
}

/** Ink 实底容器：日志 / 终端输出 / 技能内容等长文本，禁止透出背景光斑。 */
@Composable
fun InkBox(
    modifier: Modifier = Modifier,
    shape: Shape = RoundedCornerShape(GlassRadius.md),
    content: @Composable BoxScope.() -> Unit,
) {
    Box(
        modifier
            .clip(shape)
            .background(GlassTokens.inkFill)
            .border(1.dp, GlassTokens.inkBorder, shape),
        content = content,
    )
}

/** 小圆点（带 25% 同色光晕）；默认取当前主题色。 */
@Composable
fun GlassDot(color: Color = LocalAccent.current.primary) {
    Box(Modifier.size(15.dp).clip(CircleShape).background(color.copy(alpha = 0.25f)), contentAlignment = Alignment.Center) {
        Box(Modifier.size(9.dp).clip(CircleShape).background(color))
    }
}
