package com.qqbot.console

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * 保活闹钟的接收器：每 15 分钟确认前台服务还活着，
 * 被系统清理掉就重新拉起来（用户没关服务的前提下）。
 */
class KeepAliveReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent?) {
        try {
            ProotService.ensureRunning(context)
        } catch (_: Throwable) {
            // 保活用，失败不能影响系统；下一轮闹钟再试
        }
    }
}
