package com.qqbot.console.ui

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.runtime.Composable
import androidx.compose.runtime.rememberCoroutineScope
import com.qqbot.console.Store
import com.qqbot.console.patchOf
import kotlinx.coroutines.launch
import org.json.JSONArray
import org.json.JSONObject
import top.yukonga.miuix.kmp.basic.ScrollBehavior
import top.yukonga.miuix.kmp.preference.ArrowPreference

private fun fmtSeconds(v: Double): String =
    if (v == v.toLong().toDouble()) v.toLong().toString() else String.format(java.util.Locale.US, "%.1f", v)

private fun jsonArrayText(arr: JSONArray?): String =
    if (arr == null) "" else (0 until arr.length()).joinToString("，") { arr.optString(it).ifBlank { arr.opt(it).toString() } }

private fun splitWords(s: String): JSONArray {
    val arr = JSONArray()
    s.split(",", "，", "、", "\n").map(String::trim).filter(String::isNotBlank).forEach(arr::put)
    return arr
}

private fun splitNumbers(s: String): JSONArray {
    val arr = JSONArray()
    s.split(",", "，", "、", " ", "\n").mapNotNull { it.trim().toLongOrNull() }.forEach(arr::put)
    return arr
}

/** 防封模式：随机延迟、全局限频、群白名单、群里隐藏链接、敏感词替换。 */
@Composable
fun SafetyPage(padding: PaddingValues, scrollBehavior: ScrollBehavior, onBack: () -> Unit) {
    val scope = rememberCoroutineScope()
    val safety = Store.obj("safety") ?: JSONObject()

    val delayMin = safety.optDouble("reply_delay_min", 1.0)
    val delayMax = safety.optDouble("reply_delay_max", 3.0)
    val perMinute = Store.int("config", "safety", "max_per_minute", def = 20)
    val perHour = Store.int("config", "safety", "max_per_hour", def = 300)
    val bannedWords = jsonArrayText(safety.optJSONArray("banned_words"))
    val whitelist = jsonArrayText(safety.optJSONArray("group_whitelist"))

    fun patch(values: JSONObject, ok: String? = null) {
        scope.launch { Store.patch(patchOf("safety", value = values), ok) }
    }

    fun saveDelay(newMin: Double, newMax: Double) {
        val values = JSONObject()
            .put("reply_delay_min", newMin)
            .put("reply_delay_max", newMax)
        patch(values, "回复延迟已改为 ${fmtSeconds(newMin)}–${fmtSeconds(newMax)} 秒")
    }

    BackHandler { onBack() }

    PageList(padding, scrollBehavior) {
        group {
            ArrowPreference(title = "← 返回更多", onClick = onBack)
        }

        group("防封模式") {
            ConfigSwitch(
                "降低风险模式",
                "随机延迟、全局限频、群白名单、群里隐藏链接、敏感词替换",
                "safety", "low_risk", default = true,
            )

            EditPreference(
                "最小延迟（秒）",
                fmtSeconds(delayMin),
                summary = "每条消息延迟 ${fmtSeconds(delayMin)}–${fmtSeconds(delayMax)} 秒（0–30）",
                numeric = true,
            ) { text ->
                val v = (text.toDoubleOrNull() ?: delayMin).coerceIn(0.0, 30.0)
                val max = maxOf(delayMax, v)
                saveDelay(v, max)
            }
            EditPreference(
                "最大延迟（秒）",
                fmtSeconds(delayMax),
                summary = "0 = 不额外延迟（0–30）",
                numeric = true,
            ) { text ->
                val v = (text.toDoubleOrNull() ?: delayMax).coerceIn(0.0, 30.0)
                val min = minOf(delayMin, v)
                saveDelay(min, v)
            }

            EditPreference(
                "每分钟最多发送",
                perMinute.toString(),
                summary = "1–600，默认 20；超出的消息会被丢弃",
                numeric = true,
            ) { text ->
                text.toIntOrNull()?.let { v ->
                    Store.patch(patchOf("safety", "max_per_minute", value = v.coerceIn(1, 600)), "每分钟上限已改为 ${v.coerceIn(1, 600)}")
                }
            }
            EditPreference(
                "每小时最多发送",
                perHour.toString(),
                summary = "1–10000，默认 300；超出的消息会被丢弃",
                numeric = true,
            ) { text ->
                text.toIntOrNull()?.let { v ->
                    Store.patch(patchOf("safety", "max_per_hour", value = v.coerceIn(1, 10000)), "每小时上限已改为 ${v.coerceIn(1, 10000)}")
                }
            }

            ConfigSwitch(
                "群里隐藏链接",
                "群里发消息时把链接替换掉，降低被判定广告的风险",
                "safety", "block_links_group", default = true,
            )
        }

        group("内容过滤") {
            EditPreference(
                "敏感词",
                bannedWords,
                summary = "用逗号分隔；命中后会被替换成安全说法",
                multiline = true,
            ) { text ->
                Store.patch(patchOf("safety", "banned_words", value = splitWords(text)), "敏感词已保存")
            }
        }

        group("生效范围") {
            EditPreference(
                "只在以下群生效",
                whitelist,
                summary = "群号用逗号分隔；留空 = 所有群",
                multiline = true,
            ) { text ->
                Store.patch(patchOf("safety", "group_whitelist", value = splitNumbers(text)), "群白名单已保存")
            }
        }

        item { Hint("开启后所有 QQ 消息发送会随机延迟并限频；群白名单非空时只在名单群说话；不会影响控制台内部操作。") }
        item { Hint("延迟只在真正发消息时生效，不影响机器人接收和处理消息；限频超出的消息会直接丢弃。") }
        item { Hint("群白名单按群号匹配：填了群号后，机器人只在列表里的群发送消息；私聊不受影响。") }
    }
}
