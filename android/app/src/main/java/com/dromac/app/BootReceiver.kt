package com.dromac.app

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import androidx.core.content.ContextCompat

// Starts the control server right after boot, so Dromac is usable again
// without the user having to manually reopen the app every time the phone
// restarts -- matches the rest of the app's "no manual step" philosophy.
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED) {
            try {
                ContextCompat.startForegroundService(context, Intent(context, StationServerService::class.java))
            } catch (_: Exception) {}
        }
    }
}
