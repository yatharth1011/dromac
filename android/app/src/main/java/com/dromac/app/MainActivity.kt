package com.dromac.app

import android.Manifest
import android.app.Activity
import android.app.NotificationManager
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.net.wifi.WifiManager
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.graphics.Color
import android.graphics.drawable.GradientDrawable
import android.text.format.Formatter
import android.view.Gravity
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat

class MainActivity : Activity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        val ipText = findViewById<TextView>(R.id.ipText)
        val statusContainer = findViewById<LinearLayout>(R.id.statusContainer)
        val bleHint = findViewById<TextView>(R.id.bleHint)
        val notifButton = findViewById<Button>(R.id.notifAccessButton)
        val batteryButton = findViewById<Button>(R.id.batteryExemptButton)
        val dndButton = findViewById<Button>(R.id.dndAccessButton)
        val usageButton = findViewById<Button>(R.id.usageAccessButton)
        val lockButton = findViewById<Button>(R.id.lockAdminButton)
        val overlayButton = findViewById<Button>(R.id.overlayAccessButton)
        val mirrorButton = findViewById<Button>(R.id.mirrorConsentButton)
        val mirrorStopButton = findViewById<Button>(R.id.mirrorStopButton)
        val accessibilityButton = findViewById<Button>(R.id.accessibilityButton)
        val keyboardButton = findViewById<Button>(R.id.keyboardButton)
        val cameraButton = findViewById<Button>(R.id.cameraAccessButton)
        val openCameraButton = findViewById<Button>(R.id.openCameraButton)

        bleHint.text = "Bluetooth keeps broadcasting even once connected — it's how the Mac " +
            "reconnects instantly if the link ever drops, and the cost is negligible."

        fun refresh() {
            val status = buildStatus()
            ipText.text = status.ip
            statusContainer.removeAllViews()
            for (item in status.items) addStatusCard(statusContainer, item)
            notifButton.visibility = if (status.items[1].on) android.view.View.GONE else android.view.View.VISIBLE
            batteryButton.visibility = if (status.items[2].on) android.view.View.GONE else android.view.View.VISIBLE
            dndButton.visibility = if (status.items[4].on) android.view.View.GONE else android.view.View.VISIBLE
            usageButton.visibility = if (status.items[5].on) android.view.View.GONE else android.view.View.VISIBLE
            lockButton.visibility = if (status.items[6].on) android.view.View.GONE else android.view.View.VISIBLE
            overlayButton.visibility = if (status.items[7].on) android.view.View.GONE else android.view.View.VISIBLE
            mirrorButton.visibility = if (status.items[8].on) android.view.View.GONE else android.view.View.VISIBLE
            accessibilityButton.visibility = if (status.items[9].on) android.view.View.GONE else android.view.View.VISIBLE
            mirrorStopButton.visibility =
                if (StationServerService.instance?.isMirrorActive() == true) android.view.View.VISIBLE else android.view.View.GONE
            cameraButton.visibility = if (status.items[11].on) android.view.View.GONE else android.view.View.VISIBLE
        }

        refresh()
        val refreshHandler = android.os.Handler(android.os.Looper.getMainLooper())
        val refreshRunnable = object : Runnable {
            override fun run() {
                refresh()
                refreshHandler.postDelayed(this, 2000)
            }
        }
        refreshHandler.postDelayed(refreshRunnable, 2000)

        notifButton.setOnClickListener {
            startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS))
        }

        batteryButton.setOnClickListener { requestBatteryExemption() }

        dndButton.setOnClickListener {
            startActivity(Intent(Settings.ACTION_NOTIFICATION_POLICY_ACCESS_SETTINGS))
        }

        usageButton.setOnClickListener {
            startActivity(Intent(Settings.ACTION_USAGE_ACCESS_SETTINGS))
        }

        overlayButton.setOnClickListener {
            val intent = Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:$packageName"))
            try { startActivity(intent) } catch (_: Exception) {
                Toast.makeText(this, "Couldn't open the overlay permission screen", Toast.LENGTH_LONG).show()
            }
        }

        mirrorButton.setOnClickListener {
            val mpm = getSystemService(Context.MEDIA_PROJECTION_SERVICE) as android.media.projection.MediaProjectionManager
            startActivityForResult(mpm.createScreenCaptureIntent(), 200)
        }

        mirrorStopButton.setOnClickListener {
            StationServerService.instance?.stopMirroringFromUi()
            Toast.makeText(this, "Screen sharing stopped", Toast.LENGTH_SHORT).show()
        }

        accessibilityButton.setOnClickListener {
            startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
            Toast.makeText(this, "Find Dromac in the list and turn it on", Toast.LENGTH_LONG).show()
        }

        keyboardButton.setOnClickListener {
            startActivity(Intent(Settings.ACTION_INPUT_METHOD_SETTINGS))
            Toast.makeText(
                this,
                "Enable Dromac here, then switch to it (keyboard icon) whenever you want to type from the Mac",
                Toast.LENGTH_LONG
            ).show()
        }

        cameraButton.setOnClickListener {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
                ActivityCompat.requestPermissions(this, arrayOf(Manifest.permission.CAMERA), 103)
            } else {
                Toast.makeText(this, "Camera access already granted", Toast.LENGTH_SHORT).show()
            }
        }

        openCameraButton.setOnClickListener {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
                Toast.makeText(this, "Grant camera access first", Toast.LENGTH_SHORT).show()
            } else {
                startActivity(Intent(this, CameraActivity::class.java))
            }
        }

        lockButton.setOnClickListener {
            val admin = ComponentName(this, DromacDeviceAdminReceiver::class.java)
            val intent = Intent(android.app.admin.DevicePolicyManager.ACTION_ADD_DEVICE_ADMIN)
            intent.putExtra(android.app.admin.DevicePolicyManager.EXTRA_DEVICE_ADMIN, admin)
            intent.putExtra(
                android.app.admin.DevicePolicyManager.EXTRA_ADD_EXPLANATION,
                "Lets the Mac dashboard lock this phone remotely. Nothing else."
            )
            try { startActivity(intent) } catch (_: Exception) {
                Toast.makeText(this, "Couldn't open the device admin screen", Toast.LENGTH_LONG).show()
            }
        }

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED
            ) {
                ActivityCompat.requestPermissions(this, arrayOf(Manifest.permission.POST_NOTIFICATIONS), 100)
            }
        }

        // Needed so the phone can continuously broadcast a small "I'm here, connect
        // to this IP" beacon over BLE — lets the Mac find it instantly by proximity
        // instead of scanning IP ranges. No UI flow needed; just ask once like POST_NOTIFICATIONS.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.BLUETOOTH_ADVERTISE)
                != PackageManager.PERMISSION_GRANTED
            ) {
                ActivityCompat.requestPermissions(this, arrayOf(Manifest.permission.BLUETOOTH_ADVERTISE), 101)
            }
        }

        // Call answer/caller-ID, SMS OTP capture, next-calendar-event, and "locate
        // phone" all need one of these each -- asked together in one dialog, same
        // "just ask once" treatment as the permissions above.
        val dangerous = listOfNotNull(
            Manifest.permission.READ_PHONE_STATE,
            Manifest.permission.READ_CALL_LOG,
            Manifest.permission.ANSWER_PHONE_CALLS.takeIf { Build.VERSION.SDK_INT >= Build.VERSION_CODES.O },
            Manifest.permission.RECEIVE_SMS,
            Manifest.permission.READ_CALENDAR,
            Manifest.permission.ACCESS_FINE_LOCATION,
            Manifest.permission.CAMERA,
        ).filter { ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED }
        if (dangerous.isNotEmpty()) {
            ActivityCompat.requestPermissions(this, dangerous.toTypedArray(), 102)
        }

        ContextCompat.startForegroundService(this, Intent(this, StationServerService::class.java))
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        @Suppress("DEPRECATION")
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == 200 && resultCode == RESULT_OK && data != null) {
            StationServerService.instance?.setProjectionConsent(resultCode, data)
            Toast.makeText(this, "Screen mirroring allowed", Toast.LENGTH_SHORT).show()
        }
    }

    private fun requestBatteryExemption() {
        val powerManager = getSystemService(POWER_SERVICE) as PowerManager
        if (powerManager.isIgnoringBatteryOptimizations(packageName)) {
            Toast.makeText(this, "Already exempted from battery optimization", Toast.LENGTH_SHORT).show()
            return
        }

        // Level 1: direct per-app exemption dialog (needs REQUEST_IGNORE_BATTERY_OPTIMIZATIONS permission).
        try {
            val intent = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS)
            intent.data = Uri.parse("package:$packageName")
            startActivity(intent)
            return
        } catch (_: Exception) {
            // fall through
        }

        // Level 2: general battery optimization list screen (find Dromac manually).
        try {
            startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
            Toast.makeText(this, "Find Dromac in this list and set it to \"Don't optimize\"", Toast.LENGTH_LONG).show()
            return
        } catch (_: Exception) {
            // fall through
        }

        // Level 3: this app's own settings page, always resolvable on every OEM.
        try {
            val intent = Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS)
            intent.data = Uri.parse("package:$packageName")
            startActivity(intent)
            Toast.makeText(this, "Open Battery from here and disable optimization", Toast.LENGTH_LONG).show()
        } catch (_: Exception) {
            Toast.makeText(this, "Couldn't open battery settings automatically — please find Dromac in Settings > Apps > Battery manually", Toast.LENGTH_LONG).show()
        }
    }

    data class StatusItem(val label: String, val on: Boolean, val note: String? = null)
    data class Status(val ip: String, val items: List<StatusItem>)

    private fun buildStatus(): Status {
        val ip = try {
            val wifiManager = applicationContext.getSystemService(WIFI_SERVICE) as WifiManager
            Formatter.formatIpAddress(wifiManager.connectionInfo.ipAddress)
        } catch (_: Exception) {
            "unknown"
        }

        val notifGranted = android.provider.Settings.Secure.getString(
            contentResolver, "enabled_notification_listeners"
        )?.contains(packageName) == true

        val powerManager = getSystemService(POWER_SERVICE) as PowerManager
        val batteryExempt = powerManager.isIgnoringBatteryOptimizations(packageName)

        val btAdvertiseGranted = Build.VERSION.SDK_INT < Build.VERSION_CODES.S ||
            ContextCompat.checkSelfPermission(this, Manifest.permission.BLUETOOTH_ADVERTISE) ==
            PackageManager.PERMISSION_GRANTED
        val btAdapter = (getSystemService(BLUETOOTH_SERVICE) as? android.bluetooth.BluetoothManager)?.adapter
        val btOn = btAdapter?.isEnabled == true
        val btBroadcasting = btAdvertiseGranted && btOn

        val nm = getSystemService(NotificationManager::class.java)
        val dndGranted = nm.isNotificationPolicyAccessGranted

        val sinceLast = System.currentTimeMillis() - StationServerService.lastRequestAt
        val macConnected = StationServerService.lastRequestAt > 0 && sinceLast < 5000

        val usageGranted = try {
            val appOps = getSystemService(Context.APP_OPS_SERVICE) as android.app.AppOpsManager
            val mode = appOps.unsafeCheckOpNoThrow(
                "android:get_usage_stats", android.os.Process.myUid(), packageName
            )
            mode == android.app.AppOpsManager.MODE_ALLOWED
        } catch (_: Exception) {
            false
        }

        val dpm = getSystemService(Context.DEVICE_POLICY_SERVICE) as android.app.admin.DevicePolicyManager
        val lockGranted = dpm.isAdminActive(ComponentName(this, DromacDeviceAdminReceiver::class.java))
        val overlayGranted = android.provider.Settings.canDrawOverlays(this)
        val cameraGranted = ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED

        return Status(
            ip = "Wi-Fi IP: $ip · port 8822",
            items = listOf(
                StatusItem("Mac dashboard", macConnected),
                StatusItem("Notification access", notifGranted),
                StatusItem("Battery optimization exempt", batteryExempt),
                StatusItem(
                    "Bluetooth broadcasting", btBroadcasting,
                    if (!btOn) "Bluetooth is off" else if (!btAdvertiseGranted) "permission needed" else null
                ),
                StatusItem("Do Not Disturb access", dndGranted),
                StatusItem("Usage access", usageGranted),
                StatusItem("Remote lock enabled", lockGranted),
                StatusItem("Display over apps (needed to launch apps)", overlayGranted),
                StatusItem("Screen mirroring allowed", StationServerService.instance?.hasProjectionConsent() == true),
                StatusItem("Remote control (Accessibility)", DromacAccessibilityService.instance != null),
                StatusItem(
                    "Remote keyboard active", DromacInputMethodService.instance != null,
                    "switch to it via the keyboard-switcher icon while typing from the Mac"
                ),
                StatusItem("Camera access", cameraGranted),
            )
        )
    }

    private fun addStatusCard(container: LinearLayout, item: StatusItem) {
        val onColor = Color.parseColor("#33ff99")
        val offColor = Color.parseColor("#67686c")
        val color = if (item.on) onColor else offColor

        val row = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(12), dp(10), dp(12), dp(10))
            val lp = LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT)
            lp.bottomMargin = dp(6)
            layoutParams = lp
            background = GradientDrawable().apply {
                cornerRadius = dp(10).toFloat()
                setColor(Color.parseColor("#18191c"))
                setStroke(dp(1), Color.argb(90, Color.red(color), Color.green(color), Color.blue(color)))
            }
        }

        val dot = android.widget.TextView(this).apply {
            text = if (item.on) "●" else "○"
            setTextColor(color)
            textSize = 13f
            setPadding(0, 0, dp(10), 0)
        }
        row.addView(dot)

        val label = android.widget.TextView(this).apply {
            text = item.label + (item.note?.let { " ($it)" } ?: "")
            setTextColor(Color.parseColor("#e4e4e6"))
            textSize = 13f
            layoutParams = LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f)
        }
        row.addView(label)

        val state = android.widget.TextView(this).apply {
            text = if (item.on) "ON" else "OFF"
            setTextColor(color)
            textSize = 11f
            setTypeface(typeface, android.graphics.Typeface.BOLD)
        }
        row.addView(state)

        container.addView(row)
    }

    private fun dp(v: Int): Int = (v * resources.displayMetrics.density).toInt()
}
