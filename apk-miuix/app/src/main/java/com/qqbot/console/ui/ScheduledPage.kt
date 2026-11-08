package com.qqbot.console.ui

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import com.qqbot.console.Store
import com.qqbot.console.patchOf
import kotlinx.coroutines.launch
import org.json.JSONObject
import top.yukonga.miuix.kmp.basic.ScrollBehavior
import top.yukonga.miuix.kmp.preference.ArrowPreference
import top.yukonga.miuix.kmp.preference.OverlayDropdownPreference

private const val SCHEDULED_INTERVAL_DEFAULT = 301
private val TARGET_TYPES = listOf("auto", "private", "group")
private val TARGET_LABELS = listOf(
    "自动（最近私聊 / 管理员）",
    "指定私聊",
    "指定群",
)
private val IMAGE_SIZES = listOf("1024x1024", "1024x1792", "1792x1024")

/** 定时消息：每隔一段时间用语言模型 + 画图模型合成一条问候发出去。 */
@Composable
fun ScheduledMessagePage(padding: PaddingValues, scrollBehavior: ScrollBehavior, onBack: () -> Unit) {
    val scope = rememberCoroutineScope()
    val cfg = Store.cfg("plugins", "scheduled") ?: JSONObject()

    val interval = Store.int("config", "plugins", "scheduled", "interval_minutes", def = SCHEDULED_INTERVAL_DEFAULT)
    val targetType = cfg.optStr("target_type").ifBlank { "auto" }.let { if (it in TARGET_TYPES) it else "auto" }
    val targetId = Store.int("config", "plugins", "scheduled", "target_id", def = 0)
    val useImage = Store.bool("config", "plugins", "scheduled", "use_image", def = true)
    val imageSize = cfg.optStr("image_size").ifBlank { IMAGE_SIZES.first() }
    var showTargetEditor by remember { mutableStateOf(targetType != "auto") }

    fun saveTargetType(value: String) {
        val values = JSONObject().put("target_type", value)
        if (value == "auto") values.put("target_id", 0)
        scope.launch { Store.patch(patchOf("plugins", "scheduled", value = values)) }
    }

    BackHandler { onBack() }

    PageList(padding, scrollBehavior) {
        group {
            ArrowPreference(title = "← 返回更多", onClick = onBack)
        }

        group("定时消息") {
            ConfigSwitch(
                "启用定时消息",
                "到点自动发一条问候，顺便刷新 5 小时额度",
                "plugins", "scheduled", "enabled", default = false,
            )
            EditPreference(
                "间隔（分钟）",
                interval.toString(),
                summary = "默认 5 小时 01 分（301 分钟），范围 5–1440",
                numeric = true,
            ) {
                it.toIntOrNull()?.let { v ->
                    Store.patch(
                        patchOf("plugins", "scheduled", "interval_minutes", value = v.coerceIn(5, 1440)),
                        "间隔已改为 ${v.coerceIn(5, 1440)} 分钟",
                    )
                }
            }

            OverlayDropdownPreference(
                title = "发送到",
                summary = when (targetType) {
                    "private" -> "指定私聊" + if (targetId > 0) "（$targetId）" else "（还没填 QQ 号）"
                    "group" -> "指定群" + if (targetId > 0) "（$targetId）" else "（还没填群号）"
                    else -> "自动（最近私聊 / 管理员）"
                },
                items = TARGET_LABELS,
                selectedIndex = TARGET_TYPES.indexOf(targetType).coerceAtLeast(0),
                onSelectedIndexChange = { i ->
                    val value = TARGET_TYPES[i]
                    showTargetEditor = value != "auto"
                    saveTargetType(value)
                },
            )
            if (showTargetEditor) {
                EditPreference(
                    if (targetType == "group") "目标群号" else "目标 QQ 号",
                    if (targetId > 0) targetId.toString() else "",
                    summary = if (targetType == "group") "填要发送到的群号" else "填要发送到的 QQ 号",
                    numeric = true,
                ) {
                    it.toLongOrNull()?.let { v ->
                        if (v > 0) {
                            scope.launch {
                                Store.patch(patchOf("plugins", "scheduled", "target_id", value = v), "目标已设置")
                            }
                        }
                    }
                }
            } else {
                Hint("「自动」会发给最近聊过天的私聊 / 管理员，不用填号码。")
            }

            ConfigSwitch(
                "同时生成一张图",
                "用画图模型生成配图，和问候合成一条消息发出",
                "plugins", "scheduled", "use_image", default = true,
            )
            if (useImage) {
                OverlayDropdownPreference(
                    title = "图片尺寸",
                    summary = imageSize,
                    items = IMAGE_SIZES,
                    selectedIndex = IMAGE_SIZES.indexOf(imageSize).coerceAtLeast(0),
                    onSelectedIndexChange = { i ->
                        scope.launch {
                            Store.patch(
                                patchOf("plugins", "scheduled", "image_size", value = IMAGE_SIZES[i]),
                                "图片尺寸已改为 ${IMAGE_SIZES[i]}",
                            )
                        }
                    },
                )
            }
        }

        item { Hint("到点会用语言模型生成问候（按时间说早上好 / 下午好等）+ 画图模型生成配图，合成一条消息发出；顺便刷新 5 小时额度。") }
        item { Hint("发送内容由主模型按当前时间生成；/报时 可立即发一次测试。") }
        item { Hint("语言模型和画图模型都使用「AI 接口」和「画图」里当前的设置，和主对话账号 / 画图账号一致。") }
    }
}
