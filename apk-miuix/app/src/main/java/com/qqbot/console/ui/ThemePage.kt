package com.qqbot.console.ui

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.runtime.Composable
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.qqbot.console.Store
import com.qqbot.console.patchOf
import kotlinx.coroutines.launch
import top.yukonga.miuix.kmp.basic.ScrollBehavior
import top.yukonga.miuix.kmp.basic.Text
import top.yukonga.miuix.kmp.preference.ArrowPreference
import top.yukonga.miuix.kmp.preference.OverlayDropdownPreference

private val THEME_IDS = listOf("dark", "light", "system")
private val THEME_LABELS = listOf("深色（默认）", "浅色", "跟随系统")

/** 主题色选择页：外观模式（深/浅/跟随系统）+ 强调色预设，点击立即写入 config 并全局生效。 */
@Composable
fun ThemePage(padding: PaddingValues, scrollBehavior: ScrollBehavior, onBack: () -> Unit) {
    val scope = rememberCoroutineScope()
    val currentId = Store.str("config", "ui", "accent")
    val current = accentById(currentId)
    val themeMode = Store.str("config", "ui", "theme").ifBlank { "dark" }

    BackHandler { onBack() }

    PageList(padding, scrollBehavior) {
        group {
            ArrowPreference(title = "← 返回更多", onClick = onBack)
        }

        // 当前主题预览：大渐变胶囊 + 示例按钮 / 选中态
        item {
            GlassCard(
                modifier = Modifier.padding(horizontal = 16.dp).padding(bottom = 12.dp),
                level = GlassLevel.Regular,
                tint = current.primary,
            ) {
                Column(Modifier.padding(20.dp)) {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Box(
                            Modifier
                                .size(44.dp)
                                .clip(CircleShape)
                                .background(Brush.linearGradient(listOf(current.primary, current.secondary))),
                        )
                        Spacer(Modifier.size(14.dp))
                        Column {
                            Text(
                                "当前主题色：${current.name}",
                                fontSize = 17.sp,
                                fontWeight = FontWeight.SemiBold,
                                color = GlassText.primary,
                            )
                            Text("切换后立即生效", fontSize = 13.sp, color = GlassText.secondary)
                        }
                    }
                    Spacer(Modifier.height(16.dp))
                    Row(
                        Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.spacedBy(12.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        GlassButton(text = "示例按钮", onClick = {}, modifier = Modifier.weight(1f))
                        Box(
                            Modifier
                                .height(44.dp)
                                .clip(RoundedCornerShape(22.dp))
                                .background(current.fill)
                                .border(1.dp, current.primary.copy(alpha = 0.55f), RoundedCornerShape(22.dp))
                                .padding(horizontal = 18.dp),
                            contentAlignment = Alignment.Center,
                        ) {
                            Text(
                                "选中态",
                                fontSize = 14.sp,
                                fontWeight = FontWeight.Medium,
                                color = current.onGlass,
                            )
                        }
                    }
                }
            }
        }

        group("外观") {
            OverlayDropdownPreference(
                title = "外观模式",
                summary = THEME_LABELS[THEME_IDS.indexOf(themeMode).coerceAtLeast(0)],
                items = THEME_LABELS,
                selectedIndex = THEME_IDS.indexOf(themeMode).coerceAtLeast(0),
                onSelectedIndexChange = { i ->
                    scope.launch {
                        Store.patch(
                            patchOf("ui", "theme", value = THEME_IDS[i]),
                            "外观已切换为「${THEME_LABELS[i]}」",
                        )
                    }
                },
            )
            ConfigSwitch(
                "低功耗模式",
                "适合低端机 / 发热 / 卡顿时开启：停背景动画、弱化玻璃高光与阴影、关闭页间缩放视差和按压回弹",
                "ui", "low_power", default = false,
            )
            Hint("浅色模式使用白系玻璃 + 深色文字；「跟随系统」跟随手机的深色模式开关。")
        }

        group("主题色") {
            Column(Modifier.fillMaxWidth().padding(12.dp)) {
                AccentPresets.chunked(2).forEach { rowItems ->
                    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                        rowItems.forEach { accent ->
                            AccentSwatch(
                                accent = accent,
                                selected = accent.id == current.id,
                                modifier = Modifier.weight(1f),
                                onClick = {
                                    scope.launch {
                                        Store.patch(
                                            patchOf("ui", "accent", value = accent.id),
                                            "主题色已切换为「${accent.name}」",
                                        )
                                    }
                                },
                            )
                        }
                        if (rowItems.size == 1) Spacer(Modifier.weight(1f))
                    }
                    Spacer(Modifier.height(8.dp))
                }
            }
        }

        item { Hint("主题色只影响强调元素（按钮 / 选中态 / 开关 / 背景光斑），不影响文字可读性。") }
        item { Hint("默认深色玻璃不变；切换后所有页面立即生效，无需重启。") }
    }
}

@Composable
private fun AccentSwatch(
    accent: AccentPreset,
    selected: Boolean,
    modifier: Modifier,
    onClick: () -> Unit,
) {
    Column(
        modifier
            .clip(RoundedCornerShape(GlassRadius.md))
            .clickable(onClick = onClick)
            .padding(vertical = 10.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Box(contentAlignment = Alignment.Center) {
            Box(
                Modifier
                    .size(46.dp)
                    .clip(CircleShape)
                    .background(Brush.linearGradient(listOf(accent.primary, accent.secondary))),
            )
            if (selected) {
                Box(
                    Modifier
                        .size(46.dp)
                        .clip(CircleShape)
                        .border(2.dp, Color.White.copy(alpha = 0.9f), CircleShape),
                )
            }
        }
        Spacer(Modifier.height(8.dp))
        Text(
            accent.name,
            fontSize = 13.sp,
            color = if (selected) GlassText.primary else GlassText.secondary,
            fontWeight = if (selected) FontWeight.SemiBold else FontWeight.Normal,
        )
    }
}
