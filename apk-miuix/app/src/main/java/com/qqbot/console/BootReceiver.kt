package com.qqbot.console

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/** 开机 / App 更新后：如果用户之前开着服务，就自动拉起。 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        if (ProotService.isWanted(ctx) && Env.validate(ctx)) {
            try { ProotService.start(ctx) } catch (_: Exception) {}
        }
    }
}
