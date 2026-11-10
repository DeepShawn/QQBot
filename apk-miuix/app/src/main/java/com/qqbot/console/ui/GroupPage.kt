package com.qqbot.console.ui

import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.runtime.Composable
import com.qqbot.console.Store
import com.qqbot.console.patchOf
import org.json.JSONArray
import org.json.JSONObject
import top.yukonga.miuix.kmp.basic.ScrollBehavior
import androidx.compose.runtime.rememberCoroutineScope
import kotlinx.coroutines.launch

private val ACTIONS = listOf("recall", "recall+mute", "mute", "recall+mute+kick")
private val ACTION_LABELS = listOf("撤回", "撤回并禁言", "只禁言", "撤回、禁言并踢出")

private fun JSONArray?.joinText(): String =
    if (this == null) "" else (0 until length()).joinToString("，") { optString(it) }

private fun splitList(s: String): JSONArray {
    val arr = JSONArray()
    s.split(",", "，", "\n").map(String::trim).filter(String::isNotBlank).forEach(arr::put)
    return arr
}

private fun splitQQ(s: String): JSONArray {
    val arr = JSONArray()
    s.split(",", "，", " ", "\n").mapNotNull { it.trim().toLongOrNull() }.forEach(arr::put)
    return arr
}

@Composable
fun GroupPage(padding: PaddingValues, scrollBehavior: ScrollBehavior) {
    val scope = rememberCoroutineScope()
    val ga = Store.cfg("plugins", "group_admin") ?: JSONObject()
    val bw = ga.optJSONObject("badwords") ?: JSONObject()
    val fl = ga.optJSONObject("flood") ?: JSONObject()
    val wel = ga.optJSONObject("welcome") ?: JSONObject()

    PageList(padding, scrollBehavior) {
        group {
            ConfigSwitch("群管理总开关", "关掉后下面的规则都不生效", "plugins", "group_admin", "enabled", default = true)
            ConfigSwitch("管理指令", "引用消息 + @它说「撤回 / 禁言 / 踢了」", "plugins", "group_admin", "admin_commands", default = true)
        }
        item { Hint("机器人必须是群管理员，撤回和禁言才会生效。") }

        group("入群欢迎") {
            ConfigSwitch("新人入群时欢迎", null, "plugins", "group_admin", "welcome", "enabled")
            EditPreference("欢迎语", wel.optStr("template"), multiline = true) {
                Store.patch(patchOf("plugins", "group_admin", "welcome", "template", value = it))
            }
        }

        group("违禁词") {
            ConfigSwitch("启用", null, "plugins", "group_admin", "badwords", "enabled")
            EditPreference("关键词", bw.optJSONArray("words").joinText(), multiline = true) {
                Store.patch(patchOf("plugins", "group_admin", "badwords", "words", value = splitList(it)))
            }
            EditPreference("正则", bw.optJSONArray("regex_words").joinText(), multiline = true) {
                Store.patch(patchOf("plugins", "group_admin", "badwords", "regex_words", value = splitList(it)))
            }
            GlassSelectRow(
                title = "处理方式",
                items = ACTION_LABELS,
                selectedIndex = ACTIONS.indexOf(bw.optStr("action")).coerceAtLeast(0),
                onSelectedIndexChange = { i ->
                    scope.launch { Store.patch(patchOf("plugins", "group_admin", "badwords", "action", value = ACTIONS[i])) }
                },
            )
            EditPreference("禁言秒数", bw.opt("mute_seconds")?.toString() ?: "300", numeric = true) {
                it.toIntOrNull()?.let { v -> Store.patch(patchOf("plugins", "group_admin", "badwords", "mute_seconds", value = v)) }
            }
            ConfigSwitch("豁免群主 / 管理员", null, "plugins", "group_admin", "badwords", "ignore_admins")
            EditPreference("豁免的 QQ 号", bw.optJSONArray("ignore_users").joinText()) {
                Store.patch(patchOf("plugins", "group_admin", "badwords", "ignore_users", value = splitQQ(it)))
            }
        }

        group("防刷屏") {
            ConfigSwitch("启用", null, "plugins", "group_admin", "flood", "enabled")
            EditPreference("几秒内", fl.opt("window_seconds")?.toString() ?: "10", numeric = true) {
                it.toIntOrNull()?.let { v -> Store.patch(patchOf("plugins", "group_admin", "flood", "window_seconds", value = v)) }
            }
            EditPreference("最多几条文字", fl.opt("max_messages")?.toString() ?: "8", numeric = true) {
                it.toIntOrNull()?.let { v -> Store.patch(patchOf("plugins", "group_admin", "flood", "max_messages", value = v)) }
            }
            EditPreference("最多几张图", fl.opt("max_images")?.toString() ?: "5", numeric = true) {
                it.toIntOrNull()?.let { v -> Store.patch(patchOf("plugins", "group_admin", "flood", "max_images", value = v)) }
            }
            EditPreference("禁言秒数", fl.opt("mute_seconds")?.toString() ?: "120", numeric = true) {
                it.toIntOrNull()?.let { v -> Store.patch(patchOf("plugins", "group_admin", "flood", "mute_seconds", value = v)) }
            }
            ConfigSwitch("豁免群主 / 管理员", null, "plugins", "group_admin", "flood", "ignore_admins")
            EditPreference("豁免的 QQ 号", fl.optJSONArray("ignore_users").joinText()) {
                Store.patch(patchOf("plugins", "group_admin", "flood", "ignore_users", value = splitQQ(it)))
            }
        }
    }
}
