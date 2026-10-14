package com.dromac.bthelper

import android.bluetooth.BluetoothAdapter
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log

// Turns Bluetooth on when Dromac asks (see the manifest for who may ask).
class EnableReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        @Suppress("DEPRECATION")
        val ok = try { BluetoothAdapter.getDefaultAdapter()?.enable() == true } catch (e: Exception) { false }
        Log.i("DromacBtHelper", "enable requested: $ok")
        resultCode = if (ok) 1 else 0
    }
}
