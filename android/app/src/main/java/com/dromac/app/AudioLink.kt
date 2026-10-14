package com.dromac.app

import android.content.Context

// The master switch for playing the phone's audio on the Mac ("audio link").
// Off means back to completely normal: the Mac stops taking the phone's audio,
// "headphones only" stops pausing anything, and Bluetooth is the phone's own
// again. Flipped here (the button in MainActivity) or from the Mac's dashboard;
// the Mac reads it from /api/state, so the two stay in step.
object AudioLink {
    private const val PREFS = "dromac_audio_link"

    fun isEnabled(context: Context): Boolean =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getBoolean("enabled", true)

    fun setEnabled(context: Context, on: Boolean) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putBoolean("enabled", on).apply()
        StationServerService.instance?.onAudioLinkChanged(on)
    }
}
