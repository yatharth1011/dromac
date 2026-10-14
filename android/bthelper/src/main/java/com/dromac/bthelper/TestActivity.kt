package com.dromac.bthelper

import android.app.Activity
import android.bluetooth.BluetoothAdapter
import android.os.Bundle
import android.util.Log

class TestActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        @Suppress("DEPRECATION")
        val ok = try { BluetoothAdapter.getDefaultAdapter()?.enable() == true } catch (e: Exception) { Log.w("DromacBtHelper", "enable failed: $e"); false }
        Log.i("DromacBtHelper", "test enable: $ok")
        finish()
    }
}
