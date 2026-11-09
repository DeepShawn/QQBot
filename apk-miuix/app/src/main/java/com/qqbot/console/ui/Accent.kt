package com.qqbot.console.ui

import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.lerp

/**
 * 主题色（强调色）预设。深色液态玻璃的彩色只出现在三处：
 * 背景光斑、强调元素（按钮 / 选中态 / 开关）、语义状态。这里只管强调色。
 */
data class AccentPreset(
    val id: String,
    val name: String,
    val primary: Color,
    val secondary: Color,
) {
    /** 玻璃上的强调文字/图标颜色（往白色提亮，保证深色底上可读）。 */
    val onGlass: Color get() = lerp(primary, Color.White, 0.32f)

    /** 选中胶囊 / 着色玻璃的填充色（强调 16%）。 */
    val fill: Color get() = primary.copy(alpha = 0.16f)

    /** 高光/阴影等使用的半透明强调色。 */
    val glow: Color get() = primary.copy(alpha = 0.55f)

    /** 低透明度光斑色（背景主光斑）。 */
    val blob: Color get() = primary.copy(alpha = 0.28f)
}

val AccentPresets = listOf(
    AccentPreset("ice", "冰蓝", Color(0xFF5B8CFF), Color(0xFF8A6CFF)),
    AccentPreset("violet", "紫罗兰", Color(0xFF9B7BFF), Color(0xFFC084FC)),
    AccentPreset("mint", "薄荷", Color(0xFF3DDC97), Color(0xFF4ECDC4)),
    AccentPreset("rose", "樱花", Color(0xFFFF7AA2), Color(0xFFFF9F7A)),
    AccentPreset("amber", "琥珀", Color(0xFFFFB547), Color(0xFFFF8A3D)),
    AccentPreset("ocean", "海盐", Color(0xFF38BDF8), Color(0xFF22D3EE)),
    AccentPreset("lime", "青柠", Color(0xFFA3E635), Color(0xFF4ADE80)),
    AccentPreset("sunset", "晚霞", Color(0xFFFF6B6B), Color(0xFFFFA94D)),
    AccentPreset("graphite", "石墨", Color(0xFFA0AEC0), Color(0xFF718096)),
    AccentPreset("forest", "森林", Color(0xFF34D399), Color(0xFF059669)),
    AccentPreset("indigo", "靛蓝", Color(0xFF6366F1), Color(0xFF818CF8)),
    AccentPreset("peach", "蜜桃", Color(0xFFFB923C), Color(0xFFFCA5A5)),
)

/** 按 id 取主题色；未知 / 空 → 冰蓝（默认）。 */
fun accentById(id: String?): AccentPreset =
    AccentPresets.firstOrNull { it.id == id } ?: AccentPresets[0]

/** 当前主题色；MainActivity 根据 config `ui.accent` 提供。 */
val LocalAccent = staticCompositionLocalOf { AccentPresets[0] }
