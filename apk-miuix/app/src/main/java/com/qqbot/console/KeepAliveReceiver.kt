package com.qqbot.console

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * 保活闹钟的接收器：每 15 分钟把服务「叫醒」一次。
 *
 * 不管服务进程还在不在，都直接 startForegroundService，让 ProotService.onStartCommand 统一处理：
 *   - 服务没了 → 重新创建，恢复 proot；
 *   - 服务在、proot 活着 → onStartCommand 直接返回，不会重复起；
 *   - 服务在、proot 死了 → 重新起 proot。
 */
class KeepAliveReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent?) {
        try {
            if (!ProotService.isWanted(context)) return
            if (!Env.validate(context)) return
            if (!ProotService.keepAliveEnabled(context)) return
            context.startForegroundService(Intent(context, ProotService::class.java))
        } catch (_: Exception) {
            // 直接拉不起时再走一遍 ensureRunning（同样有 try/catch 兜底）
            try {
                ProotService.ensureRunning(context)
            } catch (_: Throwable) {
                // 保活用，失败不能影响系统；下一轮闹钟再试
            }
        }
    }
}
