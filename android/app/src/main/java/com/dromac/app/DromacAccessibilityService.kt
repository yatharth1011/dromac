package com.dromac.app

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.graphics.Path
import android.view.accessibility.AccessibilityEvent

// The documented, non-root way to synthesize taps/swipes system-wide on Android --
// this is what actually lets the Mac dashboard "control" the phone once it's
// mirrored. Deliberately does nothing with onAccessibilityEvent: this service
// exists purely to dispatch gestures, never to read screen content or events.
class DromacAccessibilityService : AccessibilityService() {

    companion object {
        @Volatile var instance: DromacAccessibilityService? = null
    }

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
    }

    override fun onDestroy() {
        instance = null
        super.onDestroy()
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {}
    override fun onInterrupt() {}

    fun tap(x: Float, y: Float): Boolean {
        val path = Path().apply { moveTo(x, y) }
        val stroke = GestureDescription.StrokeDescription(path, 0, 60)
        val gesture = GestureDescription.Builder().addStroke(stroke).build()
        return try {
            dispatchGesture(gesture, null, null)
        } catch (_: Exception) {
            false
        }
    }

    fun swipe(x1: Float, y1: Float, x2: Float, y2: Float, durationMs: Long): Boolean {
        val path = Path().apply {
            moveTo(x1, y1)
            lineTo(x2, y2)
        }
        val stroke = GestureDescription.StrokeDescription(path, 0, durationMs.coerceIn(50, 5000))
        val gesture = GestureDescription.Builder().addStroke(stroke).build()
        return try {
            dispatchGesture(gesture, null, null)
        } catch (_: Exception) {
            false
        }
    }

    fun back(): Boolean = globalAction(GLOBAL_ACTION_BACK)
    fun home(): Boolean = globalAction(GLOBAL_ACTION_HOME)
    fun recents(): Boolean = globalAction(GLOBAL_ACTION_RECENTS)

    private fun globalAction(action: Int): Boolean {
        return try {
            performGlobalAction(action)
        } catch (_: Exception) {
            false
        }
    }
}
