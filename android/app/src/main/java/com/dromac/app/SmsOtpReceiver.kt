package com.dromac.app

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.provider.Telephony

// Watches for incoming SMS purely to pull out a verification code, so the
// user can grab a 2FA code without touching their phone while working on
// the Mac. Deliberately narrow: only a short numeric code is extracted and
// held in memory (StationServerService.latestOtp) -- the message body itself
// is never stored, logged, or exposed over the HTTP API.
class SmsOtpReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Telephony.Sms.Intents.SMS_RECEIVED_ACTION) return
        try {
            val messages = Telephony.Sms.Intents.getMessagesFromIntent(intent)
            val body = messages?.joinToString(" ") { it.messageBody ?: "" } ?: return
            val code = extractCode(body) ?: return
            StationServerService.latestOtp = code
            StationServerService.latestOtpAt = System.currentTimeMillis()
        } catch (_: Exception) {}
    }

    private fun extractCode(body: String): String? {
        val hint = Regex("(?i)(code|otp|verification|passcode)[^0-9]{0,12}(\\d{4,8})")
        hint.find(body)?.let { return it.groupValues[2] }
        val lone = Regex("\\b\\d{4,8}\\b")
        return lone.find(body)?.value
    }
}
