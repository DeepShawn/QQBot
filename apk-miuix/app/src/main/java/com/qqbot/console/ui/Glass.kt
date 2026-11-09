package com.qqbot.console.ui

import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsPressedAsState
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
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
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.drawWithCache
import androidx.compose.ui.draw.drawWithContent
import androidx.compose.ui.draw.shadow
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.BlendMode
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.CompositingStrategy
import androidx.compose.ui.graphics.Shape
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import top.yukonga.miuix.kmp.basic.Icon
import top.yukonga.miuix.kmp.basic.Text
import top.yukonga.miuix.kmp.overlay.OverlayDialog

// ============================================================ Design tokens（深色 · 液态玻璃）

/** 强调色 / 语义色。 */
val GlassAccent = Color(0xFF5B8CFF)
val GlassAccent2 = Color(0xFF8A6CFF)
val GlassAccentOnGlass = Color(0xFF8FB0FF)
val GlassSuccess = Color(0xFF30D158)
val GlassWarning = Color(0xFFFF9F0A)
val GlassDanger = Color(0xFFFF453A)
val GlassInfo = Color(0xFF64D2FF)
val GlassNeutral = Color(0xFF8E8E93)

/** 文字 / 分隔。 */
object GlassText {
    val primary = Color(0xFFF5F2F5FA)
    val secondary = Color(0xA3FFFFFF)
    val tertiary = Color(0x66FFFFFF)
    val disabled = Color(0x47FFFFFF)
    val divider = Color(0x14FFFFFF)
    val dim = Color(0x8C05070C)
    val mono = Color(0xFFD9E6EAF2)
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
    // 背景（L0）
    val bgTop = Color(0xFF0A0D14)
    val bgMid = Color(0xFF0B0F1A)
    val bgBottom = Color(0xFF07090F)

    // 材质填充
    val thinFill = Color(0x0FFFFFFF)          // 6% 白
    val regularFill = Color(0x17FFFFFF)       // 9% 白
    val thickFill = Color(0xC71A1F2B)         // 78% 深蓝灰
    val thickFallback = Color(0xE6151924)     // 90%
    val overlayFill = Color(0xE0141824)       // 88%
    val overlayFallback = Color(0xF2121621)   // 95%
    val inkFill = Color(0xF00A0D14)           // 94% 实底
    val insetFill = Color(0x38000000)         // 22% 黑
    val accentFill = Color(0x295B8CFF)        // 强调 16%
    val accentSoft = Color(0x1F5B8CFF)

    val thinRim = listOf(
        0f to Color(0x3DFFFFFF),
        0.55f to Color(0x0AFFFFFF),
        1f to Color(0x1FFFFFFF),
    )
    val regularRim = listOf(
        0f to Color(0x4DFFFFFF),
        0.55f to Color(0x0FFFFFFF),
        1f to Color(0x24FFFFFF),
    )
    val thickRim = listOf(
        0f to Color(0x66FFFFFF),
        0.55f to Color(0x14FFFFFF),
        1f to Color(0x33FFFFFF),
    )
    val overlayRim = listOf(
        0f to Color(0x59FFFFFF),
        0.55f to Color(0x0FFFFFFF),
        1f to Color(0x29FFFFFF),
    )
    val inkRim = listOf(0f to Color(0x14FFFFFF), 1f to Color(0x14FFFFFF))
    val insetRim = listOf(0f to Color(0x14FFFFFF), 1f to Color(0x14FFFFFF))

    fun spec(level: GlassLevel): GlassSpec = when (level) {
        GlassLevel.Thin -> GlassSpec(thinFill, 0.05f, thinRim, false, 0.dp, Color.Transparent)
        GlassLevel.Regular -> GlassSpec(regularFill, 0.08f, regularRim, false, 0.dp, Color.Transparent)
        GlassLevel.Thick -> GlassSpec(thickFill, 0.10f, thickRim, true, 16.dp, Color(0x99000000))
        GlassLevel.Overlay -> GlassSpec(overlayFill, 0.08f, overlayRim, true, 24.dp, Color(0xB3000000))
        GlassLevel.Ink -> GlassSpec(inkFill, 0f, inkRim, false, 0.dp, Color.Transparent)
        GlassLevel.Inset -> GlassSpec(insetFill, 0f, insetRim, false, 0.dp, Color.Transparent)
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
            val specular = if (spec.specular) {
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
            }
        }
        // border 在链尾：描边画在内容之上，滚动到边缘也清晰
        .border(1.dp, Brush.linearGradient(colorStops = spec.rim.toTypedArray()), shape)
}

// ============================================================ 背景（L0）

/** 深色底 + 4 团径向光斑 + 暗角；整页静态绘制（drawWithCache 缓存 Brush）。 */
@Composable
fun GlassBackdrop(modifier: Modifier = Modifier) {
    Spacer(
        modifier
            .fillMaxSize()
            .drawWithCache {
                val w = size.width
                val h = size.height
                val r = minOf(w, h).coerceAtLeast(1f)
                fun blob(c: Color, x: Float, y: Float, k: Float): Brush = Brush.radialGradient(
                    colors = listOf(c, c.copy(alpha = 0f)),
                    center = Offset(w * x, h * y),
                    radius = r * k,
                )
                val base = Brush.verticalGradient(
                    colorStops = arrayOf(
                        0f to GlassTokens.bgTop,
                        0.55f to GlassTokens.bgMid,
                        1f to GlassTokens.bgBottom,
                    ),
                )
                val blobs = listOf(
                    blob(Color(0x475B6CFF), 0.12f, 0.06f, 0.95f),
                    blob(Color(0x292BD4C8), 0.98f, 0.38f, 0.80f),
                    blob(Color(0x2EA15BFF), 0.18f, 0.80f, 1.00f),
                    blob(Color(0x14FF7A59), 0.88f, 1.02f, 0.70f),
                )
                val vignette = Brush.radialGradient(
                    colorStops = arrayOf(0.55f to Color.Transparent, 1f to Color(0x59000000)),
                    center = Offset(w / 2f, h * 0.45f),
                    radius = maxOf(w, h) * 1.1f,
                )
                onDrawBehind {
                    drawRect(base)
                    blobs.forEach { drawRect(it) }
                    drawRect(vignette)
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
    val scale by animateFloatAsState(if (pressed) 0.96f else 1f, label = "glassButtonScale")
    val shape = RoundedCornerShape(50)

    val base = modifier.height(48.dp).graphicsLayer { scaleX = scale; scaleY = scale }
    val decorated = if (primary) {
        base.shadow(6.dp, shape, ambientColor = Color.Transparent, spotColor = Color(0x8C5B8CFF))
    } else base

    Box(
        modifier = decorated
            .clip(shape)
            .background(if (primary) Brush.linearGradient(listOf(GlassAccent, GlassAccent2)) else Brush.linearGradient(listOf(GlassTokens.regularFill, GlassTokens.regularFill)))
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

/** 浮动胶囊玻璃底栏（5 Tab，选中项高光胶囊）。 */
@Composable
fun GlassTabBar(
    items: List<GlassTabItem>,
    selectedIndex: Int,
    onSelect: (Int) -> Unit,
    modifier: Modifier = Modifier,
) {
    val shape = RoundedCornerShape(GlassRadius.xl)
    Row(
        modifier = modifier
            .fillMaxWidth()
            .height(64.dp)
            .shadow(16.dp, shape, ambientColor = Color(0x33000000), spotColor = Color(0x99000000))
            .glass(GlassLevel.Thick, shape)
            .padding(6.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(0.dp),
    ) {
        items.forEachIndexed { i, item ->
            val selected = i == selectedIndex
            val tint = if (selected) GlassAccentOnGlass else Color(0x99FFFFFF)
            val interaction = remember(i) { MutableInteractionSource() }
            val pressed by interaction.collectIsPressedAsState()
            val scale by animateFloatAsState(if (pressed) 0.92f else 1f, label = "glassTabScale")
            Box(
                modifier = Modifier
                    .weight(1f)
                    .fillMaxHeight()
                    .graphicsLayer { scaleX = scale; scaleY = scale }
                    .clip(RoundedCornerShape(26.dp))
                    .then(
                        if (selected) {
                            Modifier
                                .background(GlassTokens.accentFill)
                                .border(1.dp, Brush.verticalGradient(listOf(Color(0x59FFFFFF), Color(0x0FFFFFFF))), RoundedCornerShape(26.dp))
                        } else Modifier,
                    )
                    .clickable(interactionSource = interaction, indication = null) { onSelect(i) },
                contentAlignment = Alignment.Center,
            ) {
                Column(horizontalAlignment = Alignment.CenterHorizontally) {
                    Icon(imageVector = item.icon, contentDescription = item.label, tint = tint, modifier = Modifier.size(24.dp))
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
            .border(1.dp, Color(0x14FFFFFF), shape),
        content = content,
    )
}

/** 语义色小圆点（带 25% 同色光晕）。 */
@Composable
fun GlassDot(color: Color) {
    Box(Modifier.size(15.dp).clip(CircleShape).background(color.copy(alpha = 0.25f)), contentAlignment = Alignment.Center) {
        Box(Modifier.size(9.dp).clip(CircleShape).background(color))
    }
}
