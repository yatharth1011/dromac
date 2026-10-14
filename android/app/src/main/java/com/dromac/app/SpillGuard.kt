package com.dromac.app

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothManager
import android.content.Intent
import android.media.MediaRecorder
import android.content.ComponentName
import android.content.Context
import android.media.AudioAttributes
import android.media.AudioDeviceCallback
import android.media.AudioDeviceInfo
import android.media.AudioManager
import android.media.AudioRecordingConfiguration
import android.media.session.MediaSessionManager
import android.media.session.PlaybackState
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.util.Log
import androidx.core.app.NotificationCompat

// "Headphones only": music must never come out of the phone's own speaker.
//
// The Mac plays the phone's audio (Dromac redirects it there over adb). If that
// stops -- the Mac sleeps, its lid closes, the cable or Wi-Fi drops -- Android
// sends the music back to its normal output. With the headphones also connected
// to the phone (multipoint) that's the headphones, seamlessly. If they aren't,
// it would be the speaker -- in a library -- so this pauses the music the
// moment that happens and says why.
//
// Watched two ways: the redirect's capture ending (AudioRecordingCallback fires
// as it stops, before the speaker has played anything much) and a 100 ms check
// of where media is routed while music plays, as the backstop.
class SpillGuard(private val context: Context) {

    companion object {
        private const val PREFS = "dromac_spill_guard"
        private const val CHANNEL = "dromac_spill_guard"
        private const val NOTIFICATION_ID = 4107
    }

    private val audio = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    private val handler = Handler(Looper.getMainLooper())
    private val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
    @Volatile var enabled: Boolean = prefs.getBoolean("enabled", false)
        private set
    @Volatile var lastPausedAt = 0L
        private set
    @Volatile var lastPauseReason: String? = null
        private set
    @Volatile var lastResumedAt = 0L
        private set
    // the apps this paused, so "resume" plays exactly those again
    private val pausedPackages = mutableSetOf<String>()

    private val poll = object : Runnable {
        override fun run() {
            check()
            handler.postDelayed(this, 100)
        }
    }

    // After a pause: if headphones connect (the Mac handed them over, or you
    // connected them), carry on playing on them. If none turn up within 25 s
    // the headphones aren't around at all, and the speaker is allowed again --
    // never automatically: you press play -- until headphones show up again.
    @Volatile var speakerAllowed = false
        private set
    private val giveUp = Runnable {
        if (lastPausedAt > 0 && headphoneOutputs().isEmpty()) {
            if (!bluetoothOn()) {
                // Bluetooth is off (Dromac turns it off while the Mac plays your
                // headphones): the headphones can't show up, so don't call them
                // missing -- keep it paused and offer to turn Bluetooth on
                notify("Paused so it won't play out loud",
                    "The Mac stopped playing and the phone's Bluetooth is off. Turn it on and your headphones reconnect; the music carries on by itself.",
                    bluetoothAction())
                return@Runnable
            }
            speakerAllowed = true
            notify("No headphones found", "Your headphones aren't connected to the Mac or the phone. Press play to use the phone's speaker.")
        }
    }

    private fun bluetoothOn(): Boolean = try {
        (context.getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager)?.adapter?.isEnabled == true
    } catch (_: Exception) { true }

    // one tap: Android's own "turn on Bluetooth?" prompt
    private fun bluetoothAction(): NotificationCompat.Action? = try {
        val intent = Intent(BluetoothAdapter.ACTION_REQUEST_ENABLE).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        val pi = PendingIntent.getActivity(context, 1, intent, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        NotificationCompat.Action(0, "Turn on Bluetooth", pi)
    } catch (_: Exception) { null }
    private val deviceCallback = object : AudioDeviceCallback() {
        override fun onAudioDevicesAdded(added: Array<out AudioDeviceInfo>?) {
            val ours = added.orEmpty().filter { it.type in HEADPHONE_TYPES }
                .filter { device.isEmpty() || it.productName?.toString() == device }
            if (ours.isEmpty()) return
            speakerAllowed = false
            handler.removeCallbacks(giveUp)
            if (lastPausedAt > 0 && System.currentTimeMillis() - lastPausedAt < 10 * 60_000) {
                // give Android a moment to route media onto them before playing
                handler.postDelayed({
                    if (!mediaGoesToSpeaker()) resumePaused()
                    if (handingOver) finishHandover()
                }, 1200)
            }
        }
    }

    // Resume once the Mac's capture is running: then the music goes to the Mac
    // (and from there only to the headphones). Not "is media routed away from the
    // speaker?" -- while the music is paused Android always answers "speaker",
    // so that check never passed and the music stayed paused. (Should it ever
    // head for the speaker after all, check() pauses it again at once.)
    fun macCapturing(): Boolean = try {
        audio.activeRecordingConfigurations.any { it.clientAudioSource == MediaRecorder.AudioSource.REMOTE_SUBMIX }
    } catch (_: Exception) { false }

    private fun resumeWhenRouted(since: Long) {
        handler.postDelayed({
            if (macCapturing()) {
                resumePaused()
            } else if (System.currentTimeMillis() - since < 5_000) {
                resumeWhenRouted(since)
            }
        }, 300)
    }

    private val recordingCallback = object : AudioManager.AudioRecordingCallback() {
        override fun onRecordingConfigChanged(configs: MutableList<AudioRecordingConfiguration>?) {
            // the Mac has taken the audio again (its capture is running): whatever
            // happened before, the speaker isn't in play -- re-arm
            if (configs.orEmpty().any { it.clientAudioSource == MediaRecorder.AudioSource.REMOTE_SUBMIX }) {
                speakerAllowed = false
                handler.removeCallbacks(giveUp)
                try { context.getSystemService(NotificationManager::class.java).cancel(NOTIFICATION_ID) } catch (_: Exception) {}
                // the Mac has the audio again (it plays only on the headphones):
                // carry on with whatever this paused
                if (lastPausedAt > 0 && System.currentTimeMillis() - lastPausedAt < 10 * 60_000) {
                    resumeWhenRouted(System.currentTimeMillis())
                }
            }
            check()
        }
    }

    // The cable comes out while the Mac is playing over it: pause right now,
    // not ~0.1-0.5 s later when the speaker shows up -- whatever plays in
    // between goes into a capture that's already gone, and the song would jump
    // ahead by that much once Wi-Fi takes over. The Mac resumes it the moment
    // its Wi-Fi capture is running, so the song carries on from exactly here.
    @Volatile var macOverUsbAt = 0L   // set by the server whenever the Mac talks to it over the cable
    private val unplugged = object : android.content.BroadcastReceiver() {
        override fun onReceive(c: Context, intent: Intent) {
            val cableGone = intent.action == Intent.ACTION_POWER_DISCONNECTED ||
                (intent.action == "android.hardware.usb.action.USB_STATE" && !intent.getBooleanExtra("connected", true))
            if (!cableGone || !enabled || speakerAllowed) return
            if (System.currentTimeMillis() - macOverUsbAt > 5_000) return   // the Mac wasn't on the cable
            // Android can deliver this seconds late -- after the switch is already
            // handled (paused, then resumed on Wi-Fi). Pausing then would stop
            // the music for good: only act if nothing's been done about it yet.
            val now = System.currentTimeMillis()
            if (now - lastPausedAt < 10_000 || now - lastResumedAt < 10_000) return
            if (audio.isMusicActive && pauseEverything()) {
                lastPausedAt = System.currentTimeMillis()
                lastPauseReason = "usb unplugged"
                handler.removeCallbacks(takeOver)
                handler.postDelayed(takeOver, 4_000)
                Log.i("DromacSpillGuard", "paused: USB cable unplugged while the Mac played over it")
            }
        }
    }

    // The Mac switched this phone's Bluetooth off (so the headphones listen to
    // the Mac). If the Mac then goes quiet -- asleep, out of range, the cable
    // and Wi-Fi gone -- it can't switch it back on, and without Bluetooth the
    // Mac can't even find the phone again. So: after 2 minutes without hearing
    // from the Mac, with Bluetooth still off, offer to turn it back on.
    @Volatile var btOffByMac = prefs.getBoolean("btOffByMac", false)
        private set
    private val btCheck = object : Runnable {
        override fun run() {
            val quiet = System.currentTimeMillis() - StationServerService.lastRequestAt > 120_000
            if (btOffByMac && quiet && !bluetoothOn()) {
                // the Mac's been out of touch for 2 minutes: give the phone its
                // Bluetooth back (its devices, and the beacon the Mac finds it by)
                enableBluetoothViaHelper()
            } else if (bluetoothOn() && btOffByMac) {
                setBtOffByMac(false)
            }
            handler.postDelayed(this, 60_000)
        }
    }

    // ---- the phone takes the headphones back ----
    // The Mac lost the phone's audio (no cable, no Wi-Fi) and can't be reached
    // to give Bluetooth back -- it switched it off so the headphones would play
    // the Mac. A normal app can't switch Bluetooth on any more, but Dromac's
    // helper (com.dromac.bthelper, Android-11-level, same signing key) can.
    // With Bluetooth on, the headphones reconnect here and the music resumes
    // on them (deviceCallback). Waits a few seconds first: a cable -> Wi-Fi
    // switch on the Mac's side takes ~2 s, and then the Mac keeps playing.
    private fun enableBluetoothViaHelper() {
        try {
            context.sendBroadcast(Intent("com.dromac.bthelper.ENABLE")
                .setPackage("com.dromac.bthelper")
                .addFlags(Intent.FLAG_INCLUDE_STOPPED_PACKAGES))
        } catch (_: Exception) {}
    }

    private val takeOver = Runnable {
        val now = System.currentTimeMillis()
        val macGone = now - StationServerService.lastRequestAt > 3_000 && now - lastResumedAt > 5_000
        if (enabled && !bluetoothOn() && lastPausedAt > 0 && macGone) {
            Log.i("DromacSpillGuard", "Mac out of reach: Bluetooth on, so the headphones come back to the phone")
            enableBluetoothViaHelper()
        }
    }

    fun setBtOffByMac(off: Boolean) {
        btOffByMac = off
        prefs.edit().putBoolean("btOffByMac", off).apply()
    }

    fun start() {
        handler.postDelayed(btCheck, 60_000)
        try {
            val f = android.content.IntentFilter().apply {
                addAction(Intent.ACTION_POWER_DISCONNECTED)
                addAction("android.hardware.usb.action.USB_STATE")
            }
            if (Build.VERSION.SDK_INT >= 33) context.registerReceiver(unplugged, f, Context.RECEIVER_EXPORTED)
            else context.registerReceiver(unplugged, f)
        } catch (_: Exception) {}
        try { audio.registerAudioRecordingCallback(recordingCallback, handler) } catch (_: Exception) {}
        try { audio.registerAudioDeviceCallback(deviceCallback, handler) } catch (_: Exception) {}
        if (enabled) handler.post(poll)
    }

    fun stop() {
        handler.removeCallbacks(btCheck)
        try { context.unregisterReceiver(unplugged) } catch (_: Exception) {}
        handler.removeCallbacks(poll)
        try { audio.unregisterAudioRecordingCallback(recordingCallback) } catch (_: Exception) {}
        try { audio.unregisterAudioDeviceCallback(deviceCallback) } catch (_: Exception) {}
        handler.removeCallbacks(giveUp)
    }

    // The headphones "headphones only" is about (their name, e.g. WH-CH720N):
    // the music only ever resumes on those, never on some other Bluetooth device.
    @Volatile var device: String = prefs.getString("device", "") ?: ""
        private set

    fun setEnabled(on: Boolean, deviceName: String? = null) {
        enabled = on
        if (deviceName != null) device = deviceName
        prefs.edit().putBoolean("enabled", on).putString("device", device).apply()
        handler.removeCallbacks(poll)
        if (on) handler.post(poll)
    }

    /** Headphone-type outputs connected to the phone right now (names), for the Mac. */
    fun headphoneOutputs(): List<String> = try {
        audio.getDevices(AudioManager.GET_DEVICES_OUTPUTS)
            .filter { it.type in HEADPHONE_TYPES }
            .map { it.productName?.toString() ?: "headphones" }
            .distinct()
    } catch (_: Exception) {
        emptyList()
    }

    private val HEADPHONE_TYPES = setOf(
        AudioDeviceInfo.TYPE_BLUETOOTH_A2DP,
        AudioDeviceInfo.TYPE_BLE_HEADSET,
        AudioDeviceInfo.TYPE_WIRED_HEADPHONES,
        AudioDeviceInfo.TYPE_WIRED_HEADSET,
        AudioDeviceInfo.TYPE_USB_HEADSET,
    )

    private var lastRoutes = ""

    private fun mediaGoesToSpeaker(): Boolean {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) {
            // No routing query before Android 13: fall back to "no headphones at all".
            return headphoneOutputs().isEmpty()
        }
        val attrs = AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_MEDIA).build()
        val routes = audio.getAudioDevicesForAttributes(attrs)
        lastRoutes = routes.joinToString { "type${it.type}" }
        // Anything also going to the capture (REMOTE_SUBMIX = the Mac has it) or to
        // headphones isn't out loud, even if the speaker shows up in the list too.
        if (routes.any { it.type == AudioDeviceInfo.TYPE_REMOTE_SUBMIX || it.type in HEADPHONE_TYPES }) return false
        return routes.any { it.type == AudioDeviceInfo.TYPE_BUILTIN_SPEAKER || it.type == AudioDeviceInfo.TYPE_BUILTIN_EARPIECE }
    }

    private fun check() {
        if (!enabled || speakerAllowed) return
        try {
            if (!audio.isMusicActive || !mediaGoesToSpeaker()) return
            if (pauseEverything()) {
                lastPausedAt = System.currentTimeMillis()
                lastPauseReason = lastRoutes
                Log.i("DromacSpillGuard", "paused: media routed to [$lastRoutes]")
                if (bluetoothOn()) {
                    notify("Paused so it won't play out loud",
                        "The Mac stopped playing and no headphones are connected to the phone. It carries on by itself when they connect.")
                } else {
                    notify("Paused so it won't play out loud",
                        "The Mac stopped playing and the phone's Bluetooth is off. Turn it on and your headphones reconnect; the music carries on by itself.",
                        bluetoothAction())
                }
                handler.removeCallbacks(giveUp)
                handler.postDelayed(giveUp, 25_000)
                handler.removeCallbacks(takeOver)
                handler.postDelayed(takeOver, 4_000)
            }
        } catch (_: Exception) {}
    }

    /** Pauses every media session that's playing; true if anything was. */
    fun pauseEverything(): Boolean {
        return try {
            val msm = context.getSystemService(Context.MEDIA_SESSION_SERVICE) as MediaSessionManager
            val sessions = msm.getActiveSessions(ComponentName(context, NotificationAccessService::class.java))
            var paused = false
            for (s in sessions) {
                if (s.playbackState?.state == PlaybackState.STATE_PLAYING) {
                    s.transportControls.pause()
                    synchronized(pausedPackages) { pausedPackages.add(s.packageName) }
                    paused = true
                }
            }
            paused
        } catch (_: Exception) {
            false
        }
    }

    // Audio link switched off: back to normal -- but not by blasting the
    // phone's speaker while the headphones reconnect to it (its Bluetooth was
    // off while the Mac had them). Pause now, resume on the headphones once
    // they're connected, and only then stop guarding. If they don't turn up in
    // 20 s, stay paused and stop guarding: play is yours to press.
    private var handingOver = false
    private val endHandover = Runnable { finishHandover() }

    fun handOverThenDisable() {
        if (!enabled) return
        if (audio.isMusicActive && pauseEverything()) {
            lastPausedAt = System.currentTimeMillis()
            lastPauseReason = "audio link off"
        }
        handingOver = true
        handler.removeCallbacks(endHandover)
        handler.postDelayed(endHandover, 20_000)
    }

    private fun finishHandover() {
        if (!handingOver) return
        handingOver = false
        handler.removeCallbacks(endHandover)
        setEnabled(false)
    }

    /** Plays again exactly the apps this paused (true if any). */
    fun resumePaused(): Boolean {
        val wanted = synchronized(pausedPackages) { pausedPackages.toSet().also { pausedPackages.clear() } }
        if (wanted.isEmpty()) return false
        return try {
            val msm = context.getSystemService(Context.MEDIA_SESSION_SERVICE) as MediaSessionManager
            val sessions = msm.getActiveSessions(ComponentName(context, NotificationAccessService::class.java))
            var resumed = false
            for (s in sessions) if (s.packageName in wanted) { s.transportControls.play(); resumed = true }
            if (resumed) lastResumedAt = System.currentTimeMillis()
            if (resumed) context.getSystemService(NotificationManager::class.java).cancel(NOTIFICATION_ID)
            resumed
        } catch (_: Exception) {
            false
        }
    }

    private fun notify(title: String, text: String, action: NotificationCompat.Action? = null) {
        try {
            val nm = context.getSystemService(NotificationManager::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                nm.createNotificationChannel(NotificationChannel(CHANNEL, "Headphones only", NotificationManager.IMPORTANCE_DEFAULT))
            }
            nm.notify(NOTIFICATION_ID, NotificationCompat.Builder(context, CHANNEL)
                .setSmallIcon(R.drawable.ic_notification)
                .setContentTitle(title)
                .setContentText(text)
                .setStyle(NotificationCompat.BigTextStyle().bigText(text))
                .setAutoCancel(true)
                .apply { if (action != null) addAction(action) }
                .build())
        } catch (_: Exception) {}
    }
}
